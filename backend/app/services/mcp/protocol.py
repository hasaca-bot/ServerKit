"""MCP over Streamable HTTP with JSON responses: the JSON-RPC 2.0 layer.

One POST carries one JSON-RPC message (or a batch). Requests get a response
object; notifications (no ``id``) get none, and a POST holding only
notifications is answered 202 with no body by the route.
"""
import json
import logging

from app.services.mcp import registry
from app.services.mcp.errors import ToolError

logger = logging.getLogger(__name__)

PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
SERVER_INFO = {'name': 'serverkit', 'version': '1.0.0'}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    'Publishes web apps from your own GitHub repositories (through your GitHub '
    'connection in the panel) at https://<name>.<base domain> and lets you run '
    'your own sites: environment, logs, deploys and rollbacks, start/stop, '
    'databases, cron, one-shot commands inside the site container, domains and '
    'deletion. Call get_instructions first, then list_sites; slow actions return '
    'a job id to poll with get_job.'
)


def error(mid, code, message):
    return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': code, 'message': message}}


def _ok(mid, result):
    return {'jsonrpc': '2.0', 'id': mid, 'result': result}


def call_tool(ctx, name, args):
    """Run one tool. Returns (is_error, payload)."""
    tool = registry.get(name) if isinstance(name, str) else None
    if tool is None:
        return True, {'error': f'unknown tool {str(name)[:60]!r}; see tools/list'}
    if tool.write and not ctx.can_write:
        return True, {'error': f'{name} changes resources and your role is read-only'}
    try:
        result = False, tool.handler(ctx, args)
    except ToolError as exc:
        payload = {'error': str(exc)}
        if exc.details:
            payload.update(exc.details)
        result = True, payload
    except Exception:  # noqa: BLE001 - never leak internals to the caller
        logger.exception('MCP tool %s failed', name)
        try:
            from app import db
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        result = True, {'error': f'{name} failed on the server; try again or check the panel'}
    if tool.write:
        _audit(ctx, name, args, ok=not result[0])
    return result


def _audit(ctx, name, args, ok):
    """One audit row per write tool call: tool and target, never the arguments."""
    try:
        from app.services.audit_service import AuditService
        site = args.get('site') or args.get('name')
        AuditService.log('mcp.tool_call', user_id=ctx.user.id, target_type='application',
                         details={'tool': name, 'site': site if isinstance(site, str) else None,
                                  'ok': ok,
                                  'api_key_id': getattr(ctx.api_key, 'id', None)})
    except Exception:  # noqa: BLE001 - auditing must never fail the call
        logger.exception('MCP audit failed for %s', name)


def handle(msg, ctx):
    """One JSON-RPC message -> response dict, or None for a notification."""
    if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or not isinstance(msg.get('method'), str):
        return error(msg.get('id') if isinstance(msg, dict) else None, INVALID_REQUEST, 'Invalid Request')
    method, mid = msg['method'], msg.get('id')
    if 'id' not in msg:
        return None  # notification, e.g. notifications/initialized
    params = msg.get('params')
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return error(mid, INVALID_PARAMS, 'params must be an object')

    if method == 'initialize':
        asked = params.get('protocolVersion')
        return _ok(mid, {
            'protocolVersion': asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': SERVER_INFO,
            'instructions': INSTRUCTIONS,
        })
    if method == 'ping':
        return _ok(mid, {})
    if method == 'tools/list':
        return _ok(mid, {'tools': [t.describe() for t in registry.all_tools()
                                   if ctx.can_write or not t.write]})
    if method == 'tools/call':
        name = params.get('name')
        args = params.get('arguments')
        if args is None:
            args = {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return error(mid, INVALID_PARAMS, 'tools/call needs "name" (string) and "arguments" (object)')
        if registry.get(name) is None:
            return error(mid, INVALID_PARAMS, f'Unknown tool: {name[:60]}')
        is_error, payload = call_tool(ctx, name, args)
        # Service text that may carry a secret is redacted where it enters a payload.
        text = payload if isinstance(payload, str) else json.dumps(payload, indent=1, default=str)
        return _ok(mid, {'content': [{'type': 'text', 'text': text}], 'isError': is_error})
    if method in ('resources/list', 'prompts/list'):
        return _ok(mid, {method.split('/')[0]: []})
    return error(mid, METHOD_NOT_FOUND, f'Method not found: {method[:80]}')


def handle_payload(payload, ctx):
    """A parsed POST body -> (response body | None)."""
    if isinstance(payload, list):
        if not payload:
            return error(None, INVALID_REQUEST, 'Invalid Request: empty batch')
        replies = [r for r in (handle(m, ctx) for m in payload[:50]) if r is not None]
        return replies or None
    return handle(payload, ctx)
