"""Render-style deploy controls: commit-message skip markers, monorepo build
filters, and the [skip preview] PR title marker."""
import hashlib
import hmac
import json

import pytest

from app.services import deploy_control_service as dc
from factories import make_application


# ---------------------------------------------------------------- markers

@pytest.mark.parametrize('message,expected', [
    ('fix typo [skip deploy]', True),
    ('[Deploy Skip] docs', True),
    ('wip [SKIP CD]', True),
    ('wip [cd skip]\n\nbody', True),
    ('body line\n\n[skip deploy]', True),
    ('skip deploy without brackets', False),
    ('[skip ci]', False),
    ('', False),
    (None, False),
])
def test_skip_deploy_marker(message, expected):
    assert dc.has_skip_deploy_marker(message) is expected


@pytest.mark.parametrize('title,expected', [
    ('WIP [skip preview]', True), ('[Preview Skip] x', True),
    ('[skip deploy]', False), (None, False),
])
def test_skip_preview_marker(title, expected):
    assert dc.has_skip_preview_marker(title) is expected


# ---------------------------------------------------------------- globs

@pytest.mark.parametrize('pattern,path,expected', [
    ('services/api/**', 'services/api/main.py', True),
    ('services/api/**', 'services/web/main.py', False),
    ('services/api/', 'services/api/a/b.py', True),
    ('**/*.py', 'a/b/c.py', True),
    ('**/*.py', 'c.py', True),
    ('src/*.js', 'src/a/b.js', False),
    ('*.md', 'docs/guide/README.md', True),   # no slash: any depth
    ('docs/*.md', 'README.md', False),
    ('./web/**', 'web/x', True),
    ('a?c', 'abc', True),
])
def test_glob_match(pattern, path, expected):
    assert dc.glob_match(pattern, path) is expected


def test_evaluate_include_only():
    bf = {'paths': ['services/api/**']}
    assert dc.evaluate_build_filter(None, bf, {'services/api/x.py'})[0] is True
    ok, reason = dc.evaluate_build_filter(None, bf, {'services/web/x.py', 'README.md'})
    assert ok is False and reason


def test_evaluate_ignore_only():
    bf = {'ignored_paths': ['docs/**', '*.md']}
    assert dc.evaluate_build_filter(None, bf, {'docs/a.txt', 'README.md'})[0] is False
    assert dc.evaluate_build_filter(None, bf, {'docs/a.txt', 'app.py'})[0] is True


def test_evaluate_include_and_ignore():
    bf = {'paths': ['api/**'], 'ignored_paths': ['api/**/*.md']}
    assert dc.evaluate_build_filter(None, bf, {'api/readme.md'})[0] is False
    assert dc.evaluate_build_filter(None, bf, {'api/readme.md', 'api/src/a.py'})[0] is True
    assert dc.evaluate_build_filter(None, bf, {'web/a.py'})[0] is False


def test_evaluate_empty_filter_always_deploys():
    assert dc.evaluate_build_filter(None, None, {'a'})[0] is True
    assert dc.evaluate_build_filter(None, {'paths': [], 'ignored_paths': []}, set())[0] is True
    assert dc.evaluate_build_filter(None, '{}', None)[0] is True


def test_evaluate_missing_file_list_fails_open():
    ok, reason = dc.evaluate_build_filter(None, {'paths': ['api/**']}, None)
    assert ok is True and 'fail-open' in reason


def test_evaluate_root_dir():
    assert dc.evaluate_build_filter('apps/web', None, {'apps/web/a.js'})[0] is True
    assert dc.evaluate_build_filter('apps/web/', None, {'apps/api/a.js'})[0] is False
    assert dc.evaluate_build_filter('apps/web', None, {'apps/website/a.js'})[0] is False
    assert dc.evaluate_build_filter('apps/web', {'ignored_paths': ['**/*.md']},
                                    {'apps/web/a.md'})[0] is False


def test_validate_build_filter():
    assert dc.validate_build_filter(None) == (None, None)
    assert dc.validate_build_filter({'paths': [], 'ignored_paths': ['']}) == (None, None)
    clean, err = dc.validate_build_filter({'paths': 'a/**\n\nb/**', 'ignoredPaths': ['*.md']})
    assert err is None and clean == {'paths': ['a/**', 'b/**'], 'ignored_paths': ['*.md']}
    assert dc.validate_build_filter({'paths': ['../x']})[1]
    assert dc.validate_build_filter({'bogus': []})[1]
    assert dc.validate_build_filter({'paths': 'x' * 300})[1]
    assert dc.validate_build_filter(['a'])[1]
    assert dc.validate_root_dir('../x')[1]
    assert dc.validate_root_dir('/apps/web/') == ('apps/web', None)


