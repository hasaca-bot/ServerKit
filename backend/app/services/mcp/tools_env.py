"""Environment tools. Values are written, never read back."""
from app.services.mcp.errors import ToolError
from app.services.mcp.registry import tool
from app.services.mcp import validation as v
from app.services.mcp.context import get_site, require_no_running_job
from app.services.mcp.tools_sites import SITE_PROP, JOB_NOTE, site_schema, enqueue_deploy

_VALUE_SCHEMA = {'anyOf': [
    {'type': 'string', 'maxLength': v.MAX_ENV_VALUE},
    {'type': 'object', 'properties': {'generate': {'type': 'string', 'enum': list(v.GENERATORS)}},
     'required': ['generate'], 'additionalProperties': False},
    {'type': 'object', 'properties': {'value': {'type': 'string', 'maxLength': v.MAX_ENV_VALUE},
                                      'secret': {'type': 'boolean'}},
     'required': ['value'], 'additionalProperties': False},
]}


@tool('list_env_keys', 'Environment variable NAMES of your site and whether each is secret (values are never returned).',
      site_schema())
def list_env_keys(ctx, args):
    from app.models import EnvironmentVariable
    v.no_extra(args, ('site',))
    app = get_site(ctx, args.get('site'))
    rows = EnvironmentVariable.query.filter_by(application_id=app.id).order_by(EnvironmentVariable.key).all()
    return {'site': app.name, 'keys': [
        {'key': r.key, 'secret': bool(r.is_secret or v.SECRET_NAME_RE.search(r.key)),
         'managed_by_database': r.key in v.DATABASE_ENV_KEYS and bool(r.value_from)}
        for r in rows]}


@tool('set_env', 'Set and/or remove environment variables of your site, then redeploy so the app sees them '
                 '(unless redeploy=false). Values: a string, {"generate":"hex32"|"password"|"fernet"} for a '
                 'secret generated on the server (never shown to you), or {"value":"...","secret":true}. Keys '
                 'are UPPER_SNAKE_CASE, not SERVERKIT_*, and not DATABASE_URL / REDIS_URL (managed by '
                 'create_database / delete_database).' + JOB_NOTE,
      {'type': 'object', 'required': ['site'], 'additionalProperties': False, 'properties': {
          'site': SITE_PROP,
          'set': {'type': 'object', 'description': f'KEY -> value. At most {v.MAX_ENV_PER_CALL} keys per call.',
                  'additionalProperties': _VALUE_SCHEMA},
          'unset': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Keys to remove.'},
          'redeploy': {'type': 'boolean', 'default': True}}},
      write=True)
def set_env(ctx, args):
    from app.services.env_service import EnvService
    v.no_extra(args, ('site', 'set', 'unset', 'redeploy'))
    redeploy = args.get('redeploy', True)
    if not isinstance(redeploy, bool):
        raise ToolError('redeploy: true/false')
    writes, unsets = v.env_changes(args.get('set'), args.get('unset'))
    app = get_site(ctx, args.get('site'), access='write')
    if redeploy:
        require_no_running_job(app)
    for key, (_, kind, _) in writes.items():
        if kind and EnvService.get_env_var(app.id, key) is not None:
            raise ToolError(f'env {key} is already set; unset it first if you want a new generated value')
    for key, (literal, kind, secret) in writes.items():
        value = v.generate_secret(kind) if kind else literal
        _, _, error = EnvService.set_env_var(app.id, key, value, is_secret=secret, user_id=ctx.user.id)
        if error:
            raise ToolError(f'env {key}: {error}')
    removed = []
    for key in unsets:
        ok, _ = EnvService.delete_env_var(app.id, key, user_id=ctx.user.id)
        if ok:
            removed.append(key)
    payload = {'set': sorted(writes), 'unset': removed, 'redeployed': redeploy}
    if redeploy:
        payload.update(enqueue_deploy(ctx, app))
    return payload
