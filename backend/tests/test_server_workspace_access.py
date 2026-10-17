"""A developer outside a server's workspace must not operate it over REST —
the same rule the Socket.IO server rooms and terminal already enforce."""
from unittest.mock import patch

import pytest

from factories import headers_for, make_server, make_user


@pytest.fixture
def setup(app):
    from app import db
    from app.services.workspace_service import WorkspaceService
    admin = make_user(db, 'swa_admin', 'admin')
    member = make_user(db, 'swa_member', 'developer')
    outsider = make_user(db, 'swa_outsider', 'developer')
    ws = WorkspaceService.create_workspace({'name': 'SWA'}, admin.id)
    WorkspaceService.add_member(ws.id, member.id)
    mine = WorkspaceService.create_workspace({'name': 'SWA-out'}, outsider.id)
    # The member owns it: non-admins operate only servers they own, and the
    # workspace rule narrows that further.
    server = make_server(db, name='swa-box', workspace_id=ws.id, owner_id=member.id)
    return {'server': server.id, 'member': member.id, 'outsider': outsider.id,
            'admin': admin.id, 'own_ws': mine.id}


_ROUTES = [
    ('post', '/api/v1/servers/{sid}/terminal', {}),
    ('post', '/api/v1/servers/{sid}/regenerate-token', {}),
    ('put', '/api/v1/servers/{sid}/workspace', {'workspace_id': '{own_ws}'}),
]


@pytest.mark.parametrize('method,url,body', _ROUTES)
def test_outsider_is_refused(client, setup, method, url, body):
    from app.models.server import Server
    body = {k: setup['own_ws'] if v == '{own_ws}' else v for k, v in body.items()}
    before = Server.query.get(setup['server'])
    token_hash, ws_id = before.registration_token_hash, before.workspace_id
    with patch('app.services.terminal_service.TerminalService.create_session',
               return_value={'success': True}) as create:
        resp = getattr(client, method)(url.format(sid=setup['server']), json=body,
                                       headers=headers_for(setup['outsider']))
    assert resp.status_code == 404
    create.assert_not_called()
    after = Server.query.get(setup['server'])
    assert after.registration_token_hash == token_hash
    assert after.workspace_id == ws_id


@pytest.mark.parametrize('who', ['member', 'admin'])
def test_member_and_admin_can_open_terminal(client, setup, who):
    with patch('app.services.terminal_service.TerminalService.create_session',
               return_value={'success': True, 'session_id': 's1'}) as create:
        resp = client.post(f"/api/v1/servers/{setup['server']}/terminal", json={},
                           headers=headers_for(setup[who]))
    assert resp.status_code == 200
    create.assert_called_once()


def test_member_can_regenerateheaders_for(client, setup):
    resp = client.post(f"/api/v1/servers/{setup['server']}/regenerate-token",
                       json={}, headers=headers_for(setup['member']))
    assert resp.status_code == 200
    assert resp.get_json()['registration_token']


def test_default_workspace_servers_are_owner_only(client, setup):
    """Servers in the Default workspace (which nobody joins) are operable by
    their owner only; another developer gets a 404 (tenant isolation)."""
    from app import db
    from app.services.workspace_service import WorkspaceService
    server = make_server(db, name='swa-default', owner_id=setup['outsider'],
                         workspace_id=WorkspaceService.ensure_default_workspace().id)
    with patch('app.services.terminal_service.TerminalService.create_session',
               return_value={'success': True, 'session_id': 's2'}):
        resp = client.post(f'/api/v1/servers/{server.id}/terminal', json={},
                           headers=headers_for(setup['outsider']))
        assert resp.status_code == 200
        resp = client.post(f'/api/v1/servers/{server.id}/terminal', json={},
                           headers=headers_for(setup['member']))
    assert resp.status_code == 404
