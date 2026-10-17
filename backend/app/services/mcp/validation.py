"""Input rules shared by the MCP tools (ported from the publish gateway).

Everything a remote AI sends is untrusted. These checks run before any
service call; a failure raises ToolError, whose message goes back to the
caller as the tool result (isError=true). The rules are the gateway's:
lowercase DNS-label site names, UPPER_SNAKE_CASE env keys, plain-token argv
(no shell), five-field cron schedules no more often than every 5 minutes.
"""
import re

from app.services.mcp.errors import ToolError

NAME_RE = re.compile(r'^[a-z][a-z0-9-]{1,30}[a-z0-9]$')
RESERVED_NAMES = frozenset({
    'www', 'api', 'admin', 'panel', 'app', 'apps', 'mail', 'smtp', 'imap', 'pop',
    'ftp', 'ns1', 'ns2', 'mx', 'dns', 'mcp', 'gateway', 'git', 'static', 'cdn',
    'assets', 'status', 'dashboard', 'console', 'docs', 'help', 'support',
    'login', 'auth', 'sso', 'localhost', 'serverkit', 'root', 'webmail',
    'autodiscover', 'autoconfig', 'billing', 'internal',
})
ENV_KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]{0,63}$')
RESERVED_ENV_PREFIX = 'SERVERKIT_'
# Database URL keys belong to create_database / delete_database.
DATABASE_ENV_KEYS = frozenset({'DATABASE_URL', 'REDIS_URL'})
ARGV_TOKEN_RE = re.compile(r'^[A-Za-z0-9_./:=@,+%-]{1,200}$')
# No "%" in cron argv: cron turns it into a newline.
CRON_TOKEN_RE = re.compile(r'^[A-Za-z0-9_./:=@,+-]{1,200}$')
CRON_FIELD_RE = re.compile(r'^[0-9*/,-]{1,20}$')
CRON_NAME_RE = re.compile(r'^[A-Za-z0-9 _.-]{1,60}$')
CRON_JOB_ID_RE = re.compile(r'^job_[0-9]{14}_[0-9a-f]{6}$')
SINCE_RE = re.compile(r'^(?:[0-9]{1,5}[smhd]|[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]{5,20}Z?)$')
BRANCH_RE = re.compile(r'^[A-Za-z0-9._/-]{1,100}$')
REPO_RE = re.compile(r'^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$')
HEALTH_RE = re.compile(r'^/[A-Za-z0-9._~/?=&%-]{0,200}$')
VOLUME_RE = re.compile(r'^/[A-Za-z0-9._/-]{1,100}$')
ROOT_DIR_RE = re.compile(r'^[A-Za-z0-9._/-]{1,200}$')
VERSION_RE = re.compile(r'^[0-9]{1,2}(\.[0-9]{1,2})?$')
GENERATORS = ('hex32', 'password', 'fernet')
ENGINES = ('postgresql', 'mysql', 'mariadb', 'redis')

MAX_ENV_PER_CALL = 50
MAX_ENV_VALUE = 4000
MAX_DATABASES_PER_SITE = 2
MAX_CRON_PER_SITE = 10
MIN_CRON_INTERVAL = 5
MAX_ARGV = 20
MAX_OUTPUT = 20000
MAX_LOG_LINE = 2000

SECRET_NAME_RE = re.compile(
    r'(?i)PASSWORD|PASSWD|SECRET|TOKEN|PRIVATE|CREDENTIAL|API_?KEY|_KEY$|^KEY$|_DSN$')

# Anything that looks like a secret is masked before it leaves the panel.
_SECRET_RE = re.compile(
    r'(?i)((?:password|passwd|secret|token|key|bearer)["\'\s:=]+)([^\s"\',]{8,})'
    r'|\b[A-Za-z0-9_\-]{32,}\b')
_URL_CRED_RE = re.compile(r'(://[^/\s:@]*:)[^@\s/]+(@)')


def redact(text):
    """Mask credentials in URLs and long token-like strings."""
    text = _URL_CRED_RE.sub(r'\1<redacted>\2', str(text))
    return _SECRET_RE.sub(lambda m: (m.group(1) or '') + '<redacted>', text)


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def no_extra(args, allowed):
    extra = set(args) - set(allowed)
    if extra:
        raise ToolError(f'unknown fields: {sorted(extra)}')


def site_name(value, field='site'):
    if not isinstance(value, str) or not NAME_RE.match(value) or '--' in value:
        raise ToolError(f'{field}: 3-32 chars of a-z 0-9 -, starting with a letter')
    if value in RESERVED_NAMES:
        raise ToolError(f'{field}: "{value}" is reserved')
    return value


