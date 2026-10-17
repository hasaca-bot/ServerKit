"""MCP async jobs run end to end with the core services stubbed at their edges."""
import os
import json

import pytest

from factories import make_application
from test_mcp_endpoint import make_key, _clean_state  # noqa: F401 (autouse fixture)
from test_mcp_tools import call


@pytest.fixture
def no_queue(monkeypatch):
    from app.services.deployment_job_service import DeploymentJobService
    monkeypatch.setattr(DeploymentJobService, '_enqueue_registered', classmethod(lambda cls, job: None))


@pytest.fixture
def deploys(monkeypatch):
    from app.services.deployment_service import DeploymentService
    calls = []

    def deploy(cls, app_id, user_id=None, trigger='manual', log_callback=None, **kw):
        calls.append(app_id)
        if log_callback:
            log_callback('built with token ghp_abcdefghijklmnopqrstuvwxyz0123456789')
        return {'success': True, 'deployment': {'id': 1}}
    monkeypatch.setattr(DeploymentService, 'deploy', classmethod(deploy))
    return calls


def test_publish_job_creates_the_service(client, db_session, tmp_path, monkeypatch, no_queue, deploys):
    from app import paths
    from app.models.application import Application
    from app.models import EnvironmentVariable, GitWebhook
    from app.services.build_service import BuildService
    from app.services.deployment_job_service import DeploymentJobService
    from app.services.git_service import GitService
    from app.services.mcp import publish
    from app.services.repository_manifest_service import RepositoryManifestService
    from app.services.site_domain_service import SiteDomainService
    from app.services.source_connection_service import SourceConnectionService

    monkeypatch.setattr(paths, 'APPS_DIR', str(tmp_path))
    monkeypatch.setattr(publish, 'list_user_repos', lambda uid: [{'full_name': 'alice/shop'}])
    monkeypatch.setattr(publish, '_connection', lambda uid: type('C', (), {'id': 7})())
    seen = {}

    def clone_url(cls, user_id, connection_id, full_name):
        seen['clone'] = (user_id, connection_id, full_name)
        return {'clone_url': 'https://x-access-token:SECRET@github.com/alice/shop.git',
                'public_url': 'https://github.com/alice/shop.git'}
    monkeypatch.setattr(SourceConnectionService, 'get_authenticated_clone_url', classmethod(clone_url))

    def clone(cls, path, url, branch='main'):
        os.makedirs(path)
        return {'success': True}
    monkeypatch.setattr(GitService, 'clone_repository', classmethod(clone))
    monkeypatch.setattr(GitService, 'configure_deployment', classmethod(
        lambda cls, **kw: seen.setdefault('deploy_cfg', kw) and {'success': True, 'config': {}}))
    monkeypatch.setattr(BuildService, 'configure_build', classmethod(
        lambda cls, **kw: seen.setdefault('build_cfg', kw) and {'success': True, 'config': {}}))
    monkeypatch.setattr(BuildService, 'detect_build_method', classmethod(lambda cls, p: {}))
    monkeypatch.setattr(RepositoryManifestService, 'analyze_path', classmethod(lambda cls, p: {}))
    monkeypatch.setattr(SiteDomainService, 'give_subdomain', classmethod(
        lambda cls, app, label=None, base=None: {'success': True, 'url': f'https://{app.name}.example.com'}))
    monkeypatch.setattr(SiteDomainService, 'panel_origin', classmethod(lambda cls: 'https://panel.example.com'))
    hooks = []
    monkeypatch.setattr(publish, 'github_create_hook', lambda uid, repo, url, secret: hooks.append(url) or 1)

    alice, key = make_key(db_session)
    is_error, started = call(client, key, 'publish_site', {
        'name': 'shop', 'github': 'alice/shop', 'port': 8000, 'health': '/health',
        'dockerfile': 'FROM python:3.12-slim\nCMD ["python","-m","http.server","8000"]',
        'env': {'SECRET_KEY': {'generate': 'hex32'}, 'APP_URL': 'https://shop.example.com'},
        'auto_deploy': True})
    assert not is_error, started
    result = DeploymentJobService.run_job(started['job'])
    assert result['success'], result

    app = Application.query.filter_by(name='shop').one()
    assert app.user_id == alice.id and app.port == 8000 and app.healthcheck_path == '/health'
    assert seen['clone'] == (alice.id, 7, 'alice/shop')
    assert seen['deploy_cfg']['repo_url'] == 'https://github.com/alice/shop.git'
    assert seen['build_cfg']['build_method'] == 'dockerfile'
    assert seen['build_cfg']['dockerfile_path'] == 'Dockerfile.serverkit'
    assert os.path.isfile(os.path.join(app.root_path, 'Dockerfile.serverkit'))
    keys = {r.key: r for r in EnvironmentVariable.query.filter_by(application_id=app.id)}
    assert keys['SECRET_KEY'].is_secret and len(keys['SECRET_KEY'].value) == 64
    assert deploys == [app.id]
    hook = GitWebhook.query.filter_by(app_id=app.id).one()
    assert hook.deploy_on_push and hooks == [f'https://panel.example.com/api/v1/git/webhooks/receive/{hook.webhook_token}']

    is_error, view = call(client, key, 'get_job', {'job_id': started['job']})
    assert view['status'] == 'succeeded'
    assert view['result']['url'] == 'https://shop.example.com' and view['result']['auto_deploy'] == 'on'
    text = json.dumps(view)
    assert 'SECRET' not in text.replace('SECRET_KEY', '')
    assert 'ghp_abcdefghijklmnopqrstuvwxyz0123456789' not in text
    assert keys['SECRET_KEY'].value not in text


