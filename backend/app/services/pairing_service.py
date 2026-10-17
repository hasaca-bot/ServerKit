"""
Pairing service — implements the RustDesk-style short-code pairing flow.

Two factors are required to claim an enrolled agent:
  1. The 6-character pair code displayed on the agent host (rotates).
  2. The user-set passphrase configured on the agent host.

Flow:
  1. Agent calls /pairing/enroll with its Ed25519 pubkey + passphrase hash.
     -> server returns enrollment_id + enrollment_secret + pair_code.
  2. Agent long-polls /pairing/poll with enrollment credentials.
  3. Operator enters (code, passphrase) in the panel -> /pairing/claim.
  4. Server creates a real Server row, mints api_key/api_secret, encrypts the
     credentials, and stashes them on the PendingAgent.
  5. The agent's next /poll returns the credentials. Done.
"""

import base64
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import or_

from app import db
from app.models.audit_log import AuditLog
from app.models.pending_agent import (
    PendingAgent,
    PAIR_CODE_ALPHABET,
    PAIR_CODE_LENGTH,
    DEFAULT_PAIR_CODE_TTL,
    DEFAULT_ENROLLMENT_TTL,
)
from app.models.server import Server

logger = logging.getLogger(__name__)


# ==================== Errors ====================


class PairingError(Exception):
    """Base class for pairing failures."""
    status_code = 400


class InvalidEnrollmentError(PairingError):
    status_code = 401


class InvalidPairCredentialsError(PairingError):
    status_code = 401


class LockoutError(PairingError):
    status_code = 429


class AlreadyClaimedError(PairingError):
    status_code = 409


class ExpiredEnrollmentError(PairingError):
    status_code = 410


# ==================== Helpers ====================


def generate_pair_code(length: int = PAIR_CODE_LENGTH) -> str:
    """Generate a pair code from the unambiguous alphabet."""
    return ''.join(secrets.choice(PAIR_CODE_ALPHABET) for _ in range(length))


def _generate_unique_pair_code() -> str:
    """Generate a pair code that doesn't collide with another active code."""
    for _ in range(10):
        code = generate_pair_code()
        existing = (
            PendingAgent.query
            .filter(PendingAgent.pair_code == code)
            .filter(PendingAgent.claimed_at.is_(None))
            .filter(PendingAgent.pair_code_expires_at > datetime.utcnow())
            .first()
        )
        if not existing:
            return code
    # Extremely unlikely; fall back to a longer code
    return generate_pair_code(length=PAIR_CODE_LENGTH + 2)


def format_pair_code(code: str) -> str:
    """Format `ABC123` as `ABC-123` for display."""
    half = len(code) // 2
    return f"{code[:half]}-{code[half:]}"


def normalize_pair_code(code: str) -> str:
    """Strip dashes/whitespace, uppercase. Operators may paste either form."""
    return ''.join(c for c in (code or '').upper() if c.isalnum())


# ==================== Public API ====================


