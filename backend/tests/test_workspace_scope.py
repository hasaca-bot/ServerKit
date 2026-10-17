"""Tests for the workspace scoping foundation (#33 core)."""
import pytest


def _mk_user(db, username, role='admin'):
    from app.models import User
    from werkzeug.security import generate_password_hash
    u = User(email=f'{username}@t.local', username=username,
             password_hash=generate_password_hash('x'), role=role, is_active=True)
    db.session.add(u)
    db.session.commit()
    return u


def _token(user_id):
    from flask_jwt_extended import create_access_token
    return {'Authorization': f'Bearer {create_access_token(identity=user_id)}'}


# ---- ensure_default_workspace --------------------------------------------

def test_ensure_default_workspace_idempotent(app):
    from app.services.workspace_service import WorkspaceService
    from app.models.workspace import Workspace
    ws1 = WorkspaceService.ensure_default_workspace()
    ws2 = WorkspaceService.ensure_default_workspace()
    assert ws1.id == ws2.id
    assert Workspace.query.filter_by(slug='default').count() == 1
    # Quota columns must be concrete ints (0), never NULL — the model does int math on them.
    assert ws1.max_users == 0


# ---- resolve_workspace_id -------------------------------------------------

def test_resolve_workspace_id_branches(app):
    from app import db
    from app.services.workspace_service import WorkspaceService

    admin = _mk_user(db, 'r_admin', 'admin')
    owner = _mk_user(db, 'r_owner', 'developer')
    outsider = _mk_user(db, 'r_outsider', 'developer')

    # No active context.
    assert WorkspaceService.resolve_workspace_id(outsider, None) is None
    assert WorkspaceService.resolve_workspace_id(outsider, '') is None
    assert WorkspaceService.resolve_workspace_id(outsider, 'all') is None

    # Malformed / unknown -> lenient fall back to no scope (never raises).
    assert WorkspaceService.resolve_workspace_id(outsider, 'abc') is None
    assert WorkspaceService.resolve_workspace_id(outsider, 99999) is None

    # A workspace owned by `owner` (creator becomes owner-member).
    ws = WorkspaceService.create_workspace({'name': 'Acme'}, owner.id)

    # A non-member non-admin falls back to no scope (no error, but no access either).
    assert WorkspaceService.resolve_workspace_id(outsider, ws.id) is None

    # An admin who is NOT a member resolves it (admin bypass).
    assert WorkspaceService.resolve_workspace_id(admin, ws.id) == ws.id

    # A member resolves it.
    WorkspaceService.add_member(ws.id, outsider.id, 'member')
    assert WorkspaceService.resolve_workspace_id(outsider, ws.id) == ws.id


def test_resolve_deactivated_user_falls_back(app):
    from app import db
    from app.services.workspace_service import WorkspaceService

    admin = _mk_user(db, 'd_admin', 'admin')
    ws = WorkspaceService.create_workspace({'name': 'DW'}, admin.id)
    member = _mk_user(db, 'd_member', 'developer')
    WorkspaceService.add_member(ws.id, member.id, 'member')
    member.is_active = False
    db.session.commit()

    # A deactivated account doesn't drive workspace scoping — it falls back to no scope.
    assert WorkspaceService.resolve_workspace_id(member, ws.id) is None
    assert WorkspaceService.resolve_workspace_id(member, None) is None


# ---- scope_query ----------------------------------------------------------

def test_scope_query_branches(app):
    from app import db
    from app.services.workspace_service import WorkspaceService
    from app.models import Application

    admin = _mk_user(db, 'q_admin', 'admin')
    dev = _mk_user(db, 'q_dev', 'developer')
    a1 = Application(name='a1', app_type='php', user_id=dev.id, workspace_id=1)
    a2 = Application(name='a2', app_type='php', user_id=admin.id, workspace_id=2)
    db.session.add_all([a1, a2])
    db.session.commit()

    def names(q):
        return {a.name for a in q.all()}

    # Active workspace context -> filter by workspace_id.
    q = WorkspaceService.scope_query(Application.query, Application, dev, workspace_id=1, owner_attr='user_id')
    assert names(q) == {'a1'}

    # No context, non-admin, owner_attr -> own rows only (prior behavior).
    q = WorkspaceService.scope_query(Application.query, Application, dev, workspace_id=None, owner_attr='user_id')
    assert names(q) == {'a1'}

    # No context, admin -> everything (prior behavior).
    q = WorkspaceService.scope_query(Application.query, Application, admin, workspace_id=None, owner_attr='user_id')
    assert names(q) == {'a1', 'a2'}

    # No context, owner_attr=None (a global resource like servers) -> everything, even for a non-admin.
    q = WorkspaceService.scope_query(Application.query, Application, dev, workspace_id=None, owner_attr=None)
    assert names(q) == {'a1', 'a2'}


