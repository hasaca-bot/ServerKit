#!/usr/bin/env python3
"""Auto-deploy through the panel's core GitHub webhook (config "auto_deploy_mode": "webhook").

publish.py calls ensure() after a successful deploy:

  1. creates (or updates) one core webhook record for the service through the
     panel API  (POST/PUT /api/v1/git/webhooks, source "github", deployOnPush);
     the panel receives GitHub's push at
         <panel.public_url>/api/v1/git/webhooks/receive/<token>
     verifies the HMAC signature and deploys the service;
  2. registers that URL + secret on the GitHub repository with the user's own
     credentials (gh CLI token, else the git credential helper) - nothing is
     stored on the server; if neither works, prints exact instructions.

Why a pre-deploy script: the panel's service builds from the local mirror
(/srv/git/<site>.git, branch "serverkit" = upstream + Dockerfile overlay), not
from GitHub. The webhook's pre-deploy script runs `sync.py --refresh <site>` on
the Windows side (WSL interop) which fetches GitHub with the Windows git
credentials and rebuilds the mirror branch before the panel pulls it. The same
script honours the panel's per-service "Auto deploy" switch: switched off, it
exits non-zero and the push is not deployed.

State (webhook id, GitHub hook id; never the secret) is kept in
<state_dir>/webhooks/<site>.json.
"""
import json
import os
import shlex
import shutil
import subprocess
import urllib.error
import urllib.request

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
RECEIVE_PATH = '/api/v1/git/webhooks/receive/'
GITHUB_API = 'https://api.github.com'


# -- state -------------------------------------------------------------------

def _state_file(cfg, site):
    return os.path.join(cfg.state_dir, 'webhooks', f'{site}.json')


def _load_state(cfg, site):
    try:
        with open(_state_file(cfg, site), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(cfg, site, state):
    path = _state_file(cfg, site)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)


# -- GitHub ------------------------------------------------------------------

def github_token():
    """The user's existing GitHub credential, or None. Never printed."""
    gh = shutil.which('gh')
    if gh:
        r = subprocess.run([gh, 'auth', 'token'], capture_output=True, text=True, timeout=30,
                           creationflags=NO_WINDOW)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    git = shutil.which('git')
    if git:
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
        r = subprocess.run([git, 'credential', 'fill'], input='protocol=https\nhost=github.com\n\n',
                           capture_output=True, text=True, timeout=30, env=env, creationflags=NO_WINDOW)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.startswith('password='):
                    return line[len('password='):].strip() or None
    return None


