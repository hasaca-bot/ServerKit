"""Slow MCP actions as deployment jobs (``get_job`` reads ``deployment_jobs``).

Each kind is a registered deploy kind, so it gets a DeploymentJob row, the
Deploy Console with live logs and a row in Deploy Activity like any core
deployment. The handler re-loads the requesting user and re-checks that the
target is still theirs before touching anything: the plan was written by the
tool after validation, but the job may run later.
"""
import logging
import re

from app.services.mcp.errors import ToolError
from app.services.mcp import validation as v

logger = logging.getLogger(__name__)

KIND_PUBLISH = 'mcp.publish'
KIND_DB_CREATE = 'mcp.db_create'
KIND_DB_DELETE = 'mcp.db_delete'
KIND_DELETE_SITE = 'mcp.delete_site'
KIND_ROLLBACK = 'mcp.rollback'

_STEPS = {
    KIND_PUBLISH: ['Fetch repository', 'Configure service', 'Build and start', 'Publish address'],
    KIND_DB_CREATE: ['Install database', 'Connect site', 'Redeploy site'],
    KIND_DB_DELETE: ['Disconnect site', 'Remove database', 'Redeploy site'],
    KIND_DELETE_SITE: ['Remove databases', 'Remove schedules and hooks', 'Remove service'],
    KIND_ROLLBACK: ['Roll back'],
}


def register_jobs():
    from app.services import deploy_kind_registry
    for kind, handler in _HANDLERS.items():
        deploy_kind_registry.register(kind, handler, replace=True)


def start(ctx, kind, *, plan, app=None):
    """Queue one MCP job. Returns the tool payload {"job": id, ...}."""
    from app.services.deployment_job_service import DeploymentJobService
    register_jobs()  # idempotent; covers processes that skipped startup registration
    result = DeploymentJobService.start_registered(
        kind, steps=_STEPS[kind], app_id=app.id if app is not None else None,
        user_id=ctx.user.id, trigger='mcp', plan=plan)
    if not result.get('success'):
        raise ToolError(v.redact(result.get('error') or 'could not queue the job'))
    return {'job': result['job_id'], 'status': 'queued',
            'note': 'poll get_job every 15-30 s until status is "succeeded" or "failed"'}


# ── helpers used inside handlers ─────────────────────────────────────────────

class _Run:
    """The job, its log stream and the requesting user, re-checked."""

    def __init__(self, job):
        from app.models import User
        from app.services.run_log_service import stream_for
        from app.services.mcp.context import ToolContext
        self.job = job
        self.plan = job.get_plan() or {}
        self.stream = stream_for(job)
        user = User.query.get(job.requested_by) if job.requested_by else None
        if user is None or not user.is_active or not user.is_developer:
            raise RuntimeError('the requesting user can no longer run this job')
        self.ctx = ToolContext(user=user, can_write=user.is_developer)

    def step(self, index):
        try:
            self.stream.set_step(index, _STEPS[self.job.kind][index - 1])
        except Exception:  # noqa: BLE001 - progress is cosmetic
            pass

    def log(self, message, level='info'):
        self.stream.log(level, v.redact(message)[:v.MAX_LOG_LINE])

    def result(self, **values):
        from app import db
        self.job.set_result({**(self.job.get_result() or {}), **values})
        db.session.commit()

    def site(self, access='write'):
        """The plan's app, still owned by / writable for the requester."""
        from app.services.mcp.context import apps_query, get_site
        app = apps_query(self.ctx).filter_by(id=self.plan.get('app_id')).first()
        if app is None:
            raise RuntimeError('the site no longer exists or is no longer yours')
        return get_site(self.ctx, app.name, access=access)

    def deploy(self, app):
        from app.services.deployment_service import DeploymentService
        result = DeploymentService.deploy(
            app.id, user_id=self.ctx.user.id, trigger='mcp',
            log_callback=lambda line: self.log(line))
        if not result.get('success'):
            raise RuntimeError(v.redact(result.get('error') or 'deployment failed'))
        return result