# ---------------------------------------------------------------- payloads

def _gh(message='ok', commits=None):
    return {'ref': 'refs/heads/main', 'head_commit': {'id': 'a' * 40, 'message': message},
            'commits': commits if commits is not None else
            [{'id': 'a' * 40, 'message': message, 'added': [], 'modified': ['api/x.py'], 'removed': []}],
            'pusher': {'name': 'dev'}}


def _gl(message='ok', files=('api/x.py',)):
    return {'ref': 'refs/heads/main', 'checkout_sha': 'b' * 40, 'user_name': 'dev',
            'commits': [
                {'id': 'c' * 40, 'message': 'older', 'added': [], 'modified': ['old.py'], 'removed': []},
                {'id': 'b' * 40, 'message': message, 'added': [], 'modified': list(files), 'removed': []},
            ]}


def _bb(message='ok'):
    return {'actor': {'display_name': 'dev'},
            'push': {'changes': [{'new': {'name': 'main', 'target': {'hash': 'd' * 40, 'message': message}}}]}}


def test_head_commit_message_per_provider():
    assert dc.head_commit_message('github', _gh('hi [skip cd]')) == 'hi [skip cd]'
    assert dc.head_commit_message('gitea', _gh('gitea msg')) == 'gitea msg'
    assert dc.head_commit_message('gitlab', _gl('head [skip deploy]')) == 'head [skip deploy]'
    assert dc.head_commit_message('bitbucket', _bb('bb [deploy skip]')) == 'bb [deploy skip]'
    assert dc.head_commit_message('github', None) is None


def test_changed_files_union_across_commits():
    payload = {'commits': [
        {'added': ['a'], 'modified': ['b'], 'removed': []},
        {'added': [], 'modified': [], 'removed': ['c']},
    ]}
    assert dc.changed_files('github', payload) == {'a', 'b', 'c'}
    assert dc.changed_files('gitlab', _gl()) == {'old.py', 'api/x.py'}


def test_changed_files_unknown_cases():
    assert dc.changed_files('bitbucket', _bb()) is None
    assert dc.changed_files('github', {'commits': []}) is None
    assert dc.changed_files('github', {'commits': [{'id': 'x'}]}) is None
    assert dc.changed_files('github', None) is None
    full = {'commits': [{'modified': [f'f{i}']} for i in range(20)]}
    assert dc.changed_files('github', full) is None
    assert dc.changed_files('gitlab', {'total_commits_count': 30,
                                       'commits': [{'modified': ['a']}]}) is None


# ---------------------------------------------------------------- webhook flow

SECRET = 's3cret'


def _setup(app, **app_fields):
    from app import db
    from app.models import GitWebhook
    a = make_application(db, name='mono', **app_fields)
    hook = GitWebhook(name='h', source='github', source_repo_url='https://github.com/o/r',
                      source_branch='main', secret=SECRET, webhook_token='tok123',
                      auto_sync=True, deploy_on_push=True, app_id=a.id, is_active=True)
    db.session.add(hook)
    db.session.commit()
    return a.id


@pytest.fixture
def deploys(monkeypatch):
    calls = []

    def fake_deploy(cls, **kwargs):
        calls.append(kwargs)
        return {'success': True, 'version': 1, 'deployment_id': 1}
    from app.services.git_deploy_service import GitDeployService
    monkeypatch.setattr(GitDeployService, 'deploy', classmethod(fake_deploy))
    return calls


def _post(client, provider, payload):
    body = json.dumps(payload).encode()
    if provider == 'github':
        headers = {'X-GitHub-Event': 'push',
                   'X-Hub-Signature-256': 'sha256=' + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()}
    elif provider == 'gitlab':
        headers = {'X-Gitlab-Event': 'Push Hook', 'X-Gitlab-Token': SECRET}
    else:
        headers = {'X-Event-Key': 'repo:push',
                   'X-Hub-Signature': 'sha256=' + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()}
    headers['Content-Type'] = 'application/json'
    return client.post('/api/v1/git/webhooks/receive/tok123', data=body, headers=headers)


def _set_source(provider):
    from app import db
    from app.models import GitWebhook
    GitWebhook.query.filter_by(webhook_token='tok123').update({'source': provider})
    db.session.commit()


PROVIDERS = [('github', _gh), ('gitlab', _gl), ('bitbucket', _bb)]