def github_api(token, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GITHUB_API + path, data=data, method=method, headers={
        'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'serverkit-windows-publish',
        'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b'null')
        except ValueError:
            return e.code, None


def register_github_hook(token, repo, url, secret, old_hook_id=None):
    """Create the push hook on the repo (replacing our earlier one). Returns the hook id."""
    status, hooks = github_api(token, 'GET', f'/repos/{repo}/hooks?per_page=100')
    if status == 200:
        for h in hooks or []:
            hook_url = (h.get('config') or {}).get('url', '')
            if h.get('id') == old_hook_id or (RECEIVE_PATH in hook_url and hook_url.split(RECEIVE_PATH)[0] == url.split(RECEIVE_PATH)[0]
                                              and hook_url != url):
                github_api(token, 'DELETE', f'/repos/{repo}/hooks/{h["id"]}')
    status, data = github_api(token, 'POST', f'/repos/{repo}/hooks', {
        'name': 'web', 'active': True, 'events': ['push'],
        'config': {'url': url, 'content_type': 'json', 'secret': secret, 'insecure_ssl': '0'}})
    if status not in (200, 201) or not data:
        reason = (data or {}).get('message') if isinstance(data, dict) else None
        raise RuntimeError(f'GitHub answered HTTP {status}{": " + reason if reason else ""} '
                           '(the token needs permission to manage webhooks on the repository)')
    return data['id']


def delete_github_hook(token, repo, hook_id):
    status, _ = github_api(token, 'DELETE', f'/repos/{repo}/hooks/{hook_id}')
    return status in (204, 404)


def instructions(repo, url, secret):
    return '\n'.join([
        f'   Add this webhook on GitHub once (https://github.com/{repo}/settings/hooks/new):',
        f'     Payload URL   {url}',
        '     Content type  application/json',
        f'     Secret        {secret}',
        '     Events        Just the push event',
        '   (the secret is shown only now; to get a new one, delete the webhook in the panel '
        '(Git > Webhooks) and publish again)'])


# -- panel -------------------------------------------------------------------

def pre_deploy_script(cfg, site_name):
    """Shell run by the panel (inside the distro) before it pulls: refresh the mirror from GitHub."""
    sync = cfg.repo_root_windows.rstrip('\\') + '\\windows\\sites\\sync.py'  # a Windows path: python.exe is started via interop
    return ' '.join(shlex.quote(p) for p in [cfg.python_wsl, sync, '--refresh', site_name])


def _find(publish, app_id, name):
    for w in publish.must('GET', '/git/webhooks').get('webhooks', []):
        if w.get('app_id') == app_id and w.get('name') == name:
            return w
    return None


def ensure(publish, cfg, site, app_id, say=print):
    """Create/update the core webhook for the service and register it on GitHub."""
    name, repo, branch = site['name'], site['github'], site['upstream']
    wanted = site.get('auto_deploy')
    if wanted is None:
        config = publish.must('GET', f'/deploy/apps/{app_id}/config').get('config') or {}
        wanted = bool(config.get('auto_deploy'))
    hook_name = f'serverkit-{name}'
    script = pre_deploy_script(cfg, name)
    state = _load_state(cfg, name)
    existing = _find(publish, app_id, hook_name)

    if not wanted:
        if existing and existing.get('is_active'):
            publish.must('PUT', f'/git/webhooks/{existing["id"]}', {'isActive': False})
        say('   auto-deploy: off (core webhook disabled)')
        return

    public = cfg.panel_public_url
    token = None
    try:
        token = github_token()
    except (OSError, subprocess.SubprocessError):
        token = None

    # A webhook we cannot register (no secret known, no credentials) is left alone;
    # one that exists but was never registered gets re-created so its secret can be set.
    if existing and not state.get('github_hook_id'):
        if token and public:
            publish.must('DELETE', f'/git/webhooks/{existing["id"]}')
            existing = None
        else:
            publish.must('PUT', f'/git/webhooks/{existing["id"]}', {
                'sourceBranch': branch, 'deployOnPush': True, 'preDeployScript': script, 'isActive': True})
            say('   auto-deploy: core webhook exists but is not registered on GitHub; the secret is only shown at creation -'
                ' delete the webhook in the panel (Git > Webhooks) and publish again, with gh/git logged in to GitHub')
            return

    if existing:
        publish.must('PUT', f'/git/webhooks/{existing["id"]}', {
            'sourceBranch': branch, 'deployOnPush': True, 'autoSync': True,
            'preDeployScript': script, 'isActive': True})
        say(f'   auto-deploy: core webhook #{existing["id"]} updated')
        return

    result = publish.must('POST', '/git/webhooks', {
        'name': hook_name, 'source': 'github', 'sourceRepoUrl': f'https://github.com/{repo}',
        'sourceBranch': branch, 'appId': app_id, 'deployOnPush': True, 'autoSync': True,
        'preDeployScript': script})
    created = result['webhook']
    secret = result['secret']
    state = {'webhook_id': created['id'], 'repo': repo}
    say(f'   auto-deploy: core webhook #{created["id"]} created')
    if not public:
        _save_state(cfg, name, state)
        say('   "panel.public_url" is not set in serverkit-windows.json, so GitHub cannot be told where the panel is.')
        say(instructions(repo, '<panel.public_url>' + RECEIVE_PATH + created['webhook_token'], secret))
        return
    url = public + RECEIVE_PATH + created['webhook_token']
    if token:
        try:
            state['github_hook_id'] = register_github_hook(token, repo, url, secret, None)
            _save_state(cfg, name, state)
            say(f'   auto-deploy: registered on GitHub ({repo}), pushes to {branch} deploy automatically')
            return
        except Exception as e:
            say(f'   could not register the webhook on GitHub: {e}')
    else:
        say('   no GitHub credential found (gh auth login / git credential)')
    _save_state(cfg, name, state)
    say(instructions(repo, url, secret))


def remove(publish, cfg, site, app_id, repo=None, say=print):
    """Delete the core webhook (and the GitHub hook if we created it). Best effort."""
    state = _load_state(cfg, site)
    hook_name = f'serverkit-{site}'
    existing = _find(publish, app_id, hook_name) if app_id else None
    if existing:
        publish.must('DELETE', f'/git/webhooks/{existing["id"]}')
        say('   core webhook removed')
    repo = repo or state.get('repo')
    if repo and state.get('github_hook_id'):
        try:
            token = github_token()
            if token and delete_github_hook(token, repo, state['github_hook_id']):
                say('   GitHub webhook removed')
            else:
                say(f'   GitHub webhook not removed: delete it by hand at https://github.com/{repo}/settings/hooks')
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        os.remove(_state_file(cfg, site))
    except OSError:
        pass
