#!/usr/bin/env python3
"""ServerKit publish gateway: lets an AI that is NOT on this PC publish and run a site.

Runs inside the ServerKit WSL distro (systemd unit serverkit-gateway), listens
on 127.0.0.1 only, and is reached from the internet through the Cloudflare
tunnel at https://deploy.tadadigital.shop. Standard library only.

What a caller can do (with the bearer token, nothing without it), for the sites
that were created through this gateway:
  GET  /                        plain-text instructions for an AI (no token needed)
  GET  /v1/sites                the sites on this server and their addresses
  GET  /v1/sites/<site>         site_info (status, domains, env key names, databases, cron)
  POST /v1/publish              publish or update a site from a GitHub repository
  POST /v1/sites/<site>/<action>  env_set, logs, redeploy, stop, start, db_create,
                                db_delete, cron_list, cron_add, cron_delete, exec,
                                delete_site, site_info
  GET  /v1/jobs/<id>            progress, log and result of an asynchronous action
  POST /mcp                     the same as an MCP server (Streamable HTTP, JSON
                                responses), tools: see MCP_TOOLS

Deliberately NOT possible: touching sites that were not created through this
gateway (the owner's existing sites, the panel, users, firewall), a shell on
the host (commands run only inside the site's own container), using
repositories outside the allowed GitHub owner, choosing hostnames outside
<name>.tadadigital.shop, mounting host paths, or running two jobs at once.

The actual work is done on the Windows side by remote_ops.py (Windows Python,
started through WSL interop), which reuses publish.py / sync.py: the Windows
git credentials fetch the repository, the panel API is called with the owner's
saved session. No GitHub token and no panel password live here. Every
ownership check is done here AND again in remote_ops.py.
"""
import hmac
import http.server
import json
import os
import queue
import re
import secrets
import subprocess
import threading
import time
import uuid

LISTEN = ('127.0.0.1', int(os.environ.get('GATEWAY_PORT', '8711')))
TOKEN_FILE = '/etc/serverkit-gateway/token'
WIN_PYTHON = os.environ.get(
    'GATEWAY_WIN_PYTHON',
    '/mnt/c/Users/hasan_y4hfwna/AppData/Local/Programs/Python/Python313/python.exe')
WIN_SCRIPT = os.environ.get(
    'GATEWAY_WIN_SCRIPT',
    r'C:\Users\hasan_y4hfwna\Desktop\ServerKit\windows\gateway\remote_ops.py')
SITES_DIR = '/mnt/c/Users/hasan_y4hfwna/Desktop/ServerKit/windows/sites'
DOMAIN = 'tadadigital.shop'
MAX_BODY = 256 * 1024
JOB_TIMEOUT = 3600
SYNC_TIMEOUT = 90
MAX_SYNC = 3
RATE_LIMIT = 30          # authenticated requests per minute per IP

NAME_RE = re.compile(r'^[a-z][a-z0-9-]{1,30}[a-z0-9]$')
RESERVED = {'www', 'panel', 'deploy', 'api', 'admin', 'mail', 'smtp', 'ftp', 'root',
            'tada', 'app', 'status', 'cdn', 'static', 'ns1', 'ns2'}
ENV_KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]{0,63}$')
ARGV_TOKEN_RE = re.compile(r'^[A-Za-z0-9_./:=@,+%-]{1,200}$')
CRON_FIELD_RE = re.compile(r'^[0-9*/,-]{1,20}$')
ENGINES = ('postgresql', 'mysql', 'mariadb', 'redis')
GENERATORS = ('hex32', 'password', 'fernet')
MAX_ENGINES_PER_SITE = 2

# action -> True when it runs as a queued job (poll get_job), False when answered directly
ACTIONS = {
    'publish': True, 'env_set': True, 'redeploy': True, 'db_create': True,
    'db_delete': True, 'delete_site': True,
    'site_info': False, 'logs': False, 'stop': False, 'start': False,
    'cron_list': False, 'cron_add': False, 'cron_delete': False, 'exec': False,
}

jobs = {}
job_queue = queue.Queue()
jobs_lock = threading.Lock()
failures = {}          # ip -> [timestamps] of bad-token attempts
calls = {}             # ip -> [timestamps] of authenticated requests
sync_slots = threading.BoundedSemaphore(MAX_SYNC)


def load_token():
    with open(TOKEN_FILE, encoding='utf-8') as f:
        return f.read().strip()


