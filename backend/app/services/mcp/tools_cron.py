"""Cron jobs and one-shot commands, only inside the site's own container.

The container is resolved from the app (never from the request) and the
command is a list of plain argv words: no shell is involved on our side
(``docker exec <container> <argv...>``), and the cron line carries the same
words, which CronService re-validates against shell operators.
"""
import re

from app.services.mcp.errors import ToolError
from app.services.mcp.registry import tool
from app.services.mcp import validation as v
from app.services.mcp.context import check_quota, get_site
from app.services.mcp.tools_sites import site_schema

DOCKER = '/usr/bin/docker'
CONTAINER_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
ARGV_PROP = {'type': 'array', 'items': {'type': 'string', 'pattern': v.ARGV_TOKEN_RE.pattern},
             'minItems': 1, 'maxItems': v.MAX_ARGV,
             'description': 'The command as separate words, e.g. ["python","manage.py","migrate"]. No shell: '
                            'no spaces inside a word, no quotes, pipes, $, &, ; or redirects. It runs inside '
                            'the site\'s own container.'}


def site_container(app):
    """The site's main container name (web service first)."""
    from app.services.docker_service import DockerService
    containers = DockerService.get_all_app_containers(app) or []
    containers = sorted(containers, key=lambda c: (c.get('service') not in ('web', 'app', 'main'),))
    for c in containers:
        name = c.get('name') or c.get('id')
        if name and CONTAINER_RE.match(str(name)):
            return str(name)
    raise ToolError('the site has no container yet (deploy or start it first)')


def _prefix(container):
    return f'{DOCKER} exec {container} '


def cron_view(app):
    from app.services.cron_service import CronService
    out = []
    for job in CronService.jobs_for_application(app.id):
        command = job.get('command') or ''
        argv = None
        m = re.match(rf'^{re.escape(DOCKER)} exec [A-Za-z0-9_.-]+ (.+)$', command)
        if m:
            argv = m.group(1).split()
        out.append({'id': job['id'], 'name': job.get('name'), 'schedule': job.get('schedule'),
                    'argv': argv, 'enabled': job.get('enabled', True), 'next_run': job.get('next_run')})
    return out


@tool('list_cron', 'Scheduled commands of your site: id, schedule, argv, enabled.', site_schema())
def list_cron(ctx, args):
    v.no_extra(args, ('site',))
    app = get_site(ctx, args.get('site'))
    return {'site': app.name, 'jobs': cron_view(app)}


@tool('add_cron', f'Schedule a command inside your site\'s own container (docker exec; no host shell). Max '
                  f'{v.MAX_CRON_PER_SITE} jobs per site, never more often than every {v.MIN_CRON_INTERVAL} minutes.',
      site_schema(schedule={'type': 'string', '_required': True,
                            'description': 'Five cron fields "minute hour day-of-month month weekday", e.g. '
                                           '"*/10 * * * *" or "30 3 * * *". Only digits and * / , -.'},
                  argv=dict(ARGV_PROP, _required=True),
                  name={'type': 'string', 'maxLength': 60, 'description': 'Optional label.'}),
      write=True)
def add_cron(ctx, args):
    from app.services.cron_service import CronService
    v.no_extra(args, ('site', 'schedule', 'argv', 'name'))
    schedule = v.cron_schedule(args.get('schedule'))
    argv = v.argv(args.get('argv'), v.CRON_TOKEN_RE)
    name = args.get('name')
    if name is not None and (not isinstance(name, str) or not v.CRON_NAME_RE.match(name)):
        raise ToolError('name: up to 60 letters, digits, spaces and _ . -')
    app = get_site(ctx, args.get('site'), access='write')
    if len(CronService.jobs_for_application(app.id)) >= v.MAX_CRON_PER_SITE:
        raise ToolError(f'at most {v.MAX_CRON_PER_SITE} cron jobs per site')
    check_quota(ctx, 'cron_jobs')
    container = site_container(app)
    result = CronService.add_job(schedule=schedule, command=_prefix(container) + ' '.join(argv),
                                 name=f'{app.name}: {name or "job"}',
                                 description='Created through MCP', application_id=app.id)
    if not result.get('success'):
        raise ToolError(v.redact(result.get('error') or 'could not add the job'))
    return {'job_id': result['job_id'], 'schedule': schedule}


@tool('delete_cron', 'Remove one scheduled command of your site, by the job_id from list_cron / add_cron.',
      site_schema(job_id={'type': 'string', '_required': True, 'description': 'Id of the cron job.'}),
      write=True)
def delete_cron(ctx, args):
    from app.services.cron_service import CronService
    v.no_extra(args, ('site', 'job_id'))
    job_id = args.get('job_id')
    if not isinstance(job_id, str) or not v.CRON_JOB_ID_RE.match(job_id):
        raise ToolError('job_id: an id returned by list_cron or add_cron')
    app = get_site(ctx, args.get('site'), access='write')
    if job_id not in {j['id'] for j in CronService.jobs_for_application(app.id)}:
        raise ToolError(f'job {job_id} is not a cron job of this site')
    result = CronService.remove_job(job_id)
    if not result.get('success'):
        raise ToolError(v.redact(result.get('error') or 'could not remove the job'))
    return {'deleted': True}


@tool('run_command', 'Run ONE command once inside your site\'s own container and return its exit code and output '
                     '(e.g. a migration). Not a shell. Stops after the timeout (max 60 s); output is capped at '
                     f'{v.MAX_OUTPUT} chars and secret-like strings are redacted. Cannot touch the host or other sites.',
      site_schema(argv=dict(ARGV_PROP, _required=True),
                  timeout={'type': 'integer', 'minimum': 1, 'maximum': 60, 'default': 30}),
      write=True)
def run_command(ctx, args):
    from app.services.docker_service import DockerService
    v.no_extra(args, ('site', 'argv', 'timeout'))
    argv = v.argv(args.get('argv'))
    timeout = v.int_range(args, 'timeout', 30, 1, 60)
    app = get_site(ctx, args.get('site'), access='write')
    container = site_container(app)
    result = DockerService.run(['exec', container, *argv], timeout=timeout)
    if result.get('returncode') is None:
        raise ToolError(v.redact(result.get('error') or f'the command did not finish within {timeout} s'))
    output = (result.get('output') or '') + (('\n' + result['stderr']) if result.get('stderr') else '')
    return {'exit_code': result['returncode'], 'output': v.redact(output)[:v.MAX_OUTPUT]}
