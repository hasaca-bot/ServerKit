"""MCP tools: tenant isolation, role gating, quotas and input validation."""
import json

import pytest

from factories import make_application
from test_mcp_endpoint import make_key, rpc, _clean_state  # noqa: F401 (autouse fixture)


def call(client, key, name, args=None):
    resp = rpc(client, key, 'tools/call', {'name': name, 'arguments': args or {}})
    assert resp.status_code == 200, resp.data
    result = resp.get_json()['result']
    text = result['content'][0]['text']
    try:
        payload = json.loads(text)
    except ValueError:
        payload = text
    return result['isError'], payload


@pytest.fixture
def docker_stub(monkeypatch):
    """No real docker: one running container per app, scripted exec/logs."""
    from app.services.docker_service import DockerService
    calls = []

    def run(args, timeout=60, **kwargs):
        calls.append(list(args))
        return {'success': True, 'output': 'ok SECRET_TOKEN=abcdefghijklmnop1234', 'stderr': '',
                'error': None, 'returncode': 0}

    monkeypatch.setattr(DockerService, 'run', staticmethod(run))
    monkeypatch.setattr(DockerService, 'get_all_app_containers', classmethod(
        lambda cls, app: [{'id': f'c{app.id}', 'name': f'serverkit-app-{app.id}', 'service': 'web',
                           'state': 'running'}]))
    monkeypatch.setattr(DockerService, 'get_app_container_id', classmethod(lambda cls, app: f'c{app.id}'))
    monkeypatch.setattr(DockerService, 'get_container_logs', staticmethod(
        lambda cid, tail=100, since=None, timestamps=True: {
            'success': True, 'logs': 'boot\npassword=hunter2hunter2 ok\n' + 'x' * 50}))
    monkeypatch.setattr(DockerService, 'get_containers_stats', staticmethod(lambda ids: {}))
    return calls


@pytest.fixture
def cron_stub(monkeypatch):
    """CronService without a crontab: metadata in memory."""
    from app.services.cron_service import CronService
    jobs = {}
    counter = {'n': 0}

    def add_job(cls, schedule, command, name=None, description=None, application_id=None):
        counter['n'] += 1
        job_id = f'job_{20261017000000 + counter["n"]}_abcdef'
        jobs[job_id] = {'name': name, 'schedule': schedule, 'command': command,
                        'application_id': application_id, 'enabled': True}
        return {'success': True, 'job_id': job_id}

    def jobs_for_application(cls, application_id):
        return [{'id': k, **j} for k, j in jobs.items() if j['application_id'] == application_id]

    def remove_job(cls, job_id):
        jobs.pop(job_id, None)
        return {'success': True}

    monkeypatch.setattr(CronService, 'add_job', classmethod(add_job))
    monkeypatch.setattr(CronService, 'jobs_for_application', classmethod(jobs_for_application))
    monkeypatch.setattr(CronService, 'remove_job', classmethod(remove_job))
    return jobs


@pytest.fixture
def tenants(db_session):
    alice, alice_key = make_key(db_session, role='developer')
    bob, bob_key = make_key(db_session, role='developer')
    site = make_application(db_session, name='alpha', user_id=alice.id, root_path='/tmp/alpha',
                            port=8080, status='running')
    return {'alice': alice, 'alice_key': alice_key, 'bob': bob, 'bob_key': bob_key, 'site': site}


SITE_TOOLS = [
    ('site_info', {}), ('get_logs', {}), ('get_metrics', {}), ('list_deploys', {}),
    ('list_env_keys', {}), ('list_cron', {}), ('list_domains', {}), ('list_previews', {}),
    ('set_env', {'set': {'FOO': 'bar'}, 'redeploy': False}), ('redeploy_site', {}),
    ('rollback_site', {}), ('restart_site', {}), ('stop_site', {}), ('start_site', {}),
    ('create_database', {'engine': 'postgresql'}), ('delete_database', {'engine': 'postgresql'}),
    ('add_cron', {'schedule': '*/10 * * * *', 'argv': ['python', 'job.py']}),
    ('delete_cron', {'job_id': 'job_20261017000001_abcdef'}),
    ('run_command', {'argv': ['ls']}),
    ('add_domain', {'domain': 'shop.example.com'}), ('remove_domain', {'domain': 'shop.example.com'}),
    ('delete_site', {'confirm': 'DELETE alpha'}),
]


