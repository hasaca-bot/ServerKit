#!/usr/bin/env python3
"""Auto-deploy: republish sites whose GitHub branch moved.

    python sync.py            check every site once
    python sync.py --dry-run  only report what would be published

The panel's own auto-deploy is webhook-driven and would pull from the panel's
repo URL, which here is the local mirror, not GitHub. So this script is the
bridge, and the panel's per-service "Auto deploy" switch is the on/off switch:

  for every site recipe with a "github" key,
    if the panel service has auto_deploy on, and
    the GitHub branch head differs from the mirror's copy of that branch:
      fetch the branch into a cache clone on Windows (with the Windows git
      credentials, so private repos work and no token goes to the server),
      refresh the mirror from that cache (mirror.sh), run publish.py.

Runs every few minutes from the "ServerKit Site Sync" scheduled task
(ServerKit.ps1 sync-on). Log: %LOCALAPPDATA%\\ServerKit\\sync.log.
"""
import datetime
import glob
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DISTRO = os.environ.get('SERVERKIT_WSL_DISTRO', 'ServerKit')
STATE_DIR = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'ServerKit')
CACHE_DIR = os.path.join(STATE_DIR, 'repos')
LOG = os.path.join(STATE_DIR, 'sync.log')
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

_spec = importlib.util.spec_from_file_location('publish', os.path.join(HERE, 'publish.py'))
publish = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publish)


def log(text):
    line = f'{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {text}'
    print(line)
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, creationflags=NO_WINDOW, **kw)


def github_head(repo, branch):
    r = run(['git', 'ls-remote', f'https://github.com/{repo}.git', f'refs/heads/{branch}'], timeout=60)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f'ls-remote {repo} {branch}: {r.stderr.strip() or "no such branch"}')
    return r.stdout.split()[0]


def mirror_head(mirror, branch):
    r = run(['wsl.exe', '-d', DISTRO, '-u', 'root', '--exec', 'git', '--git-dir', f'/srv/git/{mirror}.git',
             'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}'], timeout=60)
    return r.stdout.strip() or None


def refresh_cache(repo, branch, mirror):
    path = os.path.join(CACHE_DIR, mirror)
    if not os.path.isdir(os.path.join(path, '.git')):
        os.makedirs(CACHE_DIR, exist_ok=True)
        r = run(['git', 'clone', '--quiet', '--branch', branch, '--single-branch',
                 f'https://github.com/{repo}.git', path], timeout=900)
    else:
        r = run(['git', '-C', path, 'fetch', '--quiet', 'origin', f'+refs/heads/{branch}:refs/heads/{branch}'],
                timeout=900)
        if r.returncode == 0:
            # The cache is never edited; keep its checkout on the fetched branch.
            r = run(['git', '-C', path, 'reset', '--quiet', '--hard', branch], timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f'fetch {repo}: {r.stderr.strip()}')
    return path


def main():
    dry = '--dry-run' in sys.argv
    apps = {a['name']: a for a in publish.must('GET', '/apps').get('apps', [])}
    for recipe in sorted(glob.glob(os.path.join(HERE, '*', 'site.json'))):
        site_dir = os.path.dirname(recipe)
        with open(recipe, encoding='utf-8') as f:
            site = json.load(f)
        repo = site.get('github')
        if not repo:
            continue
        name, branch = site['name'], site['upstream']
        mirror = site.get('mirror', name)
        app = apps.get(name)
        if not app:
            continue
        config = publish.must('GET', f'/deploy/apps/{app["id"]}/config').get('config') or {}
        if not config.get('auto_deploy'):
            continue
        try:
            remote = github_head(repo, branch)
            local = mirror_head(mirror, branch)
            if remote == local:
                continue
            log(f'{name}: GitHub {repo}@{branch} is at {remote[:7]}, server has {(local or "-")[:7]}')
            if dry:
                continue
            cache = refresh_cache(repo, branch, mirror)
            r = run(['wsl.exe', '-d', DISTRO, '-u', 'root', '--exec', 'bash',
                     publish.wsl_path(os.path.join(HERE, 'mirror.sh')), mirror, publish.wsl_path(cache), branch],
                    timeout=900)
            if r.returncode != 0:
                raise RuntimeError(f'mirror.sh: {r.stderr.strip()}')
            # The mirror is now GitHub's; do not let a local "source" overwrite it.
            env = dict(os.environ, SERVERKIT_SKIP_SOURCE='1')
            r = run([sys.executable, os.path.join(HERE, 'publish.py'), site_dir], timeout=3600, env=env)
            tail = ' | '.join(l.strip() for l in r.stdout.splitlines()[-3:])
            log(f'{name}: published {remote[:7]} (exit {r.returncode}) {tail}')
        except Exception as e:  # one broken site must not stop the others
            log(f'{name}: FAILED {e}')


if __name__ == '__main__':
    main()