HELP = f"""ServerKit publish gateway — https://deploy.{DOMAIN}

MCP: this server is also an MCP server at https://deploy.{DOMAIN}/mcp
(Streamable HTTP, same Bearer header). Tools: get_instructions, list_sites,
publish_site, get_job, site_info, set_env, get_logs, redeploy_site, stop_site,
start_site, create_database, delete_database, list_cron, add_cron, delete_cron,
run_command, delete_site. The REST calls below do the same.

You can publish a web app that lives in a GitHub repository of the owner
(github.com/hasaca-bot/...) onto the owner's server and then run it: it goes
live at https://<name>.{DOMAIN} with HTTPS. Every request except this page needs:
    Authorization: Bearer <token the owner gave you>
Use curl (Cloudflare blocks Python's default urllib User-Agent; with Python
send a browser-like User-Agent header). At most {RATE_LIMIT} requests per minute.

1. Make the app container-ready and PUSH it to GitHub (only pushed commits
   are published). It must listen on 0.0.0.0:<port>, read any secrets from
   environment variables, and keep its data (SQLite file, uploads) under one
   directory you list in "volumes" — everything else is wiped on redeploy.

2. POST /v1/publish  (Content-Type: application/json)
   {{
     "name": "berber",                      # -> https://berber.{DOMAIN}
     "github": "hasaca-bot/berber-saas",
     "branch": "main",
     "port": 4100,                          # 4100-4999, free (see /v1/sites)
     "dockerfile": "FROM python:3.12-slim\\n...",   # omit if the repo has a Dockerfile
     "health": "/health",                   # path answering 200 without login
     "volumes": ["/data"],
     "env": {{
       "APP_URL": "https://berber.{DOMAIN}",
       "SECRET_KEY": {{"generate": "hex32"}},       # also "password", "fernet"
       "ADMIN_PASSWORD": {{"generate": "password"}}
     }},
     "auto_deploy": true                    # republish by itself on every push
   }}
   -> {{"job": "<id>"}}   Re-POSTing the same name updates that site.

3. Poll GET /v1/jobs/<id> every 15-30 s until "status" is "succeeded" or
   "failed". First builds take 1-10 minutes. Read "log" on failure (and
   "result" on success), fix the repo or the request, push, and POST again.

4. Verify yourself: curl https://<name>.{DOMAIN}<health> and the home page.
   Generated secrets are NOT returned; the owner sees them in his panel
   (Services -> <name> -> Environment). Tell him that.

Running your site afterwards (only sites created through this gateway):
  GET  /v1/sites/<site>                site_info: status, domains, env key names
                                       (never values), databases, cron jobs
  POST /v1/sites/<site>/env_set        {{"set": {{"KEY": "value" | {{"generate":"hex32"}} |
                                       {{"value":"...","secret":true}}}}, "unset": ["KEY"],
                                       "redeploy": true}}                       (job)
  POST /v1/sites/<site>/logs           {{"tail": 200, "since": "10m"}}  recent container log
  POST /v1/sites/<site>/redeploy       rebuild and restart                      (job)
  POST /v1/sites/<site>/stop|start     put the site to sleep / wake it up
  POST /v1/sites/<site>/db_create      {{"engine": "postgresql|mysql|mariadb|redis",
                                       "version": "16"}}  -> sets DATABASE_URL / REDIS_URL
                                       for your app and redeploys               (job)
  POST /v1/sites/<site>/db_delete      {{"engine": "postgresql"}} removes it and its data (job)
  POST /v1/sites/<site>/cron_list      scheduled commands of the site
  POST /v1/sites/<site>/cron_add       {{"schedule": "*/10 * * * *", "argv": ["python","job.py"],
                                       "name": "cleanup"}}
  POST /v1/sites/<site>/cron_delete    {{"job_id": 12}}
  POST /v1/sites/<site>/exec           {{"argv": ["python","manage.py","migrate"], "timeout": 30}}
  POST /v1/sites/<site>/delete_site    {{"confirm": "DELETE <site>"}}  removes everything (job)
Actions marked (job) return {{"job": "<id>"}}: poll GET /v1/jobs/<id>. The others
answer directly with their result.

Limits: you only ever touch sites created through this gateway; cron and exec
run only inside that site's own container (argv words are plain tokens: letters,
digits and _ . / : = @ , + % - ; no spaces, quotes, pipes or $; no shell, no
host access); at most {MAX_ENGINES_PER_SITE} databases per site; at most 10 cron
jobs per site and never more often than every 5 minutes; exec stops after 60 s;
environment keys are UPPER_SNAKE_CASE, may not start with SERVERKIT_, and the
database URL keys belong to the database (remove it with db_delete); deleting a
site needs confirm "DELETE <site>" and cannot be undone. One queued job runs at
a time (others wait); one job per site at once. Name = lowercase letters,
digits, hyphens (3-32 chars).
"""


def used_ports_and_names():
    ports, names = {}, {}
    for entry in os.listdir(SITES_DIR):
        path = os.path.join(SITES_DIR, entry, 'site.json')
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding='utf-8') as f:
                site = json.load(f)
        except (OSError, ValueError):
            continue
        ports[site.get('port')] = site.get('name')
        names[site.get('name')] = site
    return ports, names