@pytest.mark.parametrize('tool,args', SITE_TOOLS)
def test_other_tenant_cannot_reach_a_site(client, tenants, docker_stub, cron_stub, tool, args):
    is_error, payload = call(client, tenants['bob_key'], tool, {'site': 'alpha', **args})
    assert is_error
    assert 'not found among your sites' in payload['error']
    assert docker_stub == []
    assert cron_stub == {}


def test_other_tenant_cannot_list_or_hijack_by_name(client, tenants, monkeypatch):
    assert call(client, tenants['bob_key'], 'list_sites')[1]['sites'] == []
    names = [s['name'] for s in call(client, tenants['alice_key'], 'list_sites')[1]['sites']]
    assert names == ['alpha']
    # Re-publishing someone else's name never reaches their site.
    from app.services.mcp import publish
    monkeypatch.setattr(publish, 'user_repo', lambda uid, name: {'full_name': name})
    is_error, payload = call(client, tenants['bob_key'], 'publish_site',
                             {'name': 'alpha', 'github': 'bob/alpha'})
    assert is_error and 'taken' in payload['error']


def test_admin_key_is_tenant_scoped_too(client, db_session, tenants):
    _, admin_key = make_key(db_session, role='admin')
    assert call(client, admin_key, 'list_sites')[1]['sites'] == []
    is_error, payload = call(client, admin_key, 'site_info', {'site': 'alpha'})
    assert is_error


def test_owner_reads_site_without_secret_values(client, db_session, tenants, cron_stub):
    from app.services.env_service import EnvService
    EnvService.set_env_var(tenants['site'].id, 'API_TOKEN', 'supersecretvalue123', is_secret=True)
    is_error, info = call(client, tenants['alice_key'], 'site_info', {'site': 'alpha'})
    assert not is_error
    assert info['env'] == [{'key': 'API_TOKEN', 'secret': True}]
    assert 'supersecretvalue123' not in json.dumps(info)


def test_logs_and_command_output_are_redacted(client, tenants, docker_stub):
    is_error, logs = call(client, tenants['alice_key'], 'get_logs', {'site': 'alpha', 'tail': 10})
    assert not is_error
    assert 'hunter2hunter2' not in json.dumps(logs)
    is_error, out = call(client, tenants['alice_key'], 'run_command',
                         {'site': 'alpha', 'argv': ['python', 'manage.py', 'migrate'], 'timeout': 5})
    assert not is_error and out['exit_code'] == 0
    assert 'abcdefghijklmnop1234' not in out['output']
    assert docker_stub[-1] == ['exec', f'serverkit-app-{tenants["site"].id}', 'python', 'manage.py', 'migrate']


def test_job_of_another_tenant_is_invisible(client, db_session, tenants, monkeypatch):
    from app.services.deployment_job_service import DeploymentJobService
    monkeypatch.setattr(DeploymentJobService, '_enqueue_app_deploy', classmethod(lambda cls, job: None))
    is_error, started = call(client, tenants['alice_key'], 'redeploy_site', {'site': 'alpha'})
    assert not is_error and started['job']
    is_error, view = call(client, tenants['alice_key'], 'get_job', {'job_id': started['job']})
    assert not is_error and view['status'] == 'queued' and view['site'] == 'alpha'
    is_error, payload = call(client, tenants['bob_key'], 'get_job', {'job_id': started['job']})
    assert is_error and payload['error'] == 'unknown job id'
    # one job per site at a time
    is_error, payload = call(client, tenants['alice_key'], 'redeploy_site', {'site': 'alpha'})
    assert is_error and 'already queued' in payload['error']


