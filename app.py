import os
import hmac
import hashlib
import math
import time
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ======================= SETTINGS (all secrets come from Render > Environment) =======================
# --- PayHero (collections / STK push) ---
BASE = "https://backend.payhero.co.ke/api/v2"
AUTH = os.environ.get("PAYHERO_AUTH_TOKEN", "")          # MOVE the token out of the code: set this in Render
CHANNEL_ID = int(os.environ.get("PAYHERO_CHANNEL_ID", "10752"))

# --- your own key for the app (still shipped in the APK, so it is NOT a strong secret) ---
API_KEY = os.environ.get("BACKEND_API_KEY", "")
MAX_WITHDRAW = int(os.environ.get("MAX_WITHDRAW", "1000"))
MIN_WITHDRAW = int(os.environ.get("MIN_WITHDRAW", "100"))
MIN_DEPOSIT = int(os.environ.get("MIN_DEPOSIT", "10"))
BONUS_MIN_DEPOSIT = 200

# --- charges (percent / KES). 0 = none. Set the real values in Render. ---
DEP_FEE_FLAT = float(os.environ.get("DEP_FEE_FLAT", "0"))
DEP_TAX_PCT = float(os.environ.get("DEP_TAX_PCT", "0"))
WD_FEE_FLAT = float(os.environ.get("WD_FEE_FLAT", "0"))
WD_FEE_PCT = float(os.environ.get("WD_FEE_PCT", "0"))
WD_TAX_PCT = float(os.environ.get("WD_TAX_PCT", "0"))

# --- HashBack (B2C payouts) ---
HB_URL = "https://api.hashback.co.ke"
HB_KEY = os.environ.get("HASHBACK_API_KEY", "")
HB_CRED = os.environ.get("HASHBACK_SECURITY_CREDENTIAL", "")
HB_WALLET = os.environ.get("HASHBACK_WALLET_ID", "")

# --- Firebase Realtime Database (REST) ---
FB_URL = os.environ.get("FIREBASE_DB_URL", "").rstrip("/")
FB_SECRET = os.environ.get("FIREBASE_SECRET", "")
USERS = "aviator_users"      # aviator_users/<2547XXXXXXXX>/{balance, firstdep}
PAYS = "aviator_pay"         # aviator_pay/<payhero ref>/{uid, amount, credited}
WDS = "aviator_wd"           # aviator_wd/<ref>/{uid, amount, status}

WEBHOOK_SECRET = os.environ.get("FINOQ_WEBHOOK_SECRET", "")

HEADERS = {
    "Authorization": AUTH,
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Accept-Encoding": "identity",
}


# ======================= HELPERS =======================
def fmt_phone(p):
    """-> 2547XXXXXXXX"""
    p = str(p).strip().replace("+", "").replace(" ", "").replace("-", "")
    if p.startswith("0"):
        p = "254" + p[1:]
    elif not p.startswith("254"):
        p = "254" + p
    return p


def local_phone(p):
    """-> 07XXXXXXXX"""
    p = str(p).strip().replace("+", "").replace(" ", "").replace("-", "")
    if p.startswith("254"):
        p = "0" + p[3:]
    elif not p.startswith("0"):
        p = "0" + p
    return p


def valid_uid(u):
    return len(u) == 12 and u.startswith("254") and u.isdigit()


def authorized():
    return bool(API_KEY) and request.headers.get("X-API-Key") == API_KEY


def fb_ready():
    return bool(FB_URL and FB_SECRET)


def _u(path):
    return f"{FB_URL}/{path}.json"


def fb_get(path):
    r = requests.get(_u(path), params={"auth": FB_SECRET}, timeout=15)
    r.raise_for_status()
    return r.json()


