import os
import hmac
import hashlib
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ---------- PayHero / FinoQ settings ----------
BASE = "https://backend.payhero.co.ke/api/v2"
AUTH = "Basic NVUyRXl1S1FBTlhwa2t2TUROUzY6aE9tc2xmUDZCYmR1dTJzV1RCb2ZiSnhoRUlrWXhWTFNrZFVkRjUyNw=="
CHANNEL_ID = 10752            # collections (STK push)
CALLBACK_URL = ""             # optional: "https://payhero-callback-setup.onrender.com/callback"

# Webhook signing secret: set FINOQ_WEBHOOK_SECRET in Render > Environment
WEBHOOK_SECRET = os.environ.get("FINOQ_WEBHOOK_SECRET", "")

# ---------- Withdrawal (B2C) settings ----------
API_KEY = "rt-test-key-2026"  # YOU invent this; same value goes on the test page
MAX_WITHDRAW = 1000           # KES per request
WALLET_CHANNEL_ID = 10752     # channel sent with withdrawals
USED_REFS = set()             # resets on restart; Firebase check comes next

HEADERS = {
    "Authorization": AUTH,
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Accept-Encoding": "identity",
}


def fmt_phone(p):
    p = p.strip().replace("+", "").replace(" ", "").replace("-", "")
    if p.startswith("0"):
        p = "254" + p[1:]
    elif not p.startswith("254"):
        p = "254" + p
    return p


@app.get("/")
def home():
    return jsonify(status="running", channel=CHANNEL_ID,
                   webhook_secret_set=bool(WEBHOOK_SECRET))


# ===================== COLLECTIONS (C2B) =====================
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


# ===================== WITHDRAWALS (B2C) =====================
@app.post("/withdraw")
def withdraw():
    if request.headers.get("X-API-Key") != API_KEY:
        return jsonify(success=False, message="Unauthorized"), 401

    d = request.get_json(force=True, silent=True) or {}
    try:
        amount = int(float(d.get("amount", 0)))
    except (ValueError, TypeError):
        amount = 0
    ref = str(d.get("reference", "")).strip()

    if not d.get("phone") or amount < 1 or not ref:
        return jsonify(success=False, message="phone, amount, reference required"), 400
    if amount > MAX_WITHDRAW:
        return jsonify(success=False, message=f"Max per withdrawal is {MAX_WITHDRAW}"), 400
    if ref in USED_REFS:
        return jsonify(success=False, message="Duplicate reference"), 409

    # Attempt A format: network_code MUST be a string (a number is rejected)
    payload = {
        "external_reference": ref,
        "amount": amount,
        "phone_number": fmt_phone(str(d["phone"])),
        "network_code": "63902",       # M-Pesa
        "channel": "mobile",
        "channel_id": WALLET_CHANNEL_ID,
        "payment_service": "b2c",
    }
    if CALLBACK_URL:
        payload["callback_url"] = CALLBACK_URL

    USED_REFS.add(ref)
    print("WITHDRAW REQUEST", payload)
    try:
        r = requests.post(f"{BASE}/withdraw", headers=HEADERS, json=payload, timeout=30)
    except requests.RequestException as e:
        # request may or may not have reached PayHero: keep ref locked
        return jsonify(success=False, message=f"Network error: {e}", sent=payload), 502
    print("WITHDRAW", r.status_code, r.text)

    try:
        body = r.json()
    except ValueError:
        return jsonify(success=False, message="Bad response from PayHero", sent=payload), 502

    if r.status_code in (200, 201) and str(body.get("status", "")).upper() == "QUEUED":
        return jsonify(success=True,
                       reference=body.get("merchant_reference"),
                       checkout_id=body.get("checkout_request_id"),
                       status="QUEUED",
                       sent=payload)

    USED_REFS.discard(ref)   # rejected by PayHero: allow retry
    msg = body.get("error_message") or body.get("message") or "Failed"
    return jsonify(success=False, message=msg,
                   payhero=r.text[:400], sent=payload), 400


@app.get("/withdraw/status/<ref>")
def withdraw_status(ref):
    if request.headers.get("X-API-Key") != API_KEY:
        return jsonify(success=False, message="Unauthorized"), 401
    try:
        r = requests.get(f"{BASE}/transaction-status",
                         headers=HEADERS, params={"reference": ref}, timeout=20)
    except requests.RequestException as e:
        return jsonify(state="PENDING", raw=f"network: {e}")
    print("WITHDRAW STATUS", r.status_code, r.text)
    try:
        j = r.json()
    except ValueError:
        j = {}
    s = str(j.get("status", "")).upper()
    state = "COMPLETED" if s == "SUCCESS" else "FAILED" if s == "FAILED" else "PENDING"
    return jsonify(state=state, raw=s, detail=j)


# ===================== FINOQ WEBHOOK (logging only) =====================
@app.post("/callback")
def callback():
    raw = request.get_data()
    print("CALLBACK HEADERS", {k: v for k, v in request.headers.items()
                               if k.lower() not in ("authorization", "cookie")})
    print("CALLBACK BODY", raw.decode("utf-8", "replace"))

    # Checks the common scheme (HMAC-SHA256 of the raw body, hex).
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