def test_viewer_gets_read_tools_only(client, db_session):
    viewer, key = make_key(db_session, role='viewer')
    make_application(db_session, name='vsite', user_id=viewer.id, root_path='/tmp/vsite')
    names = {t['name'] for t in rpc(client, key, 'tools/list').get_json()['result']['tools']}
    assert 'site_info' in names and 'set_env' not in names and 'delete_site' not in names
    for tool, args in [('set_env', {'set': {'A': 'b'}}), ('run_command', {'argv': ['ls']}),
                       ('delete_site', {'confirm': 'DELETE vsite'}), ('publish_site', {'name': 'x'})]:
        is_error, payload = call(client, key, tool, {'site': 'vsite', **args} if tool != 'publish_site' else args)
        assert is_error and 'read-only' in payload['error']
    assert call(client, key, 'site_info', {'site': 'vsite'})[0] is False


def test_read_only_grant_cannot_write(client, db_session, tenants):
    from app.services.resource_grant_service import ResourceGrantService
    ResourceGrantService.grant(tenants['bob'].id, 'application', tenants['site'].id, role='viewer')
    assert call(client, tenants['bob_key'], 'site_info', {'site': 'alpha'})[0] is False
    is_error, payload = call(client, tenants['bob_key'], 'set_env',
                             {'site': 'alpha', 'set': {'A': 'b'}, 'redeploy': False})
    assert is_error and 'read-only' in payload['error']


def test_quota_error_surfaces_as_tool_error(client, tenants, docker_stub, cron_stub, monkeypatch):
    from app.services.plan_quota_service import PlanQuotaService, QuotaExceededError

    def deny(cls, user, kind, add=1):
        raise QuotaExceededError(f'Your plan allows 0 {kind}; you have 0.')
    monkeypatch.setattr(PlanQuotaService, 'check', classmethod(deny))
    is_error, payload = call(client, tenants['alice_key'], 'add_cron',
                             {'site': 'alpha', 'schedule': '*/10 * * * *', 'argv': ['python', 'job.py']})
    assert is_error and 'Your plan allows 0 cron_jobs' in payload['error']
    assert payload['quota'] == 'cron_jobs'
    is_error, payload = call(client, tenants['alice_key'], 'add_domain',
                             {'site': 'alpha', 'domain': 'shop.example.com'})
    assert is_error and payload['quota'] == 'custom_domains'
    from app.services.mcp import publish
    monkeypatch.setattr(publish, 'user_repo', lambda uid, name: {'full_name': name})
    is_error, payload = call(client, tenants['alice_key'], 'publish_site',
                             {'name': 'newsite', 'github': 'alice/newsite'})
    assert is_error and payload['quota'] == 'apps'
    assert cron_stub == {}


@pytest.mark.parametrize('args,needle', [
    ({'set': {'bad-key': 'x'}}, 'UPPER_SNAKE_CASE'),
    ({'set': {'lower': 'x'}}, 'UPPER_SNAKE_CASE'),
    ({'set': {'SERVERKIT_X': 'x'}}, 'reserved'),
    ({'set': {'DATABASE_URL': 'x'}}, 'database'),
    ({'set': {'A': {'generate': 'md5'}}}, 'generate'),
    ({'set': {'A': 'x' * 4001}}, '4000'),
    ({'set': {'A': 'x'}, 'unset': ['A']}, 'both'),
    ({}, 'nothing to do'),
    ({'set': {'A': 'x'}, 'extra': 1}, 'unknown fields'),
])
def test_set_env_validation(client, tenants, args, needle):
    is_error, payload = call(client, tenants['alice_key'], 'set_env', {'site': 'alpha', **args})
    assert is_error and needle in payload['error']