def enroll(pubkey_hex: str, passphrase: str, machine_id: Optional[str] = None,
           system_info: Optional[dict] = None) -> dict:
    """
    Register a new pending agent. Called by the agent (no authentication).

    Returns dict with: enrollment_id, enrollment_secret, pair_code,
    pair_code_expires_at, expires_at.
    """
    if not pubkey_hex or len(pubkey_hex) < 32:
        raise PairingError("Invalid public key")
    if not passphrase or len(passphrase) < 4:
        raise PairingError("Passphrase must be at least 4 characters")

    # Detect re-enrollment from the same machine: replace prior unclaimed entry.
    if machine_id:
        prior = (
            PendingAgent.query
            .filter_by(machine_id=machine_id)
            .filter(PendingAgent.claimed_at.is_(None))
            .all()
        )
        for p in prior:
            db.session.delete(p)

    enrollment_id = secrets.token_urlsafe(24)
    enrollment_secret = secrets.token_urlsafe(32)
    pair_code = _generate_unique_pair_code()
    now = datetime.utcnow()

    pending = PendingAgent(
        enrollment_id=enrollment_id,
        enrollment_secret_hash=PendingAgent.hash_enrollment_secret(enrollment_secret),
        pubkey=pubkey_hex.lower(),
        pubkey_fpr=PendingAgent.fingerprint(pubkey_hex.lower()),
        pair_code=pair_code,
        pair_code_expires_at=now + DEFAULT_PAIR_CODE_TTL,
        pair_code_frozen=False,
        passphrase_hash=PendingAgent.hash_passphrase(passphrase),
        machine_id=machine_id,
        system_info=system_info or {},
        created_at=now,
        last_seen_at=now,
        expires_at=now + DEFAULT_ENROLLMENT_TTL,
    )
    db.session.add(pending)
    db.session.commit()

    AuditLog.log(
        action='pairing.enroll',
        target_type='pending_agent',
        details={'pubkey_fpr': pending.pubkey_fpr, 'machine_id': machine_id},
    )
    db.session.commit()

    return {
        'enrollment_id': enrollment_id,
        'enrollment_secret': enrollment_secret,
        'pair_code': pair_code,
        'pair_code_formatted': format_pair_code(pair_code),
        'pair_code_expires_at': pending.pair_code_expires_at.isoformat(),
        'expires_at': pending.expires_at.isoformat(),
        'pubkey_fpr': pending.pubkey_fpr,
    }


def _load_pending_for_agent(enrollment_id: str, enrollment_secret: str) -> PendingAgent:
    pending = PendingAgent.query.filter_by(enrollment_id=enrollment_id).first()
    if not pending:
        raise InvalidEnrollmentError("Unknown enrollment")
    if not pending.verify_enrollment_secret(enrollment_secret):
        raise InvalidEnrollmentError("Invalid enrollment credentials")
    if pending.is_expired() and not pending.is_claimed():
        raise ExpiredEnrollmentError("Enrollment expired; re-enroll required")
    return pending


def rotate_code(enrollment_id: str, enrollment_secret: str, force: bool = False) -> dict:
    """Rotate the visible pair code. No-op if frozen unless force=True."""
    pending = _load_pending_for_agent(enrollment_id, enrollment_secret)
    if pending.is_claimed():
        raise AlreadyClaimedError("Already claimed")

    now = datetime.utcnow()
    if pending.pair_code_frozen and not force:
        # Keep the existing code; just confirm it's still valid.
        if not pending.is_pair_code_valid():
            pending.pair_code_expires_at = now + DEFAULT_PAIR_CODE_TTL
    else:
        pending.pair_code = _generate_unique_pair_code()
        pending.pair_code_expires_at = now + DEFAULT_PAIR_CODE_TTL

    pending.last_seen_at = now
    db.session.commit()
    return {
        'pair_code': pending.pair_code,
        'pair_code_formatted': format_pair_code(pending.pair_code),
        'pair_code_expires_at': pending.pair_code_expires_at.isoformat(),
        'pair_code_frozen': pending.pair_code_frozen,
    }


def set_freeze(enrollment_id: str, enrollment_secret: str, frozen: bool) -> dict:
    pending = _load_pending_for_agent(enrollment_id, enrollment_secret)
    pending.pair_code_frozen = bool(frozen)
    pending.last_seen_at = datetime.utcnow()
    db.session.commit()
    return {'pair_code_frozen': pending.pair_code_frozen}


def _verify_pop(pubkey_hex: str, enrollment_id: str, signature_b64: str) -> bool:
    """Verify an Ed25519 proof-of-possession signature over the enrollment_id.

    The agent signs the enrollment_id with the private key whose public half it
    submitted at enroll() time. pubkey is stored as lowercase hex of the raw
    32-byte Ed25519 key; the signature is base64 of the raw 64-byte signature.
    Any decode/verify failure returns False (never raises) so a malformed or
    wrong-key signature is treated as a failed proof, not a 500.
    """
    if not pubkey_hex or not signature_b64:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        pub.verify(base64.b64decode(signature_b64), enrollment_id.encode())
        return True
    except Exception:
        return False