def test_create_database_job_links_by_reference(client, db_session, monkeypatch, no_queue, deploys):
    from app.services import database_engine_service as engines
    from app.services.deployment_job_service import DeploymentJobService
    from app.services.env_service import EnvService

    alice, key = make_key(db_session)
    site = make_application(db_session, name='shop', user_id=alice.id, root_path='/tmp/shop')
    monkeypatch.setattr(engines, 'plan_install', lambda **kw: {'app_name': kw['instance_name'], 'variables': {}})

    def install(cls, template_id, app_name, user_variables=None, user_id=None, wait=False, **kw):
        make_application(db_session, name=app_name, user_id=user_id,
                         root_path=f'/tmp/{app_name}', status='running')
        return {'success': True}
    monkeypatch.setattr(DeploymentJobService, 'install_template', classmethod(install))
    monkeypatch.setattr(DeploymentJobService, '_enqueue_app_deploy', classmethod(lambda cls, job: None))

    is_error, started = call(client, key, 'create_database', {'site': 'shop', 'engine': 'postgresql'})
    assert not is_error, started
    assert DeploymentJobService.run_job(started['job'])['success']
    env = EnvService.get_env_var(site.id, 'DATABASE_URL')
    assert env.get_reference() == {'kind': 'service', 'service': 'shop-postgresql', 'property': 'url'}
    assert deploys == [site.id]

    is_error, info = call(client, key, 'site_info', {'site': 'shop'})
    assert info['databases'][0]['instance'] == 'shop-postgresql'
    # the engine is not a site of its own
    assert [s['name'] for s in call(client, key, 'list_sites')[1]['sites']] == ['shop']
    is_error, payload = call(client, key, 'create_database', {'site': 'shop', 'engine': 'postgresql'})
    assert is_error and 'already has' in payload['error']
    is_error, payload = call(client, key, 'set_env', {'site': 'shop', 'unset': ['DATABASE_URL']})
    assert is_error and 'database' in payload['error']

    # another tenant cannot remove it, even knowing the names
    _, bob_key = make_key(db_session)
    assert call(client, bob_key, 'delete_database', {'site': 'shop', 'engine': 'postgresql'})[0]


def test_engine_named_like_a_site_db_but_foreign_is_not_linked(client, db_session):
    from app.services.env_service import EnvService
    from app.services.mcp.context import ToolContext, site_databases
    alice, _ = make_key(db_session)
    bob, _ = make_key(db_session)
    site = make_application(db_session, name='shop', user_id=alice.id, root_path='/tmp/shop')
    make_application(db_session, name='shop-redis', user_id=bob.id, root_path='/tmp/shop-redis')
    EnvService.set_env_reference(site.id, 'REDIS_URL', {'kind': 'service', 'service': 'shop-redis',
                                                        'property': 'url'})
    assert site_databases(ToolContext(user=alice, can_write=True), site) == []


