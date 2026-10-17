"""Per-user MCP endpoint (Streamable HTTP transport, JSON responses).

    POST /api/v1/mcp            Authorization: Bearer <api key>  (or X-API-Key)
    POST /api/v1/mcp/k/<key>    the key in the path, for clients that cannot
                                send headers (claude.ai custom connectors)

Deliberately API-key capable and API-key ONLY: an MCP client is a program
acting for its owner, so it authenticates with the owner's own API key, which
must carry the ``mcp`` scope (or full access ``*``). A browser JWT is refused.
Every tool then acts only on the key owner's resources (see
app/services/mcp/context.py); viewers get the read-only tools, developers and
admins the write tools on their own services.

Bad keys are throttled per client IP through the login throttle
(auth_throttle_service), and each key may make RATE_LIMIT calls a minute.
The path key is masked in every log and audit sink (app/utils/secret_paths).
"""
import threading
import time
from collections import deque

from flask import Blueprint, Response, g, jsonify, request

from app import limiter
from app.middleware.api_scope_middleware import require_scope
from app.utils.secret_paths import install_log_filters

mcp_bp = Blueprint('mcp', __name__)

MCP_SCOPE = 'mcp'
RATE_LIMIT = 60          # tool/protocol calls per key per minute
MAX_BODY = 256 * 1024

_calls = {}
_calls_lock = threading.Lock()

install_log_filters()


def reset_rate_limits():
    """Tests only."""
    with _calls_lock:
        _calls.clear()


def _rate_limited(key_id):
    """Seconds to wait when the key used up its minute, else 0."""
    now = time.monotonic()
    with _calls_lock:
        window = _calls.setdefault(key_id, deque())
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= RATE_LIMIT:
            return max(1, int(60 - (now - window[0])))
        window.append(now)
        if len(_calls) > 5000:
            for stale in [k for k, w in _calls.items() if not w or now - w[-1] > 60]:
                _calls.pop(stale, None)
    return 0


def _unauthorized(message, status=401):
    response = jsonify({'error': message})
    response.status_code = status
    if status == 401:
        response.headers['WWW-Authenticate'] = 'Bearer'
    return response


def _authenticate(path_key):
    """(api_key, None) or (None, error response). Never logs the key."""
    from app.services import auth_throttle_service as throttle
    from app.services.api_key_service import ApiKeyService
    from app.services.mcp.auth import resolve_key
    from app.utils.client_ip import get_client_ip

    ip = get_client_ip()
    blocked, retry = throttle.is_blocked(ip)
    if blocked:
        response = jsonify({'error': 'too many failed attempts, try again later'})
        response.status_code = 429
        response.headers['Retry-After'] = str(retry)
        return None, response

    api_key = getattr(g, 'api_key', None)   # X-API-Key, already validated by the middleware
    if api_key is None:
        raw = path_key
        if raw is None:
            header = request.headers.get('Authorization', '')
            raw = header[7:].strip() if header[:7].lower() == 'bearer ' else None
        api_key = resolve_key(raw, ip) if raw else None
        if api_key is None:
            throttle.register_failure(ip)
            return None, _unauthorized('missing or invalid API key' if raw is not None or path_key
                                       else 'an API key is required (Authorization: Bearer <key>)')
        g.api_key = api_key
        g.api_key_user = api_key.user
    throttle.reset(ip)

    if not ApiKeyService.check_scope(api_key, MCP_SCOPE):
        return None, _unauthorized(f'this API key lacks the "{MCP_SCOPE}" scope', 403)
    user = api_key.user
    if user is None or not user.is_active:
        return None, _unauthorized('Account is deactivated', 403)
    wait = _rate_limited(api_key.id)
    if wait:
        response = jsonify({'error': f'rate limit: at most {RATE_LIMIT} calls per minute per key'})
        response.status_code = 429
        response.headers['Retry-After'] = str(wait)
        return None, response
    return api_key, None


@mcp_bp.route('', methods=['GET'])
@mcp_bp.route('/k/<key>', methods=['GET'])
def mcp_no_stream(key=None):
    """No server-initiated stream and no sessions: every reply rides its POST."""
    response = jsonify({'error': 'use POST for MCP messages'})
    response.status_code = 405
    response.headers['Allow'] = 'POST'
    return response


@mcp_bp.route('', methods=['POST'])
@mcp_bp.route('/k/<key>', methods=['POST'])
@limiter.exempt  # the per-IP default would cap every key behind one proxy; see _rate_limited
@require_scope(MCP_SCOPE)
def mcp_endpoint(key=None):
    """One JSON-RPC message (or a batch) in, its JSON-RPC response out."""
    from app.services.mcp import protocol
    from app.services.mcp.context import ToolContext

    # The request body (env values, argv) is not for the generic audit trail;
    # write tools record their own audit row (tool + site only).
    g.audit_logged = True
    api_key, denied = _authenticate(key)
    if denied is not None:
        return denied
    user = api_key.user
    # Authorization: the key owner's role decides which tools may run —
    # viewers get the read-only tools, developers and admins the write tools —
    # and every tool resolves resources only among the owner's own services.
    ctx = ToolContext(user=user, api_key=api_key, can_write=user.is_developer)

    if (request.content_length or 0) > MAX_BODY:
        return jsonify({'error': f'body larger than {MAX_BODY} bytes'}), 413
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify(protocol.error(None, protocol.PARSE_ERROR, 'Parse error')), 400
    reply = protocol.handle_payload(payload, ctx)
    if reply is None:
        return Response(status=202)
    response = jsonify(reply)
    response.headers['Cache-Control'] = 'no-store'
    return response