def _guard(fn):
    def handler(job):
        try:
            run = _Run(job)
            return fn(run) or {'success': True}
        except ToolError as exc:
            raise RuntimeError(str(exc))
    handler.__name__ = fn.__name__
    return handler


# ── publish ──────────────────────────────────────────────────────────────────

@_guard
def run_publish(run):
    from app.services.mcp import publish
    return publish.run_job(run)


# ── databases ────────────────────────────────────────────────────────────────

_ENGINE_ENV = {'postgresql': 'DATABASE_URL', 'mysql': 'DATABASE_URL',
               'mariadb': 'DATABASE_URL', 'redis': 'REDIS_URL'}


@_guard
def run_db_create(run):
    from app.models.application import Application
    from app.services import database_engine_service as engines
    from app.services.deployment_job_service import DeploymentJobService
    from app.services.env_service import EnvService
    from app.services.mcp.context import site_databases

    app = run.site('write')
    engine, version = run.plan['engine'], run.plan.get('version')
    env_key = _ENGINE_ENV[engine]
    instance = f'{app.name}-{engine}'
    have = site_databases(run.ctx, app)
    if any(d['engine'] == engine for d in have) or len(have) >= v.MAX_DATABASES_PER_SITE:
        raise RuntimeError('the site already has this database or the maximum number of databases')
    if EnvService.get_env_var(app.id, env_key) is not None:
        raise RuntimeError(f'{env_key} is already set on the site; unset it with set_env first')

    run.step(1)
    plan = engines.plan_install(template_id=engine, version=version, instance_name=instance,
                                expose_public=False,
                                initial_database='app' if engine != 'redis' else None)
    if 'error' in plan:
        raise RuntimeError(plan['error'])
    run.log(f'installing {engine} as {instance}')
    installed = DeploymentJobService.install_template(
        template_id=engine, app_name=plan['app_name'], user_variables=plan['variables'],
        user_id=run.ctx.user.id, wait=True, auto_domain=False)
    engine_app = Application.query_active().filter_by(name=instance).first()
    if not installed.get('success') or engine_app is None:
        if engine_app is not None:
            _purge_app(engine_app, run)
        raise RuntimeError(f'{engine} install failed: {installed.get("error") or "see the panel"}')
    if engine_app.user_id != run.ctx.user.id:
        engine_app.user_id = run.ctx.user.id
        from app import db
        db.session.commit()

    run.step(2)
    _, _, error = EnvService.set_env_reference(
        app.id, env_key, {'kind': 'service', 'service': instance, 'property': 'url'},
        user_id=run.ctx.user.id, description=f'Database {instance} (managed by create_database)')
    if error:
        raise RuntimeError(error)
    run.log(f'{env_key} now points at {instance} (value kept in the panel)')

    run.step(3)
    run.deploy(app)
    run.result(engine=engine, instance=instance, env_key=env_key)
    return {'success': True}


def _purge_app(app, run=None):
    """Soft-delete then purge one app with ALL its data.

    The recycle-bin purge reclaims a compose project and the checkout; a
    build-pack app's own containers, its named volumes, its images and its
    domain rows are removed here first, the way the gateway's delete did.
    Each step is best effort and reported in the job log.
    """
    from app import db
    from app.models.app_volume import AppVolume
    from app.models.domain import Domain
    from app.services import application_restore, recycle_bin_service
    from app.services.docker_service import DockerService
    from app.services.volume_service import VolumeService

    def note(message):
        if run is not None:
            run.log(message, level='warn')

    application_restore.suspend_application(app, user_id=run.ctx.user.id if run else None)
    own = re.compile(rf'^serverkit-app-{app.id}(-[A-Za-z0-9_.-]+)?$')
    try:
        for container in DockerService.get_all_app_containers(app) or []:
            name = str(container.get('name') or '')
            if own.match(name):
                DockerService.remove_container(container.get('id') or name, force=True)
    except Exception as exc:  # noqa: BLE001
        note(f'containers of {app.name} not removed: {exc}')
    for volume in AppVolume.query.filter_by(application_id=app.id).all():
        try:
            VolumeService.delete(volume, wipe=True)
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            note(f'volume {volume.name} not removed: {exc}')
    try:
        for image in DockerService.list_images():
            if image.get('repository') == f'serverkit-app-{app.id}':
                DockerService.remove_image(image.get('id'), force=True)
    except Exception as exc:  # noqa: BLE001
        note(f'images of {app.name} not removed: {exc}')
    for domain in Domain.query.filter_by(application_id=app.id).all():
        db.session.delete(domain)
    db.session.commit()
    app._purge_remove_data = True
    ok, warning = recycle_bin_service.purge('application', app.id)
    if run is not None:
        run.log(f'{app.name} removed' + (f' ({warning})' if warning else ''))
    return ok