def test_set_env_writes_and_generates(client, tenants):
    from app.services.env_service import EnvService
    is_error, payload = call(client, tenants['alice_key'], 'set_env', {
        'site': 'alpha', 'redeploy': False,
        'set': {'APP_URL': 'https://alpha.example', 'SECRET_KEY': {'generate': 'hex32'},
                'PW': {'value': 'given', 'secret': True}}})
    assert not is_error and payload['redeployed'] is False
    secret = EnvService.get_env_var(tenants['site'].id, 'SECRET_KEY')
    assert secret.is_secret and len(secret.value) == 64 and secret.value not in json.dumps(payload)
    assert EnvService.get_env_var(tenants['site'].id, 'PW').is_secret
    is_error, payload = call(client, tenants['alice_key'], 'set_env', {
        'site': 'alpha', 'redeploy': False, 'set': {'SECRET_KEY': {'generate': 'hex32'}}})
    assert is_error and 'already set' in payload['error']


@pytest.mark.parametrize('argv', [
    ['sh', '-c', 'rm -rf /'], ['ls;id'], ['echo', '$HOME'], ['a|b'], ['`id`'], [], ['x'] * 21, 'ls',
    ['quote"'], ['back\\slash'],
])
def test_run_command_argv_validation(client, tenants, docker_stub, argv):
    is_error, payload = call(client, tenants['alice_key'], 'run_command', {'site': 'alpha', 'argv': argv})
    assert is_error and 'argv' in payload['error']
    assert docker_stub == []


@pytest.mark.parametrize('schedule', [
    '* * * * *', '*/2 * * * *', '0,3 * * * *', '* * *', '0 25 * * *', '0 0 * * * *', 'a b c d e',
    '0 0 $(id) * *',
])
def test_cron_schedule_validation(client, tenants, docker_stub, cron_stub, schedule):
    is_error, payload = call(client, tenants['alice_key'], 'add_cron',
                             {'site': 'alpha', 'schedule': schedule, 'argv': ['python', 'job.py']})
    assert is_error and 'schedule' in payload['error']
    assert cron_stub == {}


def test_cron_add_list_delete_and_limits(client, tenants, docker_stub, cron_stub):
    key = tenants['alice_key']
    is_error, payload = call(client, key, 'add_cron', {'site': 'alpha', 'schedule': '*/10 * * * *',
                                                       'argv': ['python', 'job.py', '--x=1'], 'name': 'clean'})
    assert not is_error
    job = cron_stub[payload['job_id']]
    assert job['command'] == f'/usr/bin/docker exec serverkit-app-{tenants["site"].id} python job.py --x=1'
    is_error, listed = call(client, key, 'list_cron', {'site': 'alpha'})
    assert listed['jobs'][0]['argv'] == ['python', 'job.py', '--x=1']
    # "%" is refused in cron argv (cron turns it into a newline)
    is_error, payload = call(client, key, 'add_cron', {'site': 'alpha', 'schedule': '0 3 * * *',
                                                       'argv': ['date', '+%s']})
    assert is_error
    for _ in range(9):
        assert call(client, key, 'add_cron', {'site': 'alpha', 'schedule': '0 3 * * *',
                                              'argv': ['true']})[0] is False
    is_error, payload = call(client, key, 'add_cron', {'site': 'alpha', 'schedule': '0 3 * * *',
                                                       'argv': ['true']})
    assert is_error and 'at most 10' in payload['error']
    # another site's job id cannot be deleted through this site
    other = make_application(__import__('app').db, name='beta', user_id=tenants['alice'].id,
                             root_path='/tmp/beta')
    is_error, payload = call(client, key, 'delete_cron', {'site': 'beta',
                                                          'job_id': listed['jobs'][0]['id']})
    assert is_error and 'not a cron job of this site' in payload['error']
    assert other.id
    assert call(client, key, 'delete_cron', {'site': 'alpha', 'job_id': listed['jobs'][0]['id']})[0] is False