def poll(enrollment_id: str, enrollment_secret: str,
         signature: Optional[str] = None) -> dict:
    """
    Called by the agent to check whether an operator has claimed it yet.

    signature (optional): base64 Ed25519 signature over the enrollment_id,
    proving possession of the private key matching the enrolled pubkey. When
    supplied it MUST verify before anything is returned — so a stolen
    enrollment_secret alone can't claim the credentials of a device whose
    private key the attacker lacks. Verification is best-effort for backward
    compatibility: agents that predate signing omit it and fall back to
    bearer-secret-only auth (this becomes mandatory once signing agents have
    rolled out — see SECURITY.md).

    Returns:
      {'status': 'pending', ...} if still waiting.
      {'status': 'claimed', 'credentials': {...}} once claimed (delivered ONCE).
    """
    pending = _load_pending_for_agent(enrollment_id, enrollment_secret)

    # Proof-of-possession: a present signature must verify against the enrolled
    # pubkey. A present-but-invalid signature is an attack (or key mismatch) and
    # is rejected outright, before last_seen_at or any state is touched.
    if signature is not None:
        if not _verify_pop(pending.pubkey, enrollment_id, signature):
            logger.warning(
                "Pairing poll signature verification FAILED for enrollment %s",
                enrollment_id,
            )
            raise InvalidEnrollmentError("Invalid proof-of-possession signature")

    pending.last_seen_at = datetime.utcnow()

    if pending.is_claimed() and pending.claim_payload_encrypted:
        try:
            payload = json.loads(pending.claim_payload)
        except Exception as e:
            logger.exception("Failed to decrypt claim payload: %s", e)
            raise PairingError("Internal pairing error")

        # One-shot delivery: clear the encrypted payload after read.
        pending.claim_payload_encrypted = None
        db.session.commit()

        AuditLog.log(
            action='pairing.delivered',
            target_type='pending_agent',
            target_id=None,
            details={'pubkey_fpr': pending.pubkey_fpr,
                     'server_id': pending.claimed_server_id},
        )
        db.session.commit()
        return {'status': 'claimed', 'credentials': payload}

    if pending.is_claimed():
        # Already delivered; nothing more to give.
        return {'status': 'claimed', 'credentials': None}

    db.session.commit()
    return {
        'status': 'pending',
        'pair_code': pending.pair_code,
        'pair_code_expires_at': pending.pair_code_expires_at.isoformat(),
    }


def find_by_code(code: str) -> Optional[PendingAgent]:
    """Look up an unclaimed PendingAgent by pair code. Returns None if not found."""
    code = normalize_pair_code(code)
    if not code:
        return None
    return (
        PendingAgent.query
        .filter(PendingAgent.pair_code == code)
        .filter(PendingAgent.claimed_at.is_(None))
        .filter(PendingAgent.pair_code_expires_at > datetime.utcnow())
        .first()
    )


