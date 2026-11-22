"""Secure starter-loan API for integration into an existing Flask app.

Requires Firebase Authentication ID tokens and a trusted eligibility decision
written to RTDB at nyota_eligibility_decisions/<firebase auth uid>.
Never accepts approval, phone or payout amount from the browser.
"""
import os
import time
import uuid
import requests
from flask import Blueprint, request, jsonify
from firebase_admin import auth


def install_loan_routes(app, fb_get, fb_txn, fb_url, fb_secret, hb_url, hb_key, hb_cred):
    bp = Blueprint('nyota_loan', __name__)

    def identity():
        bearer = request.headers.get('Authorization', '')
        if not bearer.startswith('Bearer '):
            raise ValueError('Sign in with a verified account first')
        token = auth.verify_id_token(bearer[7:], check_revoked=True)
        phone = token.get('phone_number', '').lstrip('+')
        if not (len(phone) == 12 and phone.startswith('254') and phone.isdigit()):
            raise ValueError('A verified Kenyan phone number is required')
        return token['uid'], phone

    def state(uid):
        return fb_get(f'nyota_starter_loans/{uid}')

    @bp.get('/api/loan/offer')
    def offer():
        try:
            uid, phone = identity()
            decision = fb_get(f'nyota_eligibility_decisions/{uid}') or {}
            approved = decision.get('status') == 'approved' and decision.get('phone') == phone and decision.get('starter_amount') == 50
            rec = state(uid) or {}
            return jsonify(eligible=approved, starterAmount=50 if approved else None,
                           potentialLimit=decision.get('potential_limit') if approved else None,
                           state=rec.get('status', 'NOT_REQUESTED'), reference=rec.get('reference'), phoneLast4=phone[-4:])
        except Exception as exc:
            return jsonify(error=str(exc)), 401

    @bp.post('/api/loan/initial-disbursement')
    def disburse():
        try:
            uid, phone = identity()
            decision = fb_get(f'nyota_eligibility_decisions/{uid}') or {}
            if not (decision.get('status') == 'approved' and decision.get('phone') == phone and decision.get('starter_amount') == 50):
                return jsonify(error='No verified starter-loan approval found'), 403
            ref = 'NY' + uuid.uuid4().hex[:24].upper()
            path = f'nyota_starter_loans/{uid}'
            def claim(current):
                if current is not None:
                    return None
                return {'reference': ref, 'phone': phone, 'amount': 50,
                        'status': 'PROCESSING', 'created_at': int(time.time())}
            rec, fresh = fb_txn(path, claim)
            if not fresh:
                return jsonify(reference=rec['reference'], state=rec['status']), 200
            if not (hb_key and hb_cred):
                # Do not reset the claim automatically: requires operator reconciliation.
                fb_txn(path, lambda c: {**c, 'status': 'NEEDS_REVIEW'} if c and c.get('reference') == ref else None)
                return jsonify(reference=ref, state='NEEDS_REVIEW', error='Payout provider is not configured'), 503
            payload = {'api_key': hb_key, 'msisdn': '0' + phone[3:],
                       'amount': 50, 'SecurityCredential': hb_cred}
            try:
                response = requests.post(hb_url + '/V2/processwithdrawal', json=payload, timeout=40)
                result = response.json()
            except (requests.RequestException, ValueError):
                fb_txn(path, lambda c: {**c, 'status': 'NEEDS_REVIEW'} if c and c.get('reference') == ref else None)
                return jsonify(reference=ref, state='NEEDS_REVIEW'), 202
            # Provider success is acceptance, not proof of settlement.
            status = 'ACCEPTED_PENDING_CONFIRMATION' if result.get('success') is True else ('FAILED' if response.status_code in (400,401,402,403) else 'NEEDS_REVIEW')
            fb_txn(path, lambda c: {**c, 'status': status, 'provider_response_at': int(time.time())} if c and c.get('reference') == ref else None)
            return jsonify(reference=ref, state=status), 202
        except ValueError as exc:
            return jsonify(error=str(exc)), 401
        except Exception:
            app.logger.exception('Starter loan error')
            return jsonify(error='Unable to process. Check existing request before retrying.'), 503

    @bp.get('/api/loan/disbursement-status')
    def disbursement_status():
        try:
            uid, _ = identity()
            rec = state(uid)
            if not rec or rec.get('reference') != request.args.get('reference'):
                return jsonify(error='Unknown reference'), 404
            status = rec.get('status')
            return jsonify(reference=rec['reference'], state=('COMPLETED' if status == 'CONFIRMED_DELIVERED' else 'FAILED' if status == 'FAILED' else 'PENDING'),
                           disbursementConfirmed=status == 'CONFIRMED_DELIVERED', detail=status)
        except ValueError as exc:
            return jsonify(error=str(exc)), 401

    app.register_blueprint(bp)
