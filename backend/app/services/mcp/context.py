"""Who is calling, and the ONLY way a tool reaches a resource.

Every tool resolves apps, databases, domains, cron jobs and deploy jobs
through the helpers here, which narrow to the caller's own applications plus
the ones explicitly shared with them (a ResourceGrant) — the same filter
WorkspaceService.scope_query applies to a non-admin. It is applied to panel
admins too: an MCP key acts as its owner's tenant, never as the whole panel,
so an AI holding an admin's key still cannot see or touch other tenants'
services (operate those from the panel UI).

A name that exists but belongs to someone else gets exactly the same answer
as a name that does not exist.
"""
from dataclasses import dataclass

from app.services.mcp.errors import ToolError
from app.services.mcp import validation as v


@dataclass
class ToolContext:
    """The caller. ``can_write`` is decided at the boundary from the user's role:
    viewers get the read-only tools, developers and admins the write tools."""
    user: object
    api_key: object = None
    can_write: bool = False


def apps_query(ctx):
    """Live applications the caller owns or was granted (never anyone else's)."""
    from app import db
    from app.models.application import Application
    from app.services.resource_grant_service import ResourceGrantService

    own = Application.user_id == ctx.user.id
    granted = ResourceGrantService.granted_ids(ctx.user.id, 'application')
    query = Application.query_active()
    return query.filter(db.or_(own, Application.id.in_(granted))) if granted else query.filter(own)


def linked_database_names(ctx):
    """Names of engines the caller's sites use through create_database."""
    from app.models import EnvironmentVariable
    from app.models.application import Application
    app_ids = [row.id for row in apps_query(ctx).with_entities(Application.id).all()]
    if not app_ids:
        return set()
    names = set()
    rows = EnvironmentVariable.query.filter(EnvironmentVariable.application_id.in_(app_ids),
                                            EnvironmentVariable.key.in_(tuple(v.DATABASE_ENV_KEYS)),
                                            EnvironmentVariable.value_from.isnot(None)).all()
    for row in rows:
        ref = row.get_reference() or {}
        if ref.get('kind') == 'service' and ref.get('service'):
            names.add(ref['service'])
    return names


def is_database_app(app, ctx=None, linked=None):
    """An engine service (a site's database), not a site the tools may drive."""
    if linked is None and ctx is not None:
        linked = linked_database_names(ctx)
    if linked and app.name in linked:
        return True
    try:
        from app.services.database_engine_service import is_engine_app
        return is_engine_app(app)
    except Exception:  # noqa: BLE001 - a broken template config reads as "not an engine"
        return False


def not_found(name):
    return ToolError(f'site "{name}" was not found among your sites; run list_sites')


def get_site(ctx, name, access='read'):
    """The caller's site named ``name``. access: 'read' | 'write' | 'admin'."""
    from app.services.resource_grant_service import ResourceGrantService

    v.site_name(name)
    app = apps_query(ctx).filter_by(name=name).first()
    if app is None or is_database_app(app, ctx):
        raise not_found(name)
    if access in ('write', 'admin') and not ctx.can_write:
        raise ToolError('your role is read-only; this tool changes a site')
    if access == 'write' and not ResourceGrantService.can_edit_app(ctx.user, app):
        raise ToolError(f'you can only read site "{name}" (it was shared with you read-only)')
    if access == 'admin' and not (app.user_id == ctx.user.id
                                  or ResourceGrantService.can_admin_app(ctx.user, app)):
        raise ToolError(f'only the owner of "{name}" can do this')
    return app


def owned_app_by_name(ctx, name):
    """An app the caller OWNS (not merely granted), by exact name, or None."""
    from app.models.application import Application
    return Application.query_active().filter_by(name=name, user_id=ctx.user.id).first()


def site_databases(ctx, app):
    """Databases attached to ``app`` by create_database.

    The link is the site's own env var holding a service reference to an
    engine named ``<site>-<engine>`` that the caller owns; nothing is taken
    from the request.
    """
    from app.models import EnvironmentVariable

    found = []
    rows = EnvironmentVariable.query.filter_by(application_id=app.id).all()
    for row in rows:
        if row.key not in v.DATABASE_ENV_KEYS or not row.value_from:
            continue
        ref = row.get_reference() or {}
        instance = ref.get('service') if ref.get('kind') == 'service' else None
        if not instance or not instance.startswith(app.name + '-'):
            continue
        engine = instance[len(app.name) + 1:]
        if engine not in v.ENGINES:
            continue
        engine_app = owned_app_by_name(ctx, instance)
        if engine_app is None or engine_app.user_id != app.user_id:
            continue
        found.append({'engine': engine, 'instance': instance, 'env_key': row.key,
                      'app': engine_app})
    return found


def site_url(app):
    """https://<name>.<base>, or the app's primary live domain."""
    from app.models.domain import Domain
    primary = (Domain.query_active().filter_by(application_id=app.id, is_primary=True).first()
               or Domain.query_active().filter_by(application_id=app.id).first())
    if primary:
        return f'https://{primary.name}'
    base = base_domain()
    return f'https://{app.name}.{base}' if base else None


def base_domain():
    try:
        from app.services.site_domain_service import SiteDomainService
        return SiteDomainService.base_domain() or ''
    except Exception:  # noqa: BLE001
        return ''


def active_job_for(app_id):
    from app.models.deployment_job import DeploymentJob
    return (DeploymentJob.query
            .filter(DeploymentJob.app_id == app_id,
                    DeploymentJob.status.in_(('pending', 'running')))
            .first())


def require_no_running_job(app):
    job = active_job_for(app.id)
    if job is not None:
        raise ToolError(f'a job for {app.name} is already queued or running ({job.id}); '
                        'poll it with get_job first')


def check_quota(ctx, kind, add=1):
    """PlanQuotaService.check, surfaced as a tool error."""
    from app.services.plan_quota_service import PlanQuotaService, QuotaExceededError
    try:
        PlanQuotaService.check(ctx.user, kind, add=add)
    except QuotaExceededError as exc:
        raise ToolError(str(exc) or 'Plan limit reached', quota=kind)