@pytest.mark.parametrize('provider,build', PROVIDERS)
def test_skip_marker_skips_and_records_event(app, client, deploys, provider, build):
    from app.models import GitDeployment
    with app.app_context():
        app_id = _setup(app)
        resp = _post(client, provider, build('docs only [Skip Deploy]'))
        assert resp.status_code == 200
        assert resp.get_json()['action'] == 'skipped'
        assert deploys == []
        row = GitDeployment.query.filter_by(app_id=app_id).one()
        assert row.status == 'skipped' and row.triggered_by == 'webhook'
        assert 'skip' in row.error_message.lower()


@pytest.mark.parametrize('provider,build', PROVIDERS)
def test_no_marker_deploys(app, client, deploys, provider, build):
    with app.app_context():
        _setup(app)
        resp = _post(client, provider, build('normal commit'))
        assert resp.status_code == 200
        assert len(deploys) == 1 and deploys[0]['triggered_by'] == 'webhook'


def test_gitea_payload_shape_uses_github_path(app, client, deploys):
    """Gitea sends X-GitHub-Event with a GitHub-shaped body."""
    with app.app_context():
        _setup(app)
        resp = _post(client, 'github', _gh('x [cd skip]'))
        assert resp.get_json()['action'] == 'skipped' and deploys == []


def test_build_filter_skips_unmatched_push(app, client, deploys):
    from app.models import GitDeployment
    with app.app_context():
        app_id = _setup(app, build_filter=json.dumps({'paths': ['services/api/**']}))
        resp = _post(client, 'github', _gh('web change', commits=[
            {'id': 'a' * 40, 'message': 'web', 'added': [], 'modified': ['services/web/a.js'], 'removed': []}]))
        assert resp.get_json()['action'] == 'skipped' and deploys == []
        assert 'build filter' in GitDeployment.query.filter_by(app_id=app_id).one().error_message


def test_build_filter_deploys_on_any_matching_commit(app, client, deploys):
    with app.app_context():
        _setup(app, build_filter=json.dumps({'paths': ['services/api/**'], 'ignored_paths': ['**/*.md']}))
        resp = _post(client, 'github', _gh('two commits', commits=[
            {'id': '1', 'message': 'a', 'added': [], 'modified': ['services/api/README.md'], 'removed': []},
            {'id': '2', 'message': 'b', 'added': ['services/api/app.py'], 'modified': [], 'removed': []}]))
        assert resp.status_code == 200 and len(deploys) == 1


def test_build_filter_fails_open_without_file_lists(app, client, deploys):
    with app.app_context():
        _setup(app, build_filter=json.dumps({'paths': ['services/api/**']}))
        assert _post(client, 'bitbucket', _bb('anything')).status_code == 200
        assert len(deploys) == 1
        _set_source('github')
        no_files = _gh('x', commits=[{'id': '1', 'message': 'm'}])
        assert _post(client, 'github', no_files).status_code == 200
        assert len(deploys) == 2


def test_root_dir_limits_deploys(app, client, deploys):
    with app.app_context():
        _setup(app, root_dir='apps/web')
        _post(client, 'github', _gh('api', commits=[
            {'id': '1', 'message': 'a', 'added': [], 'modified': ['apps/api/x.py'], 'removed': []}]))
        assert deploys == []
        _post(client, 'github', _gh('web', commits=[
            {'id': '1', 'message': 'a', 'added': [], 'modified': ['apps/web/x.js'], 'removed': []}]))
        assert len(deploys) == 1


def test_manual_deploy_is_never_skipped(app, monkeypatch):
    """GitDeployService.manual_deploy goes straight to deploy(); the webhook
    skip logic lives in WebhookService only."""
    from app.services.git_deploy_service import GitDeployService
    seen = {}

    def fake_deploy(cls, **kwargs):
        seen.update(kwargs)
        return {'success': True}
    monkeypatch.setattr(GitDeployService, 'deploy', classmethod(fake_deploy))
    with app.app_context():
        a_id = _setup(app, build_filter=json.dumps({'paths': ['nothing/**']}))
        GitDeployService.manual_deploy(a_id)
    assert seen.get('triggered_by') == 'manual'


def test_skipped_event_not_a_rollback_target(app):
    """version 0 + status skipped never satisfies the rollback query."""
    from app import db
    from app.models import GitDeployment
    with app.app_context():
        a = make_application(db, name='rb')
        db.session.add(GitDeployment(app_id=a.id, version=0, status='skipped'))
        db.session.commit()
        assert GitDeployment.query.filter_by(app_id=a.id, status='success').count() == 0


# ---------------------------------------------------------------- app settings API