def validate(spec):
    """Return an error string, or None. Publish rules; remote_publish.py re-checks everything."""
    if not isinstance(spec, dict):
        return 'body must be a JSON object'
    allowed = {'name', 'github', 'branch', 'port', 'dockerfile', 'health', 'volumes', 'env', 'auto_deploy'}
    extra = set(spec) - allowed
    if extra:
        return f'unknown fields: {sorted(extra)}'
    name = spec.get('name')
    if not isinstance(name, str) or not NAME_RE.match(name) or name in RESERVED:
        return 'name: 3-32 chars, a-z 0-9 -, not reserved'
    repo = spec.get('github')
    if not isinstance(repo, str) or not re.match(r'^hasaca-bot/[A-Za-z0-9._-]{1,100}$', repo):
        return 'github: must be "hasaca-bot/<repo>"'
    branch = spec.get('branch', 'main')
    if not isinstance(branch, str) or not re.match(r'^[A-Za-z0-9._/-]{1,100}$', branch) or '..' in branch:
        return 'branch: invalid'
    port = spec.get('port')
    if not isinstance(port, int) or isinstance(port, bool) or not 4100 <= port <= 4999:
        return 'port: integer 4100-4999'
    ports, names = used_ports_and_names()
    existing = names.get(name)
    if existing is not None and existing.get('origin') != 'gateway':
        return f'name "{name}" belongs to a site that was not created through this gateway'
    if port in ports and ports[port] != name:
        return f'port {port} is used by {ports[port]}'
    if 'dockerfile' in spec and (not isinstance(spec['dockerfile'], str) or len(spec['dockerfile']) > 20000):
        return 'dockerfile: string up to 20000 chars'
    health = spec.get('health', '/')
    if not isinstance(health, str) or not re.match(r'^/[A-Za-z0-9._~/?=&%-]{0,200}$', health):
        return 'health: a path starting with /'
    vols = spec.get('volumes', [])
    if not isinstance(vols, list) or len(vols) > 5 or not all(
            isinstance(v, str) and re.match(r'^/[A-Za-z0-9._/-]{1,100}$', v) and '..' not in v for v in vols):
        return 'volumes: up to 5 absolute container paths'
    env = spec.get('env', {})
    if not isinstance(env, dict) or len(env) > 50:
        return 'env: object with up to 50 keys'
    for k, v in env.items():
        if not re.match(r'^[A-Z][A-Z0-9_]{0,63}$', k):
            return f'env key {k!r}: UPPER_SNAKE_CASE'
        if isinstance(v, dict):
            if v.get('generate') not in GENERATORS or len(v) != 1:
                return f'env {k}: {{"generate": "hex32"|"password"|"fernet"}}'
        elif not isinstance(v, str) or len(v) > 4000:
            return f'env {k}: string up to 4000 chars'
    if not isinstance(spec.get('auto_deploy', False), bool):
        return 'auto_deploy: true/false'
    return None


# ── per-action validation ────────────────────────────────────────────────────

def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _check_argv(argv):
    if not isinstance(argv, list) or not 1 <= len(argv) <= 20:
        return 'argv: list of 1-20 words'
    for t in argv:
        if not isinstance(t, str) or not ARGV_TOKEN_RE.match(t):
            return 'argv: every word must be 1-200 chars of A-Z a-z 0-9 _ . / : = @ , + % - (no spaces, quotes, $, &, ;, |, backticks, backslashes)'
    return None


def _minutes(field):
    """Expand a cron minute field to a sorted list of minutes, or None when malformed."""
    out = set()
    for part in field.split(','):
        m = re.match(r'^(\*|\d{1,2}|\d{1,2}-\d{1,2})(?:/(\d{1,2}))?$', part)
        if not m:
            return None
        base, step = m.group(1), m.group(2)
        if base == '*':
            lo, hi = 0, 59
        elif '-' in base:
            lo, hi = (int(x) for x in base.split('-'))
        else:
            lo = hi = int(base)
            if step:
                hi = 59
        if lo > hi or hi > 59:
            return None
        n = int(step) if step else 1
        if n < 1:
            return None
        out.update(range(lo, hi + 1, n))
    return sorted(out)


def _check_schedule(sched):
    if not isinstance(sched, str):
        return 'schedule: five fields "minute hour day month weekday"'
    fields = sched.split()
    if len(fields) != 5 or not all(CRON_FIELD_RE.match(f) for f in fields):
        return 'schedule: five fields "minute hour day month weekday", each of 0-9 * / , -'
    mins = _minutes(fields[0])
    if not mins:
        return 'schedule: bad minute field'
    gaps = [b - a for a, b in zip(mins, mins[1:])] + [mins[0] + 60 - mins[-1]]
    if len(mins) > 1 and min(gaps) < 5:
        return 'schedule: jobs may not run more often than every 5 minutes'
    return None


def _no_extra(spec, allowed):
    extra = set(spec) - allowed - {'action', 'site'}
    return f'unknown fields: {sorted(extra)}' if extra else None


