"""Site tools: list/inspect, logs, metrics, deploy history, lifecycle, publish, delete."""
from app.services.mcp.errors import ToolError
from app.services.mcp.registry import tool, schema
from app.services.mcp import validation as v
from app.services.mcp import jobs
from app.services.mcp.context import (
    apps_query, get_site, is_database_app, linked_database_names, require_no_running_job,
    site_databases, site_url,
)

SITE_PROP = {'type': 'string', 'description': 'Name of one of YOUR sites, e.g. "shop". Run list_sites to see them.'}
JOB_NOTE = (' Returns {"job": id} right away: poll get_job every 15-30 s until status is '
            '"succeeded" or "failed"; the outcome is in "result" and "log".')


def site_schema(**props):
    return schema(['site'] + [k for k, p in props.items() if p.pop('_required', False)],
                  {'site': SITE_PROP, **props})


def _auto_deploy(app):
    from app.models import GitWebhook
    from app.services.git_service import GitService
    config = GitService.get_config().get('apps', {}).get(str(app.id)) or {}
    hook = GitWebhook.query.filter_by(app_id=app.id, deploy_on_push=True, is_active=True).first()
    return bool(hook) or bool(config.get('auto_deploy'))


def _repo(app):
    from app.services.git_service import GitService
    url = (GitService.get_config().get('apps', {}).get(str(app.id)) or {}).get('repo_url') or ''
    prefix = 'https://github.com/'
    return url[len(prefix):].removesuffix('.git') if url.startswith(prefix) else None


@tool('list_sites', 'Your sites (services) with public address, status, repository and branch. '
                    'Only these can be controlled; other tenants\' services are never shown.')
def list_sites(ctx, args):
    from app.services.git_service import GitService
    v.no_extra(args, ())
    config = GitService.get_config().get('apps', {})
    sites = []
    linked = linked_database_names(ctx)
    for app in apps_query(ctx).order_by('name').all():
        if is_database_app(app, linked=linked):
            continue
        cfg = config.get(str(app.id)) or {}
        sites.append({'name': app.name, 'url': site_url(app), 'status': app.status,
                      'github': _repo(app), 'branch': cfg.get('branch'),
                      'owned': app.user_id == ctx.user.id,
                      'last_deployed_at': app.last_deployed_at.isoformat() if app.last_deployed_at else None})
    return {'sites': sites, 'count': len(sites)}


@tool('site_info', 'Status of one of your sites: public url, running state, port, auto_deploy, domains, '
                   'environment KEY NAMES (never values), attached databases and cron jobs.',
      site_schema())
def site_info(ctx, args):
    from app.models.domain import Domain
    from app.models import EnvironmentVariable
    from app.services.mcp.tools_cron import cron_view
    v.no_extra(args, ('site',))
    app = get_site(ctx, args.get('site'))
    env = [{'key': row.key, 'secret': bool(row.is_secret or v.SECRET_NAME_RE.search(row.key))}
           for row in EnvironmentVariable.query.filter_by(application_id=app.id)
           .order_by(EnvironmentVariable.key).all()]
    return {
        'name': app.name, 'url': site_url(app), 'status': app.status, 'port': app.port,
        'github': _repo(app), 'auto_deploy': _auto_deploy(app), 'health': app.healthcheck_path,
        'root_dir': app.root_dir,
        'domains': [d.name for d in Domain.query_active().filter_by(application_id=app.id).all()],
        'env': env,
        'databases': [{'engine': d['engine'], 'instance': d['instance'], 'env_key': d['env_key'],
                       'status': d['app'].status} for d in site_databases(ctx, app)],
        'cron': cron_view(app),
        'last_deployed_at': app.last_deployed_at.isoformat() if app.last_deployed_at else None,
    }


@tool('get_logs', 'Recent output of your site\'s container (token-like strings are redacted). '
                  'Use it to find out why a deploy or a request fails.',
      site_schema(tail={'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 200,
                        'description': 'Number of last lines.'},
                  since={'type': 'string', 'description': 'Only lines newer than this, e.g. "30s", "10m", "2h", "1d".'}))
