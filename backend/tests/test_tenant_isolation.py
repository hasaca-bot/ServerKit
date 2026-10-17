"""Tenant isolation: repositories only through the caller's own source
connection, servers only for their owner, and migration 112."""
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
import sqlalchemy as sa

from factories import headers_for, make_user


# ------------------------------------------------------------ repositories


@pytest.fixture
def two_devs(app):
    from app import db
    from app.models import SourceConnection
    a = make_user(db, role='developer')
    b = make_user(db, role='developer')
    conns = {}
    for user in (a, b):
        conn = SourceConnection(user_id=user.id, provider='github',
                                provider_username=f'u{user.id}')
        conn.access_token = f'token-{user.id}'
        db.session.add(conn)
        db.session.commit()
        conns[user.id] = conn.id
    return a, b, conns


def _create(client, user, **body):
    body.setdefault('name', 'repo-svc')
    return client.post('/api/v1/apps/from-repository', json=body, headers=headers_for(user))


def test_non_admin_cannot_deploy_a_raw_git_url(client, two_devs):
    a, _b, _conns = two_devs
    with patch('app.services.git_service.GitService.clone_repository') as clone:
        r = _create(client, a, repo_url='https://github.com/someone/else.git')
    assert r.status_code == 403
    clone.assert_not_called()


def test_non_admin_cannot_use_another_users_connection(client, two_devs):
    a, b, conns = two_devs
    with patch('app.services.git_service.GitService.clone_repository') as clone:
        r = _create(client, a, source_connection_id=conns[b.id],
                    repository_full_name='b/private')
    assert r.status_code == 400
    assert 'not found' in r.get_json()['error'].lower()
    clone.assert_not_called()


def test_non_admin_cannot_use_a_repo_their_connection_does_not_grant(client, two_devs):
    a, _b, conns = two_devs
    with patch('app.services.source_connection_service.SourceConnectionService.'
               'connection_can_reach', return_value=False), \
            patch('app.services.git_service.GitService.clone_repository') as clone:
        r = _create(client, a, source_connection_id=conns[a.id],
                    repository_full_name='torvalds/linux')
    assert r.status_code == 403
    clone.assert_not_called()


def test_non_admin_cannot_mix_a_url_into_a_connection_deploy(client, two_devs):
    a, _b, conns = two_devs
    r = _create(client, a, source_connection_id=conns[a.id],
                repository_full_name='a/app', repo_url='https://evil.example/x.git')
    assert r.status_code == 403


def test_non_admin_cannot_set_host_build_commands(client, two_devs):
    a, _b, conns = two_devs
    r = _create(client, a, source_connection_id=conns[a.id],
                repository_full_name='a/app', custom_build_cmd='curl x | sh')
    assert r.status_code == 403


def test_own_connection_and_granted_repo_reaches_the_clone(client, two_devs):
    a, _b, conns = two_devs
    with patch('app.services.source_connection_service.SourceConnectionService.'
               'connection_can_reach', return_value=True), \
            patch('app.services.git_service.GitService.clone_repository',
                  return_value={'success': False, 'error': 'stop here'}) as clone:
        r = _create(client, a, source_connection_id=conns[a.id],
                    repository_full_name='a/app')
    assert r.status_code == 400 and r.get_json()['error'] == 'stop here'
    clone_url = clone.call_args[0][1]
    assert clone_url.startswith('https://x-access-token:') and 'github.com/a/app' in clone_url


def test_viewer_cannot_create_from_repository(client, app):
    from app import db
    r = _create(client, make_user(db, role='viewer'), repo_url='https://github.com/a/b.git')
    assert r.status_code == 403


def test_admin_keeps_raw_url_deploys(client, app):
    from app import db
    admin = make_user(db, role='admin')
    with patch('app.services.git_service.GitService.clone_repository',
               return_value={'success': False, 'error': 'stop here'}) as clone:
        r = _create(client, admin, repo_url='https://github.com/a/b.git')
    assert r.status_code == 400 and r.get_json()['error'] == 'stop here'
    assert clone.call_args[0][1] == 'https://github.com/a/b.git'


def test_buildpack_detect_refuses_paths_and_urls_for_non_admins(client, two_devs):
    a, _b, _conns = two_devs
    r = client.post('/api/v1/buildpacks/detect', json={'path': '/etc'}, headers=headers_for(a))
    assert r.status_code == 403
    r = client.post('/api/v1/buildpacks/detect', json={'repo_url': 'https://x/y.git'},
                    headers=headers_for(a))
    assert r.status_code == 403
    r = client.post('/api/v1/deploy/branches', json={'repo_url': 'https://x/y.git'},
                    headers=headers_for(a))
    assert r.status_code == 403


def test_connection_can_reach_github_oauth_requires_push(app, two_devs):
    from app.services.source_connection_service import SourceConnectionService as S
    a, _b, conns = two_devs
    with patch.object(S, '_github_app_slug', return_value=None), \
            patch.object(S, '_github_get', return_value={'permissions': {'pull': True}}):
        assert S.connection_can_reach(a.id, conns[a.id], 'x/y') is False
    with patch.object(S, '_github_app_slug', return_value=None), \
            patch.object(S, '_github_get', return_value={'permissions': {'push': True}}):
        assert S.connection_can_reach(a.id, conns[a.id], 'x/y') is True
    with patch.object(S, '_github_app_slug', return_value='app'), \
            patch.object(S, '_list_github_app_repos', return_value=[{'full_name': 'A/App'}]):
        assert S.connection_can_reach(a.id, conns[a.id], 'a/app') is True
        assert S.connection_can_reach(a.id, conns[a.id], 'a/other') is False
    # Another user's connection id never reaches anything.
    assert S.connection_can_reach(a.id, conns[_b.id], 'a/app') is False


