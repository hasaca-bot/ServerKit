"""Fallback audit logging for authenticated mutating API requests."""
import json
import logging
from datetime import datetime

from flask import g, request

from app import db
from app.models.audit_log import AuditLog
from app.utils.secret_paths import mask_path

logger = logging.getLogger(__name__)

MUTATING_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
MAX_BODY_BYTES = 32 * 1024
MAX_STRING_LENGTH = 500
MAX_LIST_ITEMS = 20
MAX_DICT_ITEMS = 50
from app.utils.sensitive_data_filter import (  # noqa: F401 (re-export)
    REDACTED,
    SENSITIVE_KEY_PARTS,
    is_sensitive_key as _shared_is_sensitive_key,
)

NOISY_ENDPOINTS = {
    'auth.refresh',
}


def register_audit_fallback(app):
    """Register an after-request fallback for missing explicit audit logs."""

    @app.after_request
    def audit_unlogged_mutation(response):
        if not _should_audit(response):
            return response

        user_id = _get_user_id()
        if user_id is None:
            return response

        details = _build_details(response)
        values = {
            'action': AuditLog.ACTION_API_MUTATION,
            'user_id': user_id,
            'target_type': _target_type(),
            'target_id': _target_id(),
            'details': json.dumps(details),
            'ip_address': _ip_address(),
            'user_agent': (request.headers.get('User-Agent') or '')[:500],
            'created_at': datetime.utcnow(),
        }

        try:
            # Use a separate transaction so fallback audit logging does not
            # accidentally commit application changes left pending on db.session.
            with db.engine.begin() as conn:
                conn.execute(AuditLog.__table__.insert().values(**values))
        except Exception as exc:
            logger.warning('Fallback audit log failed: %s', exc)

        return response


def _should_audit(response):
    if request.method not in MUTATING_METHODS:
        return False
    if not request.path.startswith('/api/'):
        return False
    if request.endpoint in NOISY_ENDPOINTS:
        return False
    if getattr(g, 'audit_logged', False):
        return False
    return True


def _get_user_id():
    api_key_user = getattr(g, 'api_key_user', None)
    if api_key_user is not None:
        return api_key_user.id

    try:
        from flask_jwt_extended import get_jwt_identity
        identity = get_jwt_identity()
    except Exception:
        return None

    if identity is None:
        return None

    try:
        return int(identity)
    except (TypeError, ValueError):
        return identity


def _build_details(response):
    details = {
        'method': request.method,
        'path': mask_path(request.path),
        'endpoint': request.endpoint,
        'status_code': response.status_code,
        'success': 200 <= response.status_code < 400,
    }

    if request.view_args:
        details['route_args'] = _sanitize(
            {k: ('<key>' if k == 'key' and request.blueprint == 'mcp' else val)
             for k, val in request.view_args.items()})

    query = request.args.to_dict(flat=False)
    if query:
        details['query'] = _sanitize(query)

    if request.is_json:
        if (request.content_length or 0) > MAX_BODY_BYTES:
            details['payload'] = '[omitted: request body too large]'
        else:
            payload = request.get_json(silent=True)
            if payload is not None:
                details['payload'] = _sanitize(payload)

    if request.files:
        details['files'] = {
            field: _sanitize({'filename': file.filename, 'content_type': file.content_type})
            for field, file in request.files.items()
        }

    return details


def _target_type():
    if request.blueprint:
        return request.blueprint[:50]
    if request.endpoint:
        return request.endpoint.split('.')[0][:50]
    parts = [part for part in request.path.split('/') if part]
    return (parts[2] if len(parts) > 2 else 'api')[:50]


def _target_id():
    for key, value in (request.view_args or {}).items():
        if key == 'id' or key.endswith('_id'):
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _sanitize(value, key=None, depth=0):
    if key and _is_sensitive_key(key):
        return REDACTED
    if depth >= 4:
        return '[truncated]'

    if isinstance(value, dict):
        items = list(value.items())
        result = {}
        for item_key, item_value in items[:MAX_DICT_ITEMS]:
            safe_key = str(item_key)[:MAX_STRING_LENGTH]
            result[safe_key] = _sanitize(item_value, key=safe_key, depth=depth + 1)
        if len(items) > MAX_DICT_ITEMS:
            result['__truncated__'] = len(items) - MAX_DICT_ITEMS
        return result

    if isinstance(value, (list, tuple)):
        result = [_sanitize(item, depth=depth + 1) for item in value[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            result.append(f'[truncated {len(value) - MAX_LIST_ITEMS} items]')
        return result

    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            return value[:MAX_STRING_LENGTH] + '...[truncated]'
        return value

    if value is None or isinstance(value, (bool, int, float)):
        return value

    return str(value)[:MAX_STRING_LENGTH]


def _is_sensitive_key(key):
    return _shared_is_sensitive_key(str(key))


def _ip_address():
    # Trusted client IP only — see app.utils.client_ip. Never hand-parse XFF.
    from app.utils.client_ip import get_client_ip
    return get_client_ip()
