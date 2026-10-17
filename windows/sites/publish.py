#!/usr/bin/env python3
"""Publish a site on the local ServerKit panel from a site.json description.

    python publish.py <site-dir>

Each step goes through the panel's REST API (via the serverkit skill's
skctl.py session), so the result is what clicking through the panel would
produce. Steps are idempotent: re-running updates an existing site.

site.json:
    name       panel service name
    mirror     repository name under /srv/git (defaults to name)
    upstream   upstream branch the overlay is applied on
    port       port the container listens on
    host       hostname to serve it on (e.g. foo.localhost); or, for a site
               reached by port only, url (e.g. http://localhost:13999)
    volumes    [{"name": ..., "mount_path": ...}]
    env        {KEY: value}; the value {"generate": "hex32" | "password" |
               "fernet"} is created once and then kept; {"panel_only": true}
               marks a secret that lives only in the panel (never touched here)
    health     path that must answer 200 once deployed
    source     optional: Windows path of a git working copy on this PC; the
               mirror is refreshed from its committed branches first
    public_host  optional: public hostname (e.g. foo.<your-domain>) routed
               through the Cloudflare tunnel straight to `port` (or, with
               "tunnel.wildcard" in the config, through nginx by Host header)
    github     optional: "owner/repo"; needed for auto-deploy
    auto_deploy  optional true/false: register the panel's core GitHub webhook
               (config "auto_deploy_mode": "webhook") for this site; when
               omitted, the panel's per-service "Auto deploy" switch decides

All owner-specific values (domain, tunnel, paths ...) come from
windows/serverkit-windows.json, see swk_config.py.
"""
import base64
import datetime
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import swk_config  # noqa: E402

CFG = swk_config.load()
SKCTL = CFG.skctl
DISTRO = CFG.distro
# The one named Cloudflare tunnel that carries every <name>.<domain> address.
TUNNEL = CFG.tunnel

_spec = importlib.util.spec_from_file_location('skctl', SKCTL)
skctl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skctl)


def api(method, path, body=None, ok=(200, 201, 207)):
    try:
        status, raw = skctl.call(method, path, body=body)
    except skctl.ApiError as e:
        return e.status, skctl.decode(e.body)
    return status, skctl.decode(raw)


def must(method, path, body=None):
    status, data = api(method, path, body)
    if status >= 400:
        sys.exit(f'{method} {path} -> HTTP {status}: {data}')
    return data


def wsl_path(windows_path):
    full = os.path.abspath(windows_path)
    return '/mnt/' + full[0].lower() + full[2:].replace('\\', '/')


def apply_overlay(site, site_dir):
    """Rebuild the local-only "serverkit" branch of the site's mirror (the
    Dockerfile overlay on top of the upstream branch)."""
    subprocess.check_call([
        'wsl.exe', '-d', DISTRO, '-u', 'root', '--exec', 'bash',
        wsl_path(os.path.join(HERE, 'overlay.sh')), site.get('mirror', site['name']), site['upstream'], wsl_path(site_dir),
    ])


def generate(kind):
    if kind == 'hex32':
        return secrets.token_hex(32)
    if kind == 'password':
        return secrets.token_urlsafe(18)
    if kind == 'fernet':
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    sys.exit(f'unknown generator: {kind}')


def find_app(name):
    for app in must('GET', '/apps').get('apps', []):
        if app['name'] == name:
            return app
    return None


def wait_for_deploy(app_id, since, timeout=1800):
    """Wait until the panel has no queued or running deployment job for the
    app created at or after ``since`` (panel UTC timestamp string), then
    return the app. Jobs run one at a time across all apps, so a freshly
    created job can sit in "pending" for a while.

    Older jobs are ignored on purpose: a panel restart leaves the jobs that
    were queued at that moment in "pending" forever, and waiting on those
    would never end."""
    deadline = time.time() + timeout
    time.sleep(2)
    while time.time() < deadline:
        jobs = must('GET', '/deployment-jobs').get('jobs', [])
        busy = [j for j in jobs if j.get('app_id') == app_id
                and (j.get('created_at') or '') >= since
                and j.get('status') in ('pending', 'running')]
        if not busy:
            data = must('GET', f'/apps/{app_id}')
            return data.get('app') or data
        time.sleep(5)
    sys.exit('deployment did not finish in time')