def test_validation_helpers():
    from app.services.mcp import validation as v
    from app.services.mcp.errors import ToolError
    assert v.cron_schedule('*/5 * * * *') == '*/5 * * * *'
    assert v.cron_schedule('0,30 3 * * 1-5') == '0,30 3 * * 1-5'
    for bad in ('*/4 * * * *', '0,59 * * * *', '61 * * * *'):
        with pytest.raises(ToolError):
            v.cron_schedule(bad)
    assert 'hunter2' not in v.redact('postgresql://u:hunter2@db:5432/app')
    assert '<redacted>' in v.redact('Authorization: Bearer abcdefghijklmnopqrstuvwxyz')
    assert len(v.generate_secret('hex32')) == 64
    from cryptography.fernet import Fernet
    Fernet(v.generate_secret('fernet').encode())
    for name in ('www', 'a', 'ab', 'Shop', 'shop-', 'sh--op', '1shop', 'x' * 33):
        with pytest.raises(ToolError):
            v.site_name(name)
    assert v.site_name('shop-2') == 'shop-2'


def test_purge_removes_containers_volumes_images_and_domains(db_session, monkeypatch):
    from app.models.app_volume import AppVolume
    from app.models.application import Application
    from app.models.domain import Domain
    from app.services import application_restore
    from app.services.docker_service import DockerService
    from app.services.mcp import jobs
    from app.services.volume_service import VolumeService

    app = make_application(db_session, name='gone', root_path='/tmp/gone-app', compose_file=None)
    db_session.session.add(AppVolume(application_id=app.id, name='data-1',
                                     docker_volume_name=f'serverkit-app-{app.id}-data-1', mount_path='/data'))
    db_session.session.add(Domain(name='gone.example.com', application_id=app.id, is_primary=True))
    db_session.session.commit()
    removed = {'containers': [], 'images': [], 'volumes': []}

    def suspend(target, user_id=None):  # the real one tombstones and stops serving
        target.soft_delete()
        db_session.session.commit()
        return {}
    monkeypatch.setattr(application_restore, 'suspend_application', suspend)
    monkeypatch.setattr(DockerService, 'get_all_app_containers', classmethod(lambda cls, a: [
        {'id': 'c1', 'name': f'serverkit-app-{a.id}'}, {'id': 'c2', 'name': f'serverkit-app-{a.id}-worker'},
        {'id': 'c3', 'name': 'someone-else'}]))
    monkeypatch.setattr(DockerService, 'remove_container', staticmethod(
        lambda cid, force=False, volumes=False: removed['containers'].append(cid) or {'success': True}))
    monkeypatch.setattr(DockerService, 'list_images', staticmethod(lambda: [
        {'id': 'i1', 'repository': f'serverkit-app-{app.id}'}, {'id': 'i2', 'repository': 'nginx'}]))
    monkeypatch.setattr(DockerService, 'remove_image', staticmethod(
        lambda iid, force=False: removed['images'].append(iid) or {'success': True}))

    def wipe(cls, volume, wipe=False):
        removed['volumes'].append(volume.docker_volume_name)
        db_session.session.delete(volume)
        db_session.session.commit()
    monkeypatch.setattr(VolumeService, 'delete', classmethod(wipe))
    from app.services.cron_service import CronService
    monkeypatch.setattr(DockerService, 'compose_down', classmethod(lambda cls, *a, **k: {'success': True}))
    monkeypatch.setattr(CronService, 'clear_application', classmethod(lambda cls, app_id: 0))

    assert jobs._purge_app(app) is True
    assert removed['containers'] == ['c1', 'c2']
    assert removed['images'] == ['i1']
    assert removed['volumes'] == [f'serverkit-app-{app.id}-data-1']
    assert Domain.query.count() == 0
    assert Application.query.filter_by(name='gone').first() is None