def get_logs(ctx, args):
    from app.services.docker_service import DockerService
    v.no_extra(args, ('site', 'tail', 'since'))
    tail = v.int_range(args, 'tail', 200, 1, 1000)
    since = args.get('since')
    if since is not None and (not isinstance(since, str) or not v.SINCE_RE.match(since)):
        raise ToolError('since: a duration like "10m", "2h", "1d" or an ISO timestamp')
    app = get_site(ctx, args.get('site'))
    container = DockerService.get_app_container_id(app)
    if not container:
        raise ToolError('the site has no container yet (deploy it first)')
    result = DockerService.get_container_logs(container, tail=tail, since=since, timestamps=True)
    if not result.get('success'):
        raise ToolError(v.redact(result.get('error') or 'could not read the logs'))
    lines = (result.get('logs') or '').splitlines()[-tail:]
    return {'lines': [v.redact(line)[:v.MAX_LOG_LINE] for line in lines]}


@tool('get_metrics', 'CPU, memory, network and disk I/O of your site\'s containers right now.', site_schema())
def get_metrics(ctx, args):
    from app.services.docker_service import DockerService
    v.no_extra(args, ('site',))
    app = get_site(ctx, args.get('site'))
    containers = DockerService.get_all_app_containers(app)
    stats = DockerService.get_containers_stats([c.get('id') or c.get('name') for c in containers])
    out = []
    for c in containers:
        s = stats.get(c.get('id')) or stats.get(c.get('name')) or {}
        out.append({'service': c.get('service'), 'state': c.get('state'),
                    'cpu': s.get('CPUPerc'), 'memory': s.get('MemUsage'), 'memory_percent': s.get('MemPerc'),
                    'network_io': s.get('NetIO'), 'block_io': s.get('BlockIO'), 'pids': s.get('PIDs')})
    return {'site': app.name, 'status': app.status, 'containers': out}


