#!/usr/bin/env python3
"""Windows-side worker of the gateway for everything except the HTTP part.

    echo {"action": "site_info", "site": "berber"} | python remote_ops.py

One JSON object on stdin: {"action": ..., "site": ..., <arguments>}. Progress
lines go to stdout; the LAST line is always `RESULT <one-line JSON>` (also on
failure: `RESULT {"error": "..."}`). Exit code 0 = success, 1 = failure (an
`ERROR: ...` line comes first).

The panel calls use the owner's saved admin session (publish.py / skctl.py), so
the panel does not confine anything: every ownership check is made here. A site
is controllable only if windows\\sites\\<site>\\site.json has "origin":
"gateway"; its panel app is found by name, and container, volume, image, job
and database names are built from the app id or taken from the site's own
records, never from the caller. Everything is validated before the first panel
call. Secrets are never printed; output that could carry one is redacted.
"""
import datetime
import importlib.util
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
SITES = os.path.join(os.path.dirname(HERE), 'sites')
DOMAIN = 'tadadigital.shop'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
NETLINK = '/usr/local/sbin/serverkit-netlink'
TUNNEL_SCRIPT = '/usr/local/sbin/serverkit-domain-tunnel'

NAME_RE = re.compile(r'^[a-z][a-z0-9-]{1,30}[a-z0-9]$')
RESERVED = {'www', 'panel', 'deploy', 'api', 'admin', 'mail', 'smtp', 'ftp', 'root',
            'tada', 'app', 'status', 'cdn', 'static', 'ns1', 'ns2'}
KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]{0,63}$')
TOKEN_RE = re.compile(r'^[A-Za-z0-9_./:=@,+%-]{1,200}$')
CRON_TOKEN_RE = re.compile(r'^[A-Za-z0-9_./:=@,+-]{1,200}$')  # no "%": cron turns it into a newline
FIELD_RE = re.compile(r'^[0-9*/,-]{1,20}$')
JOB_ID_RE = re.compile(r'^job_[0-9]{14}_[0-9a-f]{6}$')
SINCE_RE = re.compile(r'^(?:[0-9]{1,5}[smhd]|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]{5,20}Z?)$')
NAME_TEXT_RE = re.compile(r'^[A-Za-z0-9 _.-]{1,60}$')
VERSION_RE = re.compile(r'^[0-9]{1,2}(\.[0-9]{1,2})?$')
GENERATORS = ('hex32', 'password', 'fernet')
SECRET_NAME_RE = re.compile(r'(?i)PASSWORD|PASSWD|SECRET|TOKEN|PRIVATE|CREDENTIAL|API_?KEY|_KEY$|^KEY$|_DSN$')

# engine -> (env key, URL template, container port, user, initial database)
ENGINES = {
    'postgresql': ('DATABASE_URL', 'postgresql://postgres:{pw}@{host}:5432/app', 'app'),
    'mysql': ('DATABASE_URL', 'mysql://root:{pw}@{host}:3306/app', 'app'),
    'mariadb': ('DATABASE_URL', 'mysql://root:{pw}@{host}:3306/app', 'app'),
    'redis': ('REDIS_URL', 'redis://:{pw}@{host}:6379/0', None),
}
MAX_ENGINES, MAX_JOBS, MAX_ENV = 2, 10, 50

sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)

# Anything that looks like a secret is masked before it leaves this script.
SECRET_RE = re.compile(r'(?i)((?:password|passwd|secret|token|key|bearer)["\'\s:=]+)([^\s"\',]{8,})|\b[A-Za-z0-9_\-]{32,}\b')
URL_CRED_RE = re.compile(r'(://[^/\s:@]*:)[^@\s/]+(@)')


class Refuse(Exception):
    """A request that is wrong or not allowed; the message is shown to the caller."""


def redact(text):
    text = URL_CRED_RE.sub(r'\1<redacted>\2', str(text))
    return SECRET_RE.sub(lambda m: (m.group(1) or '') + '<redacted>', text)