# ---- API: applications ----------------------------------------------------

def test_get_apps_scoping_api(app, client):
    from app import db
    from app.services.workspace_service import WorkspaceService
    from app.models import Application

    admin = _mk_user(db, 'api_admin', 'admin')
    dev = _mk_user(db, 'api_dev', 'developer')
    ws = WorkspaceService.create_workspace({'name': 'WS1'}, admin.id)
    other = WorkspaceService.create_workspace({'name': 'WS2'}, admin.id)
    db.session.add_all([
        Application(name='in-ws', app_type='php', user_id=dev.id, workspace_id=ws.id),
        Application(name='other-ws', app_type='php', user_id=dev.id, workspace_id=other.id),
    ])
    db.session.commit()

    # No context: dev sees its own apps regardless of workspace (prior behavior preserved).
    r = client.get('/api/v1/apps', headers=_token(dev.id))
    assert r.status_code == 200
    assert {a['name'] for a in r.get_json()['apps']} == {'in-ws', 'other-ws'}

    # Requesting a workspace the dev isn't a member of -> lenient fall back to no
    # scope (stale/forbidden context never breaks the page; shows own apps).
    r = client.get('/api/v1/apps', headers={**_token(dev.id), 'X-Workspace-Id': str(ws.id)})
    assert r.status_code == 200
    assert {a['name'] for a in r.get_json()['apps']} == {'in-ws', 'other-ws'}

    # Once a member, the list is filtered to that workspace.
    WorkspaceService.add_member(ws.id, dev.id, 'member')
    r = client.get('/api/v1/apps', headers={**_token(dev.id), 'X-Workspace-Id': str(ws.id)})
    assert r.status_code == 200
    assert {a['name'] for a in r.get_json()['apps']} == {'in-ws'}


def test_workspace_context_never_broadens_access(app, client):
    """A member activating a workspace must NOT see another user's app that lives
    in the same workspace — scoping only narrows within the user's own rows."""
    from app import db
    from app.services.workspace_service import WorkspaceService
    from app.models import Application

    admin = _mk_user(db, 'esc_admin', 'admin')
    me = _mk_user(db, 'esc_me', 'developer')
    other = _mk_user(db, 'esc_other', 'developer')
    ws = WorkspaceService.create_workspace({'name': 'Shared'}, admin.id)
    WorkspaceService.add_member(ws.id, me.id, 'member')
    db.session.add_all([
        Application(name='mine', app_type='php', user_id=me.id, workspace_id=ws.id),
        Application(name='theirs', app_type='php', user_id=other.id, workspace_id=ws.id),
    ])
    db.session.commit()

    # `me` is a member of the shared workspace but must still only see its own app.
    r = client.get('/api/v1/apps', headers={**_token(me.id), 'X-Workspace-Id': str(ws.id)})
    assert r.status_code == 200
    assert {a['name'] for a in r.get_json()['apps']} == {'mine'}

    # An admin, by contrast, sees everything in the workspace.
    r = client.get('/api/v1/apps', headers={**_token(admin.id), 'X-Workspace-Id': str(ws.id)})
    assert {a['name'] for a in r.get_json()['apps']} == {'mine', 'theirs'}


