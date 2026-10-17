"""Plan 81 M4: server timeline merge, authorization, and cursor contracts."""

import base64
from datetime import datetime, timedelta
import json

from factories import headers_for, make_application, make_server, make_user


def _restore_point(db, *, server_id, scope_type, scope_id, created_at,
                   actor_user_id=None, label=None):
    from app.models.restore_point import RestorePoint

    point = RestorePoint(
        server_id=server_id,
        scope_type=scope_type,
        scope_id=str(scope_id),
        trigger='manual',
        label=label,
        payload_hash='a' * 64,
        payload_json='{}',
        coverage_json='["outside"]',
        actor_user_id=actor_user_id,
        keep=True,
        created_at=created_at,
        updated_at=created_at,
    )
    db.session.add(point)
    db.session.commit()
    return point


def _snapshot(db, application, created_at, *, summary='config changed'):
    from app.models.deployment_snapshot import DeploymentSnapshot

    snapshot = DeploymentSnapshot(
        application_id=application.id,
        snapshot_hash='b' * 64,
        config_json='{}',
        summary=summary,
        created_at=created_at,
    )
    db.session.add(snapshot)
    db.session.commit()
    return snapshot


def _audit(db, *, action, created_at, target_type=None, target_id=None,
           details=None, user_id=None, ip_address='203.0.113.1',
           user_agent='secret-agent'):
    from app.models.audit_log import AuditLog

    row = AuditLog(
        action=action,
        target_type=target_type,
        target_id=target_id,
        user_id=user_id,
        created_at=created_at,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    row.set_details(details or {})
    db.session.add(row)
    db.session.commit()
    return row


def test_timeline_requires_developer_and_returns_typed_missing_server(
        client, db_session):
    server = make_server(db_session)
    viewer = make_user(db_session, role='viewer')
    developer = make_user(db_session, role='developer')

    url = f'/api/v1/servers/{server.id}/timeline'
    assert client.get(url).status_code == 401
    assert client.get(url, headers=headers_for(viewer)).status_code == 403

    missing = '00000000-0000-0000-0000-000000000000'
    response = client.get(
        f'/api/v1/servers/{missing}/timeline',
        headers=headers_for(developer),
    )
    assert response.status_code == 404
    assert response.get_json()['code'] == 'server_timeline_server_not_found'


def test_timeline_global_order_ties_and_cursor_have_no_duplicates(
        client, db_session):
    server = make_server(db_session)
    owner = make_user(db_session, role='developer')
    server.owner_id = owner.id  # non-admins see only servers they own
    db_session.session.commit()
    application = make_application(
        db_session, user_id=owner.id, server_id=server.id,
    )
    when = datetime(2026, 8, 23, 12, 0, 0, 123456)
    older = when - timedelta(seconds=1)

    audit_low = _audit(
        db_session, action='server.low', created_at=when,
        target_type='server', target_id=server.id,
    )
    audit_high = _audit(
        db_session, action='server.high', created_at=when,
        details={'server_id': server.id},
    )
    point = _restore_point(
        db_session, server_id=server.id, scope_type='cron', scope_id='root',
        created_at=when, actor_user_id=owner.id,
    )
    snapshot = _snapshot(db_session, application, when)
    old_audit = _audit(
        db_session, action='server.old', created_at=older,
        details={'server_id': server.id},
    )

    headers = headers_for(owner)
    # limit=1 deliberately splits equal-time rows from the same source across
    # pages, exercising the native-id component of the keyset cursor.
    url = f'/api/v1/servers/{server.id}/timeline?limit=1'
    seen = []
    cursor = None
    while True:
        page_url = url + (f'&before={cursor}' if cursor else '')
        response = client.get(page_url, headers=headers)
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        seen.extend((event['type'], event['source_id']) for event in body['events'])
        cursor = body['next_cursor']
        if cursor is None:
            break

    assert seen == [
        ('audit', audit_high.id),
        ('audit', audit_low.id),
        ('restore_point', point.id),
        ('deployment_snapshot', snapshot.id),
        ('audit', old_audit.id),
    ]
    assert len(seen) == len(set(seen))


def test_timeline_type_filter_and_snapshot_attribution(client, db_session):
    server = make_server(db_session)
    owner = make_user(db_session, role='developer')
    server.owner_id = owner.id  # non-admins see only servers they own
    db_session.session.commit()
    application = make_application(
        db_session, user_id=owner.id, server_id=server.id, name='timeline-app',
    )
    snapshot = _snapshot(
        db_session, application, datetime(2026, 8, 23, 11),
        summary='image updated',
    )
    _audit(
        db_session, action='server.other', created_at=datetime(2026, 8, 23, 12),
        details={'server_id': server.id},
    )

    response = client.get(
        f'/api/v1/servers/{server.id}/timeline?types=deployment_snapshot',
        headers=headers_for(owner),
    )
    assert response.status_code == 200
    assert response.get_json() == {
        'events': [{
            'id': f'deployment_snapshot:{snapshot.id}',
            'type': 'deployment_snapshot',
            'source_id': snapshot.id,
            'created_at': '2026-08-23T11:00:00.000000',
            'action': 'deployment.snapshot',
            'actor_user_id': None,
            'application_id': application.id,
            'application_name': 'timeline-app',
            'deployment_id': None,
            'snapshot_hash': 'b' * 64,
            'summary': 'image updated',
        }],
        'next_cursor': None,
    }


def test_app_owned_events_are_grant_filtered_before_pagination(
        client, db_session):
    from app.services.resource_grant_service import ResourceGrantService

    server = make_server(db_session)
    caller = make_user(db_session, role='developer')
    server.owner_id = caller.id  # non-admins see only servers they own
    db_session.session.commit()
    other = make_user(db_session, role='developer')
    visible = make_application(
        db_session, user_id=other.id, server_id=server.id, name='shared',
    )
    hidden = make_application(
        db_session, user_id=other.id, server_id=server.id, name='hidden',
    )
    ResourceGrantService.grant(
        caller.id, 'application', visible.id, role='viewer',
    )
    when = datetime(2026, 8, 23, 12)

    visible_snapshot = _snapshot(db_session, visible, when)
    _snapshot(db_session, hidden, when + timedelta(seconds=3))
    visible_point = _restore_point(
        db_session, server_id=server.id, scope_type='env', scope_id=visible.id,
        created_at=when - timedelta(seconds=1),
    )
    _restore_point(
        db_session, server_id=server.id, scope_type='env', scope_id=hidden.id,
        created_at=when + timedelta(seconds=2),
    )
    visible_audit = _audit(
        db_session, action='app.update', created_at=when - timedelta(seconds=2),
        target_type='app', target_id=visible.id,
    )
    _audit(
        db_session, action='app.secret', created_at=when + timedelta(seconds=1),
        target_type='app', target_id=hidden.id,
        details={'server_id': server.id},
    )

    response = client.get(
        f'/api/v1/servers/{server.id}/timeline?limit=3',
        headers=headers_for(caller),
    )
    assert response.status_code == 200
    rows = response.get_json()['events']
    assert [(row['type'], row['source_id']) for row in rows] == [
        ('deployment_snapshot', visible_snapshot.id),
        ('restore_point', visible_point.id),
        ('audit', visible_audit.id),
    ]
    assert response.get_json()['next_cursor'] is None


def test_legacy_env_fallback_and_explicit_server_history(client, db_session):
    target = make_server(db_session, name='target')
    other_server = make_server(db_session, name='other')
    owner = make_user(db_session, role='developer')
    target.owner_id = owner.id  # non-admins see only servers they own
    other_server.owner_id = owner.id  # non-admins see only servers they own
    db_session.session.commit()
    current = make_application(
        db_session, user_id=owner.id, server_id=target.id, name='current',
    )
    moved = make_application(
        db_session, user_id=owner.id, server_id=other_server.id, name='moved',
    )
    legacy = _restore_point(
        db_session, server_id=None, scope_type='env', scope_id=current.id,
        created_at=datetime(2026, 8, 23, 10), label='legacy current',
    )
    explicit_history = _restore_point(
        db_session, server_id=target.id, scope_type='env', scope_id=moved.id,
        created_at=datetime(2026, 8, 23, 9), label='before app moved',
    )
    _restore_point(
        db_session, server_id=None, scope_type='env', scope_id=moved.id,
        created_at=datetime(2026, 8, 23, 11), label='belongs elsewhere',
    )

    response = client.get(
        f'/api/v1/servers/{target.id}/timeline?types=restore_point',
        headers=headers_for(owner),
    )
    assert response.status_code == 200
    assert [row['source_id'] for row in response.get_json()['events']] == [
        legacy.id, explicit_history.id,
    ]


def test_audit_exact_attribution_duplicate_suppression_and_safe_projection(
        client, db_session):
    server = make_server(db_session)
    other = make_server(db_session)
    developer = make_user(db_session, role='developer')
    server.owner_id = developer.id  # non-admins see only servers they own
    other.owner_id = developer.id  # non-admins see only servers they own
    db_session.session.commit()
    when = datetime(2026, 8, 23, 12)
    direct = _audit(
        db_session, action='server.direct', created_at=when,
        target_type='server', target_id=server.id, user_id=developer.id,
        details={'state': 'ready', 'password': 'must-not-appear'},
    )
    routed = _audit(
        db_session, action='api.mutation', created_at=when - timedelta(seconds=1),
        target_type='servers',
        details={
            'route_args': {'server_id': server.id},
            'method': 'POST',
            'payload': {'api_secret': 'must-not-appear'},
        },
    )
    detailed = _audit(
        db_session, action='server.detail', created_at=when - timedelta(seconds=2),
        target_type='fleet_repair',
        details={'server_id': server.id, 'kind': 'service', 'ip': 'hidden'},
    )
    _audit(
        db_session, action='server.wrong', created_at=when + timedelta(seconds=1),
        details={'server_id': other.id},
    )
    _audit(
        db_session, action='restore_point.create', created_at=when + timedelta(seconds=2),
        target_type='restore_point', details={'server_id': server.id},
    )

    response = client.get(
        f'/api/v1/servers/{server.id}/timeline?types=audit',
        headers=headers_for(developer),
    )
    assert response.status_code == 200
    events = response.get_json()['events']
    assert [event['source_id'] for event in events] == [
        direct.id, routed.id, detailed.id,
    ]
    assert events[0]['actor_username'] == developer.username
    assert events[0]['details'] == {'state': 'ready'}
    assert events[1]['details'] == {'method': 'POST'}
    assert events[2]['details'] == {
        'kind': 'service', 'server_id': server.id,
    }
    for event in events:
        assert 'ip_address' not in event
        assert 'user_agent' not in event
        assert 'payload' not in event['details']


def test_timeline_rejects_invalid_query_and_mismatched_cursor(
        client, db_session):
    first_server = make_server(db_session)
    second_server = make_server(db_session)
    developer = make_user(db_session, role='developer')
    first_server.owner_id = developer.id  # non-admins see only servers they own
    second_server.owner_id = developer.id  # non-admins see only servers they own
    db_session.session.commit()
    headers = headers_for(developer)
    now = datetime(2026, 8, 23, 12)
    _audit(
        db_session, action='one', created_at=now,
        details={'server_id': first_server.id},
    )
    _audit(
        db_session, action='two', created_at=now - timedelta(seconds=1),
        details={'server_id': first_server.id},
    )
    base = f'/api/v1/servers/{first_server.id}/timeline'

    invalid = (
        '?types=unknown',
        '?limit=0',
        '?limit=201',
        '?limit=wat',
        '?before=not-a-cursor',
        '?extra=yes',
        '?limit=1&limit=2',
    )
    for query in invalid:
        response = client.get(base + query, headers=headers)
        assert response.status_code == 400, (query, response.get_json())
        assert 'code' in response.get_json()

    first = client.get(base + '?types=audit&limit=1', headers=headers)
    assert first.status_code == 200
    cursor = first.get_json()['next_cursor']
    assert cursor

    wrong_server = client.get(
        f'/api/v1/servers/{second_server.id}/timeline'
        f'?types=audit&limit=1&before={cursor}',
        headers=headers,
    )
    assert wrong_server.status_code == 400
    assert wrong_server.get_json()['code'] == 'invalid_cursor'

    wrong_types = client.get(
        base + f'?types=restore_point&limit=1&before={cursor}',
        headers=headers,
    )
    assert wrong_types.status_code == 400
    assert wrong_types.get_json()['code'] == 'invalid_cursor'

    encoded = cursor.encode('ascii')
    encoded += b'=' * (-len(encoded) % 4)
    tampered_payload = json.loads(base64.urlsafe_b64decode(encoded))
    tampered_payload['source'] = 'restore_point'
    tampered = base64.urlsafe_b64encode(json.dumps(
        tampered_payload, sort_keys=True, separators=(',', ':'),
    ).encode()).rstrip(b'=').decode()
    response = client.get(
        base + f'?types=audit&limit=1&before={tampered}', headers=headers,
    )
    assert response.status_code == 400
    assert response.get_json()['code'] == 'invalid_cursor'