def say(msg):
    print(msg, flush=True)


# ── panel plumbing ────────────────────────────────────────────────────────────

_mods = {}


def modules():
    """sync.py (and through it publish.py) loaded on first use, so requests that
    are refused before touching the panel never need the saved session."""
    if not _mods:
        spec = importlib.util.spec_from_file_location('sync', os.path.join(SITES, 'sync.py'))
        sync = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sync)
        _mods.update(sync=sync, publish=sync.publish)
    return _mods['sync'], _mods['publish']


def api(method, path, body=None):
    return modules()[1].api(method, path, body)


def brief(data):
    if isinstance(data, dict):
        data = data.get('error') or data.get('message') or data
    return redact(str(data))[:300]


def must(method, path, body=None):
    status, data = api(method, path, body)
    if status >= 400:
        raise Refuse(f'panel {method} {path.split("?")[0]} -> HTTP {status}: {brief(data)}')
    return data


def wsl(*args, timeout=120):
    sync = modules()[0]
    return subprocess.run(['wsl.exe', '-d', sync.DISTRO, '-u', 'root', '--exec', *args],
                          capture_output=True, text=True, encoding='utf-8', errors='replace',
                          timeout=timeout, creationflags=NO_WINDOW)


def wait_job(job_id, what, timeout=1800):
    """Poll one deployment job until it ends; jobs run one at a time panel-wide."""
    deadline, last = time.time() + timeout, None
    time.sleep(2)
    while time.time() < deadline:
        job = must('GET', f'/deployment-jobs/{job_id}?logs=false').get('job') or {}
        status = job.get('status')
        if status != last:
            say(f'   {what}: {status}')
            last = status
        if status == 'succeeded':
            return job
        if status in ('failed', 'cancelled'):
            raise Refuse(f'{what} {status}: {redact(job.get("error_message") or "see the panel deployment log")}')
        time.sleep(5)
    raise Refuse(f'{what} did not finish in {timeout // 60} minutes')


def redeploy(app_id):
    data = must('POST', f'/builds/apps/{app_id}/deploy', {})
    if not data.get('deploy_job_id'):
        raise Refuse('the panel did not queue a deployment')
    wait_job(data['deploy_job_id'], 'deployment')
    app = get_app(app_id)
    if app.get('status') != 'running':
        raise Refuse(f'deployed, but the site status is "{app.get("status")}"')
    return app


def get_app(app_id):
    data = must('GET', f'/apps/{app_id}')
    return data.get('app') or data


def find_app(name):
    for app in must('GET', '/apps').get('apps', []):
        if app['name'] == name:
            return app
    return None


# ── site records ──────────────────────────────────────────────────────────────

def recipe_path(site):
    return os.path.join(SITES, site, 'site.json')


def load_site(site):
    """The site.json of a gateway-owned site, or Refuse. Local file only."""
    if not isinstance(site, str) or not NAME_RE.match(site) or site in RESERVED:
        raise Refuse('site: 3-32 chars, a-z 0-9 -, not reserved')
    try:
        with open(recipe_path(site), encoding='utf-8') as f:
            recipe = json.load(f)
    except (OSError, ValueError):
        raise Refuse(f'unknown site "{site}"')
    if recipe.get('origin') != 'gateway' or recipe.get('name') != site:
        raise Refuse(f'site "{site}" was not created through the gateway')
    return recipe


def save_site(site, recipe):
    path = recipe_path(site)
    with open(path + '.tmp', 'w', encoding='utf-8', newline='\n') as f:
        json.dump(recipe, f, indent=2)
        f.write('\n')
    os.replace(path + '.tmp', path)


def site_app(site, must_exist=True):
    app = find_app(site)
    if app is None and must_exist:
        raise Refuse(f'site "{site}" has no service in the panel')
    return app


def container_name(app_id):
    return f'serverkit-app-{app_id}'


def engine_keys(recipe):
    return {e['env_key'] for e in recipe.get('engines', [])}