def test_update_app_build_filter_roundtrip(app, client):
    from app import db
    from factories import make_user, headers_for
    with app.app_context():
        user = make_user(db, role='admin')
        a = make_application(db, name='cfg', user_id=user.id)
        headers = headers_for(user)
        ok = client.put(f'/api/v1/apps/{a.id}', headers=headers, json={
            'root_dir': '/apps/web/', 'build_filter': {'paths': 'a/**\nb/**', 'ignored_paths': ['*.md']}})
        assert ok.status_code == 200
        body = ok.get_json()['app']
        assert body['root_dir'] == 'apps/web'
        assert body['build_filter'] == {'paths': ['a/**', 'b/**'], 'ignored_paths': ['*.md']}
        bad = client.put(f'/api/v1/apps/{a.id}', headers=headers, json={'build_filter': {'paths': ['../x']}})
        assert bad.status_code == 400
        cleared = client.put(f'/api/v1/apps/{a.id}', headers=headers, json={'build_filter': None, 'root_dir': ''})
        assert cleared.get_json()['app']['build_filter'] == {'paths': [], 'ignored_paths': []}


# ---------------------------------------------------------------- previews

def test_skip_preview_title_skips_creation(app):
    from app.models.application_preview import ApplicationPreview
    from app.services.preview_service import PreviewService
    from app import db
    with app.app_context():
        a = make_application(db, name='pv')
        PreviewService.enable_previews(a.id, {'enabled': True})
        res = PreviewService.create_preview(a, {'pr_number': 5, 'pr_title': 'WIP [skip preview]'})
        assert res == {'skipped': True, 'reason': 'skip_preview_marker'}
        assert ApplicationPreview.query.filter_by(application_id=a.id).count() == 0
        ok = PreviewService.create_preview(a, {'pr_number': 6, 'pr_title': 'Real change'})
        assert ok.get('pr_number') == 6


def test_skip_preview_webhook_response(app, client):
    from app import db
    from app.models import GitWebhook
    from app.services.preview_service import PreviewService
    with app.app_context():
        a = make_application(db, name='pvh')
        PreviewService.enable_previews(a.id, {'enabled': True})
        db.session.add(GitWebhook(name='h', source='github', source_repo_url='https://github.com/o/r',
                                  secret=SECRET, webhook_token='pvtok', app_id=a.id, is_active=True))
        db.session.commit()
        body = json.dumps({'action': 'opened', 'number': 9, 'pull_request': {
            'title': '[preview skip] chore', 'number': 9, 'head': {'ref': 'x', 'sha': 'abc'}}}).encode()
        sig = 'sha256=' + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        resp = client.post('/api/v1/webhooks/pull-request/pvtok', data=body, headers={
            'X-GitHub-Event': 'pull_request', 'X-Hub-Signature-256': sig,
            'Content-Type': 'application/json'})
        assert resp.status_code == 200 and resp.get_json()['action'] == 'skipped'


# ---------------------------------------------------------------- migration

def test_migration_111_adds_columns_idempotently(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).parents[1] / 'migrations/versions/111_app_build_filter.py'
    spec = importlib.util.spec_from_file_location('m111', path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert mig.down_revision == '110_drop_workflow_schedules_job'

    engine = sa.create_engine('sqlite:///' + (tmp_path / 'm.db').as_posix())
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE applications (id INTEGER PRIMARY KEY)')
        monkeypatch.setattr(mig, 'op', Operations(MigrationContext.configure(conn)))
        mig.upgrade()
        mig.upgrade()   # re-run is a no-op
        cols = {c['name'] for c in sa.inspect(conn).get_columns('applications')}
        assert {'root_dir', 'build_filter'} <= cols
        mig.downgrade()
        cols = {c['name'] for c in sa.inspect(conn).get_columns('applications')}
        assert not ({'root_dir', 'build_filter'} & cols)
    engine.dispose()


# ---------------------------------------------------------------- manifests

def test_manifest_spec_maps_render_fields():
    from app.services.manifest_spec_service import ManifestSpecService
    manifest = {'version': 1, 'services': [{
        'name': 'web', 'type': 'web', 'rootDir': 'apps/web',
        'buildFilter': {'paths': ['apps/web/**'], 'ignoredPaths': ['**/*.md']}}]}
    svc = ManifestSpecService.normalize(manifest)['services'][0]
    assert svc.get('root_dir') == 'apps/web'
    assert svc.get('build_filter') == {'paths': ['apps/web/**'], 'ignored_paths': ['**/*.md']}


def test_render_yaml_detection_maps_build_filter():
    from app.services.repository_manifest_service import RepositoryManifestService
    rm = RepositoryManifestService
    assert rm._render_build_filter({'paths': ['a/**'], 'ignoredPaths': ['b/**']}) == {
        'paths': ['a/**'], 'ignored_paths': ['b/**']}
    assert rm._render_build_filter(None) is None
