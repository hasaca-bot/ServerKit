"""Source provider connections (GitHub, GitLab, Bitbucket) for repository imports."""

import base64
import os
import secrets
from datetime import datetime
from urllib.parse import urlencode, quote

import requests
from flask import session

from app import db
from app.utils.crypto import encrypt_secret
from app.models import SourceConnection
from app.services.repository_manifest_service import RepositoryManifestService
from app.services.settings_service import SettingsService


class SourceConnectionService:
    """OAuth and API helpers for source-code provider connections."""

    GITHUB_AUTHORIZE_URL = 'https://github.com/login/oauth/authorize'
    GITHUB_TOKEN_URL = 'https://github.com/login/oauth/access_token'
    GITHUB_API_URL = 'https://api.github.com'
    GITHUB_APP_NEW_URL = 'https://github.com/settings/apps/new'
    GITHUB_SCOPES = ['repo', 'read:user', 'user:email']

    GITLAB_AUTHORIZE_URL = 'https://gitlab.com/oauth/authorize'
    GITLAB_TOKEN_URL = 'https://gitlab.com/oauth/token'
    GITLAB_API_URL = 'https://gitlab.com/api/v4'
    GITLAB_SCOPES = ['read_user', 'read_api', 'read_repository']

    @classmethod
    def get_github_config(cls, redacted=False):
        config = cls._provider_config('github', redacted)
        # When GitHub was set up via the one-click GitHub App manifest flow, we
        # also track the app slug so the UI can show it and the connect flow can
        # route through the app's install URL. `provider_kind` is 'app' for a
        # manifest-provisioned GitHub App, 'oauth' for a hand-entered OAuth app.
        app_slug = SettingsService.get('source_github_app_slug', '') or ''
        config.update({
            'provider_kind': 'app' if app_slug else 'oauth',
            'app_slug': app_slug,
            'app_name': SettingsService.get('source_github_app_name', '') or '',
            'app_html_url': SettingsService.get('source_github_app_html_url', '') or '',
            'install_url': f'https://github.com/apps/{app_slug}/installations/new' if app_slug else '',
        })
        return config

    @classmethod
    def update_github_config(cls, data, user_id=None):
        return cls._update_provider_config('github', data, user_id)

    # ==================================================================
    # One-click GitHub App setup (manifest flow)
    # ------------------------------------------------------------------
    # An open-source, self-hosted panel can't ship a client secret, and asking
    # every operator to hand-register an OAuth app and copy two secrets is
    # friction. GitHub's app-manifest flow lets each instance create its OWN
    # GitHub App in a couple of clicks: we hand GitHub a manifest, the operator
    # confirms on github.com, and GitHub hands back freshly-minted credentials
    # that we store locally. No shared secret, no infra on our side.
    # ==================================================================

    @classmethod
    def build_github_app_manifest(cls, redirect_uri, base_url):
        """Return ``(manifest, state, post_url)`` for the GitHub App manifest flow.

        The frontend POSTs ``manifest`` (JSON) to ``post_url?state=<state>``; the
        operator confirms on github.com; GitHub redirects to ``redirect_uri`` with
        ``?code=&state=`` which :meth:`complete_github_app_manifest` converts into
        stored credentials.
        """
        if not redirect_uri or not base_url:
            raise ValueError('redirect_uri and base_url are required')

        base_url = base_url.rstrip('/')
        state = secrets.token_urlsafe(32)
        session['source_github_app_state'] = state

        # App names are globally unique on GitHub; a short random suffix avoids
        # collisions between instances/re-runs.
        suffix = secrets.token_hex(3)
        manifest = {
            'name': f'ServerKit {suffix}',
            'url': base_url,
            'redirect_url': redirect_uri,
            # GitHub requires a webhook URL when the manifest subscribes to
            # events. Keep delivery disabled until ServerKit has an app-level
            # receiver; deployments configure their own webhooks separately.
            'hook_attributes': {
                'url': f'{base_url}/api/v1/source-connections/github/webhook',
                'active': False,
            },
            # Where GitHub sends the user after they install the app (also the
            # user-to-server OAuth callback, so install + authorize land here).
            'callback_urls': [f'{base_url}/connections/callback/github'],
            'setup_url': f'{base_url}/connections/callback/github',
            'setup_on_update': False,
            'public': False,
            'default_permissions': {
                'contents': 'read',
                'metadata': 'read',
                'pull_requests': 'read',
            },
            'default_events': ['push', 'pull_request'],
            # Issue a user-to-server token as part of installation so the connect
            # step is a single hop (install + authorize together).
            'request_oauth_on_install': True,
        }
        return manifest, state, cls.GITHUB_APP_NEW_URL

    @classmethod
    def complete_github_app_manifest(cls, code, state, user_id=None):
        """Convert a manifest ``code`` into a GitHub App and persist its
        credentials. Returns a small summary incl. the install URL."""
        expected_state = session.pop('source_github_app_state', None)
        if not expected_state or state != expected_state:
            raise ValueError('Invalid GitHub App manifest state')
        if not code:
            raise ValueError('Missing manifest code')

        response = requests.post(
            f'{cls.GITHUB_API_URL}/app-manifests/{quote(code)}/conversions',
            headers={'Accept': 'application/vnd.github+json'},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        client_id = data.get('client_id')
        client_secret = data.get('client_secret')
        slug = data.get('slug')
        if not (client_id and client_secret and slug):
            raise ValueError('GitHub did not return complete app credentials')

        SettingsService.set('source_github_client_id', client_id, user_id=user_id)
        SettingsService.set('source_github_client_secret', client_secret, user_id=user_id)
        SettingsService.set('source_github_app_id', str(data.get('id') or ''), user_id=user_id)
        SettingsService.set('source_github_app_slug', slug, user_id=user_id)
        SettingsService.set('source_github_app_name', data.get('name') or '', user_id=user_id)
        SettingsService.set('source_github_app_html_url', data.get('html_url') or '', user_id=user_id)
        # The private key and webhook secret are sensitive — store encrypted.
        if data.get('pem'):
            SettingsService.set('source_github_app_pem', encrypt_secret(data['pem']), user_id=user_id)
        if data.get('webhook_secret'):
            SettingsService.set(
                'source_github_app_webhook_secret',
                encrypt_secret(data['webhook_secret']), user_id=user_id,
            )

        return {
            'slug': slug,
            'name': data.get('name') or slug,
            'html_url': data.get('html_url') or '',
            'install_url': f'https://github.com/apps/{slug}/installations/new',
        }

    @classmethod
    def _github_app_slug(cls):
        return SettingsService.get('source_github_app_slug', '') or ''

    @classmethod
    def generate_github_authorize_url(cls, redirect_uri):
        # GitHub App: send the user to the install screen (with OAuth-on-install
        # this grants repo access AND returns an authorization code to our
        # callback in one hop). Classic OAuth app: the plain authorize screen.
        slug = cls._github_app_slug()
        if slug:
            cls._require_configured('github')
            state = cls._new_state('github')
            return f'https://github.com/apps/{slug}/installations/new?{urlencode({"state": state})}', state
        return cls._authorize_url('github', redirect_uri, allow_signup='true')

    @classmethod
    def complete_github_callback(cls, user_id, code, state, redirect_uri):
        return cls._complete_callback('github', user_id, code, state, redirect_uri)

    @classmethod
    def get_connection(cls, user_id, provider='github'):
        return SourceConnection.query.filter_by(user_id=user_id, provider=provider).first()

    @classmethod
    def get_status(cls, user_id):
        connection = cls.get_connection(user_id)
        return {
            'configured': cls.get_github_config()['configured'],
            'connection': connection.to_dict() if connection else None,
        }

    @classmethod
    def disconnect(cls, user_id, provider='github'):
        connection = cls.get_connection(user_id, provider)
        if not connection:
            raise ValueError('Connection not found')
        db.session.delete(connection)
        db.session.commit()

    @classmethod
    def _list_github_app_repos(cls, token):
        """List repositories a GitHub App user-to-server token can reach, across
        every installation the user has granted. (GitHub Apps don't use
        ``/user/repos`` — repo access is per-installation.)"""
        repos = []
        installations = cls._github_get(token, '/user/installations?per_page=100')
        items = installations.get('installations', []) if isinstance(installations, dict) else []
        for installation in items:
            inst_id = installation.get('id')
            if not inst_id:
                continue
            page = 1
            while True:
                data = cls._github_get(
                    token, f'/user/installations/{inst_id}/repositories?per_page=100&page={page}')
                batch = data.get('repositories', []) if isinstance(data, dict) else []
                repos.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
        return repos

    @classmethod
    def list_github_repositories(cls, user_id, search='', page=1, per_page=50):
        token = cls._get_github_token(user_id)
        if cls._github_app_slug():
            repos = cls._list_github_app_repos(token)
            repos.sort(key=lambda r: r.get('updated_at') or '', reverse=True)
        else:
            params = {
                'visibility': 'all',
                'affiliation': 'owner,collaborator,organization_member',
                'sort': 'updated',
                'direction': 'desc',
                'page': max(int(page or 1), 1),
                'per_page': min(max(int(per_page or 50), 1), 100),
            }
            repos = cls._github_get(token, f'/user/repos?{urlencode(params)}')
        search_text = (search or '').strip().lower()
        if search_text:
            repos = [
                repo for repo in repos
                if search_text in repo.get('full_name', '').lower()
                or search_text in (repo.get('description') or '').lower()
            ]

        connection = cls.get_connection(user_id)
        if connection:
            connection.last_used_at = datetime.utcnow()
            db.session.commit()

        return [cls._repo_payload(repo) for repo in repos]

    @classmethod
    def list_github_branches(cls, user_id, full_name):
        token = cls._get_github_token(user_id)
        owner, repo = cls._split_full_name(full_name)
        path = f'/repos/{quote(owner)}/{quote(repo)}/branches?per_page=100'
        branches = cls._github_get(token, path)
        return [
            {
                'name': branch.get('name'),
                'protected': branch.get('protected', False),
            }
            for branch in branches
        ]

    @classmethod
    def get_github_repository_manifest(cls, user_id, full_name, ref=None):
        token = cls._get_github_token(user_id)
        owner, repo = cls._split_full_name(full_name)
        root_files = cls._github_list_root_files(token, owner, repo, ref)
        file_map = {}

        for file_name in RepositoryManifestService.supported_files():
            if root_files and file_name not in root_files:
                continue
            content = cls._github_get_file_content(token, owner, repo, file_name, ref)
            if content is not None:
                file_map[file_name] = content

        manifest = RepositoryManifestService.analyze_files(file_map, root_files=root_files)
        connection = cls.get_connection(user_id)
        if connection:
            connection.last_used_at = datetime.utcnow()
            db.session.commit()
        return manifest

    @classmethod
    def get_authenticated_clone_url(cls, user_id, connection_id, full_name):
        connection = SourceConnection.query.filter_by(
            id=connection_id,
            user_id=user_id,
        ).first()
        if not connection or connection.provider not in ('github', 'gitlab', 'bitbucket'):
            raise ValueError('Source connection not found')

        token = cls._decrypt_connection_token(connection)
        if connection.provider == 'gitlab':
            repo_path = cls._gitlab_repo_path(full_name)
            public_url = f'https://gitlab.com/{repo_path}.git'
            auth_url = f'https://oauth2:{quote(token, safe="")}@gitlab.com/{repo_path}.git'
        elif connection.provider == 'bitbucket':
            workspace, repo_slug = cls._split_full_name(full_name)
            public_url = f'https://bitbucket.org/{workspace}/{repo_slug}.git'
            auth_url = f'https://x-token-auth:{quote(token, safe="")}@bitbucket.org/{workspace}/{repo_slug}.git'
        else:
            owner, repo = cls._split_full_name(full_name)
            public_url = f'https://github.com/{owner}/{repo}.git'
            auth_url = f'https://x-access-token:{quote(token, safe="")}@github.com/{owner}/{repo}.git'
        return {'clone_url': auth_url, 'public_url': public_url}

    @classmethod
    def connection_can_reach(cls, user_id, connection_id, full_name):
        """Whether *full_name* is a repository the user's OWN connection grants
        (tenant isolation, docs/HOSTING-ROADMAP.md §1): on GitHub, a repository of one of
        the App's installations, or — for an OAuth connection — one the user can
        push to; on GitLab/Bitbucket, one the token can read. Any lookup failure
        answers False."""
        connection = SourceConnection.query.filter_by(id=connection_id, user_id=user_id).first()
        if not connection or not full_name:
            return False
        try:
            token = cls._decrypt_connection_token(connection)
            if connection.provider == 'github':
                wanted = full_name.strip().lower()
                if cls._github_app_slug():
                    return any((r.get('full_name') or '').lower() == wanted
                               for r in cls._list_github_app_repos(token))
                owner, repo = cls._split_full_name(full_name)
                data = cls._github_get(token, f'/repos/{quote(owner)}/{quote(repo)}')
                perms = (data or {}).get('permissions') or {}
                return bool(perms.get('push') or perms.get('admin') or perms.get('maintain'))
            if connection.provider == 'gitlab':
                repo_path = cls._gitlab_repo_path(full_name)
                return bool(cls._gitlab_get(token, f'/projects/{quote(repo_path, safe="")}'))
            if connection.provider == 'bitbucket':
                workspace, repo_slug = cls._split_full_name(full_name)
                return bool(cls._bitbucket_get(
                    token, f'/repositories/{quote(workspace)}/{quote(repo_slug)}'))
        except Exception:
            return False
        return False

    # ==================================================================
    # Shared OAuth plumbing. The three providers differ only in the facts
    # below; config, the authorize hop, the code exchange and the stored
    # connection are one implementation.
    # ==================================================================

    @classmethod
    def _oauth(cls, provider):
        return {
            'github': {
                'label': 'GitHub', 'scopes': cls.GITHUB_SCOPES,
                'authorize_url': cls.GITHUB_AUTHORIZE_URL, 'token_url': cls.GITHUB_TOKEN_URL,
                'response_type': False, 'scope_param': True,
                'basic_auth': False, 'grant_type': False,
                'get': cls._github_get, 'profile': _github_profile, 'scope': _scope_string,
            },
            'gitlab': {
                'label': 'GitLab', 'scopes': cls.GITLAB_SCOPES,
                'authorize_url': cls.GITLAB_AUTHORIZE_URL, 'token_url': cls.GITLAB_TOKEN_URL,
                'response_type': True, 'scope_param': True,
                'basic_auth': False, 'grant_type': True,
                'get': cls._gitlab_get, 'profile': _gitlab_profile, 'scope': _scope_string,
            },
            'bitbucket': {
                # Bitbucket takes its scopes from the OAuth consumer, not the
                # URL, and authenticates the code exchange with Basic auth.
                'label': 'Bitbucket', 'scopes': cls.BITBUCKET_SCOPES,
                'authorize_url': cls.BITBUCKET_AUTHORIZE_URL, 'token_url': cls.BITBUCKET_TOKEN_URL,
                'response_type': True, 'scope_param': False,
                'basic_auth': True, 'grant_type': True,
                'get': cls._bitbucket_get, 'profile': _bitbucket_profile, 'scope': _scope_list,
            },
        }[provider]

    @classmethod
    def _provider_config(cls, provider, redacted=False):
        upper = provider.upper()
        client_id = (
            SettingsService.get(f'source_{provider}_client_id', '')
            or os.environ.get(f'SOURCE_{upper}_CLIENT_ID', '')
            or os.environ.get(f'SERVERKIT_{upper}_CLIENT_ID', '')
        )
        client_secret = (
            SettingsService.get(f'source_{provider}_client_secret', '')
            or os.environ.get(f'SOURCE_{upper}_CLIENT_SECRET', '')
            or os.environ.get(f'SERVERKIT_{upper}_CLIENT_SECRET', '')
        )
        if redacted and client_secret:
            client_secret = f'****{client_secret[-4:]}'
        return {
            'client_id': client_id,
            'client_secret': client_secret,
            'configured': bool(client_id and client_secret),
            'scopes': cls._oauth(provider)['scopes'],
        }

    @classmethod
    def _update_provider_config(cls, provider, data, user_id=None):
        updated = []
        if 'client_id' in data:
            SettingsService.set(f'source_{provider}_client_id', data.get('client_id') or '', user_id=user_id)
            updated.append('client_id')
        if 'client_secret' in data:
            value = data.get('client_secret') or ''
            # A masked value is the redacted secret echoed back: keep the real one.
            if not (isinstance(value, str) and value.startswith('****')):
                SettingsService.set(f'source_{provider}_client_secret', value, user_id=user_id)
                updated.append('client_secret')
        config = getattr(cls, f'get_{provider}_config')(redacted=True)
        return {'updated': updated, 'config': config}

    @classmethod
    def _require_configured(cls, provider):
        config = getattr(cls, f'get_{provider}_config')()
        if not config['configured']:
            raise ValueError(f"{cls._oauth(provider)['label']} source connection is not configured")
        return config

    @staticmethod
    def _new_state(provider):
        state = secrets.token_urlsafe(32)
        session[f'source_{provider}_state'] = state
        return state

    @classmethod
    def _authorize_url(cls, provider, redirect_uri, **extra):
        spec = cls._oauth(provider)
        config = cls._require_configured(provider)
        state = cls._new_state(provider)
        # redirect_uri is what the callback posts to the token endpoint, so the
        # authorize hop has to ask for the same one.
        params = {'client_id': config['client_id'], 'redirect_uri': redirect_uri}
        if spec['response_type']:
            params['response_type'] = 'code'
        if spec['scope_param']:
            params['scope'] = ' '.join(spec['scopes'])
        params['state'] = state
        params.update(extra)
        return f"{spec['authorize_url']}?{urlencode(params)}", state

    @classmethod
    def _complete_callback(cls, provider, user_id, code, state, redirect_uri):
        spec = cls._oauth(provider)
        expected_state = session.pop(f'source_{provider}_state', None)
        if not expected_state or state != expected_state:
            raise ValueError(f"Invalid {spec['label']} OAuth state")
        config = cls._require_configured(provider)

        data = {'code': code, 'redirect_uri': redirect_uri}
        if spec['grant_type']:
            data['grant_type'] = 'authorization_code'
        request_kwargs = {'headers': {'Accept': 'application/json'}, 'timeout': 15}
        if spec['basic_auth']:
            request_kwargs['auth'] = (config['client_id'], config['client_secret'])
        else:
            data.update(client_id=config['client_id'], client_secret=config['client_secret'])
        token_response = requests.post(spec['token_url'], data=data, **request_kwargs)
        token_response.raise_for_status()
        token_data = token_response.json()
        if token_data.get('error'):
            raise ValueError(token_data.get('error_description') or token_data['error'])

        access_token = token_data.get('access_token')
        if not access_token:
            raise ValueError(f"{spec['label']} did not return an access token")

        profile = spec['get'](access_token, '/user')
        connection = SourceConnection.query.filter_by(user_id=user_id, provider=provider).first()
        if not connection:
            connection = SourceConnection(user_id=user_id, provider=provider)
            db.session.add(connection)

        (connection.provider_account_id, connection.provider_username,
         connection.display_name, connection.avatar_url) = spec['profile'](profile)
        connection.access_token = access_token
        connection.scope = spec['scope'](token_data) or ' '.join(spec['scopes'])
        connection.updated_at = datetime.utcnow()
        connection.last_used_at = datetime.utcnow()

        db.session.commit()
        return connection

    @classmethod
    def _provider_status(cls, provider, user_id):
        connection = cls.get_connection(user_id, provider)
        return {
            'configured': getattr(cls, f'get_{provider}_config')()['configured'],
            'connection': connection.to_dict() if connection else None,
        }

    @classmethod
    def _provider_token(cls, provider, user_id):
        connection = cls.get_connection(user_id, provider)
        if not connection:
            raise ValueError(f"{cls._oauth(provider)['label']} is not connected")
        return cls._decrypt_connection_token(connection)

    @staticmethod
    def _api_get(url, headers):
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------
    # GitLab
    # ------------------------------------------------------------------

    @classmethod
    def get_gitlab_config(cls, redacted=False):
        return cls._provider_config('gitlab', redacted)

    @classmethod
    def update_gitlab_config(cls, data, user_id=None):
        return cls._update_provider_config('gitlab', data, user_id)

    @classmethod
    def generate_gitlab_authorize_url(cls, redirect_uri):
        return cls._authorize_url('gitlab', redirect_uri)

    @classmethod
    def complete_gitlab_callback(cls, user_id, code, state, redirect_uri):
        return cls._complete_callback('gitlab', user_id, code, state, redirect_uri)

    @classmethod
    def get_gitlab_status(cls, user_id):
        return cls._provider_status('gitlab', user_id)

    @classmethod
    def list_gitlab_repositories(cls, user_id, search='', page=1, per_page=50):
        token = cls._get_gitlab_token(user_id)
        params = {
            'membership': 'true',
            'order_by': 'updated_at',
            'sort': 'desc',
            'page': max(int(page or 1), 1),
            'per_page': min(max(int(per_page or 50), 1), 100),
        }
        search_text = (search or '').strip()
        if search_text:
            params['search'] = search_text
        repos = cls._gitlab_get(token, f'/projects?{urlencode(params)}')

        connection = cls.get_connection(user_id, 'gitlab')
        if connection:
            connection.last_used_at = datetime.utcnow()
            db.session.commit()

        return [cls._gitlab_repo_payload(repo) for repo in repos]

    @classmethod
    def list_gitlab_branches(cls, user_id, full_name):
        token = cls._get_gitlab_token(user_id)
        project_id = cls._gitlab_project_id(full_name)
        path = f'/projects/{project_id}/repository/branches?per_page=100'
        branches = cls._gitlab_get(token, path)
        return [
            {
                'name': branch.get('name'),
                'protected': branch.get('protected', False),
            }
            for branch in branches
        ]

    @classmethod
    def get_gitlab_repository_manifest(cls, user_id, full_name, ref=None):
        token = cls._get_gitlab_token(user_id)
        project_id = cls._gitlab_project_id(full_name)
        root_files = cls._gitlab_list_root_files(token, project_id, ref)
        file_map = {}

        for file_name in RepositoryManifestService.supported_files():
            if root_files and file_name not in root_files:
                continue
            content = cls._gitlab_get_file_content(token, project_id, file_name, ref)
            if content is not None:
                file_map[file_name] = content

        manifest = RepositoryManifestService.analyze_files(file_map, root_files=root_files)
        connection = cls.get_connection(user_id, 'gitlab')
        if connection:
            connection.last_used_at = datetime.utcnow()
            db.session.commit()
        return manifest

    # ------------------------------------------------------------------
    # Bitbucket
    # ------------------------------------------------------------------

    BITBUCKET_AUTHORIZE_URL = 'https://bitbucket.org/site/oauth2/authorize'
    BITBUCKET_TOKEN_URL = 'https://bitbucket.org/site/oauth2/access_token'
    BITBUCKET_API_URL = 'https://api.bitbucket.org/2.0'
    BITBUCKET_SCOPES = ['repository:read', 'account:read']

    @classmethod
    def get_bitbucket_config(cls, redacted=False):
        return cls._provider_config('bitbucket', redacted)

    @classmethod
    def update_bitbucket_config(cls, data, user_id=None):
        return cls._update_provider_config('bitbucket', data, user_id)

    @classmethod
    def generate_bitbucket_authorize_url(cls, redirect_uri):
        return cls._authorize_url('bitbucket', redirect_uri)

    @classmethod
    def complete_bitbucket_callback(cls, user_id, code, state, redirect_uri):
        return cls._complete_callback('bitbucket', user_id, code, state, redirect_uri)

    @classmethod
    def get_bitbucket_status(cls, user_id):
        return cls._provider_status('bitbucket', user_id)

    @classmethod
    def list_bitbucket_repositories(cls, user_id, search='', page=1, per_page=50):
        token = cls._get_bitbucket_token(user_id)
        params = {
            'role': 'member',
            'sort': '-updated_on',
            'pagelen': min(max(int(per_page or 50), 1), 100),
            'page': max(int(page or 1), 1),
        }
        data = cls._bitbucket_get(token, f'/repositories?{urlencode(params)}')
        repos = data.get('values', [])

        search_text = (search or '').strip().lower()
        if search_text:
            repos = [
                repo for repo in repos
                if search_text in repo.get('full_name', '').lower()
                or search_text in (repo.get('description') or '').lower()
            ]

        connection = cls.get_connection(user_id, 'bitbucket')
        if connection:
            connection.last_used_at = datetime.utcnow()
            db.session.commit()

        return [cls._bitbucket_repo_payload(repo) for repo in repos]

    @classmethod
    def list_bitbucket_branches(cls, user_id, full_name):
        token = cls._get_bitbucket_token(user_id)
        workspace, repo_slug = cls._split_full_name(full_name)
        path = f'/repositories/{quote(workspace)}/{quote(repo_slug)}/refs/branches?pagelen=100'
        data = cls._bitbucket_get(token, path)
        return [
            {
                'name': branch.get('name'),
                'protected': False,
            }
            for branch in data.get('values', [])
        ]

    @classmethod
    def get_bitbucket_repository_manifest(cls, user_id, full_name, ref=None):
        token = cls._get_bitbucket_token(user_id)
        workspace, repo_slug = cls._split_full_name(full_name)
        root_files = cls._bitbucket_list_root_files(token, workspace, repo_slug, ref)
        file_map = {}

        for file_name in RepositoryManifestService.supported_files():
            if root_files and file_name not in root_files:
                continue
            content = cls._bitbucket_get_file_content(token, workspace, repo_slug, file_name, ref)
            if content is not None:
                file_map[file_name] = content

        manifest = RepositoryManifestService.analyze_files(file_map, root_files=root_files)
        connection = cls.get_connection(user_id, 'bitbucket')
        if connection:
            connection.last_used_at = datetime.utcnow()
            db.session.commit()
        return manifest

    @classmethod
    def _get_bitbucket_token(cls, user_id):
        return cls._provider_token('bitbucket', user_id)

    @classmethod
    def _bitbucket_get(cls, token, path):
        return cls._api_get(f'{cls.BITBUCKET_API_URL}{path}', cls._bitbucket_headers(token))

    @staticmethod
    def _bitbucket_headers(token):
        return {
            'Accept': 'application/json',
            'Authorization': f'Bearer {token}',
        }

    @classmethod
    def _bitbucket_list_root_files(cls, token, workspace, repo_slug, ref=None):
        params = {}
        if ref:
            params['at'] = ref
        response = requests.get(
            f'{cls.BITBUCKET_API_URL}/repositories/{quote(workspace)}/{quote(repo_slug)}/src/{ref or "master"}/',
            headers=cls._bitbucket_headers(token),
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data.get('values'), list):
            return []
        return [
            item.get('path') for item in data['values']
            if item.get('type') == 'commit_file' and item.get('path')
        ]

    @classmethod
    def _bitbucket_get_file_content(cls, token, workspace, repo_slug, file_name, ref=None):
        params = {}
        if ref:
            params['at'] = ref
        response = requests.get(
            f'{cls.BITBUCKET_API_URL}/repositories/{quote(workspace)}/{quote(repo_slug)}/src/{ref or "master"}/{quote(file_name, safe="")}',
            headers=cls._bitbucket_headers(token),
            params=params,
            timeout=20,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text

    @staticmethod
    def _bitbucket_repo_payload(repo):
        clone_links = repo.get('links', {}).get('clone', []) or []
        https_clone = next((link.get('href') for link in clone_links if link.get('name') == 'https'), None)
        html_url = (repo.get('links', {}).get('html', {}) or {}).get('href', '')
        mainbranch = repo.get('mainbranch', {}) or {}
        owner = repo.get('owner', {}) or {}
        return {
            'id': str(repo.get('uuid', '')).strip('{}'),
            'name': repo.get('name'),
            'full_name': repo.get('full_name'),
            'description': repo.get('description'),
            'private': repo.get('is_private', False),
            'fork': False,
            'archived': False,
            'default_branch': mainbranch.get('name') or 'master',
            'html_url': html_url,
            'clone_url': https_clone or f"https://bitbucket.org/{repo.get('full_name')}.git",
            'owner': {
                'login': owner.get('username'),
                'avatar_url': (owner.get('links', {}).get('avatar', {}) or {}).get('href', ''),
            },
        }

    @classmethod
    def _get_github_token(cls, user_id):
        return cls._provider_token('github', user_id)

    @staticmethod
    def _decrypt_connection_token(connection):
        token = connection.access_token
        if not token:
            raise ValueError('GitHub token could not be decrypted')
        return token

    @classmethod
    def _github_get(cls, token, path):
        return cls._api_get(f'{cls.GITHUB_API_URL}{path}', cls._github_headers(token))

    @staticmethod
    def _github_headers(token):
        return {
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {token}',
            'X-GitHub-Api-Version': '2022-11-28',
        }

    @classmethod
    def _github_list_root_files(cls, token, owner, repo, ref=None):
        params = {}
        if ref:
            params['ref'] = ref
        response = requests.get(
            f'{cls.GITHUB_API_URL}/repos/{quote(owner)}/{quote(repo)}/contents',
            headers=cls._github_headers(token),
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            return []
        return [item.get('name') for item in data if item.get('type') == 'file' and item.get('name')]

    @classmethod
    def _github_get_file_content(cls, token, owner, repo, file_name, ref=None):
        params = {}
        if ref:
            params['ref'] = ref
        response = requests.get(
            f'{cls.GITHUB_API_URL}/repos/{quote(owner)}/{quote(repo)}/contents/{quote(file_name)}',
            headers=cls._github_headers(token),
            params=params,
            timeout=20,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
        if data.get('type') != 'file' or data.get('encoding') != 'base64':
            return None
        raw = base64.b64decode(data.get('content') or '', validate=False)
        return raw.decode('utf-8', errors='replace')

    @classmethod
    def _get_gitlab_token(cls, user_id):
        return cls._provider_token('gitlab', user_id)

    @classmethod
    def _gitlab_get(cls, token, path):
        return cls._api_get(f'{cls.GITLAB_API_URL}{path}', cls._gitlab_headers(token))

    @staticmethod
    def _gitlab_headers(token):
        return {
            'Accept': 'application/json',
            'Authorization': f'Bearer {token}',
        }

    @staticmethod
    def _gitlab_repo_path(full_name):
        path = (full_name or '').strip().strip('/')
        if '/' not in path:
            raise ValueError('repository_full_name must be namespace/project')
        return path

    @classmethod
    def _gitlab_project_id(cls, full_name):
        return quote(cls._gitlab_repo_path(full_name), safe='')

    @classmethod
    def _gitlab_list_root_files(cls, token, project_id, ref=None):
        params = {'per_page': 100}
        if ref:
            params['ref'] = ref
        response = requests.get(
            f'{cls.GITLAB_API_URL}/projects/{project_id}/repository/tree',
            headers=cls._gitlab_headers(token),
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            return []
        return [item.get('name') for item in data if item.get('type') == 'blob' and item.get('name')]

    @classmethod
    def _gitlab_get_file_content(cls, token, project_id, file_name, ref=None):
        params = {'ref': ref or 'HEAD'}
        response = requests.get(
            f'{cls.GITLAB_API_URL}/projects/{project_id}/repository/files/{quote(file_name, safe="")}/raw',
            headers=cls._gitlab_headers(token),
            params=params,
            timeout=20,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text

    @staticmethod
    def _gitlab_repo_payload(repo):
        namespace = repo.get('namespace') or {}
        return {
            'id': repo.get('id'),
            'name': repo.get('name'),
            'full_name': repo.get('path_with_namespace'),
            'description': repo.get('description'),
            'private': repo.get('visibility') != 'public',
            'fork': bool(repo.get('forked_from_project')),
            'archived': repo.get('archived', False),
            'default_branch': repo.get('default_branch') or 'main',
            'html_url': repo.get('web_url'),
            'language': None,
            'updated_at': repo.get('last_activity_at'),
            'owner': {
                'login': namespace.get('path') or namespace.get('full_path'),
                'avatar_url': repo.get('avatar_url') or namespace.get('avatar_url'),
            },
        }

    @staticmethod
    def _split_full_name(full_name):
        parts = (full_name or '').strip().split('/')
        if len(parts) != 2 or not all(parts):
            raise ValueError('repository_full_name must be owner/repo')
        return parts[0], parts[1]

    @staticmethod
    def _repo_payload(repo):
        return {
            'id': repo.get('id'),
            'name': repo.get('name'),
            'full_name': repo.get('full_name'),
            'description': repo.get('description'),
            'private': repo.get('private', False),
            'fork': repo.get('fork', False),
            'archived': repo.get('archived', False),
            'default_branch': repo.get('default_branch') or 'main',
            'html_url': repo.get('html_url'),
            'language': repo.get('language'),
            'updated_at': repo.get('updated_at'),
            'owner': {
                'login': (repo.get('owner') or {}).get('login'),
                'avatar_url': (repo.get('owner') or {}).get('avatar_url'),
            },
        }


# Profile -> (account id, username, display name, avatar URL), per provider.
def _github_profile(profile):
    login = profile.get('login') or ''
    return (str(profile.get('id') or ''), login, profile.get('name') or login,
            profile.get('avatar_url') or '')


def _gitlab_profile(profile):
    username = profile.get('username') or ''
    return (str(profile.get('id') or ''), username, profile.get('name') or username,
            profile.get('avatar_url') or '')


def _bitbucket_profile(profile):
    username = profile.get('username') or ''
    avatar = (profile.get('links', {}).get('avatar', {}) or {}).get('href', '')
    return (str(profile.get('uuid') or '').strip('{}'), username,
            profile.get('display_name') or username, avatar)


# Granted scopes as the token response reports them.
def _scope_string(token_data):
    return token_data.get('scope') or ''


def _scope_list(token_data):
    return ' '.join(token_data.get('scopes', []))