def check_env_key(recipe, key):
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise Refuse(f'env key {key!r}: UPPER_SNAKE_CASE, up to 64 chars')
    if key.startswith('SERVERKIT_'):
        raise Refuse(f'env key {key}: names starting with SERVERKIT_ are reserved')
    if key in engine_keys(recipe):
        raise Refuse(f'env key {key} belongs to the site\'s database; remove it with db_delete')


def check_args(spec, allowed):
    extra = set(spec) - allowed - {'action', 'site'}
    if extra:
        raise Refuse(f'unknown fields: {sorted(extra)}')


def check_argv(argv, token_re=TOKEN_RE):
    if not isinstance(argv, list) or not 1 <= len(argv) <= 20:
        raise Refuse('argv: a list of 1-20 words')
    for word in argv:
        if not isinstance(word, str) or not token_re.match(word):
            raise Refuse(f'argv word {str(word)[:40]!r}: only letters, digits and _ . / : = @ , + - '
                         f'{"% " if token_re is TOKEN_RE else ""}(no spaces, quotes, $, &, ;, |, `, \\)')


# ── publish ───────────────────────────────────────────────────────────────────

def a_publish(spec):
    spec = {k: v for k, v in spec.items() if k not in ('action', 'site')}
    name = spec.get('name')
    if not isinstance(name, str) or not NAME_RE.match(name) or name in RESERVED:
        raise Refuse('name: 3-32 chars, a-z 0-9 -, not reserved')
    for key in (spec.get('env') or {}) if isinstance(spec.get('env'), dict) else ():
        if isinstance(key, str) and key.startswith('SERVERKIT_'):
            raise Refuse(f'env key {key}: names starting with SERVERKIT_ are reserved')
    if os.path.exists(recipe_path(name)):
        recipe = load_site(name)
        for key in spec.get('env') or {}:
            if key in engine_keys(recipe):
                raise Refuse(f'env key {key} belongs to the site\'s database; remove it with db_delete')
    elif find_app(name) is not None:
        # The panel's other services (including database engines) are not ours to replace.
        raise Refuse(f'name "{name}" is already used by a service that was not created through this gateway')
    remote_publish = load_module('remote_publish', os.path.join(HERE, 'remote_publish.py'))
    try:
        url = remote_publish.main(spec)
    except remote_publish.PublishError as e:
        raise Refuse(str(e))
    return {'url': url}


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── site_info ─────────────────────────────────────────────────────────────────

def parse_argv(command, app_id):
    prefix = f'/usr/bin/docker exec {container_name(app_id)} '
    return command[len(prefix):].split() if isinstance(command, str) and command.startswith(prefix) else None


def app_jobs(app_id):
    """The site's own cron jobs: ids from the per-app list, details from the full one."""
    ids = {j['id'] for j in must('GET', f'/cron/jobs/for-app/{app_id}').get('jobs', [])}
    jobs = [j for j in must('GET', '/cron/jobs').get('jobs', []) if j.get('id') in ids]
    return [{'id': j['id'], 'name': j.get('name'), 'schedule': j.get('schedule'),
             'argv': parse_argv(j.get('command'), app_id), 'enabled': j.get('enabled', True)} for j in jobs]


def a_site_info(spec, site, recipe):
    check_args(spec, set())
    app = site_app(site)
    app_id = app['id']
    detail = get_app(app_id)
    status, data = api('GET', f'/deploy/apps/{app_id}/config')
    config = (data.get('config') if status < 400 and isinstance(data, dict) else None) or {}
    rows = must('GET', f'/apps/{app_id}/env?mask=true').get('env_vars', [])
    declared = recipe.get('env', {})
    env = [{'key': r['key'], 'secret': bool(r.get('is_secret') or isinstance(declared.get(r['key']), dict)
                                            or SECRET_NAME_RE.search(r['key']))} for r in rows]
    engines = []
    for e in recipe.get('engines', []):
        status, data = api('GET', f'/apps/{e["app_id"]}')
        row = (data.get('app') or data) if status < 400 and isinstance(data, dict) else {}
        engines.append({'engine': e['engine'], 'instance': e['instance'], 'env_key': e['env_key'],
                        'status': row.get('status', 'missing')})
    return {'name': site, 'url': f'https://{recipe.get("public_host") or site + "." + DOMAIN}',
            'status': detail.get('status'), 'app_id': app_id, 'port': recipe.get('port'),
            'auto_deploy': bool(config.get('auto_deploy')),
            'domains': [d['name'] for d in detail.get('domains', [])],
            'env': env, 'engines': engines, 'cron': app_jobs(app_id)}


