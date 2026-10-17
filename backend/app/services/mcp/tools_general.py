"""get_instructions, get_job, get_usage, list_repos."""
from app.services.mcp.errors import ToolError
from app.services.mcp.registry import tool, schema
from app.services.mcp import validation as v
from app.services.mcp.context import apps_query, base_domain

JOB_VIEW_LOG_LINES = 120


def instructions_text():
    base = base_domain() or '<base domain, not configured yet>'
    return f"""ServerKit MCP — publish and run web apps from your own GitHub repositories.

Your sites go live at https://<name>.{base} with HTTPS. Everything here acts only
on YOUR resources (the services you created, or that were shared with you);
other tenants' services are invisible. Read-only keys/roles get the read tools.

1. Make the app container-ready and PUSH it to GitHub (only pushed commits are
   published). It must listen on 0.0.0.0:<port>, read secrets from environment
   variables, and keep its data under directories listed in "volumes" —
   everything else is replaced on redeploy. A Dockerfile in the repo (or the
   "dockerfile" argument) is the most predictable build; without one the
   panel detects the stack (build packs, render.yaml / app.json / compose).

2. list_repos shows the repositories your GitHub connection grants; only those
   can be published. publish_site {{"name": "shop", "github": "you/shop",
   "branch": "main", "port": 8000, "health": "/health", "volumes": ["/data"],
   "env": {{"SECRET_KEY": {{"generate": "hex32"}}}}, "auto_deploy": true}}
   returns {{"job": "<id>"}}. Poll get_job every 15-30 s until "succeeded" or
   "failed"; read "log" on failure, fix, push, publish again (same name =
   update). Monorepos: "root_dir" and "build_filter" {{"paths": [...],
   "ignored_paths": [...]}}; a head commit containing "[skip deploy]" is not
   auto-deployed.

3. Verify yourself: request https://<name>.{base}<health>. Generated secrets are
   never shown to you; the owner sees them in the panel (service -> Environment).

Running your sites: list_sites, site_info, get_logs, get_metrics, list_deploys,
redeploy_site, rollback_site, restart_site, stop_site, start_site,
list_env_keys, set_env (values: string | {{"generate": "hex32"|"password"|
"fernet"}} | {{"value": "...", "secret": true}}), create_database /
delete_database (postgresql, mysql, mariadb, redis; DATABASE_URL / REDIS_URL is
set for you), list_cron / add_cron / delete_cron, run_command, list_domains /
add_domain / remove_domain, list_previews, get_usage, delete_site.

Limits: names are 3-32 chars a-z 0-9 -, unique on this panel; env keys are
UPPER_SNAKE_CASE, not SERVERKIT_*, and DATABASE_URL / REDIS_URL belong to the
database tools; at most {v.MAX_DATABASES_PER_SITE} databases and {v.MAX_CRON_PER_SITE} cron jobs per site, cron
never more often than every {v.MIN_CRON_INTERVAL} minutes; run_command and cron run ONLY inside
the site's own container with plain argv words (letters, digits and
_ . / : = @ , + % -; no shell, quotes, pipes or $), run_command stops after
60 s and its output is capped and redacted; plan limits (services,
databases, cron jobs, custom domains) are reported by get_usage; one job per
site at a time; delete_site needs confirm "DELETE <name>" and cannot be undone.
"""


@tool('get_instructions',
      'How this server works: what the app must look like, the publish workflow, '
      'everything you can do with your sites afterwards and the limits. Read this first.')
def get_instructions(ctx, args):
    v.no_extra(args, ())
    return instructions_text()


def job_visible(ctx, job):
    """A deploy job is visible when the caller started it or it is for one of their apps."""
    if job.requested_by == ctx.user.id:
        return True
    return bool(job.app_id) and apps_query(ctx).filter_by(id=job.app_id).first() is not None


@tool('get_job',
      'Progress, log and result of an asynchronous job (publish_site, set_env, redeploy_site, '
      'rollback_site, create_database, delete_database, delete_site). Poll every 15-30 s until '
      'status is "succeeded" or "failed".',
      schema(['job_id'], {'job_id': {'type': 'string', 'description': 'The id returned by the tool that started the job.'}}))
def get_job(ctx, args):
    from app.models.deployment_job import DeploymentJob, DeploymentJobLog

    v.no_extra(args, ('job_id',))
    job_id = args.get('job_id')
    if not isinstance(job_id, str) or len(job_id) > 64:
        raise ToolError('unknown job id')
    job = DeploymentJob.query.get(job_id)
    if job is None or not job_visible(ctx, job):
        raise ToolError('unknown job id')
    status = {'pending': 'queued'}.get(job.status, job.status)
    logs = (DeploymentJobLog.query.filter_by(job_id=job.id)
            .order_by(DeploymentJobLog.id.desc()).limit(JOB_VIEW_LOG_LINES).all())
    result = job.get_result() or {}
    safe_result = {k: result[k] for k in ('app_id', 'site', 'url', 'auto_deploy', 'engine',
                                          'instance', 'env_key', 'removed', 'deleted',
                                          'warnings', 'version', 'deployment_id')
                   if k in result}
    return {
        'id': job.id, 'kind': job.kind, 'status': status,
        'site': job.app.name if job.app else (job.get_plan() or {}).get('site'),
        'step': job.current_step_name, 'progress_percent': job.progress_percent,
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'started_at': job.started_at.isoformat() if job.started_at else None,
        'finished_at': job.completed_at.isoformat() if job.completed_at else None,
        'error': v.redact(job.error_message)[:2000] if job.error_message else None,
        'result': safe_result,
        'log': [v.redact(row.message)[:v.MAX_LOG_LINE] for row in reversed(logs)],
    }


@tool('get_usage', 'Your plan limits and current usage (services, databases, cron jobs, custom domains, previews, MCP keys).')
def get_usage(ctx, args):
    from app.services.plan_quota_service import PlanQuotaService
    v.no_extra(args, ())
    return {'limits': PlanQuotaService.limits_for(ctx.user),
            'usage': PlanQuotaService.usage_for(ctx.user),
            'note': 'null limit = unlimited'}


@tool('list_repos',
      'GitHub repositories your GitHub connection grants (only these can be published).',
      schema([], {'search': {'type': 'string', 'maxLength': 100,
                             'description': 'Optional filter on owner/name.'}}))
def list_repos(ctx, args):
    from app.services.mcp.publish import list_user_repos
    v.no_extra(args, ('search',))
    search = args.get('search') or ''
    if not isinstance(search, str) or len(search) > 100:
        raise ToolError('search: text up to 100 chars')
    repos = list_user_repos(ctx.user.id)
    out = [{'full_name': r.get('full_name'), 'private': bool(r.get('private')),
            'default_branch': r.get('default_branch'), 'language': r.get('language'),
            'updated_at': r.get('updated_at')}
           for r in repos if search.lower() in (r.get('full_name') or '').lower()]
    return {'repos': out[:200], 'count': len(out)}
