#!/usr/bin/env python3
"""Auto-deploy helper: GitHub -> local mirror -> panel.

    python sync.py --refresh <site>  refresh one site's mirror from GitHub (used by the
                                     panel's core webhook as its pre-deploy script)
    python sync.py                   POLL fallback: check every site once, only when the
                                     config has "auto_deploy_mode": "poll"
    python sync.py --now             poll once whatever the mode is
    python sync.py --dry-run         only report what would be published

Default auto-deploy is the panel's core GitHub webhook (autodeploy.py, mode
"webhook"). The panel builds from the local mirror, not from GitHub, so each
push first needs the mirror refreshed: `--refresh` does that with the Windows
git credentials, and refuses when the service's "Auto deploy" switch is off.

The polling fallback is the original bridge; the panel's per-service
"Auto deploy" switch is its on/off switch:

  for every site recipe with a "github" key,
    if the panel service has auto_deploy on, and
    the GitHub branch head differs from the mirror's copy of that branch:
      fetch the branch into a cache clone on Windows (with the Windows git
      credentials, so private repos work and no token goes to the server),
      refresh the mirror from that cache (mirror.sh), run publish.py.

Runs every few minutes from the "ServerKit Site Sync" scheduled task
(ServerKit.ps1 sync-on). Log: <state_dir>\\sync.log (default %LOCALAPPDATA%\\ServerKit).
"""
import datetime
import glob
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import swk_config  # noqa: E402

CFG = swk_config.load()
DISTRO = CFG.distro
STATE_DIR = CFG.state_dir
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


def refresh_site(site_dir, site):
    """GitHub branch -> cache clone -> mirror -> overlay branch ("serverkit"); no redeploy."""
    name, repo, branch = site['name'], site['github'], site['upstream']
    mirror = site.get('mirror', name)
    cache = refresh_cache(repo, branch, mirror)
    r = run(['wsl.exe', '-d', DISTRO, '-u', 'root', '--exec', 'bash',
             publish.wsl_path(os.path.join(HERE, 'mirror.sh')), mirror, publish.wsl_path(cache), branch],
            timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f'mirror.sh: {r.stderr.strip()}')
    publish.apply_overlay(site, site_dir)


def refresh_main(name):
    """--refresh <site>: pre-deploy script of the panel's core webhook."""
    site_dir = os.path.join(HERE, name)
    recipe = os.path.join(site_dir, 'site.json')
    if os.sep in name or name.startswith('.') or not os.path.isfile(recipe):
        print(f'unknown site: {name}')
        return 2
    with open(recipe, encoding='utf-8') as f:
        site = json.load(f)
    if not site.get('github'):
        print(f'{name}: site.json has no "github" key')
        return 2
    app = next((a for a in publish.must('GET', '/apps').get('apps', []) if a['name'] == site['name']), None)
    if not app:
        print(f'{name}: no such service in the panel')
        return 2
    config = publish.must('GET', f'/deploy/apps/{app["id"]}/config').get('config') or {}
    if not config.get('auto_deploy'):
        print(f'{name}: auto deploy is switched off for this service; push not deployed')
        log(f'{name}: webhook push skipped (auto deploy switch off)')
        return 3
    try:
        refresh_site(site_dir, site)
    except Exception as e:
        log(f'{name}: webhook refresh FAILED {e}')
        print(f'{name}: refresh failed: {e}')
        return 1
    log(f'{name}: webhook refresh done ({site["github"]}@{site["upstream"]})')
    print(f'{name}: mirror refreshed from GitHub')
    return 0


def main():
    if '--refresh' in sys.argv:
        i = sys.argv.index('--refresh')
        if i + 1 >= len(sys.argv):
            sys.exit('usage: sync.py --refresh <site>')
        sys.exit(refresh_main(sys.argv[i + 1]))
    if CFG.auto_deploy_mode != 'poll' and '--now' not in sys.argv:
        print('auto_deploy_mode is "webhook": polling is off (set "poll" in serverkit-windows.json, or pass --now)')
        return
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
