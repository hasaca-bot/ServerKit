"""Repository manifest detection for service imports."""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


class RepositoryManifestService:
    """Detect deploy metadata from common PaaS and ServerKit manifest files."""

    SERVERKIT_FILES = ('serverkit.json', 'serverkit.yaml', 'serverkit.yml')
    RENDER_FILES = ('render.yaml', 'render.yml', 'render.json')
    RAILWAY_FILES = ('railway.json', 'railway.toml')
    COMPOSE_FILES = ('docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml')
    APP_FILES = ('app.json',)
    BUILD_FILES = (
        'Dockerfile',
        'nixpacks.toml',
        'package.json',
        'pyproject.toml',
        'requirements.txt',
        'Procfile',
    )
    MAX_FILE_BYTES = 512 * 1024

    @classmethod
    def supported_files(cls) -> List[str]:
        return list(dict.fromkeys(
            cls.SERVERKIT_FILES
            + cls.RENDER_FILES
            + cls.RAILWAY_FILES
            + cls.COMPOSE_FILES
            + cls.APP_FILES
            + cls.BUILD_FILES
        ))

    @classmethod
    def analyze_public_repo(cls, repo_url: str, branch: str = 'main') -> Dict:
        """Inspect a PUBLIC repository by fetching its supported manifest files
        from the provider's raw host (raw.githubusercontent.com, GitLab/Bitbucket
        raw endpoints). No clone and no OAuth — this is the connection-less path
        used to inspect a deploy-template repo the operator has not connected.

        Returns the same shape as :meth:`analyze_files`. On an unsupported host
        or a total fetch failure, returns an empty result with a warning so the
        caller can fall back to declared template hints.
        """
        raw_base = cls._raw_base_url(repo_url, branch)
        if not raw_base:
            return cls._empty_result([f'Unsupported repository host: {repo_url}'])

        file_map = {}
        for file_name in cls.supported_files():
            content = cls._fetch_raw(f'{raw_base}/{file_name}')
            if content is not None:
                file_map[file_name] = content

        if not file_map:
            return cls._empty_result(
                [f'No supported manifest files found in {repo_url}@{branch}'])

        return cls.analyze_files(file_map, root_files=sorted(file_map.keys()))

    @staticmethod
    def _raw_base_url(repo_url: str, branch: str) -> Optional[str]:
        """Map a repository web/clone URL to its raw-file base URL for ``branch``.
        Supports GitHub, GitLab, and Bitbucket; returns None otherwise."""
        url = (repo_url or '').strip()
        branch = branch or 'main'
        m = re.match(r'^(?:https?://|git@)github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$', url)
        if m:
            return f'https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{branch}'
        m = re.match(r'^(?:https?://|git@)gitlab\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$', url)
        if m:
            return f'https://gitlab.com/{m.group(1)}/{m.group(2)}/-/raw/{branch}'
        m = re.match(r'^(?:https?://|git@)bitbucket\.org[:/]([^/]+)/([^/]+?)(?:\.git)?/?$', url)
        if m:
            return f'https://bitbucket.org/{m.group(1)}/{m.group(2)}/raw/{branch}'
        return None

    @staticmethod
    def _fetch_raw(url: str) -> Optional[str]:
        """Fetch a single raw file, returning its text or None (missing/too big/
        unreachable). Isolated so tests can monkeypatch it deterministically."""
        try:
            response = requests.get(url, timeout=10)
        except requests.RequestException:
            return None
        if response.status_code != 200:
            return None
        content = response.content or b''
        if len(content) > RepositoryManifestService.MAX_FILE_BYTES:
            return None
        return content.decode('utf-8', errors='replace')

    @classmethod
    def analyze_path(cls, repo_path: str) -> Dict:
        root = Path(repo_path)
        file_map = {}
        root_files = []

        if not root.exists() or not root.is_dir():
            return cls._empty_result([f'Repository path does not exist: {repo_path}'])

        try:
            root_files = [entry.name for entry in root.iterdir() if entry.is_file()]
        except OSError:
            root_files = []

        for file_name in cls.supported_files():
            path = root / file_name
            if not path.is_file():
                continue
            try:
                if path.stat().st_size > cls.MAX_FILE_BYTES:
                    continue
                file_map[file_name] = path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue

        return cls.analyze_files(file_map, root_files=root_files)

    @classmethod
    def analyze_files(cls, file_map: Dict[str, str], root_files: Optional[List[str]] = None) -> Dict:
        root_files = root_files or list(file_map.keys())
        result = cls._empty_result()
        result['detected_files'] = sorted(set(root_files))

        cls._detect_serverkit(file_map, result)
        cls._detect_compose(file_map, result)
        cls._detect_render(file_map, result)
        cls._detect_railway(file_map, result)
        cls._detect_dockerfile(file_map, result)
        cls._detect_app_json(file_map, result)
        cls._detect_language_files(file_map, result)

        cls._dedupe_env(result)
        cls._finalize_recommendation(result)
        return result

    @staticmethod
    def _empty_result(warnings: Optional[List[str]] = None) -> Dict:
        return {
            'success': True,
            'strategy': None,
            'recommended': {
                'app_type': 'static',
                'build_method': 'custom',
                'port': None,
                'dockerfile_path': 'Dockerfile',
                'custom_build_cmd': None,
                'custom_start_cmd': None,
                'healthcheck_path': None,
            },
            'manifests': [],
            'env': [],
            'ports': [],
            'detected_files': [],
            'warnings': warnings or [],
        }

    @classmethod
    def _detect_serverkit(cls, file_map: Dict[str, str], result: Dict) -> None:
        file_name, data = cls._first_parsed(file_map, cls.SERVERKIT_FILES)
        if not isinstance(data, dict):
            return

        # v1 declarative manifest: recognize it, surface a multi-service summary,
        # and seed `recommended` from the first app service for the wizard. Legacy
        # flat files (no `version: 1`) keep the original code path untouched.
        if cls._detect_serverkit_v1(file_name, data, result, file_map.get(file_name)):
            return

        build = data.get('build') if isinstance(data.get('build'), dict) else {}
        deploy = data.get('deploy') if isinstance(data.get('deploy'), dict) else {}
        app_type = data.get('app_type') or data.get('type') or deploy.get('type')
        build_method = (
            data.get('build_method')
            or build.get('method')
            or build.get('build_method')
        )
        dockerfile_path = (
            data.get('dockerfile_path')
            or build.get('dockerfile_path')
            or build.get('dockerfilePath')
        )
        port = cls._extract_port(data.get('port') or deploy.get('port'))

        cls._set_recommended(
            result,
            strategy='serverkit',
            app_type=app_type,
            build_method=build_method,
            port=port,
            dockerfile_path=dockerfile_path,
            custom_build_cmd=data.get('build_command') or build.get('command') or build.get('buildCommand'),
            custom_start_cmd=data.get('start_command') or deploy.get('start_command') or deploy.get('startCommand'),
            healthcheck_path=deploy.get('healthcheck_path') or deploy.get('healthcheckPath'),
        )
        cls._merge_env(result, cls._env_from_mapping(data.get('env'), source=file_name))
        cls._add_manifest(result, 'serverkit', file_name, 'ServerKit manifest', 'Native import settings')

    @classmethod
    def _detect_serverkit_v1(cls, file_name: str, data: Dict, result: Dict,
                             raw: Optional[str] = None) -> bool:
        """Recognize a `version: 1` manifest. Returns True when handled."""
        from app.services.manifest_spec_service import ManifestSpecService, ManifestError

        if not ManifestSpecService.is_v1(data):
            return False

        result['manifest_v1_raw'] = raw
        result['manifest_v1_file'] = file_name

        try:
            normalized = ManifestSpecService.normalize(data)
        except ManifestError as exc:
            result['warnings'].append('Invalid serverkit.yaml: ' + '; '.join(exc.errors[:5]))
            result['manifest_v1_errors'] = exc.errors
            # still record that a v1 manifest exists so the UI can show the errors
            cls._add_manifest(result, 'serverkit', file_name,
                              'ServerKit manifest (v1)', 'Declarative manifest — has errors')
            return True

        summary = ManifestSpecService.summarize(normalized)
        result['manifest_v1'] = summary
        result['manifest_v1_normalized'] = normalized

        # Seed `recommended` from the first app service so the existing single-app
        # wizard flow still gets sensible defaults.
        first_app = next((s for s in normalized['services'] if s['kind'] == 'app'), None)
        if first_app:
            cls._set_recommended(
                result,
                strategy='serverkit',
                app_type=first_app.get('app_type'),
                build_method='dockerfile' if first_app.get('dockerfile_path') else None,
                port=first_app.get('port'),
                dockerfile_path=first_app.get('dockerfile_path'),
                custom_build_cmd=first_app.get('build_command'),
                custom_start_cmd=first_app.get('start_command'),
                healthcheck_path=first_app.get('healthcheck_path'),
            )
            # surface non-secret env keys the wizard can render
            entries = []
            for var in first_app['env_vars']:
                entries.append({
                    'key': var['key'],
                    'value': var['value'] if var['source'] == 'value' else None,
                    'required': var['source'] in ('secret', 'placeholder'),
                    'secret': var['secret'],
                    'source': file_name,
                    'description': '',
                })
            cls._merge_env(result, entries)
        else:
            result['strategy'] = result['strategy'] or 'serverkit'

        svc_count = summary['service_count']
        db_count = len(summary['databases'])
        parts = [f'{svc_count} service{"s" if svc_count != 1 else ""}']
        if db_count:
            parts.append(f'{db_count} database{"s" if db_count != 1 else ""}')
        if summary['domains']:
            parts.append(f'{len(summary["domains"])} domain{"s" if len(summary["domains"]) != 1 else ""}')
        cls._add_manifest(result, 'serverkit', file_name,
                          'ServerKit manifest (v1)', ', '.join(parts))
        return True

    @classmethod
    def _detect_compose(cls, file_map: Dict[str, str], result: Dict) -> None:
        file_name, data = cls._first_parsed(file_map, cls.COMPOSE_FILES)
        if not data:
            return

        services = data.get('services') if isinstance(data, dict) else {}
        if not isinstance(services, dict):
            services = {}

        service_name = None
        service_count = len(services)
        ports = []
        env_entries = []
        build_method = 'custom'
        dockerfile_path = None

        for name, service in services.items():
            if service_name is None:
                service_name = name
            if not isinstance(service, dict):
                continue
            ports.extend(cls._ports_from_compose_service(service))
            env_entries.extend(cls._env_from_compose_service(service, file_name))
            build = service.get('build')
            if isinstance(build, dict):
                dockerfile_path = build.get('dockerfile')
                build_method = 'dockerfile' if dockerfile_path or 'Dockerfile' in file_map else 'custom'
            elif build:
                build_method = 'dockerfile' if 'Dockerfile' in file_map else 'custom'

        for port in ports:
            cls._append_port(result, port)

        cls._set_recommended(
            result,
            strategy='docker_compose',
            app_type='docker',
            build_method=build_method,
            port=ports[0] if ports else None,
            dockerfile_path=dockerfile_path,
        )
        cls._merge_env(result, env_entries)
        summary = f'{service_count} compose service{"s" if service_count != 1 else ""}'
        if service_name:
            summary = f'{service_name}: {summary}'
        cls._add_manifest(result, 'docker_compose', file_name, 'Docker Compose', summary)

    @classmethod
    def _detect_render(cls, file_map: Dict[str, str], result: Dict) -> None:
        file_name, data = cls._first_parsed(file_map, cls.RENDER_FILES)
        if not data:
            return

        services = data.get('services') if isinstance(data, dict) else []
        if not isinstance(services, list):
            services = []
        service = next((item for item in services if isinstance(item, dict) and item.get('type') == 'web'), None)
        service = service or next((item for item in services if isinstance(item, dict)), {})
        if not service:
            return

        runtime = str(service.get('runtime') or '').lower()
        dockerfile_path = service.get('dockerfilePath') or service.get('dockerfile_path')
        build_method = 'dockerfile' if runtime == 'docker' or dockerfile_path else None
        app_type = 'docker' if build_method == 'dockerfile' else None
        env_entries = cls._env_from_render(service.get('envVars'), file_name)
        port = cls._port_from_env(env_entries)

        cls._set_recommended(
            result,
            strategy='render',
            app_type=app_type,
            build_method=build_method,
            port=port,
            dockerfile_path=dockerfile_path,
            custom_build_cmd=service.get('buildCommand'),
            custom_start_cmd=service.get('startCommand'),
            healthcheck_path=service.get('healthCheckPath') or service.get('healthcheckPath'),
            root_dir=service.get('rootDir') or service.get('root_dir'),
            build_filter=cls._render_build_filter(service.get('buildFilter')),
        )
        if port:
            cls._append_port(result, port)
        cls._merge_env(result, env_entries)

        name = service.get('name') or 'web'
        summary = f'{name} service'
        if runtime:
            summary = f'{summary} using {runtime}'
        cls._add_manifest(result, 'render', file_name, 'Render blueprint', summary)

    @staticmethod
    def _render_build_filter(raw: Any) -> Optional[Dict]:
        """render.yaml ``buildFilter: {paths, ignoredPaths}`` -> our shape."""
        from app.services.deploy_control_service import validate_build_filter
        clean, _error = validate_build_filter(raw)
        return clean

    @classmethod
    def _detect_railway(cls, file_map: Dict[str, str], result: Dict) -> None:
        file_name, data = cls._first_parsed(file_map, cls.RAILWAY_FILES)
        if not isinstance(data, dict):
            return

        build = data.get('build') if isinstance(data.get('build'), dict) else {}
        deploy = data.get('deploy') if isinstance(data.get('deploy'), dict) else {}
        dockerfile_path = build.get('dockerfilePath') or build.get('dockerfile_path')
        build_method = 'dockerfile' if dockerfile_path else None

        cls._set_recommended(
            result,
            strategy='railway',
            app_type='docker' if build_method == 'dockerfile' else None,
            build_method=build_method,
            dockerfile_path=dockerfile_path,
            custom_build_cmd=build.get('buildCommand') or build.get('build_command'),
            custom_start_cmd=deploy.get('startCommand') or deploy.get('start_command'),
            healthcheck_path=deploy.get('healthcheckPath') or deploy.get('healthcheck_path'),
        )
        summary = 'Railway deploy settings'
        if deploy.get('healthcheckPath') or deploy.get('healthcheck_path'):
            summary = f'{summary} with health check'
        cls._add_manifest(result, 'railway', file_name, 'Railway config', summary)

    @classmethod
    def _detect_dockerfile(cls, file_map: Dict[str, str], result: Dict) -> None:
        content = file_map.get('Dockerfile')
        if content is None:
            return

        ports = cls._ports_from_dockerfile(content)
        for port in ports:
            cls._append_port(result, port)

        cls._set_recommended(
            result,
            strategy='dockerfile',
            app_type='docker',
            build_method='dockerfile',
            port=ports[0] if ports else None,
            dockerfile_path='Dockerfile',
        )
        summary = 'Container build'
        if ports:
            summary = f'{summary}, exposes {ports[0]}'
        cls._add_manifest(result, 'dockerfile', 'Dockerfile', 'Dockerfile', summary)

    @classmethod
    def _detect_app_json(cls, file_map: Dict[str, str], result: Dict) -> None:
        file_name, data = cls._first_parsed(file_map, cls.APP_FILES)
        if not isinstance(data, dict):
            return

        stack = str(data.get('stack') or '').lower()
        app_type = 'docker' if stack == 'container' else None
        build_method = 'dockerfile' if stack == 'container' and 'Dockerfile' in file_map else None
        cls._set_recommended(
            result,
            strategy='app_json',
            app_type=app_type,
            build_method=build_method,
        )
        cls._merge_env(result, cls._env_from_mapping(data.get('env'), source=file_name))
        cls._add_manifest(result, 'app_json', file_name, 'Heroku app manifest', data.get('description') or 'App metadata')

    @classmethod
    def _detect_language_files(cls, file_map: Dict[str, str], result: Dict) -> None:
        if 'package.json' in file_map:
            cls._set_recommended(result, strategy='nixpacks', app_type='static', build_method='nixpacks')
            cls._add_manifest(result, 'package_json', 'package.json', 'Node package', 'Nixpacks-compatible Node project')

        if 'pyproject.toml' in file_map or 'requirements.txt' in file_map:
            framework = 'Python'
            app_type = 'flask'
            build_method = 'nixpacks'
            cls._set_recommended(result, strategy='nixpacks', app_type=app_type, build_method=build_method)
            cls._add_manifest(result, 'python', 'pyproject.toml' if 'pyproject.toml' in file_map else 'requirements.txt', framework, 'Nixpacks-compatible Python project')

        if 'nixpacks.toml' in file_map:
            cls._set_recommended(result, strategy='nixpacks', build_method='nixpacks')
            cls._add_manifest(result, 'nixpacks', 'nixpacks.toml', 'Nixpacks config', 'Explicit Nixpacks build plan')

    @classmethod
    def _finalize_recommendation(cls, result: Dict) -> None:
        if result['strategy']:
            return
        if result['manifests']:
            result['strategy'] = result['manifests'][0]['type']
            return
        result['warnings'].append('No supported deployment manifest found')

    @classmethod
    def _set_recommended(cls, result: Dict, strategy: Optional[str] = None, **values: Any) -> None:
        priority = {
            'serverkit': 90,
            'docker_compose': 80,
            'render': 70,
            'railway': 65,
            'dockerfile': 60,
            'app_json': 50,
            'nixpacks': 40,
        }
        current_priority = priority.get(result.get('strategy'), 0)
        next_priority = priority.get(strategy, 0)
        can_set_strategy = strategy and next_priority >= current_priority

        if can_set_strategy:
            result['strategy'] = strategy

        for key, value in values.items():
            if value in (None, ''):
                continue
            if key == 'port':
                value = cls._extract_port(value)
                if not value:
                    continue
                cls._append_port(result, value)
            if can_set_strategy or result['recommended'].get(key) in (None, '', 'custom', 'static'):
                result['recommended'][key] = value

    @staticmethod
    def _add_manifest(result: Dict, manifest_type: str, file_name: str, label: str, summary: str) -> None:
        if any(item['file'] == file_name for item in result['manifests']):
            return
        result['manifests'].append({
            'type': manifest_type,
            'file': file_name,
            'label': label,
            'summary': summary,
        })

    @classmethod
    def _first_parsed(cls, file_map: Dict[str, str], candidates: tuple) -> tuple:
        for file_name in candidates:
            if file_name not in file_map:
                continue
            data = cls._parse_file(file_name, file_map[file_name])
            if data is not None:
                return file_name, data
        return None, None

    @staticmethod
    def _parse_file(file_name: str, content: str) -> Optional[Any]:
        try:
            if file_name.endswith('.json'):
                return json.loads(content)
            if file_name.endswith(('.yaml', '.yml')):
                return yaml.safe_load(content) or {}
            if file_name.endswith('.toml'):
                return tomllib.loads(content)
        except Exception:
            return None
        return None

    @classmethod
    def _env_from_render(cls, env_vars: Any, source: str) -> List[Dict]:
        if not isinstance(env_vars, list):
            return []

        entries = []
        for item in env_vars:
            if not isinstance(item, dict) or not item.get('key'):
                continue
            value = item.get('value')
            secret = item.get('sync') is False or cls._looks_secret(item['key'])
            entries.append({
                'key': item['key'],
                'value': None if secret else value,
                'required': item.get('sync') is False or value in (None, ''),
                'secret': secret,
                'source': source,
                'description': item.get('description') or '',
            })
        return entries

    @classmethod
    def _env_from_mapping(cls, env: Any, source: str) -> List[Dict]:
        if not isinstance(env, dict):
            return []

        entries = []
        for key, value in env.items():
            if isinstance(value, dict):
                raw_value = value.get('value')
                required = bool(value.get('required', raw_value in (None, '')))
                description = value.get('description') or ''
            else:
                raw_value = value
                required = raw_value in (None, '')
                description = ''
            secret = cls._looks_secret(key)
            entries.append({
                'key': key,
                'value': None if secret else raw_value,
                'required': required,
                'secret': secret,
                'source': source,
                'description': description,
            })
        return entries

    @classmethod
    def _env_from_compose_service(cls, service: Dict, source: str) -> List[Dict]:
        entries = []
        env = service.get('environment')
        if isinstance(env, dict):
            entries.extend(cls._env_from_mapping(env, source))
        elif isinstance(env, list):
            for item in env:
                if not isinstance(item, str) or not item:
                    continue
                key, _, value = item.partition('=')
                secret = cls._looks_secret(key)
                entries.append({
                    'key': key,
                    'value': None if secret or not value else value,
                    'required': not bool(value),
                    'secret': secret,
                    'source': source,
                    'description': '',
                })
        env_file = service.get('env_file')
        if env_file:
            files = env_file if isinstance(env_file, list) else [env_file]
            for file_name in files:
                entries.append({
                    'key': str(file_name),
                    'value': None,
                    'required': False,
                    'secret': False,
                    'source': source,
                    'description': 'Referenced env file',
                    'kind': 'env_file',
                })
        return entries

    @staticmethod
    def _looks_secret(key: str) -> bool:
        return bool(re.search(r'(SECRET|TOKEN|PASSWORD|API_KEY|PRIVATE|CREDENTIAL)', key or '', re.I))

    @classmethod
    def _merge_env(cls, result: Dict, entries: List[Dict]) -> None:
        result['env'].extend(entries)

    @staticmethod
    def _dedupe_env(result: Dict) -> None:
        merged = {}
        for entry in result['env']:
            key = entry.get('key')
            if not key:
                continue
            current = merged.get(key, {})
            merged[key] = {
                **current,
                **entry,
                'required': bool(current.get('required')) or bool(entry.get('required')),
                'secret': bool(current.get('secret')) or bool(entry.get('secret')),
                'sources': sorted(set(current.get('sources', []) + [entry.get('source')])) if entry.get('source') else current.get('sources', []),
            }
        result['env'] = sorted(merged.values(), key=lambda item: (not item.get('required'), item.get('key', '')))

    @classmethod
    def _port_from_env(cls, entries: List[Dict]) -> Optional[int]:
        for entry in entries:
            if entry.get('key', '').upper().endswith('PORT'):
                port = cls._extract_port(entry.get('value'))
                if port:
                    return port
        return None

    @staticmethod
    def _ports_from_compose_service(service: Dict) -> List[int]:
        ports = []
        for item in service.get('ports') or []:
            port = None
            if isinstance(item, int):
                port = item
            elif isinstance(item, str):
                cleaned = item.split('/')[0]
                parts = cleaned.split(':')
                port = parts[-2] if len(parts) > 1 else parts[0]
            elif isinstance(item, dict):
                port = item.get('published') or item.get('target')
            extracted = RepositoryManifestService._extract_port(port)
            if extracted:
                ports.append(extracted)
        return ports

    @staticmethod
    def _ports_from_dockerfile(content: str) -> List[int]:
        ports = []
        for line in content.splitlines():
            match = re.match(r'^\s*EXPOSE\s+(.+)$', line, re.I)
            if not match:
                continue
            for value in match.group(1).split():
                port = RepositoryManifestService._extract_port(value.split('/')[0])
                if port:
                    ports.append(port)
        return ports

    @staticmethod
    def _extract_port(value: Any) -> Optional[int]:
        if isinstance(value, int) and 0 < value <= 65535:
            return value
        if not isinstance(value, str):
            return None
        match = re.search(r'\d{2,5}', value)
        if not match:
            return None
        port = int(match.group(0))
        if 0 < port <= 65535:
            return port
        return None

    @staticmethod
    def _append_port(result: Dict, port: Optional[int]) -> None:
        if port and port not in result['ports']:
            result['ports'].append(port)