# ── env_set ───────────────────────────────────────────────────────────────────

def plan_env(spec, recipe):
    """Validate env_set and return (writes, unsets); writes: key -> (value|None for generate,
    kind|None, secret)."""
    check_args(spec, {'set', 'unset', 'redeploy'})
    sets, unsets = spec.get('set') or {}, spec.get('unset') or []
    if not isinstance(sets, dict) or not isinstance(unsets, list):
        raise Refuse('set: object, unset: list of keys')
    if not isinstance(spec.get('redeploy', True), bool):
        raise Refuse('redeploy: true/false')
    if not sets and not unsets:
        raise Refuse('nothing to do: give "set" and/or "unset"')
    if len(sets) + len(unsets) > MAX_ENV:
        raise Refuse(f'at most {MAX_ENV} keys per call')
    writes = {}
    for key, value in sets.items():
        check_env_key(recipe, key)
        if isinstance(value, str):
            literal, kind, secret = value, None, bool(SECRET_NAME_RE.search(key))
        elif isinstance(value, dict) and set(value) == {'generate'} and value['generate'] in GENERATORS:
            literal, kind, secret = None, value['generate'], True
        elif (isinstance(value, dict) and 'value' in value and set(value) <= {'value', 'secret'}
              and isinstance(value['value'], str) and isinstance(value.get('secret', False), bool)):
            literal, kind, secret = value['value'], None, value.get('secret', False) or bool(SECRET_NAME_RE.search(key))
        else:
            raise Refuse(f'env {key}: a string, {{"generate": "hex32"|"password"|"fernet"}} or {{"value": "...", "secret": true}}')
        if literal is not None and len(literal) > 4000:
            raise Refuse(f'env {key}: string up to 4000 chars')
        writes[key] = (literal, kind, secret)
    for key in unsets:
        check_env_key(recipe, key)
        if key in sets:
            raise Refuse(f'env {key}: in both "set" and "unset"')
    return writes, list(dict.fromkeys(unsets))


def a_env_set(spec, site, recipe):
    writes, unsets = plan_env(spec, recipe)
    app_id = site_app(site)['id']
    existing = {r['key'] for r in must('GET', f'/apps/{app_id}/env?mask=true').get('env_vars', [])}
    generate = modules()[1].generate
    for key, (literal, kind, secret) in writes.items():
        if kind and key in existing:
            raise Refuse(f'env {key} is already set; unset it first if you want a new generated value')
    env = recipe.setdefault('env', {})
    for key, (literal, kind, secret) in writes.items():
        value = generate(kind) if kind else literal
        must('POST', f'/apps/{app_id}/env', {'key': key, 'value': value, 'is_secret': secret})
        env[key] = {'generate': kind} if kind else ({'panel_only': True} if secret else literal)
        say(f'   {key} set{" (secret)" if secret else ""}')
    for key in unsets:
        status, data = api('DELETE', f'/apps/{app_id}/env/{key}')
        if status >= 400 and status != 404:
            raise Refuse(f'panel DELETE env {key} -> HTTP {status}: {brief(data)}')
        env.pop(key, None)
        say(f'   {key} removed')
    save_site(site, recipe)
    do_redeploy = spec.get('redeploy', True)
    if do_redeploy:
        redeploy(app_id)
    return {'set': list(writes), 'unset': unsets, 'redeployed': do_redeploy}


# ── logs / lifecycle ──────────────────────────────────────────────────────────

