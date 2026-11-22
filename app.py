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