@_guard
def run_db_delete(run):
    from app.services.env_service import EnvService
    from app.services.mcp.context import site_databases

    app = run.site('admin')
    entry = next((d for d in site_databases(run.ctx, app) if d['engine'] == run.plan['engine']), None)
    if entry is None:
        raise RuntimeError(f'the site has no {run.plan["engine"]} database')
    run.step(1)
    EnvService.delete_env_var(app.id, entry['env_key'], user_id=run.ctx.user.id)
    run.step(2)
    _purge_app(entry['app'], run)
    run.step(3)
    run.deploy(app)
    run.result(engine=entry['engine'], removed=True)
    return {'success': True}


# ── delete_site ──────────────────────────────────────────────────────────────

@_guard
def run_delete_site(run):
    from app import db
    from app.models import GitWebhook
    from app.services.cron_service import CronService
    from app.services.git_service import GitService
    from app.services.mcp.context import site_databases

    app = run.site('admin')
    warnings = []
    run.step(1)
    for entry in site_databases(run.ctx, app):
        try:
            _purge_app(entry['app'], run)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f'database {entry["instance"]} not removed: {exc}')
    run.step(2)
    for job in CronService.jobs_for_application(app.id):
        result = CronService.remove_job(job['id'])
        if not result.get('success'):
            warnings.append(f'cron job {job["id"]} not removed')
    for hook in GitWebhook.query.filter_by(app_id=app.id).all():
        _unregister_github_hook(run, hook)
        db.session.delete(hook)
    db.session.commit()
    GitService.remove_deployment(app.id)
    run.step(3)
    name = app.name
    run.job.app_id = None  # the job outlives the app it removes
    db.session.commit()
    _purge_app(app, run)
    run.result(deleted=name, **({'warnings': warnings} if warnings else {}))
    run.log(f'{name} deleted')
    return {'success': True}


def _unregister_github_hook(run, hook):
    """Best effort: remove the GitHub webhook publish registered for this hook."""
    try:
        from app.services.mcp.publish import github_delete_hooks
        removed = github_delete_hooks(run.ctx.user.id, hook.source_repo_url, hook.webhook_token)
        if removed:
            run.log(f'GitHub webhook removed ({removed})')
    except Exception as exc:  # noqa: BLE001
        run.log(f'GitHub webhook not removed: {exc}', level='warn')


# ── rollback ─────────────────────────────────────────────────────────────────

@_guard
def run_rollback(run):
    from app.services.deployment_service import DeploymentService
    app = run.site('write')
    run.step(1)
    result = DeploymentService.rollback(app.id, target_version=run.plan.get('version'),
                                        user_id=run.ctx.user.id,
                                        log_callback=lambda line: run.log(line))
    if not result.get('success'):
        raise RuntimeError(v.redact(result.get('error') or 'rollback failed'))
    deployment = result.get('deployment') or {}
    run.result(version=deployment.get('version'))
    return {'success': True}


_HANDLERS = {
    KIND_PUBLISH: run_publish,
    KIND_DB_CREATE: run_db_create,
    KIND_DB_DELETE: run_db_delete,
    KIND_DELETE_SITE: run_delete_site,
    KIND_ROLLBACK: run_rollback,
}