def a_logs(spec, site, recipe):
    check_args(spec, {'tail', 'since'})
    tail = spec.get('tail', 200)
    if isinstance(tail, bool) or not isinstance(tail, int) or not 1 <= tail <= 1000:
        raise Refuse('tail: integer 1-1000')
    since = spec.get('since')
    if since is not None and (not isinstance(since, str) or not SINCE_RE.match(since)):
        raise Refuse('since: a duration like "10m", "2h", "1d" or an ISO timestamp')
    app_id = site_app(site)['id']
    query = {'tail': tail, 'timestamps': 'true'}
    if since:
        query['since'] = since
    data = must('GET', f'/apps/{app_id}/container-logs?{urllib.parse.urlencode(query)}')
    return {'lines': [redact(line)[:2000] for line in (data.get('logs') or '').splitlines()[-tail:]]}


def a_redeploy(spec, site, recipe):
    check_args(spec, set())
    return {'status': redeploy(site_app(site)['id']).get('status')}


def a_stop(spec, site, recipe):
    check_args(spec, set())
    app_id = site_app(site)['id']
    must('POST', f'/apps/{app_id}/sleep', {})
    return {'status': get_app(app_id).get('status')}


def a_start(spec, site, recipe):
    check_args(spec, set())
    app_id = site_app(site)['id']
    must('POST', f'/apps/{app_id}/wake', {})
    return {'status': get_app(app_id).get('status')}


# ── databases ─────────────────────────────────────────────────────────────────

def netlink(verb, app_id, strict, container=None):
    # The site container and its database containers both join serverkit-services,
    # so the site reaches a database by its instance name.
    r = wsl(NETLINK, verb, container or container_name(app_id), timeout=60)
    if r.returncode != 0:
        msg = f'serverkit-netlink {verb} failed: {redact((r.stderr or r.stdout).strip()[:200])}'
        if strict:
            raise Refuse(msg)
        say(f'   warning: {msg}')


def a_db_create(spec, site, recipe):
    check_args(spec, {'engine', 'version'})
    engine, version = spec.get('engine'), spec.get('version')
    if engine not in ENGINES:
        raise Refuse(f'engine: one of {", ".join(ENGINES)}')
    if version is not None and (not isinstance(version, str) or not VERSION_RE.match(version)):
        raise Refuse('version: a version number such as "16" (the panel checks it against its list)')
    env_key, url_template, initial_db = ENGINES[engine]
    have = recipe.get('engines', [])
    if len(have) >= MAX_ENGINES:
        raise Refuse(f'at most {MAX_ENGINES} databases per site')
    if any(e['engine'] == engine for e in have):
        raise Refuse(f'the site already has a {engine} database')
    if env_key in engine_keys(recipe):
        raise Refuse(f'the site already has a database using {env_key}; only one SQL database per site')
    app = site_app(site)
    instance = f'{site}-{engine}'
    if find_app(instance) is not None:
        raise Refuse(f'a service named {instance} already exists in the panel')
    if env_key in {r['key'] for r in must('GET', f'/apps/{app["id"]}/env?mask=true').get('env_vars', [])}:
        raise Refuse(f'{env_key} is already set on the site; unset it with env_set first')

    password = secrets.token_hex(20)  # letters and digits only: safe in a URL and on a command line
    body = {'template_id': engine, 'instance_name': instance, 'expose_public': False, 'wait': False,
            'variables': {'DB_PASSWORD': password}}
    if version:
        body['version'] = version
    if initial_db:
        body['initial_database'] = initial_db
    say(f'installing {engine} as {instance}')
    started = must('POST', '/databases/engines', body)
    try:
        wait_job(started['job_id'], f'{engine} install', timeout=1200)
    except Refuse:
        leftover = find_app(instance)
        if leftover is not None:
            api('DELETE', f'/apps/{leftover["id"]}?purge=true&remove_data=true')
        raise
    engine_app = find_app(instance)
    if engine_app is None:
        raise Refuse(f'{engine} installed but the service {instance} was not found')

    recipe.setdefault('engines', []).append(
        {'engine': engine, 'instance': instance, 'app_id': engine_app['id'], 'env_key': env_key})
    recipe.setdefault('env', {})[env_key] = {'panel_only': True}
    save_site(site, recipe)
    must('POST', f'/apps/{app["id"]}/env',
         {'key': env_key, 'value': url_template.format(pw=password, host=instance), 'is_secret': True})
    say(f'   {env_key} set (secret)')
    netlink('add', app['id'], strict=True, container=instance)
    netlink('add', app['id'], strict=True)
    redeploy(app['id'])
    return {'engine': engine, 'instance': instance, 'env_key': env_key}