def env_key(key, *, allow_database_keys=False):
    if not isinstance(key, str) or not ENV_KEY_RE.match(key):
        raise ToolError(f'env key {str(key)[:70]!r}: UPPER_SNAKE_CASE, max 64 chars')
    if key.startswith(RESERVED_ENV_PREFIX):
        raise ToolError(f'env key {key}: keys starting with {RESERVED_ENV_PREFIX} are reserved')
    if not allow_database_keys and key in DATABASE_ENV_KEYS:
        raise ToolError(f'env key {key} belongs to the database of this site; '
                        'use create_database / delete_database')
    return key


def env_value(key, value):
    """Return (literal | None, generator | None, secret) for one env value."""
    if isinstance(value, str):
        if len(value) > MAX_ENV_VALUE:
            raise ToolError(f'env {key}: string up to {MAX_ENV_VALUE} chars')
        return value, None, bool(SECRET_NAME_RE.search(key))
    if isinstance(value, dict) and set(value) == {'generate'} and value['generate'] in GENERATORS:
        return None, value['generate'], True
    if (isinstance(value, dict) and 'value' in value and set(value) <= {'value', 'secret'}
            and isinstance(value['value'], str) and isinstance(value.get('secret', False), bool)):
        if len(value['value']) > MAX_ENV_VALUE:
            raise ToolError(f'env {key}: string up to {MAX_ENV_VALUE} chars')
        return value['value'], None, bool(value.get('secret', False)) or bool(SECRET_NAME_RE.search(key))
    raise ToolError(f'env {key}: a string, {{"generate": "hex32"|"password"|"fernet"}} '
                    'or {"value": "...", "secret": true}')


def env_changes(set_values, unset):
    """Validate a set/unset pair. Returns (writes, unsets)."""
    if set_values is None:
        set_values = {}
    if unset is None:
        unset = []
    if not isinstance(set_values, dict) or not isinstance(unset, list):
        raise ToolError('set: object, unset: list of keys')
    if not set_values and not unset:
        raise ToolError('nothing to do: give "set" and/or "unset"')
    if len(set_values) + len(unset) > MAX_ENV_PER_CALL:
        raise ToolError(f'at most {MAX_ENV_PER_CALL} keys per call')
    writes = {}
    for key, value in set_values.items():
        env_key(key)
        writes[key] = env_value(key, value)
    for key in unset:
        env_key(key)
        if key in set_values:
            raise ToolError(f'env {key}: a key cannot be both set and unset')
    return writes, list(dict.fromkeys(unset))


def generate_secret(kind):
    import secrets
    if kind == 'hex32':
        return secrets.token_hex(32)
    if kind == 'password':
        return secrets.token_urlsafe(24)
    if kind == 'fernet':
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode()
    raise ToolError(f'unknown generator {kind!r}')


def argv(value, token_re=ARGV_TOKEN_RE):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ARGV:
        raise ToolError(f'argv: a list of 1-{MAX_ARGV} words')
    for word in value:
        if not isinstance(word, str) or not token_re.match(word):
            allowed = '_ . / : = @ , + -' + (' %' if token_re is ARGV_TOKEN_RE else '')
            raise ToolError(f'argv word {str(word)[:40]!r}: only letters, digits and {allowed} '
                            '(no spaces, quotes, $, &, ;, |, backticks, backslashes)')
    return list(value)


def _expand(field, lo, hi):
    out = set()
    for part in field.split(','):
        m = re.fullmatch(r'(\*|\d+(?:-\d+)?)(?:/(\d+))?', part)
        if not m:
            raise ToolError(f'schedule field {field!r} is not valid')
        base, step = m.group(1), int(m.group(2) or 1)
        if base == '*':
            first, last = lo, hi
        elif '-' in base:
            first, last = map(int, base.split('-'))
        elif m.group(2):
            raise ToolError(f'schedule field {field!r}: a step needs * or a range')
        else:
            first = last = int(base)
        if step < 1 or not lo <= first <= last <= hi:
            raise ToolError(f'schedule field {field!r} is out of range')
        out.update(range(first, last + 1, step))
    return out


def cron_schedule(schedule):
    fields = schedule.split() if isinstance(schedule, str) else []
    if len(fields) != 5 or not all(CRON_FIELD_RE.match(f) for f in fields):
        raise ToolError('schedule: five fields "minute hour day month weekday" using digits and * / , -')
    minutes = sorted(_expand(fields[0], 0, 59))
    _expand(fields[1], 0, 23)
    _expand(fields[2], 1, 31)
    _expand(fields[3], 1, 12)
    _expand(fields[4], 0, 7)
    gaps = [b - a for a, b in zip(minutes, minutes[1:])] + [minutes[0] + 60 - minutes[-1]]
    if min(gaps) < MIN_CRON_INTERVAL:
        raise ToolError(f'schedule: jobs may not run more often than every {MIN_CRON_INTERVAL} minutes')
    return ' '.join(fields)


def int_range(args, field, default, lo, hi):
    value = args.get(field, default)
    if not is_int(value) or not lo <= value <= hi:
        raise ToolError(f'{field}: integer {lo}-{hi}')
    return value