def fb_txn(path, fn, retries=6):
    """Atomic read-modify-write using Firebase ETags.
    fn(current) -> new value, or None to abort. Returns (value, changed)."""
    for _ in range(retries):
        r = requests.get(_u(path), params={"auth": FB_SECRET},
                         headers={"X-Firebase-ETag": "true"}, timeout=15)
        r.raise_for_status()
        etag = r.headers.get("ETag")
        cur = r.json()
        new = fn(cur)
        if new is None:
            return cur, False
        w = requests.put(_u(path), params={"auth": FB_SECRET}, json=new,
                         headers={"if-match": etag}, timeout=15)
        if w.status_code == 200:
            return new, True
        if w.status_code != 412:      # 412 = someone else changed it, retry
            w.raise_for_status()
    raise RuntimeError("database busy, try again")


def get_balance(uid):
    b = fb_get(f"{USERS}/{uid}/balance")
    return float(b or 0)


def add_balance(uid, delta):
    """atomically add delta (may be negative). Returns new balance, or None if it would go below 0."""
    def f(cur):
        cur = float(cur or 0)
        nb = cur + delta
        return None if nb < 0 else round(nb, 2)
    val, changed = fb_txn(f"{USERS}/{uid}/balance", f)
    return val if changed else None


def dep_charge(amount):
    return int(math.ceil(DEP_FEE_FLAT + amount * DEP_TAX_PCT / 100.0))


def wd_charge(amount):
    return int(math.ceil(WD_FEE_FLAT + amount * WD_FEE_PCT / 100.0 + amount * WD_TAX_PCT / 100.0))


@app.get("/")
def home():
    return jsonify(status="running", channel=CHANNEL_ID,
                   hashback_configured=bool(HB_KEY and HB_CRED and HB_WALLET),
                   firebase_configured=fb_ready(),
                   key_configured=bool(API_KEY))


# ======================= BALANCE =======================
@app.post("/bal/get")
def bal_get():
    if not authorized():
        return jsonify(success=False, message="Unauthorized"), 401
    if not fb_ready():
        return jsonify(success=False, message="Firebase not configured"), 500
    uid = fmt_phone((request.get_json(force=True, silent=True) or {}).get("uid", ""))
    if not valid_uid(uid):
        return jsonify(success=False, message="bad uid"), 400
    try:
        return jsonify(success=True, balance=get_balance(uid))
    except Exception as e:
        return jsonify(success=False, message=str(e)), 502


@app.post("/bal/adjust")
def bal_adjust():
    """Game money movement: kind 'bet' removes the stake, kind 'win' adds the payout.
    NOTE: the server cannot verify that a win really happened (the game runs in the app)."""
    if not authorized():
        return jsonify(success=False, message="Unauthorized"), 401
    if not fb_ready():
        return jsonify(success=False, message="Firebase not configured"), 500
    d = request.get_json(force=True, silent=True) or {}
    uid = fmt_phone(d.get("uid", ""))
    kind = str(d.get("kind", ""))
    try:
        amt = float(d.get("amount", 0))
    except (TypeError, ValueError):
        amt = 0
    if not valid_uid(uid) or amt <= 0 or kind not in ("bet", "win"):
        return jsonify(success=False, message="bad request"), 400
    if kind == "win" and amt > 1000000:
        return jsonify(success=False, message="amount too large"), 400
    try:
        nb = add_balance(uid, -amt if kind == "bet" else amt)
    except Exception as e:
        return jsonify(success=False, message=str(e)), 502
    if nb is None:
        return jsonify(success=False, message="Insufficient balance"), 402
    return jsonify(success=True, balance=nb)


