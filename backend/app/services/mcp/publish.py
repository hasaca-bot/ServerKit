"""publish_site: a GitHub repository -> a running service at https://<name>.<base>.

The repository is reached ONLY through the caller's own GitHub source
connection and must be one that connection grants (the same list list_repos
shows). No raw git URL is accepted. The job then uses the core pieces the
"New service from repository" flow uses — GitService clone + deploy config,
manifest analysis, BuildService, VolumeService, EnvService,
SiteDomainService.give_subdomain and DeploymentService.deploy — and, when
auto_deploy is on, a core git webhook (deploy_on_push) registered on GitHub
through the same connection.
"""
import json
import os
import re

import requests

from app.services.mcp.errors import ToolError
from app.services.mcp import validation as v

GITHUB_API = 'https://api.github.com'
MAX_DOCKERFILE = 20000
DOCKERFILE_NAME = 'Dockerfile.serverkit'
PUBLISH_FIELDS = {'name', 'github', 'branch', 'port', 'dockerfile', 'health', 'volumes', 'env',
                  'auto_deploy', 'root_dir', 'build_filter'}


# ── GitHub through the caller's connection ───────────────────────────────────

def _connection(user_id):
    from app.services.source_connection_service import SourceConnectionService
    connection = SourceConnectionService.get_connection(user_id, 'github')
    if connection is None:
        raise ToolError('your account has no GitHub connection; connect GitHub in the panel '
                        '(Settings -> Connections) and grant it the repository')
    return connection


def list_user_repos(user_id):
    from app.services.source_connection_service import SourceConnectionService
    _connection(user_id)
    try:
        repos = SourceConnectionService.list_github_repositories(user_id, per_page=100)
    except (ValueError, requests.RequestException) as exc:
        raise ToolError(v.redact(f'GitHub did not answer: {exc}'))
    return repos


def user_repo(user_id, full_name):
    """The caller's repository payload for ``full_name``, or ToolError."""
    if not isinstance(full_name, str) or not v.REPO_RE.match(full_name):
        raise ToolError('github: "<owner>/<repo>" of a repository your GitHub connection grants')
    for repo in list_user_repos(user_id):
        if (repo.get('full_name') or '').lower() == full_name.lower():
            return repo
    raise ToolError(f'github: {full_name} is not among the repositories your GitHub connection '
                    'grants; run list_repos, or grant it to the ServerKit GitHub App')


def _token(user_id):
    from app.services.source_connection_service import SourceConnectionService
    return SourceConnectionService._decrypt_connection_token(_connection(user_id))


def _headers(token):
    return {'Accept': 'application/vnd.github+json', 'Authorization': f'Bearer {token}',
            'X-GitHub-Api-Version': '2022-11-28'}


def _repo_path(repo_url):
    m = re.match(r'^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$', repo_url or '')
    if not m:
        raise ValueError('not a GitHub repository URL')
    return m.group(1), m.group(2)


def github_create_hook(user_id, repo_url, url, secret):
    owner, repo = _repo_path(repo_url)
    response = requests.post(
        f'{GITHUB_API}/repos/{owner}/{repo}/hooks', headers=_headers(_token(user_id)), timeout=20,
        json={'name': 'web', 'active': True, 'events': ['push'],
              'config': {'url': url, 'content_type': 'json', 'secret': secret,
                         'insecure_ssl': '0'}})
    response.raise_for_status()
    return response.json().get('id')


def github_delete_hooks(user_id, repo_url, webhook_token):
    """Delete the repo's hooks that point at this core webhook token."""
    if not webhook_token:
        return 0
    owner, repo = _repo_path(repo_url)
    headers = _headers(_token(user_id))
    response = requests.get(f'{GITHUB_API}/repos/{owner}/{repo}/hooks', headers=headers, timeout=20)
    response.raise_for_status()
    removed = 0
    for hook in response.json() or []:
        if str((hook.get('config') or {}).get('url', '')).endswith('/' + webhook_token):
            requests.delete(f'{GITHUB_API}/repos/{owner}/{repo}/hooks/{hook["id"]}',
                            headers=headers, timeout=20)
            removed += 1
    return removed


# ── validation (tool side, before anything is queued) ────────────────────────