def validate_action(action, spec):
    """Return (error, site_record). Checks the action, the site and the argument shapes."""
    if not isinstance(spec, dict):
        return 'arguments must be a JSON object', None
    if action not in ACTIONS:
        return f'unknown action {action!r}', None
    if action == 'publish':
        return validate({k: v for k, v in spec.items() if k not in ('action', 'site')}), None
    site = spec.get('site')
    if not isinstance(site, str) or not NAME_RE.match(site) or site in RESERVED:
        return 'site: the name of one of your sites (3-32 chars, a-z 0-9 -)', None
    _, names = used_ports_and_names()
    rec = names.get(site)
    if rec is None or rec.get('origin') != 'gateway':
        return f'site "{site}" does not exist or was not created through this gateway; it cannot be controlled here', None
    engines = [e for e in (rec.get('engines') or []) if isinstance(e, dict)]
    err = None
    if action in ('site_info', 'redeploy', 'stop', 'start', 'cron_list'):
        err = _no_extra(spec, set())
    elif action == 'logs':
        err = _no_extra(spec, {'tail', 'since'})
        tail = spec.get('tail', 200)
        if not err and (not _is_int(tail) or not 1 <= tail <= 1000):
            err = 'tail: integer 1-1000'
        since = spec.get('since')
        if not err and since is not None and (not isinstance(since, str) or not re.match(r'^[0-9]{1,4}[smhd]$', since)):
            err = 'since: like "30s", "10m", "2h", "1d"'
    elif action == 'env_set':
        err = _no_extra(spec, {'set', 'unset', 'redeploy'})
        setv, unset = spec.get('set', {}), spec.get('unset', [])
        owned = {e.get('env_key') for e in engines if e.get('env_key')}
        if not err and (not isinstance(setv, dict) or not isinstance(unset, list)):
            err = 'set: object, unset: list of keys'
        if not err and not setv and not unset:
            err = 'nothing to do: give "set" and/or "unset"'
        if not err and len(setv) + len(unset) > 50:
            err = 'at most 50 keys per call'
        if not err and not isinstance(spec.get('redeploy', True), bool):
            err = 'redeploy: true/false'
        for k in (list(setv) + list(unset) if not err else []):
            if not isinstance(k, str) or not ENV_KEY_RE.match(k):
                err = f'env key {k!r}: UPPER_SNAKE_CASE, max 64 chars'
            elif k.startswith('SERVERKIT_'):
                err = f'env key {k}: keys starting with SERVERKIT_ are reserved'
            elif k in owned:
                err = f'env key {k} belongs to the database of this site; remove the database with db_delete instead'
            if err:
                break
        if not err:
            for k, v in setv.items():
                if isinstance(v, dict):
                    if set(v) == {'generate'} and v['generate'] in GENERATORS:
                        continue
                    if (set(v) <= {'value', 'secret'} and isinstance(v.get('value'), str) and len(v['value']) <= 4000
                            and isinstance(v.get('secret', True), bool)):
                        continue
                    err = f'env {k}: string, {{"generate": "hex32"|"password"|"fernet"}} or {{"value": "...", "secret": true}}'
                    break
                if not isinstance(v, str) or len(v) > 4000:
                    err = f'env {k}: string up to 4000 chars'
                    break
        if not err and set(setv) & set(unset):
            err = 'a key cannot be both set and unset'
    elif action == 'db_create':
        err = _no_extra(spec, {'engine', 'version'})
        eng = spec.get('engine')
        ver = spec.get('version')
        if not err and eng not in ENGINES:
            err = 'engine: one of postgresql, mysql, mariadb, redis'
        elif not err and ver is not None and (not isinstance(ver, str) or not re.match(r'^[A-Za-z0-9._-]{1,20}$', ver)):
            err = 'version: short version tag such as "16"'
        elif not err and any(e.get('engine') == eng for e in engines):
            err = f'this site already has a {eng} database'
        elif not err and len(engines) >= MAX_ENGINES_PER_SITE:
            err = f'a site can have at most {MAX_ENGINES_PER_SITE} databases'
    elif action == 'db_delete':
        err = _no_extra(spec, {'engine'})
        eng = spec.get('engine')
        if not err and eng not in ENGINES:
            err = 'engine: one of postgresql, mysql, mariadb, redis'
        elif not err and not any(e.get('engine') == eng for e in engines):
            err = f'this site has no {eng} database'
    elif action == 'cron_add':
        err = _no_extra(spec, {'schedule', 'argv', 'name'}) or _check_schedule(spec.get('schedule')) or _check_argv(spec.get('argv'))
        nm = spec.get('name')
        if not err and nm is not None and (not isinstance(nm, str) or not re.match(r'^[A-Za-z0-9 _.-]{1,60}$', nm)):
            err = 'name: up to 60 chars of letters, digits, space, _ . -'
    elif action == 'cron_delete':
        err = _no_extra(spec, {'job_id'})
        jid = spec.get('job_id')
        if not err and not ((_is_int(jid) and jid >= 1) or (isinstance(jid, str) and re.match(r'^[A-Za-z0-9_-]{1,64}$', jid))):
            err = 'job_id: an id returned by list_cron / add_cron'
    elif action == 'exec':
        err = _no_extra(spec, {'argv', 'timeout'}) or _check_argv(spec.get('argv'))
        to = spec.get('timeout', 30)
        if not err and (not _is_int(to) or not 1 <= to <= 60):
            err = 'timeout: integer 1-60 seconds'
    elif action == 'delete_site':
        err = _no_extra(spec, {'confirm'})
        if not err and spec.get('confirm') != f'DELETE {site}':
            err = f'confirm: must be exactly "DELETE {site}" (this removes the site, its databases and its data for good)'
    return err, rec


# ── running the Windows side ─────────────────────────────────────────────────

def parse_result(lines):
    for line in reversed(lines):
        if line.startswith('RESULT '):
            try:
                value = json.loads(line[7:])
                return value if isinstance(value, dict) else {'value': value}
            except ValueError:
                return None
    return None