# ----------------------------------------------------------------- servers


@pytest.fixture
def servers(app):
    from app import db
    from app.models import Server
    a = make_user(db, role='developer')
    b = make_user(db, role='developer')
    admin = make_user(db, role='admin')
    srv = Server(name='a-box', owner_id=a.id)
    db.session.add(srv)
    db.session.commit()
    return a, b, admin, srv.id


_READS = ['/api/v1/servers/{sid}', '/api/v1/servers/{sid}/metrics',
          '/api/v1/servers/{sid}/commands', '/api/v1/servers/{sid}/status',
          '/api/v1/servers/{sid}/docker/containers', '/api/v1/servers/{sid}/proxy']


@pytest.mark.parametrize('url', _READS)
def test_other_tenant_cannot_read_a_server(client, servers, url):
    _a, b, _admin, sid = servers
    with patch('app.services.remote_docker_service.RemoteDockerService.list_containers',
               return_value={'success': True, 'data': []}) as listing:
        r = client.get(url.format(sid=sid), headers=headers_for(b))
    assert r.status_code == 404
    listing.assert_not_called()


def test_other_tenant_cannot_act_on_a_server(client, servers):
    _a, b, _admin, sid = servers
    with patch('app.services.terminal_service.TerminalService.create_session',
               return_value={'success': True}) as create:
        r = client.post(f'/api/v1/servers/{sid}/terminal', json={}, headers=headers_for(b))
    assert r.status_code == 404
    create.assert_not_called()
    r = client.delete(f'/api/v1/servers/{sid}/docker/containers/c1', headers=headers_for(b))
    assert r.status_code == 404


def test_owner_and_admin_reach_the_server(client, servers):
    a, _b, admin, sid = servers
    assert client.get(f'/api/v1/servers/{sid}', headers=headers_for(a)).status_code == 200
    assert client.get(f'/api/v1/servers/{sid}', headers=headers_for(admin)).status_code == 200


def test_lists_and_overview_are_owner_scoped(client, servers):
    a, b, admin, sid = servers
    assert [s['id'] for s in client.get('/api/v1/servers', headers=headers_for(a)).get_json()] == [sid]
    assert client.get('/api/v1/servers', headers=headers_for(b)).get_json() == []
    overview = client.get('/api/v1/servers/overview', headers=headers_for(b)).get_json()
    assert overview['summary']['total'] == 0
    assert all(s.get('id') != sid for s in
               client.get('/api/v1/servers/available', headers=headers_for(b)).get_json())
    assert len(client.get('/api/v1/servers', headers=headers_for(admin)).get_json()) == 1


def test_server_access_helpers(app, servers):
    from app.exceptions import NotFoundError
    from app.services.server_access import ServerAccess
    a, b, admin, sid = servers
    assert [s.id for s in ServerAccess.scoped_servers(a)] == [sid]
    assert ServerAccess.scoped_servers(b).count() == 0
    assert ServerAccess.scoped_servers(admin).count() == 1
    assert ServerAccess.get_owned_server_or_404(a, sid).id == sid
    with pytest.raises(NotFoundError):
        ServerAccess.get_owned_server_or_404(b, sid)


def test_created_server_is_owned_by_its_creator(client, app):
    from app import db
    from app.models import Server
    from app.services.plan_quota_service import PlanQuotaService
    dev = make_user(db, role='developer')
    PlanQuotaService.assign_plan(dev, 'pro')  # one server slot
    r = client.post('/api/v1/servers', json={}, headers=headers_for(dev))
    assert r.status_code == 201
    assert Server.query.get(r.get_json()['id']).owner_id == dev.id
    assert client.post('/api/v1/servers', json={}, headers=headers_for(dev)).status_code == 402


# --------------------------------------------------------------- migration


def _load_migration():
    path = Path(__file__).parents[1] / 'migrations/versions/112_plans_and_ownership.py'
    spec = importlib.util.spec_from_file_location('m112', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_112_seeds_plans_and_backfills_server_owners(tmp_path, monkeypatch):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    migration = _load_migration()
    assert migration.down_revision == '111_app_build_filter'
    engine = sa.create_engine('sqlite:///' + (tmp_path / 'm112.db').as_posix())
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)')
        conn.exec_driver_sql('CREATE TABLE servers (id TEXT PRIMARY KEY, name TEXT, '
                             'registered_by INTEGER REFERENCES users(id))')
        conn.exec_driver_sql("INSERT INTO users VALUES (1, 'alice')")
        conn.exec_driver_sql("INSERT INTO servers VALUES ('s1', 'box', 1), ('s2', 'orphan', NULL)")
        monkeypatch.setattr(migration, 'op', Operations(MigrationContext.configure(conn)))
        migration.upgrade()
        plans = conn.exec_driver_sql(
            'SELECT slug, max_apps, max_servers, max_cpu, max_memory_mb, sleep_after_minutes '
            'FROM plans ORDER BY id').all()
        owners = dict(conn.exec_driver_sql('SELECT id, owner_id FROM servers').all())
        user_cols = [r[1] for r in conn.exec_driver_sql('PRAGMA table_info(users)')]
        # Re-running is a no-op (idempotent seeds, add-if-missing columns).
        migration.upgrade()
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM plans').scalar() == 3
    engine.dispose()

    assert plans == [('hobby', 3, 0, 0.25, 256, 15), ('pro', 25, 1, 2.0, 4096, None),
                     ('team', 100, 5, 4.0, 8192, None)]
    assert owners == {'s1': 1, 's2': None}
    assert 'plan_id' in user_cols
