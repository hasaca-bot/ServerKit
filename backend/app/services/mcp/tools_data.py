"""create_database / delete_database: one engine container per site and engine."""
from app.services.mcp.errors import ToolError
from app.services.mcp.registry import tool
from app.services.mcp import validation as v
from app.services.mcp import jobs
from app.services.mcp.context import (
    check_quota, get_site, owned_app_by_name, require_no_running_job, site_databases,
)
from app.services.mcp.tools_sites import JOB_NOTE, site_schema

ENGINE_PROP = {'type': 'string', 'enum': list(v.ENGINES), 'description': 'Database engine.'}


@tool('create_database',
      f'Give your site its own database (postgresql, mysql, mariadb or redis; max {v.MAX_DATABASES_PER_SITE} '
      'per site, one per engine). The server installs it privately, puts the connection string into your '
      'site\'s environment (DATABASE_URL for sql engines, REDIS_URL for redis; the password is never shown '
      'to you) and redeploys. Your app only has to read that variable.' + JOB_NOTE,
      site_schema(engine=dict(ENGINE_PROP, _required=True),
                  version={'type': 'string', 'description': 'Optional engine version such as "16".'}),
      write=True)
def create_database(ctx, args):
    from app.services.env_service import EnvService
    v.no_extra(args, ('site', 'engine', 'version'))
    engine, version = args.get('engine'), args.get('version')
    if engine not in v.ENGINES:
        raise ToolError(f'engine: one of {", ".join(v.ENGINES)}')
    if version is not None and (not isinstance(version, str) or not v.VERSION_RE.match(version)):
        raise ToolError('version: a version number such as "16"')
    app = get_site(ctx, args.get('site'), access='write')
    have = site_databases(ctx, app)
    if any(d['engine'] == engine for d in have):
        raise ToolError(f'the site already has a {engine} database')
    if len(have) >= v.MAX_DATABASES_PER_SITE:
        raise ToolError(f'a site can have at most {v.MAX_DATABASES_PER_SITE} databases')
    env_key = 'REDIS_URL' if engine == 'redis' else 'DATABASE_URL'
    if any(d['env_key'] == env_key for d in have):
        raise ToolError(f'the site already has a database using {env_key}; only one SQL database per site')
    if EnvService.get_env_var(app.id, env_key) is not None:
        raise ToolError(f'{env_key} is already set on the site; unset it with set_env first')
    from app.models.application import Application
    if Application.query.filter_by(name=f'{app.name}-{engine}').first() is not None \
            and owned_app_by_name(ctx, f'{app.name}-{engine}') is None:
        raise ToolError(f'the service name {app.name}-{engine} is taken; this site cannot get a {engine} database')
    require_no_running_job(app)
    check_quota(ctx, 'databases')
    return jobs.start(ctx, jobs.KIND_DB_CREATE, app=app,
                      plan={'app_id': app.id, 'site': app.name, 'engine': engine, 'version': version})


@tool('delete_database', 'Remove a database of your site together with ALL its data (cannot be undone), '
                         'remove its environment key and redeploy.' + JOB_NOTE,
      site_schema(engine=dict(ENGINE_PROP, _required=True)), write=True)
def delete_database(ctx, args):
    v.no_extra(args, ('site', 'engine'))
    engine = args.get('engine')
    if engine not in v.ENGINES:
        raise ToolError(f'engine: one of {", ".join(v.ENGINES)}')
    app = get_site(ctx, args.get('site'), access='admin')
    if not any(d['engine'] == engine for d in site_databases(ctx, app)):
        raise ToolError(f'this site has no {engine} database')
    require_no_running_job(app)
    return jobs.start(ctx, jobs.KIND_DB_DELETE, app=app,
                      plan={'app_id': app.id, 'site': app.name, 'engine': engine})