def run_script(spec, timeout, on_line=None):
    """Run remote_ops.py with the request on stdin. Returns (exit_code, lines, result, timed_out)."""
    lines, timed_out = [], False
    proc = subprocess.Popen(
        [WIN_PYTHON, WIN_SCRIPT], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', cwd='/tmp')
    flag = {'t': False}

    def kill():
        flag['t'] = True
        try:
            proc.kill()
        except OSError:
            pass
    timer = threading.Timer(timeout, kill)
    timer.start()
    try:
        proc.stdin.write(json.dumps(spec))
        proc.stdin.close()
        for line in proc.stdout:
            line = line.rstrip()
            lines.append(line)
            if on_line:
                on_line(line)
        code = proc.wait()
    finally:
        timer.cancel()
    timed_out = flag['t']
    return code, lines, parse_result(lines), timed_out


def worker():
    while True:
        job_id = job_queue.get()
        job = jobs[job_id]
        job['status'] = 'running'
        job['started_at'] = time.time()

        def on_line(line, job=job):
            with jobs_lock:
                job['log'].append(line)
                del job['log'][:-400]
        try:
            code, _, result, timed_out = run_script(job['spec'], JOB_TIMEOUT, on_line)
            if timed_out:
                on_line('gateway: job timed out')
            job['result'] = result
            job['status'] = 'succeeded' if code == 0 and not timed_out else 'failed'
        except Exception as e:  # never let the worker die
            on_line(f'gateway: {e}')
            job['status'] = 'failed'
        job['finished_at'] = time.time()


def sites_view():
    _, names = used_ports_and_names()
    used = sorted(p for p in (s.get('port') for s in names.values()) if isinstance(p, int))
    return {'sites': [
        {'name': n, 'url': 'https://' + s['public_host'] if s.get('public_host') else None,
         'port': s.get('port'), 'github': s.get('github'),
         'managed_by_gateway': s.get('origin') == 'gateway'}
        for n, s in sorted(names.items())],
        'ports_in_use': used}


def job_view(job_id):
    job = jobs.get(job_id)
    if job is None:
        return None
    with jobs_lock:
        view = {k: job[k] for k in ('id', 'action', 'name', 'status', 'created_at', 'started_at', 'finished_at')}
        view['queue_position'] = (list(job_queue.queue).index(job['id']) + 1
                                  if job['status'] == 'queued' and job['id'] in job_queue.queue else 0)
        view['log'] = job['log'][-120:]
        view['result'] = job.get('result')
        if job['action'] != 'delete_site':
            view['url'] = f'https://{job["name"]}.{DOMAIN}'
    return view


def enqueue(action, spec, who):
    """Queue an asynchronous action (already validated). Returns (http_status, payload)."""
    site = spec['site']
    with jobs_lock:
        if any(j['name'] == site and j['status'] in ('queued', 'running') for j in jobs.values()):
            return 409, {'error': f'a job for {site} is already queued or running'}
        job_id = str(uuid.uuid4())
        jobs[job_id] = {'id': job_id, 'action': action, 'name': site, 'spec': spec, 'status': 'queued',
                        'created_at': time.time(), 'started_at': None, 'finished_at': None,
                        'log': [], 'result': None}
        # keep memory bounded: forget the oldest finished jobs
        done = sorted((j for j in jobs.values() if j['finished_at']), key=lambda j: j['finished_at'])
        for old in done[:-200]:
            jobs.pop(old['id'], None)
    job_queue.put(job_id)
    print(f'{who} {action} {site} -> job {job_id}', flush=True)
    payload = {'job': job_id, 'status_url': f'/v1/jobs/{job_id}'}
    if action != 'delete_site':
        payload['url'] = f'https://{site}.{DOMAIN}'
    return 202, payload


def run_sync(action, spec, who):
    if not sync_slots.acquire(timeout=30):
        return 503, {'error': 'too many direct actions running, retry in a moment'}
    try:
        print(f'{who} {action} {spec["site"]}', flush=True)
        code, lines, result, timed_out = run_script(spec, SYNC_TIMEOUT)
    except Exception as e:
        return 500, {'error': f'gateway: {e}'}
    finally:
        sync_slots.release()
    if timed_out:
        return 504, {'error': f'{action} took longer than {SYNC_TIMEOUT} s and was stopped'}
    if code == 0:
        return 200, result if result is not None else {'ok': True}
    if result and result.get('error'):
        return 400, result
    err = next((l[7:] for l in reversed(lines) if l.startswith('ERROR: ')), None)
    return 500, {'error': err or 'action failed on the server'}


def dispatch(action, spec, who):
    """Validate and run/queue one action. Returns (http_status, payload)."""
    if not isinstance(spec, dict):
        return 400, {'error': 'arguments must be a JSON object'}
    spec = dict(spec)
    if action == 'publish':
        if 'site' in spec and spec.get('site') != spec.get('name'):
            return 400, {'error': 'site, if given, must equal name'}
        spec.pop('site', None)
    err, _ = validate_action(action, spec)
    if err:
        return 400, {'error': err}
    if action == 'publish':
        spec['site'] = spec['name']
    spec['action'] = action
    if ACTIONS[action]:
        return enqueue(action, spec, who)
    return run_sync(action, spec, who)


# ── MCP (Model Context Protocol, Streamable HTTP transport, JSON responses) ──

MCP_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')

SITE_PROP = {'type': 'string', 'description': 'Name of one of YOUR sites (created through this server), e.g. "berber". Run list_sites to see them; sites with managed_by_gateway=false are not yours.'}
ARGV_PROP = {'type': 'array', 'items': {'type': 'string', 'pattern': '^[A-Za-z0-9_./:=@,+%-]{1,200}$'},
             'minItems': 1, 'maxItems': 20,
             'description': 'The command as a list of separate words, e.g. ["python","manage.py","migrate"]. No shell: no spaces inside a word, no quotes, pipes, $, &, ; or redirects. It runs inside the site\'s own container.'}
ENGINE_PROP = {'type': 'string', 'enum': list(ENGINES), 'description': 'Database engine.'}


def _schema(required, props):
    return {'type': 'object', 'required': required, 'properties': props, 'additionalProperties': False}


PUBLISH_SCHEMA = {
    'type': 'object',
    'required': ['name', 'github', 'port'],
    'properties': {
        'name': {'type': 'string', 'description': f'Site name -> https://<name>.{DOMAIN}. 3-32 chars, a-z 0-9 -.'},
        'github': {'type': 'string', 'description': 'Repository, must be "hasaca-bot/<repo>". Only pushed commits are published.'},
        'branch': {'type': 'string', 'default': 'main'},
        'port': {'type': 'integer', 'minimum': 4100, 'maximum': 4999,
                 'description': 'Port the app listens on inside the container (0.0.0.0). Must be free, see list_sites.'},
        'dockerfile': {'type': 'string', 'description': 'Dockerfile text. Omit only if the repository has a Dockerfile at its root.'},
        'health': {'type': 'string', 'default': '/', 'description': 'Path that answers 200 without login.'},
        'volumes': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 5,
                    'description': 'Container directories that must survive redeploys (database files, uploads).'},
        'env': {'type': 'object', 'description': 'Environment variables. Value is a string, or {"generate": "hex32"|"password"|"fernet"} for a secret created once on the server.'},
        'auto_deploy': {'type': 'boolean', 'default': False, 'description': 'Republish by itself on every push to the branch.'},
    },
}

