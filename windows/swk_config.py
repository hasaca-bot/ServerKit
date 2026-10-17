#!/usr/bin/env python3
"""The one config file of the Windows port: windows/serverkit-windows.json.

Every owner-specific value (Windows paths, domain, tunnel, GitHub owners, ports,
limits, panel address) lives there; nothing is hard-coded in the scripts.

    import swk_config
    cfg = swk_config.load()          # SystemExit with instructions if missing
    cfg.domain, cfg.tunnel, cfg.sites_dir, cfg.python_wsl ...

Works on Windows (publish.py, sync.py, remote_ops.py ...) and inside the WSL
distro (gateway.py): Windows paths in the file are translated to /mnt/<drive>/
when this runs on Linux.

Location: $SERVERKIT_WINDOWS_CONFIG, else serverkit-windows.json next to this
file. Shell scripts read single values with:  python3 swk_config.py get a.b.c
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_ENV = 'SERVERKIT_WINDOWS_CONFIG'
CONFIG_NAME = 'serverkit-windows.json'
EXAMPLE_NAME = 'serverkit-windows.example.json'

DEFAULTS = {
    'windows': {'user_profile': '', 'repo_root': '', 'python': '', 'skctl': '', 'state_dir': ''},
    'wsl': {'distro': 'ServerKit'},
    'domain': {'base': '', 'gateway_label': 'deploy', 'panel_label': 'panel'},
    'tunnel': {'name': '', 'wildcard': False},
    'github': {'owners': []},
    'ports': {'min': 4100, 'max': 4999},
    'reserved_names': ['www', 'panel', 'deploy', 'api', 'admin', 'mail', 'smtp', 'ftp', 'root',
                       'app', 'status', 'cdn', 'static', 'ns1', 'ns2'],
    'limits': {'databases_per_site': 2, 'cron_per_site': 10, 'cron_min_interval_minutes': 5,
               'rate_per_minute': 30, 'bad_key_limit': 10, 'bad_key_window_minutes': 10},
    'panel': {'url': 'http://localhost', 'port': 5000, 'public_url': ''},
    'auto_deploy_mode': 'webhook',
    'gateway': {'mode': 'local', 'port': 8711},
    'brain': {'vault': '', 'notes_dir': 'serverkit-windows'},
}

# Values that only ever appear in the committed example; a real config must replace them.
PLACEHOLDERS = {'example.com', 'your-tunnel', 'your-github-user', 'panel.example.com'}

LABEL_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$')
DOMAIN_RE = re.compile(r'^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$')
OWNER_RE = re.compile(r'^[A-Za-z0-9]([A-Za-z0-9-]{0,38})$')


class ConfigError(SystemExit):
    pass


def config_path():
    return os.environ.get(CONFIG_ENV) or os.path.join(HERE, CONFIG_NAME)


def is_windows():
    return os.name == 'nt'


def win_to_wsl(path):
    """C:\\Users\\x -> /mnt/c/Users/x (unchanged for anything else)."""
    m = re.match(r'^([A-Za-z]):[\\/]*(.*)$', path or '')
    if not m:
        return path
    return '/mnt/' + m.group(1).lower() + '/' + m.group(2).replace('\\', '/')


def wsl_to_win(path):
    m = re.match(r'^/mnt/([a-z])/(.*)$', path or '')
    if not m:
        return path
    return m.group(1).upper() + ':\\' + m.group(2).replace('/', '\\')


def _merge(base, over):
    out = dict(base)
    for key, value in (over or {}).items():
        if key.startswith('_'):
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _fail(msg):
    raise ConfigError(f'serverkit-windows.json: {msg}\nEdit {config_path()} (template: windows/{EXAMPLE_NAME}).')


class Config:
    def __init__(self, data, path):
        self.data = data
        self.path = path
        self._check()

    # -- validation ----------------------------------------------------------
    def _check(self):
        d = self.data
        base = d['domain']['base']
        if not base or not DOMAIN_RE.match(base):
            _fail('"domain.base" must be your domain, e.g. "mysites.net".')
        for key in ('gateway_label', 'panel_label'):
            if not LABEL_RE.match(d['domain'][key] or ''):
                _fail(f'"domain.{key}" must be a DNS label (a-z 0-9 -).')
        if not re.match(r'^[a-z0-9-]{1,40}$', d['tunnel']['name'] or ''):
            _fail('"tunnel.name" must be the Cloudflare tunnel name (a-z 0-9 -).')
        owners = d['github']['owners']
        if not isinstance(owners, list) or not owners or not all(isinstance(o, str) and OWNER_RE.match(o) for o in owners):
            _fail('"github.owners" must list at least one GitHub user/organisation allowed to be published.')
        p = d['ports']
        if not (isinstance(p['min'], int) and isinstance(p['max'], int) and 1024 <= p['min'] <= p['max'] <= 65535):
            _fail('"ports.min"/"ports.max" must be integers, 1024 <= min <= max <= 65535.')
        if d['auto_deploy_mode'] not in ('webhook', 'poll'):
            _fail('"auto_deploy_mode" must be "webhook" or "poll".')
        if d['gateway']['mode'] not in ('local', 'core'):
            _fail('"gateway.mode" must be "local" or "core".')
        for key, value in d['limits'].items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                _fail(f'"limits.{key}" must be a positive integer.')
        strings = set(self._strings(d))
        leftovers = sorted((PLACEHOLDERS & strings) | {s for s in strings if '\\YOU\\' in s})
        if leftovers:
            _fail('still contains the example value(s) ' + ', '.join(leftovers) + '.')

    @classmethod
    def _strings(cls, node):
        if isinstance(node, str):
            yield node
        elif isinstance(node, list):
            for item in node:
                yield from cls._strings(item)
        elif isinstance(node, dict):
            for item in node.values():
                yield from cls._strings(item)

    # -- plain values --------------------------------------------------------
    @property
    def domain(self):
        return self.data['domain']['base']

    @property
    def gateway_host(self):
        return f"{self.data['domain']['gateway_label']}.{self.domain}"

    @property
    def panel_host(self):
        return f"{self.data['domain']['panel_label']}.{self.domain}"

    @property
    def tunnel(self):
        return os.environ.get('SERVERKIT_TUNNEL') or self.data['tunnel']['name']

    @property
    def wildcard(self):
        return bool(self.data['tunnel']['wildcard'])

    @property
    def owners(self):
        return list(self.data['github']['owners'])

    @property
    def port_min(self):
        return self.data['ports']['min']

    @property
    def port_max(self):
        return self.data['ports']['max']

    @property
    def reserved(self):
        # The gateway and panel labels and the tunnel name can never be site names.
        d = self.data
        return set(d['reserved_names']) | {d['domain']['gateway_label'], d['domain']['panel_label'], d['tunnel']['name']}

    @property
    def limits(self):
        return dict(self.data['limits'])

    @property
    def distro(self):
        return os.environ.get('SERVERKIT_WSL_DISTRO') or self.data['wsl']['distro']

    @property
    def auto_deploy_mode(self):
        return self.data['auto_deploy_mode']

    @property
    def gateway_mode(self):
        return self.data['gateway']['mode']

    @property
    def gateway_port(self):
        return int(os.environ.get('GATEWAY_PORT') or self.data['gateway']['port'])

    @property
    def panel_port(self):
        return int(self.data['panel']['port'])

    @property
    def panel_url(self):
        return self.data['panel']['url'].rstrip('/')

    @property
    def panel_public_url(self):
        return (self.data['panel']['public_url'] or '').rstrip('/')

    def site_url(self, name):
        return f'https://{name}.{self.domain}'

    # -- paths (always returned for the OS this runs on) ---------------------
    def _native(self, windows_path):
        return windows_path if is_windows() else win_to_wsl(windows_path)

    def _need(self, key):
        value = self.data['windows'].get(key)
        if not value:
            _fail(f'"windows.{key}" is required here.')
        return value

    @property
    def user_profile(self):
        value = self.data['windows']['user_profile'] or (os.path.expanduser('~') if is_windows() else '')
        if not value:
            _fail('"windows.user_profile" is required.')
        return self._native(value)

    @property
    def repo_root_windows(self):
        value = self.data['windows']['repo_root']
        if value:
            return value
        if is_windows():
            return os.path.dirname(HERE)
        _fail('"windows.repo_root" is required (the Windows path of this repository).')

    @property
    def repo_root(self):
        return self._native(self.repo_root_windows)

    @property
    def windows_dir(self):
        return os.path.join(self.repo_root, 'windows')

    @property
    def sites_dir(self):
        return os.path.join(self.windows_dir, 'sites')

    @property
    def python_windows(self):
        return self.data['windows']['python'] or (sys.executable if is_windows() else self._need('python'))

    @property
    def python_wsl(self):
        return win_to_wsl(self.python_windows)

    @property
    def state_dir(self):
        value = self.data['windows']['state_dir']
        if value:
            return self._native(value)
        if is_windows():
            return os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'ServerKit')
        return self._native(os.path.join(self._need('user_profile'), 'AppData', 'Local', 'ServerKit'))

    @property
    def skctl(self):
        value = self.data['windows']['skctl']
        if value:
            return self._native(value)
        return os.path.join(self.user_profile, '.claude', 'skills', 'serverkit', 'skctl.py')

    @property
    def vault(self):
        value = self.data['brain']['vault']
        if not value:
            _fail('"brain.vault" is required for brain/note.py (path of your notes vault).')
        return self._native(value)

    def get(self, dotted, default=None):
        node = self.data
        for part in dotted.split('.'):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def load(path=None):
    path = path or config_path()
    if not os.path.isfile(path):
        raise ConfigError(
            f'ServerKit Windows config not found: {path}\n'
            f'Copy windows/{EXAMPLE_NAME} to windows/{CONFIG_NAME} and fill in your own values\n'
            f'(or set {CONFIG_ENV} to the file).')
    try:
        with open(path, encoding='utf-8-sig') as f:
            raw = json.load(f)
    except ValueError as e:
        raise ConfigError(f'{path} is not valid JSON: {e}')
    if not isinstance(raw, dict):
        raise ConfigError(f'{path} must contain a JSON object.')
    return Config(_merge(DEFAULTS, raw), path)


SPECIAL = ('domain', 'gateway_host', 'panel_host', 'tunnel', 'wildcard', 'gateway_mode', 'gateway_port',
           'auto_deploy_mode', 'distro', 'panel_port', 'panel_url', 'panel_public_url')


def _cli(argv):
    """python3 swk_config.py get <key> [default]  |  check"""
    if len(argv) >= 2 and argv[0] == 'get':
        cfg = load()
        key = argv[1]
        value = getattr(cfg, key) if key in SPECIAL else cfg.get(key, argv[2] if len(argv) > 2 else None)
        if value is None:
            sys.exit(f'config key not set: {key}')
        if isinstance(value, bool):
            value = 'true' if value else 'false'
        elif isinstance(value, (list, dict)):
            value = json.dumps(value)
        print(value)
        return 0
    if argv == ['check']:
        load()
        print('config OK:', config_path())
        return 0
    sys.exit('usage: swk_config.py get <key> [default] | check')


if __name__ == '__main__':
    sys.exit(_cli(sys.argv[1:]))
