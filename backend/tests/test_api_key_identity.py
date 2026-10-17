"""API-key callers must resolve to a real user inside the handler (plan 76, A).

`app/middleware/api_key_auth.py` authenticates an ``X-API-Key`` request by
stashing the key's owner on ``g.api_key_user``; ``rbac.get_current_user()`` is
the only identity lookup that reads it. A handler that instead resolves the
caller inline with ``User.query.get(get_jwt_identity())`` gets ``None`` for
those requests — the *policy decorator already let them in*, so the failure
lands in the middle of the handler rather than at the boundary.

That combination is not hypothetical. Every route exercised below is reached
through an API-key-capable policy decorator (``@admin_required``,
``@developer_required``, ``@viewer_required``), all of which wrap
``auth_required()``. The three failure modes each of these produced:

  * ``None.is_admin`` / ``None.id``      -> AttributeError, a 500
  * ``user_id=user.id if user else None`` -> the change is written, attributed
    to nobody, and the audit trail loses the actor
  * a bound-but-unused ``user``           -> harmless, but it is the same bug
    one edit away from mattering

These are behavioural tests, not census tests; the census lives in
``tests/test_identity_door_ratchet.py``.
"""

import pytest
from werkzeug.security import generate_password_hash


def _api_key_for(db, role, suffix, scopes=('*',)):
    """Create a user with `role` and return (user, raw API key)."""
    from app.models import User
    from app.services.api_key_service import ApiKeyService

    user = User(
        email=f'identity-{suffix}@test.local',
        username=f'identity_{suffix}',
        password_hash=generate_password_hash('testpass'),
        role=role,
        is_active=True,
    )
    db.session.add(user)
    db.session.commit()
    _, raw_key = ApiKeyService.create_key(
        user.id, name=f'identity-{suffix}', scopes=list(scopes))
    return user, raw_key


class TestApiKeyCallersReachTheHandlerWithAnIdentity:
    """A 500 here means the handler resolved the caller as None."""

    def test_developer_api_key_can_reassign_a_server_workspace(self, app, client, db_session):
        """servers.py set_server_workspace() reads `user.is_admin` directly."""
        from app.models import User
        from app.models.server import Server

        user, raw_key = _api_key_for(db_session, User.ROLE_DEVELOPER, 'srv-dev')
        # Non-admins operate only servers they own.
        server = Server(name='identity-probe', hostname='10.10.10.10', owner_id=user.id)
        db_session.session.add(server)
        db_session.session.commit()

        response = client.put(
            f'/api/v1/servers/{server.id}/workspace',
            json={'workspace_id': 'default'},
            headers={'X-API-Key': raw_key},
        )

        assert response.status_code != 500, (
            'the handler resolved the API-key caller as None and crashed on '
            f'None.is_admin; body={response.get_data(as_text=True)[:300]}')
        assert response.status_code == 200

    def test_admin_api_key_can_read_bandwidth_for_an_app(self, app, client, db_session):
        """bandwidth.py passes the caller straight into can_access_app()."""
        from app.models import User
        from app.models.application import Application

        user, raw_key = _api_key_for(db_session, User.ROLE_ADMIN, 'bw-admin')
        application = Application(name='identity-bw', app_type='static', user_id=user.id)
        db_session.session.add(application)
        db_session.session.commit()

        response = client.get(
            f'/api/v1/bandwidth/apps/{application.id}',
            headers={'X-API-Key': raw_key},
        )

        assert response.status_code != 500, (
            'can_access_app(None, app) raised on None.is_admin; '
            f'body={response.get_data(as_text=True)[:300]}')
        assert response.status_code == 200

    def test_admin_api_key_can_update_general_sso_settings(self, app, client, db_session):
        """sso.py update_general_settings() reads `user.id` unconditionally."""
        from app.models import User

        _, raw_key = _api_key_for(db_session, User.ROLE_ADMIN, 'sso-admin')

        response = client.put(
            '/api/v1/sso/admin/general',
            json={'sso_auto_provision': True},
            headers={'X-API-Key': raw_key},
        )

        assert response.status_code != 500, (
            'the handler crashed on None.id for an API-key admin; '
            f'body={response.get_data(as_text=True)[:300]}')
        assert response.status_code == 200


    def test_developer_api_key_can_create_a_server(self, app, client, db_session):
        """servers.py create_server() read the JWT to stamp ownership."""
        from app.models import User

        user, raw_key = _api_key_for(db_session, User.ROLE_DEVELOPER, 'srv-create')
        # The default (hobby) plan has no server slots.
        from app.services.plan_quota_service import PlanQuotaService
        PlanQuotaService.assign_plan(user, 'pro')

        response = client.post(
            '/api/v1/servers',
            json={'description': 'identity probe'},
            headers={'X-API-Key': raw_key},
        )

        assert response.status_code != 500, (
            'create_server crashed reading the JWT for an API-key caller; '
            f'body={response.get_data(as_text=True)[:300]}')
        assert response.status_code in (200, 201)


class TestApiKeyActionsAreAttributedToTheKeyOwner:
    """Silent misattribution: the write succeeds, the actor is lost."""

    def test_sso_settings_update_records_the_key_owner_not_none(self, app, client, db_session):
        from app.models import User
        from app.models.audit_log import AuditLog

        user, raw_key = _api_key_for(db_session, User.ROLE_ADMIN, 'sso-audit')

        response = client.put(
            '/api/v1/sso/admin/general',
            json={'sso_auto_provision': True},
            headers={'X-API-Key': raw_key},
        )
        assert response.status_code == 200

        entry = (AuditLog.query
                 .filter_by(action=AuditLog.ACTION_SETTINGS_UPDATE)
                 .order_by(AuditLog.id.desc())
                 .first())
        assert entry is not None, 'the settings update wrote no audit entry'
        assert entry.user_id == user.id, (
            f'audit entry attributed to user_id={entry.user_id!r}, expected the '
            f'API key owner {user.id}')

    def test_github_admin_config_update_records_the_key_owner(self, app, client, db_session, monkeypatch):
        """source_connections.py used `user.id if user else None` — the write
        lands, the actor silently becomes None."""
        from app.models import User
        from app.services import source_connection_service

        user, raw_key = _api_key_for(db_session, User.ROLE_ADMIN, 'gh-admin')

        seen = {}

        def _record(payload, user_id=None):
            seen['user_id'] = user_id
            return {'ok': True}

        monkeypatch.setattr(
            source_connection_service.SourceConnectionService,
            'update_github_config', staticmethod(_record))

        response = client.put(
            '/api/v1/source-connections/admin/github',
            json={'client_id': 'abc'},
            headers={'X-API-Key': raw_key},
        )

        assert response.status_code == 200
        assert seen.get('user_id') == user.id, (
            f'update attributed to user_id={seen.get("user_id")!r}, expected the '
            f'API key owner {user.id}')


def test_jwt_callers_are_unaffected(client, auth_headers, db_session):
    """The migration must not regress the JWT path it shares."""
    response = client.put(
        '/api/v1/sso/admin/general',
        json={'sso_auto_provision': True},
        headers=auth_headers,
    )
    assert response.status_code == 200