# tool -> action; tools not listed here are handled directly
TOOL_ACTIONS = {
    'publish_site': 'publish', 'site_info': 'site_info', 'set_env': 'env_set', 'get_logs': 'logs',
    'redeploy_site': 'redeploy', 'stop_site': 'stop', 'start_site': 'start',
    'create_database': 'db_create', 'delete_database': 'db_delete', 'list_cron': 'cron_list',
    'add_cron': 'cron_add', 'delete_cron': 'cron_delete', 'run_command': 'exec',
    'delete_site': 'delete_site',
}
JOB_NOTE = ' Returns {"job": id} right away: poll get_job every 15-30 s until status is "succeeded" or "failed"; the outcome is in "result" and "log".'

MCP_TOOLS = [
    {'name': 'get_instructions',
     'description': 'How this server works: what the app must look like, the publish workflow, everything you can do with your sites afterwards and the limits. Read this first.',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'list_sites',
     'description': 'All sites on this server with public address, port and github repo, plus the ports already in use. Only sites with managed_by_gateway=true can be controlled by you.',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'publish_site',
     'description': f'Publish a new site or update one of your sites from a GitHub repository at https://<name>.{DOMAIN} (build from the repo, container, HTTPS, domain). Only new names or sites created through this server are allowed.' + JOB_NOTE,
     'inputSchema': PUBLISH_SCHEMA},
    {'name': 'get_job',
     'description': 'Progress, log and result of an asynchronous job (publish_site, set_env, redeploy_site, create_database, delete_database, delete_site). Poll every 15-30 s until status is "succeeded" or "failed".',
     'inputSchema': _schema(['job_id'], {'job_id': {'type': 'string', 'description': 'The id returned by the tool that started the job.'}})},
    {'name': 'site_info',
     'description': 'Status of one of your sites: public url, running state, panel app id, port, auto_deploy, domains, environment KEY NAMES (never values; secret or not), attached databases and cron jobs.',
     'inputSchema': _schema(['site'], {'site': SITE_PROP})},
    {'name': 'set_env',
     'description': 'Set and/or remove environment variables of your site, then redeploy so the app sees them (unless redeploy=false). Values: a string, {"generate":"hex32"|"password"|"fernet"} for a secret generated on the server (never shown to you), or {"value":"...","secret":true} for a secret you supply (kept only in the panel, not in any file). Keys are UPPER_SNAKE_CASE, not SERVERKIT_*, and not the database URL keys (DATABASE_URL / REDIS_URL are managed by create_database / delete_database).' + JOB_NOTE,
     'inputSchema': _schema(['site'], {
         'site': SITE_PROP,
         'set': {'type': 'object', 'description': 'KEY -> value. At most 50 keys per call, values up to 4000 chars.',
                 'additionalProperties': {'anyOf': [
                     {'type': 'string', 'maxLength': 4000},
                     {'type': 'object', 'properties': {'generate': {'type': 'string', 'enum': list(GENERATORS)}}, 'required': ['generate'], 'additionalProperties': False},
                     {'type': 'object', 'properties': {'value': {'type': 'string', 'maxLength': 4000}, 'secret': {'type': 'boolean'}}, 'required': ['value'], 'additionalProperties': False}]}},
         'unset': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Keys to remove.'},
         'redeploy': {'type': 'boolean', 'default': True, 'description': 'Rebuild/restart so the change takes effect. Default true.'}})},
    {'name': 'get_logs',
     'description': 'Recent output of your site\'s container (long token-like strings are redacted). Use it to find out why a deploy or a request fails.',
     'inputSchema': _schema(['site'], {
         'site': SITE_PROP,
         'tail': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 200, 'description': 'Number of last lines.'},
         'since': {'type': 'string', 'description': 'Only lines newer than this, e.g. "30s", "10m", "2h", "1d".'}})},
    {'name': 'redeploy_site',
     'description': 'Rebuild your site from the latest pushed commit and restart it with its current environment.' + JOB_NOTE,
     'inputSchema': _schema(['site'], {'site': SITE_PROP})},
    {'name': 'stop_site',
     'description': 'Put your site to sleep (container stopped, data kept, address stops answering). Undo with start_site.',
     'inputSchema': _schema(['site'], {'site': SITE_PROP})},
    {'name': 'start_site',
     'description': 'Wake up a site that was stopped with stop_site.',
     'inputSchema': _schema(['site'], {'site': SITE_PROP})},
    {'name': 'create_database',
     'description': f'Give your site its own database (postgresql, mysql, mariadb or redis; max {MAX_ENGINES_PER_SITE} per site, one per engine). The server creates it, puts the connection string into your site\'s environment (DATABASE_URL for sql engines, REDIS_URL for redis; the password is never shown to you), connects the network and redeploys. Your app only has to read that variable.' + JOB_NOTE,
     'inputSchema': _schema(['site', 'engine'], {
         'site': SITE_PROP, 'engine': ENGINE_PROP,
         'version': {'type': 'string', 'description': 'Optional engine version tag such as "16". Default: the server\'s default.'}})},
    {'name': 'delete_database',
     'description': 'Remove a database of your site together with ALL its data (cannot be undone), remove its environment key and redeploy.' + JOB_NOTE,
     'inputSchema': _schema(['site', 'engine'], {'site': SITE_PROP, 'engine': ENGINE_PROP})},
    {'name': 'list_cron',
     'description': 'Scheduled commands of your site: id, schedule, argv, enabled.',
     'inputSchema': _schema(['site'], {'site': SITE_PROP})},
    {'name': 'add_cron',
     'description': 'Schedule a command inside your site\'s own container (docker exec; no host shell). Max 10 jobs per site, never more often than every 5 minutes (the minute field may not be "*" or "*/n" with n<5).',
     'inputSchema': _schema(['site', 'schedule', 'argv'], {
         'site': SITE_PROP,
         'schedule': {'type': 'string', 'description': 'Five cron fields "minute hour day-of-month month weekday", e.g. "*/10 * * * *" or "30 3 * * *". Only digits and * / , - are allowed.'},
         'argv': ARGV_PROP,
         'name': {'type': 'string', 'maxLength': 60, 'description': 'Optional label.'}})},
    {'name': 'delete_cron',
     'description': 'Remove one scheduled command of your site, by the job_id from list_cron / add_cron.',
     'inputSchema': _schema(['site', 'job_id'], {'site': SITE_PROP, 'job_id': {'type': ['integer', 'string'], 'description': 'Id of the cron job.'}})},
    {'name': 'run_command',
     'description': 'Run ONE command once inside your site\'s own container and return its exit code and output (e.g. a migration or a management script). Not a shell: no pipes, quotes, $ or redirects. Stops after the timeout (max 60 s); output is capped at 20000 chars and secrets-like strings are redacted. Cannot touch the host or other sites.',
     'inputSchema': _schema(['site', 'argv'], {
         'site': SITE_PROP, 'argv': ARGV_PROP,
         'timeout': {'type': 'integer', 'minimum': 1, 'maximum': 60, 'default': 30, 'description': 'Seconds before the command is stopped.'}})},
    {'name': 'delete_site',
     'description': 'PERMANENTLY remove your site: container, images, volumes (all data), databases, cron jobs, the public address, the git mirror. Cannot be undone. Only do it when the owner asked for it. "confirm" must be exactly "DELETE <site>".' + JOB_NOTE,
     'inputSchema': _schema(['site', 'confirm'], {
         'site': SITE_PROP,
         'confirm': {'type': 'string', 'description': 'The literal text "DELETE " followed by the site name, e.g. "DELETE berber".'}})},
]