def test_publish_requires_own_github_repo(client, tenants, monkeypatch):
    is_error, payload = call(client, tenants['alice_key'], 'publish_site',
                             {'name': 'newsite', 'github': 'alice/newsite'})
    assert is_error and 'GitHub connection' in payload['error']

    from app.services.mcp import publish
    monkeypatch.setattr(publish, 'list_user_repos', lambda uid: [{'full_name': 'alice/mine'}])
    is_error, payload = call(client, tenants['alice_key'], 'publish_site',
                             {'name': 'newsite', 'github': 'someone/else'})
    assert is_error and 'not among the repositories' in payload['error']
    for bad in ({'name': 'www', 'github': 'alice/mine'}, {'name': 'A', 'github': 'alice/mine'},
                {'name': 'ok-name', 'github': 'https://evil/x.git'},
                {'name': 'ok-name', 'github': 'alice/mine', 'branch': '--upload-pack=x'},
                {'name': 'ok-name', 'github': 'alice/mine', 'env': {'SERVERKIT_A': 'x'}},
                {'name': 'ok-name', 'github': 'alice/mine', 'volumes': ['../etc']}):
        assert call(client, tenants['alice_key'], 'publish_site', bad)[0] is True


def test_publish_queues_a_job(client, tenants, monkeypatch):
    from app.services.deployment_job_service import DeploymentJobService
    from app.services.mcp import publish
    monkeypatch.setattr(publish, 'list_user_repos', lambda uid: [{'full_name': 'Alice/Mine'}])
    monkeypatch.setattr(DeploymentJobService, '_enqueue_registered', classmethod(lambda cls, job: None))
    is_error, payload = call(client, tenants['alice_key'], 'publish_site',
                             {'name': 'newsite', 'github': 'alice/mine', 'port': 8000,
                              'env': {'SECRET_KEY': {'generate': 'hex32'}}})
    assert not is_error, payload
    assert payload['url'].startswith('https://newsite.')
    is_error, view = call(client, tenants['alice_key'], 'get_job', {'job_id': payload['job']})
    assert view['kind'] == 'mcp.publish' and view['site'] == 'newsite'
    assert call(client, tenants['bob_key'], 'get_job', {'job_id': payload['job']})[0] is True
    is_error, again = call(client, tenants['alice_key'], 'publish_site',
                           {'name': 'newsite', 'github': 'alice/mine'})
    assert is_error and 'already queued' in again['error']


def test_delete_site_job_runs_full_cleanup(client, db_session, tenants, cron_stub, monkeypatch):
    from app.models import GitWebhook
    from app.models.application import Application
    from app.services.deployment_job_service import DeploymentJobService
    from app.services.mcp import jobs
    monkeypatch.setattr(DeploymentJobService, '_enqueue_registered', classmethod(lambda cls, job: None))
    purged = []
    monkeypatch.setattr(jobs, '_purge_app', lambda app, run=None: purged.append(app.name) or True)
    monkeypatch.setattr(jobs, '_unregister_github_hook', lambda run, hook: None)
    cron_stub['job_20261017000099_abcdef'] = {'name': 'x', 'schedule': '0 3 * * *', 'command': 'c',
                                              'application_id': tenants['site'].id, 'enabled': True}
    db_session.session.add(GitWebhook(name='h', source='github', source_repo_url='https://github.com/a/b',
                                      secret='s', webhook_token='tok123', app_id=tenants['site'].id))
    db_session.session.commit()

    is_error, payload = call(client, tenants['alice_key'], 'delete_site', {'site': 'alpha', 'confirm': 'DELETE beta'})
    assert is_error and 'DELETE alpha' in payload['error']
    is_error, payload = call(client, tenants['alice_key'], 'delete_site', {'site': 'alpha', 'confirm': 'DELETE alpha'})
    assert not is_error
    result = DeploymentJobService.run_job(payload['job'])
    assert result['success'], result
    assert purged == ['alpha']
    assert cron_stub == {}
    assert GitWebhook.query.count() == 0
    is_error, view = call(client, tenants['alice_key'], 'get_job', {'job_id': payload['job']})
    assert view['status'] == 'succeeded' and view['result']['deleted'] == 'alpha'
    assert Application.query.filter_by(name='alpha').first() is not None  # purge was stubbed


def test_get_usage_and_instructions(client, tenants):
    is_error, usage = call(client, tenants['alice_key'], 'get_usage')
    assert not is_error and 'apps' in usage['limits'] and 'apps' in usage['usage']