def remove_engine(site, recipe, entry):
    """Delete one recorded database service and forget it; no env change, no redeploy."""
    row = find_app(entry['instance'])
    if row is not None:
        if row['id'] != entry['app_id']:
            raise Refuse(f'service {entry["instance"]} does not match the recorded database; not deleting it')
        netlink('remove', None, strict=False, container=entry['instance'])
        must('DELETE', f'/apps/{row["id"]}?purge=true&remove_data=true')
        say(f'   database service {entry["instance"]} deleted')
    recipe['engines'] = [e for e in recipe['engines'] if e is not entry]
    save_site(site, recipe)


def a_db_delete(spec, site, recipe):
    check_args(spec, {'engine'})
    engine = spec.get('engine')
    entry = next((e for e in recipe.get('engines', []) if e['engine'] == engine), None)
    if entry is None:
        raise Refuse(f'the site has no {engine} database')
    app = site_app(site)
    status, data = api('DELETE', f'/apps/{app["id"]}/env/{entry["env_key"]}')
    if status >= 400 and status != 404:
        raise Refuse(f'panel DELETE env {entry["env_key"]} -> HTTP {status}: {brief(data)}')
    recipe.get('env', {}).pop(entry['env_key'], None)
    remove_engine(site, recipe, entry)
    if not recipe['engines']:
        netlink('remove', app['id'], strict=False)
    redeploy(app['id'])
    return {'engine': engine, 'removed': True}


# ── cron ──────────────────────────────────────────────────────────────────────

def expand(field, lo, hi):
    out = set()
    for part in field.split(','):
        m = re.fullmatch(r'(\*|\d+(?:-\d+)?)(?:/(\d+))?', part)
        if not m:
            raise Refuse(f'schedule field {field!r} is not valid')
        base, step = m.group(1), int(m.group(2) or 1)
        if base == '*':
            first, last = lo, hi
        elif '-' in base:
            first, last = map(int, base.split('-'))
        elif m.group(2):
            raise Refuse(f'schedule field {field!r}: a step needs * or a range')
        else:
            first = last = int(base)
        if step < 1 or not lo <= first <= last <= hi:
            raise Refuse(f'schedule field {field!r} is out of range')
        out.update(range(first, last + 1, step))
    return out


def check_schedule(schedule):
    fields = schedule.split() if isinstance(schedule, str) else []
    if len(fields) != 5 or not all(FIELD_RE.match(f) for f in fields):
        raise Refuse('schedule: five fields "m h dom mon dow" using digits and * / , -')
    minutes = sorted(expand(fields[0], 0, 59))
    expand(fields[1], 0, 23)
    expand(fields[2], 1, 31)
    expand(fields[3], 1, 12)
    expand(fields[4], 0, 7)
    gaps = [b - a for a, b in zip(minutes, minutes[1:])] + [minutes[0] + 60 - minutes[-1]]
    if min(gaps) < 5:
        raise Refuse('schedule: jobs may run at most once every 5 minutes')
    return ' '.join(fields)


def a_cron_list(spec, site, recipe):
    check_args(spec, set())
    return {'jobs': app_jobs(site_app(site)['id'])}


