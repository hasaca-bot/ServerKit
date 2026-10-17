#!/usr/bin/env python3
"""Windows-side worker of the publish gateway (gateway.py starts it).

Reads one publish request (JSON) on stdin, checks it again, writes the site
recipe to windows\\sites\\<name>\\, fetches the GitHub branch with the Windows
git credentials, refreshes the local mirror and runs publish.py. Exit code 0
means the site is deployed and answered its health check.

Everything a remote caller sends is treated as untrusted: the same checks as
the gateway run here, the Dockerfile is the only file written from the
request, and sites not created through the gateway are never touched.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SITES = os.path.join(os.path.dirname(HERE), 'sites')
DOMAIN = 'tadadigital.shop'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PublishError(Exception):
    pass


def fail(msg):
    raise PublishError(msg)


def main(spec=None):
    """Publish one request; returns the public URL. remote_ops.py calls this
    with the spec, `python remote_publish.py` reads it from stdin."""
    if spec is None:
        spec = json.loads(sys.stdin.read())
    gateway = load('gateway_rules', os.path.join(HERE, 'gateway.py'))
    gateway.SITES_DIR = SITES  # same rules, Windows path
    err = gateway.validate(spec)
    if err:
        fail(err)

    name, repo = spec['name'], spec['github']
    branch = spec.get('branch', 'main')
    site_dir = os.path.join(SITES, name)
    recipe = os.path.join(site_dir, 'site.json')
    if os.path.exists(recipe):
        with open(recipe, encoding='utf-8') as f:
            if json.load(f).get('origin') != 'gateway':
                fail(f'{name} was not created through the gateway')
    print(f'publishing {repo}@{branch} as https://{name}.{DOMAIN}')

    sync = load('sync', os.path.join(SITES, 'sync.py'))
    publish = sync.publish

    # 1. Source: GitHub -> cache clone (Windows credentials) -> local mirror.
    try:
        cache = sync.refresh_cache(repo, branch, name)
    except Exception as e:
        fail(f'could not fetch {repo}@{branch}: {e}')
    head = subprocess.run(['git', '-C', cache, 'rev-parse', '--short', 'HEAD'],
                          capture_output=True, text=True, creationflags=NO_WINDOW).stdout.strip()
    print(f'fetched {repo}@{branch} at {head}')
    if 'dockerfile' not in spec and not os.path.isfile(os.path.join(cache, 'Dockerfile')):
        fail('the repository has no Dockerfile at its root; send one in "dockerfile"')
    r = subprocess.run(['wsl.exe', '-d', sync.DISTRO, '-u', 'root', '--exec', 'bash',
                        publish.wsl_path(os.path.join(SITES, 'mirror.sh')), name,
                        publish.wsl_path(cache), branch],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        fail(f'mirror: {r.stderr.strip()}')

    # 2. Recipe. Only the Dockerfile comes verbatim from the request.
    os.makedirs(site_dir, exist_ok=True)
    if 'dockerfile' in spec:
        with open(os.path.join(site_dir, 'Dockerfile'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(spec['dockerfile'].rstrip('\n') + '\n')
        with open(os.path.join(site_dir, '.dockerignore'), 'w', encoding='utf-8', newline='\n') as f:
            f.write('.git\nnode_modules\n__pycache__\n.env\n')
    old = {}
    if os.path.exists(recipe):
        with open(recipe, encoding='utf-8') as f:
            old = json.load(f)
    site = {
        'name': name,
        'origin': 'gateway',
        'github': repo,
        'mirror': name,
        'upstream': branch,
        'port': spec['port'],
        'host': f'{name}.localhost',
        'public_host': f'{name}.{DOMAIN}',
        'health': spec.get('health', '/'),
        'volumes': [{'name': f'{name}-{i + 1}', 'mount_path': p} for i, p in enumerate(spec.get('volumes', []))],
        'env': spec.get('env', {}),
    }
    with open(recipe, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(site, f, indent=2)
        f.write('\n')

    # 3. Publish through the panel (same script the owner uses).
    env = dict(os.environ, SERVERKIT_SKIP_SOURCE='1', PYTHONIOENCODING='utf-8')
    proc = subprocess.Popen([sys.executable, os.path.join(SITES, 'publish.py'), site_dir],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding='utf-8', errors='replace', env=env, creationflags=NO_WINDOW)
    out = []
    for line in proc.stdout:
        line = line.rstrip()
        out.append(line)
        print(line)
    if proc.wait() != 0:
        fail('publish.py failed (see above)')

    # 4. Auto-deploy switch on the panel service.
    app = next((a for a in publish.must('GET', '/apps').get('apps', []) if a['name'] == name), None)
    if app is None:
        fail('service not found after publishing')
    want = bool(spec.get('auto_deploy', False))
    publish.must('POST', f'/deploy/apps/{app["id"]}/config', {
        'repo_url': f'git://127.0.0.1/{name}.git', 'branch': 'serverkit', 'auto_deploy': want})
    print(f'auto_deploy: {"on" if want else "off"}')

    # 5. Verdict: the public health check line printed by publish.py.
    public = [l for l in out if l.strip().startswith(f'https://{name}.{DOMAIN}')]
    if not public or not re.search(r'HTTP 2\d\d', public[-1]):
        fail(f'site deployed but the public check did not return 2xx: {public[-1] if public else "no check"}')
    print(f'DONE https://{name}.{DOMAIN}')
    return f'https://{name}.{DOMAIN}'


if __name__ == '__main__':
    try:
        main()
    except PublishError as e:
        print(f'ERROR: {e}')
        sys.exit(1)