def mcp_call(name, args, who):
    """Run one tool. Returns (is_error, payload)."""
    if name == 'get_instructions':
        return False, HELP
    if name == 'list_sites':
        return False, sites_view()
    if name == 'get_job':
        view = job_view(str(args.get('job_id', '')))
        return (True, {'error': 'unknown job id'}) if view is None else (False, view)
    action = TOOL_ACTIONS.get(name)
    if action is None:
        return True, {'error': f'unknown tool {name}'}
    code, payload = dispatch(action, args, who)
    return code >= 400, payload


def mcp_handle(msg, who):
    """One JSON-RPC message -> response dict, or None for notifications."""
    if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or 'method' not in msg:
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}
    method, mid, params = msg['method'], msg.get('id'), msg.get('params') or {}
    if mid is None:  # notification (e.g. notifications/initialized)
        return None

    def ok(result):
        return {'jsonrpc': '2.0', 'id': mid, 'result': result}

    if method == 'initialize':
        asked = params.get('protocolVersion')
        return ok({
            'protocolVersion': asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': {'name': 'serverkit-publish', 'version': '2.0.0'},
            'instructions': (f'Publishes web apps from GitHub (hasaca-bot) onto the owner\'s server at '
                             f'https://<name>.{DOMAIN} and lets you run the sites you created: environment, logs, '
                             f'redeploy, stop/start, databases, cron and one-shot commands inside the site\'s container, '
                             f'delete. Call get_instructions first, then list_sites; slow actions return a job id to '
                             f'poll with get_job.'),
        })
    if method == 'ping':
        return ok({})
    if method == 'tools/list':
        return ok({'tools': MCP_TOOLS})
    if method == 'tools/call':
        name = params.get('name')
        args = params.get('arguments') or {}
        if not isinstance(args, dict):
            return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32602, 'message': 'arguments must be an object'}}
        is_error, payload = mcp_call(name, args, who)
        text = payload if isinstance(payload, str) else json.dumps(payload, indent=1)
        return ok({'content': [{'type': 'text', 'text': text}], 'isError': is_error})
    if method in ('resources/list', 'prompts/list'):
        return ok({method.split('/')[0]: []})
    return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': f'Method not found: {method}'}}