def claim(operator_user_id: int, code: str, passphrase: str,
          name: Optional[str] = None, group_id: Optional[str] = None,
          ip_address: Optional[str] = None) -> dict:
    """
    Operator-initiated claim. Returns {'server': {...}, 'fingerprint': '...'}.

    Failed attempts increment the per-PendingAgent counter and set a lockout.
    """
    code = normalize_pair_code(code)
    pending = find_by_code(code)
    if not pending:
        # Don't reveal whether the code matched anything — generic 401.
        AuditLog.log(
            action='pairing.claim_failed',
            user_id=operator_user_id,
            details={'reason': 'no_match', 'ip': ip_address},
        )
        db.session.commit()
        raise InvalidPairCredentialsError("Invalid pair code or passphrase")

    if pending.is_locked_out():
        AuditLog.log(
            action='pairing.claim_locked',
            user_id=operator_user_id,
            target_type='pending_agent',
            details={'pubkey_fpr': pending.pubkey_fpr, 'ip': ip_address},
        )
        db.session.commit()
        raise LockoutError("Too many failed attempts; try again shortly")

    if not pending.verify_passphrase(passphrase):
        pending.failed_attempts = (pending.failed_attempts or 0) + 1
        # Exponential backoff up to 5 minutes
        backoff_seconds = min(60 * (2 ** max(0, pending.failed_attempts - 1)), 300)
        pending.lockout_until = datetime.utcnow() + timedelta(seconds=backoff_seconds)
        db.session.commit()
        AuditLog.log(
            action='pairing.claim_failed',
            user_id=operator_user_id,
            target_type='pending_agent',
            details={'pubkey_fpr': pending.pubkey_fpr,
                     'reason': 'bad_passphrase',
                     'attempts': pending.failed_attempts,
                     'ip': ip_address},
        )
        db.session.commit()
        raise InvalidPairCredentialsError("Invalid pair code or passphrase")

    # Success — mint a real Server row and credentials.
    api_key, api_secret = Server.generate_api_credentials()
    sysinfo = pending.system_info or {}

    server = Server(
        name=(name or sysinfo.get('hostname') or f"agent-{pending.pubkey_fpr[:6]}"),
        hostname=sysinfo.get('hostname'),
        os_type=sysinfo.get('os'),
        os_version=sysinfo.get('platform_version'),
        platform=sysinfo.get('platform'),
        architecture=sysinfo.get('architecture'),
        cpu_cores=sysinfo.get('cpu_cores'),
        total_memory=sysinfo.get('total_memory'),
        total_disk=sysinfo.get('total_disk'),
        agent_version=sysinfo.get('agent_version'),
        status='connecting',
        registered_at=datetime.utcnow(),
        registered_by=operator_user_id,
        owner_id=operator_user_id,
        group_id=group_id,
    )
    server.id = str(__import__('uuid').uuid4())
    server.agent_id = str(__import__('uuid').uuid4())
    server.set_api_key(api_key)
    server.set_api_secret_encrypted(api_secret)

    db.session.add(server)

    # Stash credentials for one-shot delivery on next /poll.
    payload = json.dumps({
        'agent_id': server.agent_id,
        'server_id': server.id,
        'name': server.name,
        'api_key': api_key,
        'api_secret': api_secret,
    })
    pending.claim_payload = payload
    pending.claimed_at = datetime.utcnow()
    pending.claimed_server_id = server.id
    pending.failed_attempts = 0
    pending.lockout_until = None

    db.session.commit()

    AuditLog.log(
        action='pairing.claim_success',
        user_id=operator_user_id,
        target_type='server',
        target_id=None,  # server.id is a UUID string; AuditLog target_id is int
        details={
            'pubkey_fpr': pending.pubkey_fpr,
            'server_id': server.id,
            'ip': ip_address,
        },
    )
    db.session.commit()

    return {
        'server': server.to_dict(),
        'fingerprint': pending.pubkey_fpr,
        'pubkey': pending.pubkey,
    }


def prune_expired(now: Optional[datetime] = None) -> int:
    """Delete expired/unclaimed pending agents. Returns count removed."""
    now = now or datetime.utcnow()
    q = (
        PendingAgent.query
        .filter(PendingAgent.claimed_at.is_(None))
        .filter(PendingAgent.expires_at < now)
    )
    count = q.count()
    if count:
        for p in q.all():
            db.session.delete(p)
        db.session.commit()
        logger.info("Pruned %d expired pending agents", count)
    # Also clean up claimed entries older than 7 days (delivered or not).
    cutoff = now - timedelta(days=7)
    stale = (
        PendingAgent.query
        .filter(PendingAgent.claimed_at.isnot(None))
        .filter(PendingAgent.claimed_at < cutoff)
        .all()
    )
    for p in stale:
        db.session.delete(p)
    if stale:
        db.session.commit()
    return count + len(stale)