def a_cron_add(spec, site, recipe):
    check_args(spec, {'schedule', 'argv', 'name'})
    schedule = check_schedule(spec.get('schedule'))
    check_argv(spec.get('argv'), CRON_TOKEN_RE)
    name = spec.get('name')
    if name is not None and (not isinstance(name, str) or not NAME_TEXT_RE.match(name)):
        raise Refuse('name: up to 60 letters, digits, spaces and _ . -')
    app_id = site_app(site)['id']
    if len(app_jobs(app_id)) >= MAX_JOBS:
        raise Refuse(f'at most {MAX_JOBS} cron jobs per site')
    command = f'/usr/bin/docker exec {container_name(app_id)} {" ".join(spec["argv"])}'
    data = must('POST', '/cron/jobs', {
        'schedule': schedule, 'command': command, 'name': f'{site}: {name or "job"}',
        'description': 'Created through the publish gateway', 'application_id': app_id})
    return {'job_id': data['job_id']}


def a_cron_delete(spec, site, recipe):
    check_args(spec, {'job_id'})
    job_id = spec.get('job_id')
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise Refuse('job_id: an id returned by cron_list or cron_add')
    app_id = site_app(site)['id']
    if job_id not in {j['id'] for j in must('GET', f'/cron/jobs/for-app/{app_id}').get('jobs', [])}:
        raise Refuse(f'job {job_id} does not belong to this site')
    must('DELETE', f'/cron/jobs/{job_id}')
    return {'deleted': True}


# ── exec ──────────────────────────────────────────────────────────────────────

def a_exec(spec, site, recipe):
    check_args(spec, {'argv', 'timeout'})
    check_argv(spec.get('argv'))
    timeout = spec.get('timeout', 30)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 60:
        raise Refuse('timeout: integer 1-60 seconds')
    app_id = site_app(site)['id']
    # The panel itself stops a command after 60 s; its answer is HTTP 200 even for a failing command.
    data = must('POST', f'/docker/containers/{container_name(app_id)}/exec', {'command': ' '.join(spec['argv'])})
    if data.get('return_code') is None:
        raise Refuse(f'command did not run: {brief(data.get("error") or data)}')
    output = (data.get('stdout') or '') + (('\n' + data['stderr']) if data.get('stderr') else '')
    return {'exit_code': data['return_code'], 'output': redact(output)[:20000]}


# ── delete_site ───────────────────────────────────────────────────────────────

def force_rmtree(path):
    def writable(func, p, _):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=writable)


def inside(path, base):
    path, base = os.path.realpath(path), os.path.realpath(base)
    return path.startswith(base + os.sep)


def remove_containers(app_id, warnings):
    pattern = re.compile(rf'^serverkit-app-{app_id}(-[A-Za-z0-9_.-]+)?$')
    r = wsl('docker', 'ps', '-a', '--format', '{{.Names}}')
    for name in (n for n in r.stdout.split() if pattern.match(n)):
        status, _ = api('DELETE', f'/docker/containers/{name}', {'force': True})
        if status >= 400:
            # The panel refuses to touch containers with "serverkit" in the name; this exact, checked name is ours.
            r = wsl('docker', 'rm', '-f', name)
            if r.returncode != 0:
                warnings.append(f'container {name} not removed')
                continue
        say(f'   container {name} removed')


def remove_volumes(app_id, warnings):
    for vol in must('GET', f'/apps/{app_id}/volumes').get('volumes', []):
        if not str(vol.get('docker_volume_name', '')).startswith(f'serverkit-app-{app_id}-'):
            warnings.append(f'volume {vol.get("name")} skipped (unexpected name)')
            continue
        status, data = api('DELETE', f'/apps/{app_id}/volumes/{vol["id"]}?wipe=true')
        if status >= 400:
            warnings.append(f'volume {vol["name"]} not removed: {brief(data)}')
        else:
            say(f'   volume {vol["name"]} removed')


def remove_images(app_id, warnings):
    status, data = api('GET', '/docker/images')
    for img in (data.get('images') or []) if status < 400 and isinstance(data, dict) else []:
        if img.get('repository') == container_name(app_id) and img.get('tag') not in (None, '<none>'):
            ref = f'{img["repository"]}:{img["tag"]}'
            status, data = api('DELETE', f'/docker/images/{urllib.parse.quote(ref, safe=":")}', {'force': True})
            if status >= 400:
                warnings.append(f'image {ref} not removed')


