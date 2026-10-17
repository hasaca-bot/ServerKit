"""Hosting plans: quotas per resource kind, admin bypass, plan API, per-service
caps and the plan's idle-sleep policy on new apps."""
import json
from datetime import datetime, timedelta

import pytest

from factories import headers_for, make_application, make_user


@pytest.fixture
def dev(app):
    from app import db
    return make_user(db, role='developer')


@pytest.fixture
def admin(app):
    from app import db
    return make_user(db, role='admin')


def _seed(kind, user, n, tmp_path, monkeypatch):
    """Give *user* n owned resources of *kind*."""
    from app import db
    from app.models import ApplicationPreview, Domain, ManagedDatabase, Server
    from app.services.api_key_service import ApiKeyService
    if kind == 'apps':
        for i in range(n):
            make_application(db, name=f'app-{user.id}-{i}', user_id=user.id)
        return
    if kind == 'mcp_keys':
        for i in range(n):
            ApiKeyService.create_key(user.id, name=f'k{i}')
        return
    if kind == 'servers':
        for i in range(n):
            db.session.add(Server(name=f'srv-{i}', owner_id=user.id))
        db.session.commit()
        return
    host = make_application(db, name=f'host-{user.id}', user_id=user.id)
    if kind == 'databases':
        for i in range(n):
            db.session.add(ManagedDatabase(engine='postgresql', name=f'db{i}',
                                           owner_application_id=host.id))
    elif kind == 'custom_domains':
        for i in range(n):
            db.session.add(Domain(name=f'site{i}.example.org', application_id=host.id))
    elif kind == 'previews':
        for i in range(n):
            db.session.add(ApplicationPreview(application_id=host.id, pr_number=i + 1,
                                              status='running'))
    elif kind == 'cron_jobs':
        from app.services import cron_service
        jobs_file = tmp_path / 'cron_jobs.json'
        jobs_file.write_text(json.dumps({'jobs': {
            f'j{i}': {'application_id': host.id, 'schedule': '* * * * *'} for i in range(n)}}))
        monkeypatch.setattr(cron_service, 'JOBS_FILE', str(jobs_file))
    db.session.commit()


# hobby: apps 3, databases 1, cron 2, custom domains 0, previews 0, servers 0, keys 1
HOBBY = {'apps': 3, 'databases': 1, 'cron_jobs': 2, 'custom_domains': 0,
         'previews': 0, 'servers': 0, 'mcp_keys': 1}


@pytest.mark.parametrize('kind', sorted(HOBBY))
def test_each_kind_blocks_at_the_hobby_limit(dev, admin, kind, tmp_path, monkeypatch):
    from app.services.plan_quota_service import PlanQuotaService, QuotaExceededError
    limit = HOBBY[kind]
    if limit:
        _seed(kind, dev, limit - 1, tmp_path, monkeypatch)
        PlanQuotaService.check(dev, kind)  # one more still fits
        _seed(kind, dev, 1, tmp_path, monkeypatch) if kind != 'cron_jobs' else \
            _seed(kind, dev, limit, tmp_path, monkeypatch)
    assert PlanQuotaService.usage_for(dev)[kind] >= limit
    with pytest.raises(QuotaExceededError) as exc:
        PlanQuotaService.check(dev, kind)
    assert exc.value.status_code == 402 and exc.value.code == 'quota_exceeded'
    assert exc.value.details['kind'] == kind
    # Admins are never limited.
    PlanQuotaService.check(admin, kind, add=1000)


def test_usage_excludes_soft_deleted_apps_and_others_resources(dev, app):
    from app import db
    from app.services.plan_quota_service import PlanQuotaService
    other = make_user(db, role='developer')
    gone = make_application(db, name='gone', user_id=dev.id)
    gone.soft_delete(dev.id)
    make_application(db, name='theirs', user_id=other.id)
    make_application(db, name='mine', user_id=dev.id)
    db.session.commit()
    assert PlanQuotaService.usage_for(dev)['apps'] == 1