# ======================= COLLECTIONS (PayHero STK push) =======================
@app.post("/pay")
def pay():
    if not authorized():
        return jsonify(success=False, message="Unauthorized"), 401
    if not fb_ready():
        return jsonify(success=False, message="Firebase not configured"), 500
    d = request.get_json(force=True, silent=True) or {}
    try:
        amount = int(float(d.get("amount", 0)))
    except (ValueError, TypeError):
        amount = 0
    uid = fmt_phone(d.get("uid", ""))                # the account that gets the money (registered number)
    if not d.get("phone") or not valid_uid(uid):
        return jsonify(success=False, message="phone and uid required"), 400
    if amount < MIN_DEPOSIT:
        return jsonify(success=False, message=f"Minimum deposit is {MIN_DEPOSIT}"), 400

    payload = {
        "amount": amount,
        "phone_number": fmt_phone(d["phone"]),       # the M-Pesa number that pays (may differ from uid)
        "channel_id": CHANNEL_ID,
        "provider": "m-pesa",
        "external_reference": "Aviator",
    }
    try:
        r = requests.post(f"{BASE}/payments", headers=HEADERS, json=payload, timeout=30)
    except requests.RequestException as e:
        return jsonify(success=False, message=f"Network error: {e}"), 502
    print("PAY", r.status_code, r.text)
    try:
        body = r.json()
    except ValueError:
        return jsonify(success=False, message="Bad response from PayHero"), 502

    ref = body.get("reference")
    if r.status_code in (200, 201) and ref:
        # remember who this payment is for, so only the server decides who gets credited
        requests.put(_u(f"{PAYS}/{ref}"), params={"auth": FB_SECRET},
                     json={"uid": uid, "amount": amount, "credited": False, "ts": int(time.time())},
                     timeout=15)
        return jsonify(success=True, reference=ref)
    msg = body.get("error_message") or body.get("message") or "Failed"
    return jsonify(success=False, message=msg), 400


@app.get("/status/<ref>")
def status(ref):
    if not authorized():
        return jsonify(state="PENDING", message="Unauthorized"), 401
    if not fb_ready():
        return jsonify(state="PENDING", message="Firebase not configured"), 500
    try:
        rec = fb_get(f"{PAYS}/{ref}")
    except Exception as e:
        return jsonify(state="PENDING", message=str(e))
    if not rec:                                     # a reference this server never issued
        return jsonify(state="FAILED", message="unknown reference"), 404

    try:
        r = requests.get(f"{BASE}/transaction-status", headers=HEADERS,
                         params={"reference": ref}, timeout=20)
        j = r.json()
    except (requests.RequestException, ValueError) as e:
        return jsonify(state="PENDING", message=f"network: {e}")
    print("STATUS", ref, r.status_code, r.text)

    s = str(j.get("status", "")).upper()
    if s == "FAILED":
        return jsonify(state="FAILED")
    if s != "SUCCESS":
        return jsonify(state="PENDING")

    # --- paid: credit exactly once ---
    try:
        paid = float(j.get("amount") or rec.get("amount") or 0)
    except (TypeError, ValueError):
        paid = float(rec.get("amount") or 0)
    uid = rec["uid"]

    def claim(cur):
        if not cur or cur.get("credited"):
            return None
        cur["credited"] = True
        cur["paid"] = paid
        return cur
    _, first_time = fb_txn(f"{PAYS}/{ref}", claim)

    bonus = 0
    net = max(0, paid - dep_charge(paid))
    if first_time:
        def first_dep(cur):
            return None if cur else True
        _, is_first = fb_txn(f"{USERS}/{uid}/firstdep", first_dep)
        if is_first and paid >= BONUS_MIN_DEPOSIT:
            bonus = net
        add_balance(uid, net + bonus)

    return jsonify(state="COMPLETED", amount=paid, charge=dep_charge(paid),
                   bonus=bonus, credited=first_time, balance=get_balance(uid))


# ======================= PAYOUTS (HashBack wallet B2C) =======================
@app.post("/hb/balance")
def hb_balance():
    if not authorized():
        return jsonify(success=False, message="Unauthorized"), 401
    if not (HB_KEY and HB_WALLET):
        return jsonify(success=False, message="HashBack env vars not set"), 500
    try:
        r = requests.post(f"{HB_URL}/walletbalance",
                          json={"api_key": HB_KEY, "account_id": HB_WALLET}, timeout=20)
    except requests.RequestException as e:
        return jsonify(success=False, message=f"Network error: {e}"), 502
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        return jsonify(raw=r.text[:500]), r.status_code