def test_create_app_stamps_workspace(app, client):
    from app.services.workspace_service import WorkspaceService
    from app import db

    dev = _mk_user(db, 'cre_dev', 'developer')

    # No context -> stamped with the default workspace.
    r = client.post('/api/v1/apps', headers=_token(dev.id), json={'name': 'newapp', 'app_type': 'php'})
    assert r.status_code == 201
    assert r.get_json()['app']['workspace_id'] == WorkspaceService.ensure_default_workspace().id

    # With a workspace context (member) -> stamped with that workspace.
    admin = _mk_user(db, 'cre_admin', 'admin')
    ws = WorkspaceService.create_workspace({'name': 'CreWS'}, admin.id)
    WorkspaceService.add_member(ws.id, dev.id, 'member')
    r = client.post('/api/v1/apps', headers={**_token(dev.id), 'X-Workspace-Id': str(ws.id)},
                    json={'name': 'wsapp', 'app_type': 'php'})
    assert r.status_code == 201
    assert r.get_json()['app']['workspace_id'] == ws.id


# ---- migration backfill (raw SQL, run against real SQLite) ----------------

def test_migration_backfill_attaches_resources_and_members(app):
    import importlib.util
    import os
    from app import db
    from app.models import Application, Server
    from app.models.workspace import Workspace, WorkspaceMember

    u1 = _mk_user(db, 'bf_u1', 'admin')
    u2 = _mk_user(db, 'bf_u2', 'developer')
    db.session.add_all([
        Application(name='bf-app', app_type='php', user_id=u1.id),
        Server(name='bf-srv', registered_by=u1.id),
    ])
    db.session.commit()

    # Load the migration module by path (its name starts with a digit) and run
    # the backfill against the live connection — exercising the actual raw SQL.
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        'migrations', 'versions', '015_workspace_scope.py')
    spec = importlib.util.spec_from_file_location('mig015', path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    mig._backfill(db.session.connection())
    db.session.commit()

    default = Workspace.query.filter_by(slug='default').first()
    assert default is not None
    assert Application.query.filter_by(name='bf-app').first().workspace_id == default.id
    assert Server.query.filter_by(name='bf-srv').first().workspace_id == default.id
    assert WorkspaceMember.query.filter_by(workspace_id=default.id, user_id=u1.id).first() is not None
    assert WorkspaceMember.query.filter_by(workspace_id=default.id, user_id=u2.id).first() is not None

    # Idempotent: a second run creates no duplicate members and doesn't error.
    mig._backfill(db.session.connection())
    db.session.commit()
    assert WorkspaceMember.query.filter_by(workspace_id=default.id, user_id=u1.id).count() == 1
    assert Workspace.query.filter_by(slug='default').count() == 1


# ---- API: servers ---------------------------------------------------------

def test_set_app_workspace_api(app, client):
    from app import db
    from app.services.workspace_service import WorkspaceService
    from app.models import Application

    owner = _mk_user(db, 'mv_owner', 'developer')
    other = _mk_user(db, 'mv_other', 'developer')
    admin = _mk_user(db, 'mv_admin', 'admin')
    ws = WorkspaceService.create_workspace({'name': 'MoveWS'}, admin.id)
    default_id = WorkspaceService.ensure_default_workspace().id
    a = Application(name='movable', app_type='php', user_id=owner.id, workspace_id=default_id)
    db.session.add(a)
    db.session.commit()
    app_id = a.id

    # Owner isn't a member of the target -> 403.
    r = client.put(f'/api/v1/apps/{app_id}/workspace', json={'workspace_id': ws.id}, headers=_token(owner.id))
    assert r.status_code == 403

    # Once a member, the move succeeds.
    WorkspaceService.add_member(ws.id, owner.id, 'member')
    r = client.put(f'/api/v1/apps/{app_id}/workspace', json={'workspace_id': ws.id}, headers=_token(owner.id))
    assert r.status_code == 200 and r.get_json()['app']['workspace_id'] == ws.id

    # A null target moves it back to Default.
    r = client.put(f'/api/v1/apps/{app_id}/workspace', json={'workspace_id': None}, headers=_token(owner.id))
    assert r.status_code == 200 and r.get_json()['app']['workspace_id'] == default_id

    # A non-owner non-admin can't reassign it.
    r = client.put(f'/api/v1/apps/{app_id}/workspace', json={'workspace_id': None}, headers=_token(other.id))
    assert r.status_code == 403

    # Unknown target workspace -> 404 (admin passes the ownership gate).
    r = client.put(f'/api/v1/apps/{app_id}/workspace', json={'workspace_id': 999999}, headers=_token(admin.id))
    assert r.status_code == 404


def test_set_server_workspace_api(app, client):
    from app import db
    from app.services.workspace_service import WorkspaceService
    from app.models import Server

    admin = _mk_user(db, 'sw_admin', 'admin')
    dev = _mk_user(db, 'sw_dev', 'developer')
    viewer = _mk_user(db, 'sw_viewer', 'viewer')
    ws = WorkspaceService.create_workspace({'name': 'SrvWS'}, admin.id)
    s = Server(name='srv-move', registered_by=admin.id, owner_id=dev.id,
               workspace_id=WorkspaceService.ensure_default_workspace().id)
    db.session.add(s)
    db.session.commit()
    sid = s.id

    # A developer who isn't a member of the target -> 403.
    r = client.put(f'/api/v1/servers/{sid}/workspace', json={'workspace_id': ws.id}, headers=_token(dev.id))
    assert r.status_code == 403

    # Admin can move it (admin bypass).
    r = client.put(f'/api/v1/servers/{sid}/workspace', json={'workspace_id': ws.id}, headers=_token(admin.id))
    assert r.status_code == 200 and r.get_json()['server']['workspace_id'] == ws.id

    # A viewer is blocked by @developer_required.
    r = client.put(f'/api/v1/servers/{sid}/workspace', json={'workspace_id': None}, headers=_token(viewer.id))
    assert r.status_code == 403


def test_servers_list_is_owner_scoped_without_context(app, client):
    from app import db
    from app.models import Server

    admin = _mk_user(db, 'srv_admin', 'admin')
    dev = _mk_user(db, 'srv_dev', 'developer')
    db.session.add(Server(name='srv-a', registered_by=admin.id, owner_id=admin.id))
    db.session.add(Server(name='srv-dev', registered_by=dev.id, owner_id=dev.id))
    db.session.commit()

    # Tenant isolation: a non-admin lists only the servers they own.
    r = client.get('/api/v1/servers', headers=_token(dev.id))
    assert r.status_code == 200
    assert [s['name'] for s in r.get_json()] == ['srv-dev']
    # The admin still sees every server.
    r = client.get('/api/v1/servers', headers=_token(admin.id))
    assert {s['name'] for s in r.get_json()} == {'srv-a', 'srv-dev'}


def test_wordpress_sites_scoping_api(app, client, wp_extension):
    from app import db
    from app.services.workspace_service import WorkspaceService
    from app.models import Application, WordPressSite

    admin = _mk_user(db, 'wp_admin', 'admin')
    ws_a = WorkspaceService.create_workspace({'name': 'WP-A'}, admin.id)
    ws_b = WorkspaceService.create_workspace({'name': 'WP-B'}, admin.id)

    def mk_site(name, ws_id):
        a = Application(name=name, app_type='wordpress', user_id=admin.id, workspace_id=ws_id)
        db.session.add(a)
        db.session.commit()
        db.session.add(WordPressSite(application_id=a.id, is_production=True))
        db.session.commit()

    mk_site('site-a', ws_a.id)
    mk_site('site-b', ws_b.id)

    # No context: the WP hub is global -> both sites.
    r = client.get('/api/v1/wordpress/sites', headers=_token(admin.id))
    assert r.status_code == 200
    assert {s['name'] for s in r.get_json()['sites']} == {'site-a', 'site-b'}

    # Scoped to workspace A (via the site's parent application) -> only site-a.
    r = client.get('/api/v1/wordpress/sites', headers={**_token(admin.id), 'X-Workspace-Id': str(ws_a.id)})
    assert r.status_code == 200
    assert {s['name'] for s in r.get_json()['sites']} == {'site-a'}


# ---- API: domains (app-children, scoped through parent Application) --------

def test_domains_scoping_api(app, client):
    from app import db
    from app.services.workspace_service import WorkspaceService
    from app.services.resource_grant_service import ResourceGrantService
    from app.models import Application, Domain

    admin = _mk_user(db, 'dom_admin', 'admin')
    dev = _mk_user(db, 'dom_dev', 'developer')
    other = _mk_user(db, 'dom_other', 'developer')
    ws_a = WorkspaceService.create_workspace({'name': 'DomA'}, admin.id)
    ws_b = WorkspaceService.create_workspace({'name': 'DomB'}, admin.id)
    WorkspaceService.add_member(ws_a.id, dev.id, 'member')

    def mk(name, owner_id, ws_id, domain):
        a = Application(name=name, app_type='docker', user_id=owner_id, workspace_id=ws_id)
        db.session.add(a)
        db.session.commit()
        db.session.add(Domain(name=domain, application_id=a.id))
        db.session.commit()
        return a

    mk('dom-a', dev.id, ws_a.id, 'a.example.com')
    mk('dom-b', dev.id, ws_b.id, 'b.example.com')
    shared = mk('dom-shared', other.id, ws_a.id, 'shared.example.com')

    # No context: dev sees its own apps' domains across workspaces, not other's.
    r = client.get('/api/v1/domains', headers=_token(dev.id))
    assert r.status_code == 200
    assert {d['name'] for d in r.get_json()['domains']} == {'a.example.com', 'b.example.com'}

    # Scoped to ws_a: only domains of dev's apps in ws_a.
    r = client.get('/api/v1/domains', headers={**_token(dev.id), 'X-Workspace-Id': str(ws_a.id)})
    assert {d['name'] for d in r.get_json()['domains']} == {'a.example.com'}

    # A grant on another user's app surfaces that app's domain too (and it's in ws_a).
    ResourceGrantService.grant(dev.id, 'application', shared.id, granted_by=admin.id, role='viewer')
    r = client.get('/api/v1/domains', headers={**_token(dev.id), 'X-Workspace-Id': str(ws_a.id)})
    assert {d['name'] for d in r.get_json()['domains']} == {'a.example.com', 'shared.example.com'}

    # Admin with no context sees every domain.
    r = client.get('/api/v1/domains', headers=_token(admin.id))
    got = {d['name'] for d in r.get_json()['domains']}
    assert {'a.example.com', 'b.example.com', 'shared.example.com'} <= got


# ---- API: docker databases (app-children) ---------------------------------

def test_docker_databases_scoping_api(app, client, monkeypatch):
    from app import db
    from app.services.workspace_service import WorkspaceService
    from app.services.database_service import DatabaseService
    from app.models import Application

    admin = _mk_user(db, 'ddb_admin', 'admin')
    dev = _mk_user(db, 'ddb_dev', 'developer')
    ws_a = WorkspaceService.create_workspace({'name': 'DdbA'}, admin.id)
    ws_b = WorkspaceService.create_workspace({'name': 'DdbB'}, admin.id)
    WorkspaceService.add_member(ws_a.id, dev.id, 'member')

    db.session.add_all([
        Application(name='ddb-a', app_type='docker', user_id=dev.id, workspace_id=ws_a.id, root_path='/srv/a'),
        Application(name='ddb-b', app_type='docker', user_id=dev.id, workspace_id=ws_b.id, root_path='/srv/b'),
    ])
    db.session.commit()

    # Stub the compose/.env read so each app yields one database tagged by app name.
    monkeypatch.setattr(DatabaseService, 'get_app_database_info',
                        lambda name, root: [{'database': name}])

    # No context: dev sees both its docker apps' databases.
    r = client.get('/api/v1/databases/docker/databases', headers=_token(dev.id))
    assert r.status_code == 200
    assert {d['app_name'] for d in r.get_json()['databases']} == {'ddb-a', 'ddb-b'}

    # Scoped to ws_a -> only ddb-a's database.
    r = client.get('/api/v1/databases/docker/databases',
                   headers={**_token(dev.id), 'X-Workspace-Id': str(ws_a.id)})
    assert {d['app_name'] for d in r.get_json()['databases']} == {'ddb-a'}


# ---- role reconciliation (#33) --------------------------------------------

def test_effective_role_reconciliation(app):
    from app import db
    from app.services.workspace_service import WorkspaceService

    sysadmin = _mk_user(db, 'er_admin', 'admin')
    dev_viewer = _mk_user(db, 'er_devv', 'developer')
    dev_member = _mk_user(db, 'er_devm', 'developer')
    global_viewer = _mk_user(db, 'er_gv', 'viewer')
    ws = WorkspaceService.create_workspace({'name': 'ER'}, sysadmin.id)
    WorkspaceService.add_member(ws.id, dev_viewer.id, 'viewer')
    WorkspaceService.add_member(ws.id, dev_member.id, 'member')
    WorkspaceService.add_member(ws.id, global_viewer.id, 'owner')

    # No context -> global role unchanged.
    assert WorkspaceService.effective_role(dev_member, None) == 'developer'
    assert WorkspaceService.can_write_in_workspace(dev_member, None) is True

    # A 'viewer' membership caps a global developer to viewer (read-only).
    assert WorkspaceService.effective_role(dev_viewer, ws.id) == 'viewer'
    assert WorkspaceService.can_write_in_workspace(dev_viewer, ws.id) is False

    # A 'member' membership does not cap (stays developer).
    assert WorkspaceService.effective_role(dev_member, ws.id) == 'developer'
    assert WorkspaceService.can_write_in_workspace(dev_member, ws.id) is True

    # Narrow-only: a workspace role NEVER elevates. A global viewer who is even an
    # 'owner' member is still effectively a viewer.
    assert WorkspaceService.effective_role(global_viewer, ws.id) == 'viewer'
    assert WorkspaceService.can_write_in_workspace(global_viewer, ws.id) is False

    # A system admin is never capped by a workspace membership.
    assert WorkspaceService.effective_role(sysadmin, ws.id) == 'admin'
    assert WorkspaceService.can_write_in_workspace(sysadmin, ws.id) is True


def test_create_app_viewer_blocked_in_workspace(app, client):
    from app import db
    from app.services.workspace_service import WorkspaceService

    admin = _mk_user(db, 'vb_admin', 'admin')
    dev = _mk_user(db, 'vb_dev', 'developer')
    ws = WorkspaceService.create_workspace({'name': 'VB'}, admin.id)
    WorkspaceService.add_member(ws.id, dev.id, 'viewer')

    # A 'viewer' member can't create in that workspace.
    r = client.post('/api/v1/apps', headers={**_token(dev.id), 'X-Workspace-Id': str(ws.id)},
                    json={'name': 'vb-nope', 'app_type': 'php'})
    assert r.status_code == 403

    # With no workspace context the same user still creates (the cap is
    # workspace-scoped and narrow-only — no-context behavior is unchanged).
    r = client.post('/api/v1/apps', headers=_token(dev.id), json={'name': 'vb-yes', 'app_type': 'php'})
    assert r.status_code == 201
    app_id = r.get_json()['app']['id']

    # And the app can't be moved INTO the workspace they're a viewer of.
    r = client.put(f'/api/v1/apps/{app_id}/workspace', json={'workspace_id': ws.id}, headers=_token(dev.id))
    assert r.status_code == 403


def test_list_workspaces_surfaces_effective_role(app, client):
    from app import db
    from app.services.workspace_service import WorkspaceService

    admin = _mk_user(db, 'lw_admin', 'admin')
    dev = _mk_user(db, 'lw_dev', 'developer')
    ws = WorkspaceService.create_workspace({'name': 'LW'}, admin.id)
    WorkspaceService.add_member(ws.id, dev.id, 'viewer')

    r = client.get('/api/v1/workspaces/', headers=_token(dev.id))
    assert r.status_code == 200
    rows = {w['name']: w for w in r.get_json()['workspaces']}
    assert rows['LW']['my_role'] == 'viewer'
    assert rows['LW']['my_effective_role'] == 'viewer'


def test_create_workspace_requires_admin(app, client):
    """GHSA-r4q7-x795-vr4j: POST /api/v1/workspaces/ was gated only by
    @jwt_required(), so a read-only viewer could create a workspace, become its
    owner, then add arbitrary members and mint workspace API keys. It now
    carries @admin_required like every other panel-wide create."""
    from app import db

    for role in ('viewer', 'developer'):
        u = _mk_user(db, f'cw_{role}', role)
        r = client.post('/api/v1/workspaces/', headers=_token(u.id),
                        json={'name': f'cw-{role}', 'slug': f'cw-{role}'})
        assert r.status_code == 403, f'{role} reached workspace creation: {r.status_code}'

    admin = _mk_user(db, 'cw_admin', 'admin')
    r = client.post('/api/v1/workspaces/', headers=_token(admin.id),
                    json={'name': 'cw-ok', 'slug': 'cw-ok'})
    assert r.status_code == 201
