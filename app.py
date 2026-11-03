import os
import hmac
import hashlib
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ======================= SETTINGS =======================
# --- PayHero (collections / STK push) ---
BASE = "https://backend.payhero.co.ke/api/v2"
AUTH = os.environ.get("PAYHERO_AUTH_TOKEN") or \
    "Basic NVUyRXl1S1FBTlhwa2t2TUROUzY6aE9tc2xmUDZCYmR1dTJzV1RCb2ZiSnhoRUlrWXhWTFNrZFVkRjUyNw=="
CHANNEL_ID = int(os.environ.get("PAYHERO_CHANNEL_ID", "10752"))
CALLBACK_URL = ""   # optional, used by STK push only

# --- Your own backend key (protects /hb/* routes). Set BACKEND_API_KEY in Render. ---
API_KEY = os.environ.get("BACKEND_API_KEY", "rt-test-key-2026")
MAX_WITHDRAW = 1000     # KES per request
USED_REFS = set()       # resets on restart; a Firebase check comes next

# --- HashBack (B2C payouts). Set these three in Render > Environment. ---
HB_URL = "https://api.hashback.co.ke"
HB_KEY = os.environ.get("HASHBACK_API_KEY", "")
HB_CRED = os.environ.get("HASHBACK_SECURITY_CREDENTIAL", "")
HB_WALLET = os.environ.get("HASHBACK_WALLET_ID", "")

# --- FinoQ webhook signing secret (logging only for now) ---
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
    p = p.strip().replace("+", "").replace(" ", "").replace("-", "")
    if p.startswith("0"):
        p = "254" + p[1:]
    elif not p.startswith("254"):
        p = "254" + p
    return p


def local_phone(p):
    """-> 07XXXXXXXX"""
    p = p.strip().replace("+", "").replace(" ", "").replace("-", "")
    if p.startswith("254"):
        p = "0" + p[3:]
    elif not p.startswith("0"):
        p = "0" + p
    return p


def authorized():
    return request.headers.get("X-API-Key") == API_KEY


@app.get("/")
def home():
    return jsonify(
        status="running",
        channel=CHANNEL_ID,
        hashback_configured=bool(HB_KEY and HB_CRED and HB_WALLET),
        webhook_secret_set=bool(WEBHOOK_SECRET),
    )


# ======================= COLLECTIONS (PayHero STK push) =======================
@app.post("/pay")
def pay():
    d = request.get_json(force=True, silent=True) or {}
    try:
        amount = int(float(d.get("amount", 0)))
    except (ValueError, TypeError):
        amount = 0
    if not d.get("phone") or amount < 1:
        return jsonify(success=False, message="Phone and amount required"), 400

    payload = {
        "amount": amount,
        "phone_number": fmt_phone(str(d["phone"])),
        "channel_id": CHANNEL_ID,
        "provider": "m-pesa",
        "external_reference": d.get("reference", "Payment"),
    }
    if CALLBACK_URL:
        payload["callback_url"] = CALLBACK_URL

    print("PAY REQUEST", payload)
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
        return jsonify(success=True, reference=ref)

    msg = body.get("error_message") or body.get("message") or "Failed"
    return jsonify(success=False, message=f"{msg} | {r.text[:300]}"), 400


@app.get("/status/<ref>")
def status(ref):
    try:
        r = requests.get(f"{BASE}/transaction-status",
                         headers=HEADERS, params={"reference": ref}, timeout=20)
    except requests.RequestException as e:
        return jsonify(state="PENDING", paid=False, raw=f"network: {e}")
    print("STATUS", r.status_code, r.text)

    try:
        j = r.json()
    except ValueError:
        j = {}

    s = str(j.get("status", "")).upper()
    state = "COMPLETED" if s == "SUCCESS" else "FAILED" if s == "FAILED" else "PENDING"
    return jsonify(state=state, paid=(state == "COMPLETED"), raw=s,
                   amount=j.get("amount"),
                   receipt=j.get("provider_reference"),
                   phone=j.get("phone_number"))


# ======================= PAYOUTS (HashBack wallet B2C) =======================
@app.post("/hb/balance")
def hb_balance():
    if not authorized():
        return jsonify(success=False, message="Unauthorized"), 401
    if not (HB_KEY and HB_WALLET):
        return jsonify(success=False, message="HashBack env vars not set"), 500
    try:
        r = requests.post(f"{HB_URL}/walletbalance",
                          json={"api_key": HB_KEY, "account_id": HB_WALLET},
                          timeout=20)
    except requests.RequestException as e:
        return jsonify(success=False, message=f"Network error: {e}"), 502
    print("HB BALANCE", r.status_code, r.text)
    try:
        return jsonify(r.json()), r.status_code
    except ValueError:
        return jsonify(raw=r.text[:500]), r.status_code


@app.post("/hb/withdraw")
def hb_withdraw():
    if not authorized():
        return jsonify(success=False, message="Unauthorized"), 401

    d = request.get_json(force=True, silent=True) or {}
    try:
        amount = int(float(d.get("amount", 0)))
    except (ValueError, TypeError):
        amount = 0
    ref = str(d.get("reference", "")).strip()
    intl = bool(d.get("intl"))   # True = send 2547..., default 07...

    if not d.get("phone") or amount < 1 or not ref:
        return jsonify(success=False, message="phone, amount, reference required"), 400
    if amount > MAX_WITHDRAW:
        return jsonify(success=False, message=f"Max per withdrawal is {MAX_WITHDRAW}"), 400
    if ref in USED_REFS:
        return jsonify(success=False, message="Duplicate reference"), 409
    if not (HB_KEY and HB_CRED):
        return jsonify(success=False, message="HashBack env vars not set"), 500

    phone = fmt_phone(str(d["phone"])) if intl else local_phone(str(d["phone"]))
    payload = {
        "api_key": HB_KEY,
        "msisdn": phone,
        "amount": amount,
        "SecurityCredential": HB_CRED,
    }

    USED_REFS.add(ref)
    print("HB WITHDRAW REQUEST", {"msisdn": phone, "amount": amount, "ref": ref})
    try:
        r = requests.post(f"{HB_URL}/V2/processwithdrawal", json=payload, timeout=40)
    except requests.RequestException as e:
        # may or may not have gone through: keep the ref locked, check balance first
        return jsonify(success=False,
                       message=f"Network error (check balance before retrying): {e}"), 502
    print("HB WITHDRAW", r.status_code, r.text)

    try:
        body = r.json()
    except ValueError:
        return jsonify(success=False,
                       message="Unclear reply, check balance before retrying",
                       raw=r.text[:300]), 502

    if body.get("success") is True:
        return jsonify(success=True, reply=body)

    if r.status_code in (400, 401, 402, 403):
        USED_REFS.discard(ref)    # clear rejection: safe to retry
    return jsonify(success=False, reply=body), r.status_code


# ======================= CALLBACK (logging only) =======================
@app.post("/callback")
def callback():
    raw = request.get_data()
    print("CALLBACK HEADERS", {k: v for k, v in request.headers.items()
                               if k.lower() not in ("authorization", "cookie")})
    print("CALLBACK BODY", raw.decode("utf-8", "replace"))

    # Only logged; NOTHING acts on callbacks yet.
    if WEBHOOK_SECRET:
        expected = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        matches = [h for h, v in request.headers.items() if expected in v]
        print("SIGNATURE MATCH HEADERS:", matches or "none")
    else:
        print("FINOQ_WEBHOOK_SECRET not set")
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
