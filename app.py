import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ---------- PayHero settings ----------
BASE = "https://backend.payhero.co.ke/api/v2"
AUTH = "Basic NVUyRXl1S1FBTlhwa2t2TUROUzY6aE9tc2xmUDZCYmR1dTJzV1RCb2ZiSnhoRUlrWXhWTFNrZFVkRjUyNw=="
CHANNEL_ID = 10752
CALLBACK_URL = "https://payhero-callback-setup.onrender.com/callback"   # optional: e.g. "https://YOUR-SERVICE.onrender.com/callback"

HEADERS = {"Authorization": AUTH, "Content-Type": "application/json"}


def fmt_phone(p):
    p = p.strip().replace("+", "").replace(" ", "").replace("-", "")
    if p.startswith("0"):
        p = "254" + p[1:]
    elif not p.startswith("254"):
        p = "254" + p
    return p


@app.get("/")
def home():
    return jsonify(status="running", channel=CHANNEL_ID)


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
        "external_reference": d.get("reference", "Payment"),
    }
    if CALLBACK_URL:
        payload["callback_url"] = CALLBACK_URL

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
    return jsonify(success=False,
                   message=body.get("error_message") or body.get("message") or "Failed"), 400


@app.get("/status/<ref>")
def status(ref):
    try:
        r = requests.get(f"{BASE}/transaction-status",
                         headers=HEADERS, params={"reference": ref}, timeout=20)
    except requests.RequestException as e:
        return jsonify(state="PENDING", paid=False, raw=f"network: {e}")
    print("STATUS", r.status_code, r.text)
    try:
        s = str(r.json().get("status", "")).upper()
    except ValueError:
        s = ""
    state = "COMPLETED" if s == "SUCCESS" else "FAILED" if s == "FAILED" else "PENDING"
    return jsonify(state=state, paid=(state == "COMPLETED"), raw=s)


@app.post("/callback")   # optional backup, only logs for now
def callback():
    print("CALLBACK", request.get_data(as_text=True))
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