@app.post("/hb/withdraw")
def hb_withdraw():
    if not authorized():
        return jsonify(success=False, message="Unauthorized"), 401
    if not fb_ready():
        return jsonify(success=False, message="Firebase not configured"), 500
    if not (HB_KEY and HB_CRED):
        return jsonify(success=False, message="HashBack env vars not set"), 500

    d = request.get_json(force=True, silent=True) or {}
    try:
        amount = int(float(d.get("amount", 0)))      # amount taken from the player's balance
    except (ValueError, TypeError):
        amount = 0
    ref = str(d.get("reference", "")).strip()
    uid = fmt_phone(d.get("uid", ""))                # the registered number IS the destination
    if not valid_uid(uid) or not ref or amount < 1:
        return jsonify(success=False, message="uid, amount, reference required"), 400
    if amount < MIN_WITHDRAW:
        return jsonify(success=False, message=f"Minimum withdrawal is {MIN_WITHDRAW}"), 400
    if amount > MAX_WITHDRAW:
        return jsonify(success=False, message=f"Max per withdrawal is {MAX_WITHDRAW}"), 400
    charge = wd_charge(amount)
    payout = amount - charge
    if payout < 1:
        return jsonify(success=False, message="Amount too small after charges"), 400

    # one record per reference: a repeated reference can never pay twice
    def claim(cur):
        return None if cur else {"uid": uid, "amount": amount, "payout": payout, "status": "started", "ts": int(time.time())}
    _, new_ref = fb_txn(f"{WDS}/{ref}", claim)
    if not new_ref:
        return jsonify(success=False, message="Duplicate reference"), 409

    # take the money first; refuse if the SERVER balance is not enough
    nb = add_balance(uid, -amount)
    if nb is None:
        requests.put(_u(f"{WDS}/{ref}/status"), params={"auth": FB_SECRET}, json="rejected_balance", timeout=15)
        return jsonify(success=False, message="Insufficient balance"), 402

    payload = {"api_key": HB_KEY, "msisdn": local_phone(uid), "amount": payout, "SecurityCredential": HB_CRED}
    print("HB WITHDRAW REQUEST", {"uid": uid, "amount": amount, "payout": payout, "ref": ref})

    def mark(st):
        requests.put(_u(f"{WDS}/{ref}/status"), params={"auth": FB_SECRET}, json=st, timeout=15)

    try:
        r = requests.post(f"{HB_URL}/V2/processwithdrawal", json=payload, timeout=40)
    except requests.RequestException as e:
        mark("unclear_network")                      # may have been sent: keep deducted, check manually
        return jsonify(success=False, unclear=True,
                       message=f"Network error, being checked. Ref {ref}"), 502
    print("HB WITHDRAW", r.status_code, r.text)

    try:
        body = r.json()
    except ValueError:
        mark("unclear_reply")
        return jsonify(success=False, unclear=True, message=f"Unclear reply, being checked. Ref {ref}"), 502

    if body.get("success") is True:
        mark("paid")
        return jsonify(success=True, payout=payout, charge=charge, balance=nb)

    if r.status_code in (400, 401, 402, 403):        # clear rejection: give the money back
        add_balance(uid, amount)
        mark("refused")
        return jsonify(success=False, reply=body, message="Payout refused"), r.status_code

    mark("unclear_status")
    return jsonify(success=False, unclear=True, message=f"Being checked. Ref {ref}"), 502


# ======================= CALLBACK (logging only) =======================
@app.post("/callback")
def callback():
    raw = request.get_data()
    print("CALLBACK BODY", raw.decode("utf-8", "replace"))
    if WEBHOOK_SECRET:
        expected = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        print("SIGNATURE MATCH HEADERS:", [h for h, v in request.headers.items() if expected in v] or "none")
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