def test_assigned_plan_and_default_setting(dev):
    from app.services.plan_quota_service import PlanQuotaService
    from app.services.settings_service import SettingsService
    assert PlanQuotaService.plan_for(dev).slug == 'hobby'
    SettingsService.set('default_plan_slug', 'team')
    assert PlanQuotaService.limits_for(dev)['apps'] == 100
    PlanQuotaService.assign_plan(dev, 'pro')
    assert PlanQuotaService.limits_for(dev)['apps'] == 25


def test_check_resources_caps_cpu_and_memory(dev, admin):
    from app.services.plan_quota_service import (
        PlanQuotaService, QuotaExceededError, memory_to_mb,
    )
    PlanQuotaService.check_resources(dev, cpu=0.25, memory_mb=256)
    with pytest.raises(QuotaExceededError):
        PlanQuotaService.check_resources(dev, cpu=0.5)
    with pytest.raises(QuotaExceededError):
        PlanQuotaService.check_resources(dev, memory_mb=memory_to_mb('1g'))
    PlanQuotaService.check_resources(admin, cpu=64, memory_mb=10 ** 6)
    assert memory_to_mb('512m') == 512 and memory_to_mb('2g') == 2048


# ------------------------------------------------------------------ API


def test_app_create_blocks_at_limit_and_stamps_plan_defaults(client, dev, admin):
    from app.models import Application
    from app.models.container_sleep_policy import ContainerSleepPolicy
    for i in range(3):
        r = client.post('/api/v1/apps', json={'name': f'svc{i}', 'app_type': 'docker'},
                        headers=headers_for(dev))
        assert r.status_code == 201, r.get_json()
    r = client.post('/api/v1/apps', json={'name': 'svc3', 'app_type': 'docker'},
                    headers=headers_for(dev))
    assert r.status_code == 402
    assert r.get_json()['code'] == 'quota_exceeded'

    created = Application.query.filter_by(name='svc0').first()
    assert created.cpu_limit == '0.25' and created.memory_limit == '256m'
    policy = ContainerSleepPolicy.query.filter_by(application_id=created.id).first()
    assert policy.enabled and policy.idle_timeout_minutes == 15

    # Admin: no limit, no plan defaults.
    r = client.post('/api/v1/apps', json={'name': 'adminsvc', 'app_type': 'docker'},
                    headers=headers_for(admin))
    assert r.status_code == 201
    assert Application.query.filter_by(name='adminsvc').first().cpu_limit is None


def test_resources_update_is_capped_by_the_owner_plan(client, dev, admin):
    from app import db
    app_row = make_application(db, name='capped', user_id=dev.id)
    url = f'/api/v1/apps/{app_row.id}/resources'
    r = client.put(url, json={'cpu_limit': '2'}, headers=headers_for(dev))
    assert r.status_code == 402
    r = client.put(url, json={'memory_limit': '128m'}, headers=headers_for(dev))
    assert r.status_code == 200
    # Clearing a cap falls back to the plan's maximum.
    r = client.put(url, json={'cpu_limit': ''}, headers=headers_for(dev))
    assert r.status_code == 200 and r.get_json()['cpu_limit'] == '0.25'
    r = client.put(url, json={'cpu_limit': '8'}, headers=headers_for(admin))
    assert r.status_code == 200


def test_sleep_policy_cannot_be_disabled_on_a_sleeping_plan(client, dev):
    from app import db
    app_row = make_application(db, name='sleepy', user_id=dev.id)
    url = f'/api/v1/apps/{app_row.id}/sleep-policy'
    assert client.put(url, json={'enabled': False}, headers=headers_for(dev)).status_code == 402
    assert client.put(url, json={'idle_timeout_minutes': 60},
                      headers=headers_for(dev)).status_code == 402
    assert client.put(url, json={'enabled': True, 'idle_timeout_minutes': 5},
                      headers=headers_for(dev)).status_code == 200