SITE_PATH_RE = re.compile(r'^/v1/sites/([a-z][a-z0-9-]{1,30}[a-z0-9])(?:/([a-z_]+))?$')


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'serverkit-gateway'
    sys_version = ''

    def log_message(self, fmt, *args):
        line = fmt % args
        line = re.sub(r'/mcp/[A-Za-z0-9_-]{20,128}', '/mcp/<key>', line)
        print(f'{self.client_ip()} {line}', flush=True)

    def client_ip(self):
        return self.headers.get('CF-Connecting-IP') or self.client_address[0]

    def send(self, code, payload, ctype='application/json', headers=None):
        body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload, indent=1)
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def take_path_key(self):
        """claude.ai custom connectors cannot send an Authorization header, so the
        MCP endpoint also accepts the key as the last path segment:
        /mcp/<key>. The key is moved out of self.path so it never reaches the
        access log or any other code."""
        m = re.match(r'^/mcp/([A-Za-z0-9_-]{20,128})/?$', self.path)
        self.path_key = None
        if m:
            self.path_key = m.group(1)
            self.path = '/mcp'

    def authorized(self):
        ip = self.client_ip()
        now = time.time()
        recent = [t for t in failures.get(ip, []) if now - t < 600]
        if len(recent) >= 10:
            self.send(429, {'error': 'too many failed attempts, wait 10 minutes'})
            return False
        given = self.headers.get('Authorization', '')
        key = given[7:].strip() if given.startswith('Bearer ') else (getattr(self, 'path_key', None) or '')
        if key and hmac.compare_digest(key, load_token()):
            failures.pop(ip, None)
            with jobs_lock:
                used = [t for t in calls.get(ip, []) if now - t < 60]
                if len(used) >= RATE_LIMIT:
                    calls[ip] = used
                    wait = max(1, int(60 - (now - used[0])))
                    self.send(429, {'error': f'rate limit: at most {RATE_LIMIT} requests per minute, retry in {wait} s'},
                              headers={'Retry-After': str(wait)})
                    return False
                used.append(now)
                calls[ip] = used
                if len(calls) > 500:  # forget idle addresses
                    for k in [k for k, v in calls.items() if not v or now - v[-1] > 60]:
                        calls.pop(k, None)
            return True
        failures[ip] = recent + [now]
        self.send(401, {'error': 'missing or wrong token'}, headers={'WWW-Authenticate': 'Bearer'})
        return False

    def read_json(self, allow_empty=False):
        length = int(self.headers.get('Content-Length') or 0)
        if length <= 0 and allow_empty:
            return {}, True
        if length <= 0 or length > MAX_BODY:
            self.send(413, {'error': f'body must be 1..{MAX_BODY} bytes'})
            return None, False
        try:
            return json.loads(self.rfile.read(length)), True
        except ValueError:
            self.send(400, {'error': 'invalid JSON'})
            return None, False

    def do_GET(self):
        self.take_path_key()
        if self.path in ('/', '/help'):
            return self.send(200, HELP, 'text/plain')
        if self.path == '/mcp':
            # No server-initiated stream: every response comes back on its POST.
            return self.send(405, {'error': 'use POST for MCP messages'}, headers={'Allow': 'POST'})
        if not self.authorized():
            return
        if self.path == '/v1/sites':
            return self.send(200, sites_view())
        m = re.match(r'^/v1/jobs/([0-9a-f-]{36})$', self.path)
        if m:
            view = job_view(m.group(1))
            if view is not None:
                return self.send(200, view)
        m = SITE_PATH_RE.match(self.path)
        if m and m.group(2) is None:
            code, payload = dispatch('site_info', {'site': m.group(1)}, self.client_ip())
            return self.send(code, payload)
        self.send(404, {'error': 'not found'})

    def do_DELETE(self):
        self.take_path_key()
        if self.path == '/mcp':
            return self.send(405, {'error': 'sessions are not used'}, headers={'Allow': 'POST'})
        self.send(404, {'error': 'not found'})

    def do_POST(self):
        self.take_path_key()
        if not self.authorized():
            return
        if self.path == '/v1/publish':
            spec, ok = self.read_json()
            if not ok:
                return
            code, payload = dispatch('publish', spec, self.client_ip())
            return self.send(code, payload)
        if self.path == '/mcp':
            msg, ok = self.read_json()
            if not ok:
                return
            who = f'{self.client_ip()} (mcp)'
            if isinstance(msg, list):
                replies = [r for r in (mcp_handle(m, who) for m in msg) if r is not None]
                return self.send(200, replies) if replies else self.send(202, b'')
            reply = mcp_handle(msg, who)
            return self.send(202, b'') if reply is None else self.send(200, reply)
        m = SITE_PATH_RE.match(self.path)
        if m and m.group(2):
            action = m.group(2)
            if action not in ACTIONS or action == 'publish':
                return self.send(404, {'error': f'unknown action {action}'})
            args, ok = self.read_json(allow_empty=True)
            if not ok:
                return
            if not isinstance(args, dict):
                return self.send(400, {'error': 'body must be a JSON object'})
            if args.get('site', m.group(1)) != m.group(1):
                return self.send(400, {'error': 'site in the body differs from the site in the path'})
            args = dict(args, site=m.group(1))
            code, payload = dispatch(action, args, self.client_ip())
            return self.send(code, payload)
        self.send(404, {'error': 'not found'})


def main():
    if not os.path.isfile(TOKEN_FILE):
        os.makedirs(os.path.dirname(TOKEN_FILE), mode=0o700, exist_ok=True)
        with open(os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT, 0o600), 'w') as f:
            f.write(secrets.token_urlsafe(32))
    threading.Thread(target=worker, daemon=True).start()
    server = http.server.ThreadingHTTPServer(LISTEN, Handler)
    print(f'gateway listening on {LISTEN[0]}:{LISTEN[1]}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
