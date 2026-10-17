"""Core MCP endpoint: transport, JSON-RPC handshake and authentication."""
import pytest

from factories import make_user, headers_for

MCP = '/api/v1/mcp'


@pytest.fixture(autouse=True)
def _clean_state():
    from app.api.mcp import reset_rate_limits
    from app.services import auth_throttle_service
    reset_rate_limits()
    auth_throttle_service.reset_all()
    yield
    reset_rate_limits()
    auth_throttle_service.reset_all()


def make_key(db, role='developer', scopes=('mcp',), user=None):
    from app.services.api_key_service import ApiKeyService
    user = user or make_user(db, role=role)
    _, raw = ApiKeyService.create_key(user.id, name='mcp', scopes=list(scopes) if scopes is not None else None)
    return user, raw


def rpc(client, key, method, params=None, mid=1, path=MCP, header='bearer'):
    body = {'jsonrpc': '2.0', 'method': method}
    if mid is not None:
        body['id'] = mid
    if params is not None:
        body['params'] = params
    headers = {}
    if key and header == 'bearer':
        headers['Authorization'] = f'Bearer {key}'
    elif key and header == 'x-api-key':
        headers['X-API-Key'] = key
    return client.post(path, json=body, headers=headers)


def test_initialize_negotiates_known_versions(client, db_session):
    _, key = make_key(db_session)
    for asked in ('2025-06-18', '2025-03-26'):
        resp = rpc(client, key, 'initialize', {'protocolVersion': asked, 'capabilities': {},
                                              'clientInfo': {'name': 't', 'version': '1'}})
        assert resp.status_code == 200
        result = resp.get_json()['result']
        assert result['protocolVersion'] == asked
        assert result['serverInfo']['name'] == 'serverkit'
        assert 'tools' in result['capabilities']
    resp = rpc(client, key, 'initialize', {'protocolVersion': '1999-01-01'})
    assert resp.get_json()['result']['protocolVersion'] == '2025-06-18'


def test_notification_gets_202_without_body(client, db_session):
    _, key = make_key(db_session)
    resp = rpc(client, key, 'notifications/initialized', mid=None)
    assert resp.status_code == 202
    assert resp.data == b''


def test_ping_and_unknown_method(client, db_session):
    _, key = make_key(db_session)
    assert rpc(client, key, 'ping').get_json() == {'jsonrpc': '2.0', 'id': 1, 'result': {}}
    error = rpc(client, key, 'nope/what').get_json()['error']
    assert error['code'] == -32601


def test_invalid_request_and_parse_error(client, db_session):
    _, key = make_key(db_session)
    resp = client.post(MCP, data='{not json', headers={'Authorization': f'Bearer {key}',
                                                      'Content-Type': 'application/json'})
    assert resp.status_code == 400
    assert resp.get_json()['error']['code'] == -32700
    resp = client.post(MCP, json={'id': 3, 'method': 'ping'}, headers={'Authorization': f'Bearer {key}'})
    assert resp.get_json()['error']['code'] == -32600
    resp = rpc(client, key, 'tools/call', {'name': 'no_such_tool', 'arguments': {}})
    assert resp.get_json()['error']['code'] == -32602


