"""
Pairing API — short-code RustDesk-style agent pairing.

Endpoints:
  POST   /api/v1/pairing/enroll              (no auth, IP rate-limited)
  POST   /api/v1/pairing/code/refresh        (enrollment auth)
  POST   /api/v1/pairing/code/freeze         (enrollment auth)
  GET    /api/v1/pairing/poll                (enrollment auth, long-poll)
  POST   /api/v1/pairing/claim               (operator JWT, developer role)
  GET    /api/v1/pairing/lookup              (operator JWT, developer role — pre-flight code check)
"""

import time
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import get_jwt_identity

from app import limiter
from app.middleware.rbac import developer_required
from app.services import pairing_service
from app.services.pairing_service import (
    PairingError,
    InvalidEnrollmentError,
    InvalidPairCredentialsError,
    LockoutError,
    AlreadyClaimedError,
    ExpiredEnrollmentError,
)
from app.api._request import json_object

pairing_bp = Blueprint('pairing', __name__)


def _enrollment_creds():
    """Read enrollment_id + enrollment_secret from headers or JSON body."""
    enrollment_id = request.headers.get('X-Enrollment-Id')
    enrollment_secret = request.headers.get('X-Enrollment-Secret')
    if not enrollment_id and request.is_json:
        body = json_object(optional=True)
        enrollment_id = body.get('enrollment_id')
        enrollment_secret = body.get('enrollment_secret')
    return enrollment_id, enrollment_secret


def _error_response(exc: PairingError):
    return jsonify({'error': str(exc)}), getattr(exc, 'status_code', 400)


# ==================== Enroll ====================


@pairing_bp.route('/enroll', methods=['POST'])
@limiter.limit("10 per minute")
def enroll():
    data = json_object(optional=True)
    pubkey = data.get('pubkey')
    passphrase = data.get('passphrase')
    machine_id = data.get('machine_id')
    system_info = data.get('system_info') or {}

    if not pubkey or not passphrase:
        return jsonify({'error': 'pubkey and passphrase are required'}), 400

    try:
        result = pairing_service.enroll(
            pubkey_hex=pubkey,
            passphrase=passphrase,
            machine_id=machine_id,
            system_info=system_info,
        )
    except PairingError as exc:
        return _error_response(exc)

    return jsonify(result), 201


# ==================== Code refresh / freeze ====================


@pairing_bp.route('/code/refresh', methods=['POST'])
@limiter.limit("30 per minute")
def code_refresh():
    enrollment_id, enrollment_secret = _enrollment_creds()
    body = json_object(optional=True)
    force = bool(body.get('force', False))
    try:
        result = pairing_service.rotate_code(enrollment_id, enrollment_secret, force=force)
    except PairingError as exc:
        return _error_response(exc)
    return jsonify(result), 200


@pairing_bp.route('/code/freeze', methods=['POST'])
@limiter.limit("30 per minute")
def code_freeze():
    enrollment_id, enrollment_secret = _enrollment_creds()
    body = json_object(optional=True)
    frozen = bool(body.get('frozen', True))
    try:
        result = pairing_service.set_freeze(enrollment_id, enrollment_secret, frozen=frozen)
    except PairingError as exc:
        return _error_response(exc)
    return jsonify(result), 200


# ==================== Long-poll ====================


@pairing_bp.route('/poll', methods=['GET', 'POST'])
@limiter.limit("120 per minute")
def poll():
    """
    Long-poll for claim status. Up to ~25 seconds of waiting per request.
    Agent should reconnect immediately after each response.
    """
    enrollment_id, enrollment_secret = _enrollment_creds()
    if not enrollment_id or not enrollment_secret:
        return jsonify({'error': 'Missing enrollment credentials'}), 401

    # Optional Ed25519 proof-of-possession over the enrollment_id. Sent as a
    # header (works for GET long-polls) or in the JSON body. It's constant for a
    # given enrollment, so read it once before the long-poll loop.
    signature = request.headers.get('X-Enrollment-Signature')
    if not signature and request.is_json:
        signature = json_object(optional=True).get('signature')

    deadline = time.time() + 25.0  # leave headroom under typical proxy timeouts
    poll_interval = 1.0

    while True:
        try:
            result = pairing_service.poll(enrollment_id, enrollment_secret,
                                          signature=signature)
        except PairingError as exc:
            return _error_response(exc)

        if result.get('status') == 'claimed':
            return jsonify(result), 200

        if time.time() >= deadline:
            return jsonify(result), 200  # status: pending

        time.sleep(poll_interval)


# ==================== Operator claim ====================


@pairing_bp.route('/lookup', methods=['POST'])
@developer_required
@limiter.limit("20 per minute")
def lookup():
    """Pre-flight: confirm a code exists (without revealing details)."""
    data = json_object(optional=True)
    code = data.get('code', '')
    pending = pairing_service.find_by_code(code)
    if not pending:
        return jsonify({'found': False}), 404
    return jsonify({
        'found': True,
        'pubkey_fpr': pending.pubkey_fpr,
        'system_info': pending.system_info or {},
        'machine_id': pending.machine_id,
    }), 200


@pairing_bp.route('/claim', methods=['POST'])
@developer_required
@limiter.limit("5 per 10 minute")
def claim():
    user_id = int(get_jwt_identity())
    data = json_object(optional=True)
    code = data.get('code')
    passphrase = data.get('passphrase')
    name = data.get('name')
    group_id = data.get('group_id')

    if not code or not passphrase:
        return jsonify({'error': 'code and passphrase are required'}), 400

    # Hosting plan: a claimed agent becomes a server the operator owns.
    from app.middleware.rbac import get_current_user
    from app.services.plan_quota_service import PlanQuotaService
    PlanQuotaService.check(get_current_user(), 'servers')

    try:
        result = pairing_service.claim(
            operator_user_id=user_id,
            code=code,
            passphrase=passphrase,
            name=name,
            group_id=group_id,
            # Recorded for the pairing audit trail — the real operator IP once
            # TRUST_PROXY_HEADERS is on (ProxyFix-corrected), not nginx's.
            ip_address=request.remote_addr,
        )
    except PairingError as exc:
        return _error_response(exc)

    return jsonify(result), 201