def a_delete_site(spec, site, recipe):
    check_args(spec, {'confirm'})
    if spec.get('confirm') != f'DELETE {site}':
        raise Refuse(f'confirm must be exactly "DELETE {site}"')
    sync, publish = modules()
    warnings = []
    app = site_app(site, must_exist=False)
    for entry in list(recipe.get('engines', [])):
        remove_engine(site, recipe, entry)
    if app is not None:
        app_id = app['id']
        say(f'deleting {site} (service {app_id})')
        for job in app_jobs(app_id):
            must('DELETE', f'/cron/jobs/{job["id"]}')
            say(f'   cron job {job["id"]} removed')
        api('POST', f'/apps/{app_id}/sleep', {})
        remove_containers(app_id, warnings)
        remove_volumes(app_id, warnings)
        remove_images(app_id, warnings)
        must('DELETE', f'/apps/{app_id}?purge=true')
        say('   service deleted from the panel')
        netlink('remove', app_id, strict=False)
    public = recipe.get('public_host') or f'{site}.{DOMAIN}'
    r = wsl(TUNNEL_SCRIPT, 'remove', publish.TUNNEL, public)
    if r.returncode != 0:
        warnings.append(f'tunnel hostname {public} not removed')
    r = wsl('rm', '-rf', f'/srv/git/{site}.git')
    if r.returncode != 0:
        warnings.append('git mirror not removed')
    cache = os.path.join(sync.CACHE_DIR, site)
    if os.path.isdir(cache) and inside(cache, sync.CACHE_DIR):
        force_rmtree(cache)
    site_dir = os.path.join(SITES, site)
    if inside(site_dir, SITES):
        force_rmtree(site_dir)
    for w in warnings:
        say(f'   warning: {w}')
    say(f'{site} deleted')
    return {'deleted': True, **({'warnings': warnings} if warnings else {})}


# ── entry point ───────────────────────────────────────────────────────────────

ACTIONS = {
    'site_info': a_site_info, 'env_set': a_env_set, 'logs': a_logs, 'redeploy': a_redeploy,
    'stop': a_stop, 'start': a_start, 'db_create': a_db_create, 'db_delete': a_db_delete,
    'cron_list': a_cron_list, 'cron_add': a_cron_add, 'cron_delete': a_cron_delete,
    'exec': a_exec, 'delete_site': a_delete_site,
}


def run(spec):
    if not isinstance(spec, dict):
        raise Refuse('body must be a JSON object')
    action = spec.get('action')
    if action == 'publish':
        if spec.get('site') and spec.get('name') and spec['site'] != spec['name']:
            raise Refuse('site and name differ')
        return a_publish({**spec, 'name': spec.get('name') or spec.get('site')})
    if action not in ACTIONS:
        raise Refuse(f'unknown action {str(action)[:40]!r}')
    site = spec.get('site')
    return ACTIONS[action](spec, site, load_site(site))


def main():
    try:
        result = run(json.loads(sys.stdin.read()))
    except Refuse as e:
        return finish(1, str(e))  # our own wording; panel text inside it was redacted by brief()
    except ValueError:
        return finish(1, 'body must be JSON')
    except SystemExit as e:  # publish.py / skctl.py report failures with sys.exit(message)
        return finish(1, redact(e.code) if e.code not in (None, 0, 1) else 'a helper script stopped')
    except Exception as e:
        return finish(1, redact(f'{type(e).__name__}: {e}'))
    print('RESULT ' + json.dumps(result, separators=(',', ':'), ensure_ascii=False), flush=True)
    return 0


def finish(code, message):
    print(f'ERROR: {message}')
    print('RESULT ' + json.dumps({'error': message}, ensure_ascii=False), flush=True)
    return code


if __name__ == '__main__':
    sys.exit(main())