def validate(ctx, args):
    """Return the normalized spec, or raise ToolError."""
    v.no_extra(args, PUBLISH_FIELDS)
    spec = {'name': v.site_name(args.get('name'), 'name')}
    spec['github'] = args.get('github')
    branch = args.get('branch', 'main')
    if not isinstance(branch, str) or not v.BRANCH_RE.match(branch) or '..' in branch \
            or branch.startswith(('-', '/')):
        raise ToolError('branch: invalid')
    spec['branch'] = branch
    port = args.get('port')
    if port is not None and (not v.is_int(port) or not 1 <= port <= 65535):
        raise ToolError('port: the port your app listens on inside the container, 1-65535')
    spec['port'] = port
    dockerfile = args.get('dockerfile')
    if dockerfile is not None and (not isinstance(dockerfile, str) or len(dockerfile) > MAX_DOCKERFILE
                                   or not dockerfile.strip()):
        raise ToolError(f'dockerfile: Dockerfile text up to {MAX_DOCKERFILE} chars')
    spec['dockerfile'] = dockerfile
    health = args.get('health', '/')
    if not isinstance(health, str) or not v.HEALTH_RE.match(health):
        raise ToolError('health: a path starting with /')
    spec['health'] = health
    volumes = args.get('volumes', [])
    if not isinstance(volumes, list) or len(volumes) > 5 or not all(
            isinstance(p, str) and v.VOLUME_RE.match(p) and '..' not in p for p in volumes):
        raise ToolError('volumes: up to 5 absolute container paths')
    spec['volumes'] = list(dict.fromkeys(volumes))
    env = args.get('env', {})
    if not isinstance(env, dict) or len(env) > v.MAX_ENV_PER_CALL:
        raise ToolError(f'env: object with up to {v.MAX_ENV_PER_CALL} keys')
    for key, value in env.items():
        v.env_key(key)
        v.env_value(key, value)
    spec['env'] = env
    auto_deploy = args.get('auto_deploy', False)
    if not isinstance(auto_deploy, bool):
        raise ToolError('auto_deploy: true/false')
    spec['auto_deploy'] = auto_deploy
    from app.services import deploy_control_service as dc
    root_dir = args.get('root_dir')
    if root_dir is not None:
        root_dir, error = dc.validate_root_dir(root_dir)
        if error:
            raise ToolError(f'root_dir: {error}')
    spec['root_dir'] = root_dir
    build_filter = args.get('build_filter')
    if build_filter is not None:
        build_filter, error = dc.validate_build_filter(build_filter)
        if error:
            raise ToolError(error)
    spec['build_filter'] = build_filter
    return spec


def start(ctx, args):
    """publish_site tool body: validate, authorize, then queue the job."""
    from app.models.application import Application
    from app.models.domain import Domain
    from app.services.mcp import jobs
    from app.services.mcp.context import base_domain, get_site, check_quota, require_no_running_job

    spec = validate(ctx, args)
    name = spec['name']
    base = base_domain()
    if not base:
        raise ToolError('this panel has no managed-sites base domain yet; ask the operator to set one')

    existing = Application.query.filter_by(name=name, server_id=None).first()
    app = None
    if existing is not None:
        # Re-publishing updates one of YOUR sites; any other holder of the name
        # (another tenant, a deleted app in the bin) gets the same refusal.
        try:
            app = get_site(ctx, name, access='write')
        except ToolError:
            raise ToolError(f'name "{name}" is taken; choose another name')
        from app.services.git_service import GitService
        configured = (GitService.get_config().get('apps', {}).get(str(app.id)) or {}).get('repo_url')
        if not configured or configured.rstrip('/').lower().removesuffix('.git') != \
                f'https://github.com/{str(spec["github"]).lower()}':
            raise ToolError(f'site "{name}" was published from another repository; '
                            'delete it first or choose another name')
        require_no_running_job(app)
    else:
        host = f'{name}.{base}'
        if Domain.query_active().filter_by(name=host).first() is not None:
            raise ToolError(f'name "{name}" is taken; choose another name')
        check_quota(ctx, 'apps')

    repo = user_repo(ctx.user.id, spec['github'])
    spec['github'] = repo['full_name']
    _no_publish_in_flight(name)
    if app is None:
        _reserve_check(name)
    payload = jobs.start(ctx, jobs.KIND_PUBLISH, app=app,
                         plan={'site': name, 'app_id': app.id if app else None, 'spec': spec})
    payload['url'] = f'https://{name}.{base}'
    return payload


def _no_publish_in_flight(name):
    from app.models.deployment_job import DeploymentJob
    marker = f'"site": {json.dumps(name)}'
    running = (DeploymentJob.query
               .filter(DeploymentJob.kind == 'mcp.publish',
                       DeploymentJob.status.in_(('pending', 'running')),
                       DeploymentJob.plan.contains(marker))
               .first())
    if running is not None:
        raise ToolError(f'a publish of {name} is already queued or running ({running.id})')


def _reserve_check(name):
    from app.services.application_lifecycle_service import _assert_managed_app_path
    try:
        path = _assert_managed_app_path(name)
    except ValueError:
        raise ToolError('name: invalid')
    if os.path.exists(path):
        raise ToolError(f'name "{name}" is taken; choose another name')