def _load_autodeploy():
    spec = importlib.util.spec_from_file_location('autodeploy', os.path.join(HERE, 'autodeploy.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    site_dir = os.path.abspath(sys.argv[1])
    with open(os.path.join(site_dir, 'site.json'), 'r', encoding='utf-8') as f:
        site = json.load(f)
    name = site['name']
    # Panel timestamps are naive UTC; 60 s of slack covers clock drift
    # between Windows and the WSL distro.
    started = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(seconds=60)).isoformat()
    mirror = site.get('mirror', name)
    print(f'== {name}')

    # 0. Optional: refresh the mirror from a working copy on this PC (the
    #    project being worked on), instead of a one-off clone.
    if site.get('source') and not os.environ.get('SERVERKIT_SKIP_SOURCE'):
        mirror_args = [mirror, wsl_path(site['source'])]
        if site.get('upstream'):
            mirror_args.append(site['upstream'])
        subprocess.check_call([
            'wsl.exe', '-d', DISTRO, '-u', 'root', '--exec', 'bash',
            wsl_path(os.path.join(HERE, 'mirror.sh')), *mirror_args,
        ])

    # 1. Overlay branch in the local mirror.
    apply_overlay(site, site_dir)

    # 2. Service.
    app = find_app(name)
    created = app is None
    if created:
        data = must('POST', '/apps/from-repository', {
            'name': name,
            'repo_url': f'git://127.0.0.1/{mirror}.git',
            'branch': 'serverkit',
            'app_type': 'docker',
            'build_method': 'dockerfile',
            'dockerfile_path': 'Dockerfile',
            'port': site['port'],
            'auto_deploy': False,
        })
        app = data['app']
        print(f'   service created (id {app["id"]}), first build running')
        app = wait_for_deploy(app['id'], started)
    app_id = app['id']

    # 3. Variables: keep what is already set, generate what is missing.
    existing = {v['key'] for v in must('GET', f'/apps/{app_id}/env').get('env_vars', [])}
    wanted = {}
    for key, value in site.get('env', {}).items():
        if isinstance(value, dict):
            if value.get('panel_only') or key in existing:
                continue
            value = generate(value['generate'])
        wanted[key] = value
    if wanted:
        must('POST', f'/apps/{app_id}/env/bulk', {'env_vars': wanted})
        print(f'   variables set: {", ".join(sorted(wanted))}')

    # 4. Volumes.
    have = {v['mount_path'] for v in must('GET', f'/apps/{app_id}/volumes').get('volumes', [])}
    for vol in site.get('volumes', []):
        if vol['mount_path'] not in have:
            must('POST', f'/apps/{app_id}/volumes', vol)
            print(f'   volume {vol["name"]} -> {vol["mount_path"]}')

    # 5. Domain.
    host = site.get('host')
    domains = {d['name'] for d in (must('GET', f'/apps/{app_id}').get('app') or {}).get('domains', [])}
    if host and host not in domains:
        status, data = api('POST', '/domains', {'name': host, 'application_id': app_id, 'is_primary': True})
        if status >= 400:
            print(f'   domain {host} NOT added: HTTP {status} {data}')
        else:
            print(f'   domain {host}')

    # 5b. Public address under the Cloudflare tunnel: route the hostname to
    #     the container port, and record it in the panel as the primary,
    #     HTTPS domain (Cloudflare serves the certificate).
    #     With "tunnel.wildcard" the tunnel already sends *.<domain> to nginx
    #     (domain-tunnel.sh wildcard), so there is no per-site DNS record or
    #     ingress rule: the panel domain (nginx vhost) is all that is needed,
    #     and it stays plain http behind Cloudflare (a redirect to https there
    #     would loop).
    public = site.get('public_host')
    if public:
        if not CFG.wildcard:
            subprocess.check_call([
                'wsl.exe', '-d', DISTRO, '-u', 'root', '--exec',
                '/usr/local/sbin/serverkit-domain-tunnel', 'add', TUNNEL, f'{public}={site["port"]}',
            ], stdout=subprocess.DEVNULL)
        rows = (must('GET', f'/apps/{app_id}').get('app') or {}).get('domains', [])
        row = next((d for d in rows if d['name'] == public), None)
        if row is None:
            row = must('POST', '/domains', {'name': public, 'application_id': app_id, 'is_primary': True})['domain']
        want_ssl = not CFG.wildcard
        if (want_ssl and not row.get('ssl_enabled')) or not row.get('is_primary'):
            body = {'is_primary': True}
            if want_ssl:
                body['ssl_enabled'] = True
            must('PUT', f'/domains/{row["id"]}', body)
        print(f'   public address https://{public}' + (' (wildcard tunnel, no per-site DNS)' if CFG.wildcard else ''))

    # 6. Redeploy so the variables and volumes reach the container.
    must('POST', f'/deploy/apps/{app_id}/deploy', {})
    time.sleep(3)
    app = wait_for_deploy(app_id, started)
    print(f'   status: {app.get("status")}')

    # 6b. Auto-deploy: the panel's core GitHub webhook (default), or nothing
    #     here when "auto_deploy_mode" is "poll" (sync.py does the checking).
    if site.get('github') and CFG.auto_deploy_mode == 'webhook':
        try:
            autodeploy = _load_autodeploy()
            autodeploy.ensure(sys.modules[__name__], CFG, site, app_id)
        except (Exception, SystemExit) as e:  # the site is live; a webhook problem must not fail the publish
            print(f'   auto-deploy webhook NOT set up: {e}')

    # 7. Health: through nginx by hostname, or straight on the published port.
    path = site.get('health', '/')
    if host:
        shown = f'http://{host}{path}'
        req = urllib.request.Request('http://127.0.0.1' + path, headers={'Host': host})
    else:
        shown = site['url'] + path
        req = urllib.request.Request(f'http://127.0.0.1:{site["port"]}{path}')
    for _ in range(45):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                print(f'   {shown} -> HTTP {resp.status}')
                break
        except urllib.error.HTTPError as e:
            print(f'   {shown} -> HTTP {e.code}')
            break
        except OSError:
            time.sleep(2)
    else:
        print(f'   {shown} -> no answer')

    # 8. The public address, end to end through Cloudflare.
    if public:
        url = f'https://{public}{path}'
        for _ in range(30):
            try:
                # Cloudflare's bot check answers 403 to Python's default
                # User-Agent; a browser-like one gets the real page.
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (serverkit publish check)'})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    print(f'   {url} -> HTTP {resp.status}')
                    break
            except urllib.error.HTTPError as e:
                print(f'   {url} -> HTTP {e.code}')
                break
            except OSError:
                time.sleep(5)
        else:
            print(f'   {url} -> no answer (a brand-new DNS name can take a few minutes)')


if __name__ == '__main__':
    main()