def test_custom_domain_add_is_blocked_on_hobby(client, dev):
    from app import db
    app_row = make_application(db, name='withdomain', user_id=dev.id, port=8080)
    r = client.post('/api/v1/domains', json={'name': 'shop.example.org',
                                             'application_id': app_row.id},
                    headers=headers_for(dev))
    assert r.status_code == 402


def test_api_key_and_server_creation_hit_the_plan(client, dev):
    from app.services.api_key_service import ApiKeyService
    ApiKeyService.create_key(dev.id, name='first')
    r = client.post('/api/v1/api-keys/', json={'name': 'second'}, headers=headers_for(dev))
    assert r.status_code == 402
    r = client.post('/api/v1/servers', json={}, headers=headers_for(dev))
    assert r.status_code == 402  # hobby has no server slots


def test_preview_creation_respects_the_plan(dev):
    from app import db
    from app.models import ApplicationPreview, ApplicationPreviewSettings
    from app.services.preview_service import PreviewService
    app_row = make_application(db, name='prev', user_id=dev.id)
    db.session.add(ApplicationPreviewSettings(application_id=app_row.id, enabled=True))
    db.session.commit()
    result = PreviewService.create_preview(app_row, {'pr_number': 7, 'branch': 'feat'})
    assert result.get('reason') == 'quota_exceeded'
    assert ApplicationPreview.query.count() == 0


def test_my_plan_endpoint(client, dev, admin):
    r = client.get('/api/v1/plan', headers=headers_for(dev))
    assert r.status_code == 200
    body = r.get_json()
    assert body['plan']['slug'] == 'hobby' and body['unlimited'] is False
    assert body['limits']['apps'] == 3 and body['usage']['apps'] == 0
    r = client.get('/api/v1/plan', headers=headers_for(admin))
    assert r.get_json()['unlimited'] is True


def test_admin_plan_management(client, dev, admin):
    r = client.get('/api/v1/plans', headers=headers_for(dev))
    assert r.status_code == 403
    r = client.get('/api/v1/plans', headers=headers_for(admin))
    assert [p['slug'] for p in r.get_json()['plans']] == ['hobby', 'pro', 'team']

    r = client.put('/api/v1/plans/hobby', json={'max_apps': 5}, headers=headers_for(admin))
    assert r.status_code == 200 and r.get_json()['plan']['limits']['apps'] == 5
    r = client.put('/api/v1/plans/hobby', json={'max_apps': -1}, headers=headers_for(admin))
    assert r.status_code == 400
    assert client.put('/api/v1/plans/hobby', json={'max_apps': 9},
                      headers=headers_for(dev)).status_code == 403

    r = client.put(f'/api/v1/plans/users/{dev.id}', json={'plan': 'team'},
                   headers=headers_for(admin))
    assert r.status_code == 200 and r.get_json()['plan']['slug'] == 'team'
    assert client.put(f'/api/v1/plans/users/{dev.id}', json={'plan': 'nope'},
                      headers=headers_for(admin)).status_code == 400
    assert client.put(f'/api/v1/plans/users/{dev.id}', json={'plan': 'pro'},
                      headers=headers_for(dev)).status_code == 403

    r = client.put('/api/v1/plans/settings/default', json={'slug': 'pro'},
                   headers=headers_for(admin))
    assert r.status_code == 200
    client.put(f'/api/v1/plans/users/{dev.id}', json={'plan': None}, headers=headers_for(admin))
    assert client.get('/api/v1/plan', headers=headers_for(dev)).get_json()['plan']['slug'] == 'pro'


def test_expired_and_revoked_keys_do_not_count(dev):
    from app import db
    from app.services.api_key_service import ApiKeyService
    from app.services.plan_quota_service import PlanQuotaService
    key, _ = ApiKeyService.create_key(dev.id, name='old',
                                      expires_at=datetime.utcnow() - timedelta(days=1))
    other, _ = ApiKeyService.create_key(dev.id, name='revoked')
    ApiKeyService.revoke_key(other.id, dev.id)
    db.session.commit()
    assert PlanQuotaService.usage_for(dev)['mcp_keys'] == 0