# ── the job ──────────────────────────────────────────────────────────────────

def run_job(run):
    spec = run.plan['spec']
    if run.plan.get('app_id'):
        return _update(run, spec)
    return _create(run, spec)


def _apply_env(run, app, env):
    from app.services.env_service import EnvService
    for key, value in (env or {}).items():
        literal, kind, secret = v.env_value(key, value)
        if kind and EnvService.get_env_var(app.id, key) is not None:
            run.log(f'{key} already set; generated value kept')
            continue
        _, _, error = EnvService.set_env_var(app.id, key, v.generate_secret(kind) if kind else literal,
                                             is_secret=secret, user_id=run.ctx.user.id)
        if error:
            raise ToolError(f'env {key}: {error}')
        run.log(f'{key} set{" (secret)" if secret else ""}')


def _write_dockerfile(app_path, spec):
    if not spec.get('dockerfile'):
        return None
    folder = os.path.join(app_path, spec['root_dir']) if spec.get('root_dir') else app_path
    with open(os.path.join(folder, DOCKERFILE_NAME), 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(spec['dockerfile'].rstrip('\n') + '\n')
    return DOCKERFILE_NAME


def _volumes(run, app, paths):
    from app.models.app_volume import AppVolume
    from app.services.volume_service import VolumeService
    have = {vol.mount_path for vol in AppVolume.query.filter_by(application_id=app.id).all()}
    for index, mount in enumerate(paths or []):
        if mount in have:
            continue
        VolumeService.create(app, f'data-{index + 1}', mount)
        run.log(f'volume for {mount} created')


def _create(run, spec):
    from app import db
    from app.models.application import Application
    from app.services import repository_application_service as ras
    from app.services.application_lifecycle_service import _assert_managed_app_path
    from app.services.build_service import BuildService
    from app.services.git_service import GitService
    from app.services.repository_manifest_service import RepositoryManifestService
    from app.services.source_connection_service import SourceConnectionService

    name = spec['name']
    if Application.query.filter_by(name=name, server_id=None).first() is not None:
        raise ToolError(f'name "{name}" was taken meanwhile; choose another name')
    from app.services.mcp.context import check_quota
    check_quota(run.ctx, 'apps')
    user_repo(run.ctx.user.id, spec['github'])  # still granted at run time

    run.step(1)
    connection = _connection(run.ctx.user.id)
    source = SourceConnectionService.get_authenticated_clone_url(
        user_id=run.ctx.user.id, connection_id=connection.id, full_name=spec['github'])
    app_path = _assert_managed_app_path(name)
    run.log(f'fetching {spec["github"]}@{spec["branch"]}')
    cloned = GitService.clone_repository(app_path, source['clone_url'], spec['branch'])
    if not cloned.get('success'):
        message = str(cloned.get('error') or 'clone failed').replace(source['clone_url'], source['public_url'])
        raise ToolError(f'could not fetch {spec["github"]}@{spec["branch"]}: {v.redact(message)}')

    run.step(2)
    dockerfile_path = _write_dockerfile(app_path, spec)
    manifest = RepositoryManifestService.analyze_path(app_path)
    recommended = manifest.get('recommended') or {}
    detection = BuildService.detect_build_method(app_path)
    if dockerfile_path:
        build_method, app_type = 'dockerfile', 'docker'
    else:
        build_method = recommended.get('build_method') or detection.get('build_method') or 'auto'
        if build_method == 'custom' and not recommended.get('custom_build_cmd'):
            build_method = 'auto'
        app_type = recommended.get('app_type') or 'docker'
        if app_type not in ('docker', 'flask', 'django', 'php', 'static'):
            app_type = 'docker'
    buildpack_plan = buildpack_type = None
    if build_method in ('nixpacks', 'auto'):
        from app.services.buildpack_service import BuildpackService
        buildpack_plan = BuildpackService.detect(app_path)
        buildpack_type = (buildpack_plan or {}).get('builder')
    app = Application(
        name=name, app_type=app_type, status='stopped', root_path=app_path,
        user_id=run.ctx.user.id, port=spec.get('port') or recommended.get('port'),
        healthcheck_path=spec.get('health') or None, root_dir=spec.get('root_dir'),
        build_filter=json.dumps(spec['build_filter']) if spec.get('build_filter') else None,
        buildpack_type=buildpack_type,
        buildpack_plan=json.dumps(buildpack_plan) if buildpack_plan else None,
    )
    try:
        db.session.add(app)
        db.session.commit()
        run.plan['app_id'] = app.id
        run.job.app_id = app.id
        db.session.commit()
        deploy = GitService.configure_deployment(
            app_id=app.id, app_path=app_path, repo_url=source['public_url'],
            branch=spec['branch'], auto_deploy=spec['auto_deploy'])
        if not deploy.get('success'):
            raise ToolError(deploy.get('error') or 'could not configure deployment')
        built = BuildService.configure_build(
            app_id=app.id, app_path=app_path, build_method=build_method,
            dockerfile_path=dockerfile_path or recommended.get('dockerfile_path') or 'Dockerfile',
            custom_build_cmd=recommended.get('custom_build_cmd'),
            custom_start_cmd=recommended.get('custom_start_cmd'),
            buildpack_plan=buildpack_plan)
        if not built.get('success'):
            raise ToolError(built.get('error') or 'could not configure the build')
        try:
            from app.services.manifest_persistence_service import ManifestPersistenceService
            ManifestPersistenceService.apply_import(app, manifest, user_id=run.ctx.user.id,
                                                    source_repo=source['public_url'],
                                                    source_ref=spec['branch'])
        except Exception:  # noqa: BLE001 - enrichment is best effort, as in the core flow
            db.session.rollback()
        _apply_env(run, app, spec.get('env'))
        _volumes(run, app, spec.get('volumes'))
    except Exception:
        db.session.rollback()
        run.job.app_id = None
        db.session.commit()
        ras.abort_repository_creation(app)
        raise
    return _finish(run, app, spec, source['public_url'])


def _update(run, spec):
    from app import db
    from app.services.git_service import GitService

    app = run.site('write')
    run.step(2)
    if spec.get('port'):
        app.port = spec['port']
    app.healthcheck_path = spec.get('health') or app.healthcheck_path
    if spec.get('root_dir') is not None:
        app.root_dir = spec['root_dir']
    if spec.get('build_filter') is not None:
        app.build_filter = json.dumps(spec['build_filter']) if spec['build_filter'] else None
    db.session.commit()
    _write_dockerfile(app.root_path, spec)
    _apply_env(run, app, spec.get('env'))
    _volumes(run, app, spec.get('volumes'))
    config = GitService.get_config().get('apps', {}).get(str(app.id)) or {}
    if bool(config.get('auto_deploy')) != spec['auto_deploy'] and config:
        config['auto_deploy'] = spec['auto_deploy']
        full = GitService.get_config()
        full.setdefault('apps', {})[str(app.id)] = config
        GitService.save_config(full)
    return _finish(run, app, spec, config.get('repo_url'))


def _finish(run, app, spec, repo_url):
    from app.services.site_domain_service import SiteDomainService

    run.step(3)
    run.deploy(app)
    run.step(4)
    domain = SiteDomainService.give_subdomain(app, label=app.name)
    url = domain.get('url') if domain.get('success') else None
    if not domain.get('success'):
        run.log(f'address not published: {domain.get("error")}', level='warn')
    elif domain.get('warning'):
        run.log(f'address: {domain["warning"]}', level='warn')
    auto = _auto_deploy(run, app, spec, repo_url) if spec['auto_deploy'] else 'off'
    run.result(app_id=app.id, site=app.name, url=url or f'https://{app.name}', auto_deploy=auto)
    run.log(f'DONE {url or app.name}')
    return {'success': True}


def _auto_deploy(run, app, spec, repo_url):
    """Core webhook (deploy_on_push) + its registration on GitHub. Best effort."""
    from app.models import GitWebhook
    from app.services.site_domain_service import SiteDomainService
    from app.services.webhook_service import WebhookService

    if GitWebhook.query.filter_by(app_id=app.id, deploy_on_push=True).first() is not None:
        return 'on'
    created = WebhookService.create_webhook(
        name=f'{app.name} (MCP auto-deploy)', source='github', source_repo_url=repo_url,
        source_branch=spec['branch'], app_id=app.id, deploy_on_push=True, auto_sync=True)
    if not created.get('success'):
        run.log(f'auto-deploy webhook not created: {created.get("error")}', level='warn')
        return 'failed'
    origin = SiteDomainService.panel_origin()
    token = created['webhook']['webhook_token']
    if not origin:
        run.log('auto-deploy: the panel has no public address, so GitHub cannot be told; '
                'the owner can add the webhook from the panel', level='warn')
        return 'webhook created, not registered on GitHub'
    try:
        github_create_hook(run.ctx.user.id, repo_url,
                           f'{origin}/api/v1/git/webhooks/receive/{token}', created['secret'])
    except Exception as exc:  # noqa: BLE001 - the App may lack webhook permission
        run.log(f'auto-deploy: GitHub refused the webhook ({v.redact(exc)}); '
                'the owner can add it from the panel', level='warn')
        return 'webhook created, not registered on GitHub'
    run.log('auto-deploy on: every push to the branch redeploys')
    return 'on'