def test_batch(client, db_session):
    _, key = make_key(db_session)
    resp = client.post(MCP, headers={'Authorization': f'Bearer {key}'}, json=[
        {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
    ])
    replies = resp.get_json()
    assert [r['id'] for r in replies] == [1, 2]


def test_get_is_405(client, db_session):
    assert client.get(MCP).status_code == 405
    assert client.get(f'{MCP}/k/sk_whatever_long_enough_key').status_code == 405


def test_missing_and_bad_key(client, db_session):
    assert rpc(client, None, 'ping').status_code == 401
    resp = rpc(client, 'sk_' + '0' * 40, 'ping')
    assert resp.status_code == 401
    assert resp.headers.get('WWW-Authenticate') == 'Bearer'
    assert client.post(f'{MCP}/k/sk_{"1" * 40}', json={'jsonrpc': '2.0', 'id': 1,
                                                      'method': 'ping'}).status_code == 401


def test_browser_jwt_is_not_accepted(client, db_session):
    user = make_user(db_session, role='admin')
    resp = client.post(MCP, json={'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, headers=headers_for(user))
    assert resp.status_code == 401


def test_bad_keys_are_throttled(client, db_session):
    for _ in range(10):
        rpc(client, 'sk_' + 'f' * 40, 'ping')
    _, key = make_key(db_session)
    resp = rpc(client, key, 'ping')
    assert resp.status_code == 429
    assert resp.headers.get('Retry-After')


def test_scope_is_required(client, db_session):
    _, key = make_key(db_session, scopes=['apps:read'])
    assert rpc(client, key, 'ping').status_code == 403
    _, key = make_key(db_session, scopes=['apps:read'])
    assert rpc(client, key, 'ping', header='x-api-key').status_code == 403
    _, full = make_key(db_session, scopes=['*'])
    assert rpc(client, full, 'ping').status_code == 200
    _, mcp = make_key(db_session, scopes=['mcp'])
    assert rpc(client, mcp, 'ping', header='x-api-key').status_code == 200


def test_path_key_works(client, db_session):
    _, key = make_key(db_session)
    resp = client.post(f'{MCP}/k/{key}', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    assert resp.status_code == 200
    assert resp.get_json()['result']['tools']


def test_revoked_and_inactive(client, db_session):
    from app.services.api_key_service import ApiKeyService
    user, key = make_key(db_session)
    ApiKeyService.revoke_key(ApiKeyService.list_keys(user.id)[0].id, user.id)
    assert rpc(client, key, 'ping').status_code == 401
    user, key = make_key(db_session)
    user.is_active = False
    db_session.session.commit()
    assert rpc(client, key, 'ping').status_code == 403


def test_rate_limit_per_key(client, db_session, monkeypatch):
    import app.api.mcp as mcp_api
    monkeypatch.setattr(mcp_api, 'RATE_LIMIT', 3)
    _, key = make_key(db_session)
    codes = [rpc(client, key, 'ping').status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    _, other = make_key(db_session)
    assert rpc(client, other, 'ping').status_code == 200


def test_path_key_is_masked_in_audit_and_logs(client, db_session):
    from app.utils.secret_paths import mask_path, SecretPathFilter
    import logging
    _, key = make_key(db_session)
    assert key not in mask_path(f'/api/v1/mcp/k/{key}')
    record = logging.LogRecord('werkzeug', logging.INFO, __file__, 1, '"%s" %s',
                               (f'POST /api/v1/mcp/k/{key} HTTP/1.1', 200), None)
    SecretPathFilter().filter(record)
    assert key not in record.getMessage()

    from app.models.audit_log import AuditLog
    rpc(client, key, 'tools/call', {'name': 'list_sites', 'arguments': {}}, path=f'{MCP}/k/{key}')
    for row in AuditLog.query.all():
        assert key not in str(row.details)


def test_tools_list_has_parity_and_new_tools(client, db_session):
    _, key = make_key(db_session)
    names = {t['name'] for t in rpc(client, key, 'tools/list').get_json()['result']['tools']}
    parity = {'get_instructions', 'list_sites', 'publish_site', 'get_job', 'site_info', 'set_env',
              'get_logs', 'redeploy_site', 'stop_site', 'start_site', 'create_database',
              'delete_database', 'list_cron', 'add_cron', 'delete_cron', 'run_command', 'delete_site'}
    new = {'list_repos', 'list_deploys', 'rollback_site', 'restart_site', 'get_metrics',
           'list_env_keys', 'list_domains', 'add_domain', 'remove_domain', 'list_previews', 'get_usage'}
    assert parity | new <= names
    for tool in rpc(client, key, 'tools/list').get_json()['result']['tools']:
        assert tool['inputSchema']['type'] == 'object'
        assert tool['description']


def test_get_instructions_is_generic(client, db_session):
    user, key = make_key(db_session)
    text = rpc(client, key, 'tools/call', {'name': 'get_instructions', 'arguments': {}}).get_json()
    body = text['result']['content'][0]['text']
    assert text['result']['isError'] is False
    assert 'publish_site' in body and user.username not in body