@tool('list_deploys', 'Deployment history of your site (newest first): version, status, commit, trigger.',
      site_schema(limit={'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 10}))
def list_deploys(ctx, args):
    from app.models.deployment import Deployment
    from app.models.deployment_job import DeploymentJob
    v.no_extra(args, ('site', 'limit'))
    limit = v.int_range(args, 'limit', 10, 1, 50)
    app = get_site(ctx, args.get('site'))
    deployments = (Deployment.query.filter_by(app_id=app.id)
                   .order_by(Deployment.version.desc()).limit(limit).all())
    recent = (DeploymentJob.query.filter_by(app_id=app.id)
              .order_by(DeploymentJob.created_at.desc()).limit(limit).all())
    return {
        'deployments': [{'version': d.version, 'status': d.status,
                         'commit': (d.commit_hash or '')[:12] or None,
                         'message': v.redact(d.commit_message or '')[:200] or None,
                         'trigger': d.deploy_trigger,
                         'error': v.redact(d.error_message)[:500] if d.error_message else None,
                         'created_at': d.created_at.isoformat() if d.created_at else None}
                        for d in deployments],
        'jobs': [{'job': j.id, 'kind': j.kind, 'status': j.status,
                  'created_at': j.created_at.isoformat() if j.created_at else None} for j in recent],
    }


@tool('redeploy_site', 'Rebuild your site from the latest pushed commit and restart it with its current '
                       'environment.' + JOB_NOTE, site_schema(), write=True)
def redeploy_site(ctx, args):
    v.no_extra(args, ('site',))
    app = get_site(ctx, args.get('site'), access='write')
    require_no_running_job(app)
    return enqueue_deploy(ctx, app)


def enqueue_deploy(ctx, app):
    from app.services.deployment_job_service import DeploymentJobService
    result = DeploymentJobService.enqueue_app_deploy(app, user_id=ctx.user.id, trigger='mcp')
    if not result.get('success'):
        raise ToolError(v.redact(result.get('error') or 'could not queue the deployment'))
    return {'job': result['job_id'], 'status': 'queued', 'url': site_url(app)}


@tool('rollback_site', 'Roll your site back to an earlier successful deployment (default: the previous one).' + JOB_NOTE,
      site_schema(version={'type': 'integer', 'minimum': 1, 'description': 'Deployment version from list_deploys.'}),
      write=True)
def rollback_site(ctx, args):
    v.no_extra(args, ('site', 'version'))
    version = args.get('version')
    if version is not None and (not v.is_int(version) or version < 1):
        raise ToolError('version: a deployment version from list_deploys')
    app = get_site(ctx, args.get('site'), access='write')
    require_no_running_job(app)
    return jobs.start(ctx, jobs.KIND_ROLLBACK, app=app, plan={'app_id': app.id, 'version': version})


def _lifecycle(ctx, args, action):
    from app.services import application_lifecycle_service as lifecycle
    from app.services.container_sleep_service import ContainerSleepService
    v.no_extra(args, ('site',))
    app = get_site(ctx, args.get('site'), access='write')
    require_no_running_job(app)
    if action == 'stop':
        result = ContainerSleepService.sleep_app(app.id)
    elif action == 'start':
        result = ContainerSleepService.wake_app(app.id)
    else:
        try:
            lifecycle.restart_application(app, user_id=ctx.user.id)
            result = {'success': True}
        except lifecycle.ApplicationLifecycleError as exc:
            result = {'success': False, 'error': str(exc)}
    if not result.get('success'):
        raise ToolError(v.redact(result.get('error') or f'{action} failed'))
    return {'site': app.name, 'status': app.status}


@tool('stop_site', 'Put your site to sleep (containers stopped, data kept, the address stops answering). '
                   'Undo with start_site.', site_schema(), write=True)
def stop_site(ctx, args):
    return _lifecycle(ctx, args, 'stop')


@tool('start_site', 'Wake up a site that was stopped with stop_site.', site_schema(), write=True)
def start_site(ctx, args):
    return _lifecycle(ctx, args, 'start')


@tool('restart_site', 'Restart your site\'s containers without rebuilding.', site_schema(), write=True)
def restart_site(ctx, args):
    return _lifecycle(ctx, args, 'restart')


PUBLISH_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['name', 'github'],
    'properties': {
        'name': {'type': 'string', 'description': 'Site name -> https://<name>.<base domain>. 3-32 chars, a-z 0-9 -. Same name again = update your site.'},
        'github': {'type': 'string', 'description': '"<owner>/<repo>" among list_repos. Only pushed commits are published.'},
        'branch': {'type': 'string', 'default': 'main'},
        'port': {'type': 'integer', 'minimum': 1, 'maximum': 65535,
                 'description': 'Port the app listens on inside the container (0.0.0.0).'},
        'dockerfile': {'type': 'string', 'description': 'Dockerfile text. Omit when the repository has its own Dockerfile or should be auto-detected.'},
        'health': {'type': 'string', 'default': '/', 'description': 'Path that answers 200 without login.'},
        'volumes': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 5,
                    'description': 'Container directories that must survive redeploys (database files, uploads).'},
        'env': {'type': 'object', 'description': 'Environment variables. Value: a string, {"generate": "hex32"|"password"|"fernet"}, or {"value": "...", "secret": true}.'},
        'auto_deploy': {'type': 'boolean', 'default': False, 'description': 'Redeploy by itself on every push to the branch (GitHub webhook).'},
        'root_dir': {'type': 'string', 'description': 'Monorepo: the app\'s directory inside the repository.'},
        'build_filter': {'type': 'object', 'description': 'Monorepo: {"paths": [globs], "ignored_paths": [globs]} deciding which pushes deploy.'},
    },
}


@tool('publish_site', 'Publish a new site, or update one of yours, from one of YOUR GitHub repositories at '
                      'https://<name>.<base domain> (clone through your GitHub connection, build, container, '
                      'HTTPS address, optional auto-deploy).' + JOB_NOTE, PUBLISH_SCHEMA, write=True)
def publish_site(ctx, args):
    from app.services.mcp import publish
    return publish.start(ctx, args)


@tool('delete_site', 'PERMANENTLY remove your site: containers, images, volumes (all data), its databases, '
                     'cron jobs, domains, auto-deploy webhook and source checkout. Cannot be undone. Only do it '
                     'when the owner asked for it. "confirm" must be exactly "DELETE <site>".' + JOB_NOTE,
      site_schema(confirm={'type': 'string', '_required': True,
                           'description': 'The literal text "DELETE " followed by the site name.'}),
      write=True)
def delete_site(ctx, args):
    v.no_extra(args, ('site', 'confirm'))
    name = args.get('site')
    app = get_site(ctx, name, access='admin')
    if args.get('confirm') != f'DELETE {app.name}':
        raise ToolError(f'confirm: must be exactly "DELETE {app.name}" (this removes the site, '
                        'its databases and its data for good)')
    require_no_running_job(app)
    return jobs.start(ctx, jobs.KIND_DELETE_SITE, app=app, plan={'app_id': app.id, 'site': app.name})
