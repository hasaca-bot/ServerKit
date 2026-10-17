"""Pure normalizer/validator for the declarative serverkit.yaml manifest (v1).

No DB, no filesystem side effects. Parses a manifest (raw text or an
already-parsed mapping), validates it against the embedded JSON Schema, and
normalizes it into a stable internal shape the resolver/planner consume.

``version: 1`` selects this spec. Files without it are legacy flat manifests and
are handled by ``RepositoryManifestService`` exactly as before — this module
never touches them.

camelCase is canonical; snake_case aliases are accepted everywhere.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

try:
    import tomllib  # noqa: F401  (parity with RepositoryManifestService)
except ModuleNotFoundError:  # pragma: no cover
    pass

try:
    import jsonschema
    from jsonschema import Draft7Validator
except Exception:  # pragma: no cover - jsonschema is a declared dependency
    jsonschema = None
    Draft7Validator = None


# Service type buckets ------------------------------------------------------
APP_TYPES = ('web', 'worker', 'static', 'docker')
DB_TYPES = ('postgres', 'mysql', 'mariadb', 'redis')
ALL_TYPES = APP_TYPES + DB_TYPES

# Map a manifest db `type` onto a ManagedDatabase engine.
DB_ENGINE_MAP = {
    'postgres': 'postgresql',
    'postgresql': 'postgresql',
    'mysql': 'mysql',
    'mariadb': 'mysql',
    'redis': 'redis',
}

# Map a manifest app `type` onto an Application.app_type.
APP_TYPE_MAP = {
    'web': 'docker',
    'worker': 'docker',
    'docker': 'docker',
    'static': 'static',
}

FROM_SERVICE_PROPERTIES = (
    'connectionString', 'host', 'port', 'database', 'username', 'password', 'url',
)

# Appliance tier: a service's own advertised identity.
FROM_SERVER_PROPERTIES = ('publicIp', 'hostname')

BACKUP_SCHEDULES = ('hourly', 'daily', 'weekly', 'monthly')

# Appliance tier: typed L4 port publishes.
PORT_PROTOCOLS = ('tcp', 'udp')
PORT_EXPOSURES = ('public', 'local')

# Recipes: the execution type is deliberately small and closed. The
# ``kind`` within each type is capability-gated by the runtime registry, so a
# catalog entry can never smuggle an arbitrary command into an older panel.
RECIPE_STEP_TYPES = ('probe', 'derive', 'apply', 'handoff', 'verify')
RECIPE_HARDWARE_MODES = ('required', 'preferred', 'none')

_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_NAME_RE = re.compile(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$')


# The JSON Schema is embedded so validation works even where docs/ is not
# shipped (e.g. the production image). ``docs/serverkit-yaml.schema.json`` is a
# byte-identical copy for editors; ``test_manifest_spec`` asserts they match.
MANIFEST_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "https://serverkit.dev/serverkit-yaml.schema.json",
    "title": "ServerKit Manifest",
    "type": "object",
    "required": ["version"],
    "additionalProperties": True,
    "properties": {
        "version": {"const": 1},
        "server": {"type": "string", "minLength": 1},
        "project": {"type": "string", "minLength": 1},
        "envVars": {"type": "array", "items": {"$ref": "#/definitions/envVar"}},
        "services": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "#/definitions/service"},
        },
        "domains": {"type": "array", "items": {"$ref": "#/definitions/domain"}},
        "capabilities": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "pattern": "^[a-z]+:[a-z0-9._-]+$"},
        },
        "requires": {"$ref": "#/definitions/recipeRequirements"},
        "configure": {
            "type": "array",
            "items": {"$ref": "#/definitions/recipeStep"},
        },
        "verify": {
            "type": "array",
            "items": {"$ref": "#/definitions/recipeVerifyStep"},
        },
    },
    "definitions": {
        "service": {
            "type": "object",
            "required": ["name", "type"],
            "additionalProperties": True,
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^[a-z0-9]([a-z0-9-]*[a-z0-9])?$",
                    "minLength": 1,
                    "maxLength": 63,
                },
                "type": {"enum": list(ALL_TYPES)},
                "runtime": {"type": "string"},
                "buildCommand": {"type": "string"},
                "startCommand": {"type": "string"},
                "dockerfilePath": {"type": "string", "minLength": 1},
                "rootDir": {"type": "string"},
                "buildFilter": {
                    "type": "object",
                    "properties": {
                        "paths": {"type": "array", "items": {"type": "string"}},
                        "ignoredPaths": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "ports": {"type": "array", "items": {"$ref": "#/definitions/portDecl"}},
                "image": {"type": "string"},
                "registry": {"type": "string"},
                "hostRequirements": {"$ref": "#/definitions/hostRequirements"},
                "bootstrap": {"$ref": "#/definitions/bootstrap"},
                "containers": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": {"$ref": "#/definitions/container"},
                },
                "healthCheckPath": {"type": "string"},
                "autoDeploy": {"type": "boolean"},
                "version": {"type": ["string", "number"]},
                "server": {"type": "string"},
                "cpu": {"type": ["number", "string"]},
                "memory": {"type": ["string", "number"]},
                "envVars": {"type": "array", "items": {"$ref": "#/definitions/envVar"}},
                "disks": {"type": "array", "items": {"$ref": "#/definitions/disk"}},
                "disk": {"$ref": "#/definitions/disk"},
                "cron": {
                    "oneOf": [
                        {"$ref": "#/definitions/cron"},
                        {"type": "array", "items": {"$ref": "#/definitions/cron"}},
                    ]
                },
            },
        },
        "bootstrap": {
            "type": "object",
            "required": ["command"],
            "additionalProperties": True,
            "properties": {
                "command": {"type": "string", "minLength": 1},
                "timeoutSeconds": {"type": "integer", "minimum": 1},
            },
        },
        "container": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "image": {"type": "string"},
                "registry": {"type": "string"},
                "ports": {"type": "array", "items": {"$ref": "#/definitions/portDecl"}},
                "disks": {"type": "array", "items": {"$ref": "#/definitions/disk"}},
                "envVars": {"type": "array", "items": {"$ref": "#/definitions/envVar"}},
                "bootstrap": {"$ref": "#/definitions/bootstrap"},
                "hostRequirements": {"$ref": "#/definitions/hostRequirements"},
                "healthCheck": {"$ref": "#/definitions/healthCheck"},
                "dependsOn": {"type": "array", "items": {"$ref": "#/definitions/dependsOn"}},
            },
        },
        "healthCheck": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "cmd": {"type": "string"},
                "httpPath": {"type": "string"},
                "interval": {"type": "string"},
                "timeout": {"type": "string"},
                "retries": {"type": "integer", "minimum": 1},
            },
        },
        "dependsOn": {
            "type": "object",
            "required": ["service"],
            "additionalProperties": True,
            "properties": {
                "service": {"type": "string"},
                "condition": {"enum": ["healthy", "started"]},
            },
        },
        "hostRequirements": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "privileged": {"type": "boolean"},
                "capAdd": {"type": "array", "items": {"type": "string"}},
                "sysctls": {"type": "object"},
                "devices": {"type": "array", "items": {"type": "string"}},
                "kernelModules": {"type": "array", "items": {"type": "string"}},
            },
        },
        "portDecl": {
            "type": "object",
            "required": ["port"],
            "additionalProperties": True,
            "properties": {
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "containerPort": {"type": "integer", "minimum": 1, "maximum": 65535},
                "protocol": {"enum": list(PORT_PROTOCOLS)},
                "expose": {"enum": list(PORT_EXPOSURES)},
            },
        },
        "envVar": {
            "type": "object",
            "required": ["key"],
            "additionalProperties": True,
            "properties": {
                "key": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
                "value": {"type": ["string", "number", "boolean"]},
                "fromSecret": {"type": "string"},
                "fromService": {
                    "type": "object",
                    "required": ["name", "property"],
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "property": {"enum": list(FROM_SERVICE_PROPERTIES)},
                    },
                },
                "fromServer": {
                    "type": "object",
                    "required": ["property"],
                    "additionalProperties": False,
                    "properties": {
                        "property": {"enum": list(FROM_SERVER_PROPERTIES)},
                    },
                },
                "generate": {"type": "boolean"},
            },
        },
        "disk": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "name": {"type": "string"},
                "mountPath": {"type": "string"},
                "size": {"type": ["string", "number"]},
                "backup": {"$ref": "#/definitions/backup"},
            },
        },
        "backup": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "schedule": {"enum": list(BACKUP_SCHEDULES)},
                "retain": {"type": "integer", "minimum": 1},
            },
        },
        "cron": {
            "type": "object",
            "required": ["schedule", "command"],
            "additionalProperties": True,
            "properties": {
                "name": {"type": "string"},
                "schedule": {"type": "string"},
                "command": {"type": "string"},
            },
        },
        "domain": {
            "type": "object",
            "required": ["host"],
            "additionalProperties": True,
            "properties": {
                "host": {"type": "string", "minLength": 1},
                "service": {"type": "string"},
                "ssl": {
                    "oneOf": [
                        {"type": "string", "enum": ["auto", "off", "on"]},
                        {"type": "boolean"},
                    ]
                },
            },
        },
        "recipeRequirements": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "cpuCores": {"type": "integer", "minimum": 1},
                "memoryMB": {"type": "integer", "minimum": 1},
                "diskGB": {"type": "integer", "minimum": 1},
                "igpu": {"enum": list(RECIPE_HARDWARE_MODES)},
                "gpu": {"enum": list(RECIPE_HARDWARE_MODES)},
            },
        },
        "recipeStep": {
            "type": "object",
            "required": ["id", "type", "kind"],
            "additionalProperties": True,
            "properties": {
                "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9._-]*$"},
                "type": {"enum": list(RECIPE_STEP_TYPES)},
                "kind": {"type": "string", "pattern": "^[a-z0-9][a-z0-9._-]*$"},
                "title": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "dependsOn": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string"},
                },
                "params": {"type": "object"},
                "ttlSeconds": {"type": "integer", "minimum": 1},
                "input": {"$ref": "#/definitions/recipeInput"},
            },
        },
        "recipeVerifyStep": {
            "type": "object",
            "required": ["id", "kind"],
            "additionalProperties": True,
            "properties": {
                "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9._-]*$"},
                "type": {"const": "verify"},
                "kind": {"type": "string", "pattern": "^[a-z0-9][a-z0-9._-]*$"},
                "title": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "dependsOn": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string"},
                },
                "params": {"type": "object"},
            },
        },
        "recipeInput": {
            "type": "object",
            "required": ["key", "label"],
            "additionalProperties": True,
            "properties": {
                "key": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
                "label": {"type": "string", "minLength": 1},
                "help": {"type": "string"},
                "url": {"type": "string"},
                "secret": {"type": "boolean"},
            },
        },
    },
}


class ManifestError(Exception):
    """Raised when a v1 manifest fails to parse, validate or normalize.

    ``errors`` carries every problem found so the UI/CLI can show them at once.
    """

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__('; '.join(errors) if errors else 'Invalid manifest')


class ManifestSpecService:
    """Parse → validate → normalize a v1 serverkit manifest. Pure."""

    # -- entry points -------------------------------------------------------

    @staticmethod
    def parse_text(content: str, filename: str = 'serverkit.yaml') -> Optional[Any]:
        """Parse raw manifest text (yaml or json). Returns None on parse error."""
        try:
            if filename.endswith('.json'):
                return json.loads(content)
            return yaml.safe_load(content)
        except Exception:
            return None

    @staticmethod
    def is_v1(data: Any) -> bool:
        """True if ``data`` is a mapping declaring ``version: 1``."""
        return isinstance(data, dict) and data.get('version') in (1, '1')

    @classmethod
    def normalize(cls, data: Any) -> Dict[str, Any]:
        """Validate and normalize a parsed v1 manifest.

        Returns the normalized manifest dict (see module docstring). Raises
        ``ManifestError`` with the full list of problems on any failure.
        """
        if not isinstance(data, dict):
            raise ManifestError(['Manifest must be a mapping'])
        if not cls.is_v1(data):
            raise ManifestError(['Manifest is missing `version: 1`'])

        errors = cls._schema_errors(data)
        if errors:
            raise ManifestError(errors)

        normalized, sem_errors = cls._normalize_checked(data)
        if sem_errors:
            raise ManifestError(sem_errors)
        return normalized

    @classmethod
    def normalize_text(cls, content: str, filename: str = 'serverkit.yaml') -> Dict[str, Any]:
        data = cls.parse_text(content, filename)
        if data is None:
            raise ManifestError([f'Could not parse {filename}'])
        return cls.normalize(data)

    # -- schema validation --------------------------------------------------

    @classmethod
    def _schema_errors(cls, data: Dict[str, Any]) -> List[str]:
        if Draft7Validator is None:  # pragma: no cover
            return cls._manual_schema_errors(data)
        validator = Draft7Validator(MANIFEST_SCHEMA)
        errors = []
        for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
            location = '/'.join(str(p) for p in err.path) or '(root)'
            errors.append(f'{location}: {err.message}')
        return errors

    @staticmethod
    def _manual_schema_errors(data: Dict[str, Any]) -> List[str]:  # pragma: no cover
        errors = []
        services = data.get('services')
        if services is not None and not isinstance(services, list):
            errors.append('services: must be an array')
        return errors

    # -- normalization + semantic checks -----------------------------------

    @classmethod
    def _normalize_checked(cls, data: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        errors: List[str] = []

        services_raw = data.get('services') or []
        names: List[str] = []
        services: List[Dict[str, Any]] = []
        for idx, svc in enumerate(services_raw):
            name = svc.get('name')
            names.append(name)
            services.append(cls._normalize_service(svc, idx, errors))

        # service-name uniqueness
        seen = set()
        for name in names:
            if name in seen:
                errors.append(f'services: duplicate service name `{name}`')
            seen.add(name)
        valid_names = set(n for n in names if n)

        # reference-resolution checks: fromService must name a sibling
        for svc in services:
            for var in svc['env_vars']:
                ref = var.get('service_ref')
                if ref and ref['name'] not in valid_names:
                    errors.append(
                        f"services/{svc['name']}: env `{var['key']}` references "
                        f"unknown service `{ref['name']}`"
                    )

        manifest_env = [
            cls._normalize_env_var(v, f'envVars[{i}]', errors)
            for i, v in enumerate(data.get('envVars') or [])
        ]
        for var in manifest_env:
            ref = var.get('service_ref')
            if ref and ref['name'] not in valid_names:
                errors.append(
                    f"envVars: `{var['key']}` references unknown service `{ref['name']}`"
                )

        domains = []
        for i, dom in enumerate(data.get('domains') or []):
            normalized_dom = cls._normalize_domain(dom)
            if normalized_dom['service'] and normalized_dom['service'] not in valid_names:
                errors.append(
                    f"domains[{i}]: `{normalized_dom['host']}` routes to unknown "
                    f"service `{normalized_dom['service']}`"
                )
            domains.append(normalized_dom)

        declared_capabilities = list(data.get('capabilities') or [])
        configure = []
        verify = []
        seen_step_ids = set()
        for idx, raw in enumerate(data.get('configure') or []):
            step = cls._normalize_recipe_step(raw, raw.get('type'), f'configure[{idx}]')
            cls._check_recipe_step(
                step, declared_capabilities, seen_step_ids, f'configure[{idx}]', errors)
            configure.append(step)
            seen_step_ids.add(step['id'])
        for idx, raw in enumerate(data.get('verify') or []):
            step = cls._normalize_recipe_step(raw, 'verify', f'verify[{idx}]')
            cls._check_recipe_step(
                step, declared_capabilities, seen_step_ids, f'verify[{idx}]', errors)
            verify.append(step)
            seen_step_ids.add(step['id'])

        normalized = {
            'version': 1,
            'server': data.get('server') or None,
            'project': data.get('project') or None,
            'env_vars': manifest_env,
            'services': services,
            'domains': domains,
            'capabilities': declared_capabilities,
            'requires': cls._normalize_recipe_requirements(data.get('requires')),
            'configure': configure,
            'verify': verify,
        }
        return normalized, errors

    @classmethod
    def _normalize_recipe_step(cls, raw: Dict[str, Any], step_type: str,
                               prefix: str) -> Dict[str, Any]:
        """Normalize one closed-vocabulary Recipe step.

        ``params`` remains data, never executable code. The runtime resolves
        ``type:kind`` through RecipeStepRegistry and refuses unknown entries.
        """
        depends = cls._alias(raw, 'dependsOn', 'depends_on', default=[]) or []
        ttl = cls._alias(raw, 'ttlSeconds', 'ttl_seconds')
        input_spec = raw.get('input') if isinstance(raw.get('input'), dict) else None
        return {
            'id': raw.get('id'),
            'type': step_type,
            'kind': raw.get('kind'),
            'capability': f'{step_type}:{raw.get("kind")}',
            'title': raw.get('title') or raw.get('id'),
            'description': raw.get('description') or '',
            'depends_on': list(depends),
            'params': dict(raw.get('params') or {}),
            'ttl_seconds': int(ttl) if ttl else None,
            'input': ({
                'key': input_spec.get('key'),
                'label': input_spec.get('label'),
                'help': input_spec.get('help') or '',
                'url': input_spec.get('url'),
                'secret': input_spec.get('secret', True),
            } if input_spec else None),
        }

    @staticmethod
    def _check_recipe_step(step: Dict[str, Any], declared_capabilities: List[str],
                           seen_step_ids: set, prefix: str, errors: List[str]) -> None:
        step_id = step.get('id')
        if step_id in seen_step_ids:
            errors.append(f'{prefix}: duplicate recipe step id `{step_id}`')
        for dependency in step.get('depends_on') or []:
            if dependency not in seen_step_ids:
                errors.append(
                    f'{prefix}: dependency `{dependency}` must name an earlier recipe step')
        capability = step.get('capability')
        if capability not in declared_capabilities:
            errors.append(
                f'{prefix}: capability `{capability}` is used but not declared')
        if step.get('type') == 'handoff' and not step.get('input'):
            errors.append(f'{prefix}: handoff steps require an `input` declaration')
        if step.get('type') != 'handoff' and step.get('ttl_seconds') is not None:
            errors.append(f'{prefix}: `ttlSeconds` only applies to handoff steps')

    @classmethod
    def _normalize_recipe_requirements(cls, raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        return {
            'cpu_cores': cls._alias(raw, 'cpuCores', 'cpu_cores'),
            'memory_mb': cls._alias(raw, 'memoryMB', 'memory_mb'),
            'disk_gb': cls._alias(raw, 'diskGB', 'disk_gb'),
            'igpu': raw.get('igpu') or 'none',
            'gpu': raw.get('gpu') or 'none',
        }

    @classmethod
    def _normalize_service(cls, svc: Dict[str, Any], idx: int, errors: List[str]) -> Dict[str, Any]:
        name = svc.get('name')
        stype = svc.get('type')
        kind = 'database' if stype in DB_TYPES else 'app'
        prefix = f'services/{name or idx}'

        env_vars = [
            cls._normalize_env_var(v, f'{prefix}/env[{i}]', errors)
            for i, v in enumerate(svc.get('envVars') or [])
        ]

        disks = []
        raw_disks = svc.get('disks') or []
        # db services carry a single `disk`
        single_disk = svc.get('disk')
        if single_disk:
            raw_disks = list(raw_disks) + [single_disk]
        for d in raw_disks:
            disks.append(cls._normalize_disk(d))

        crons = svc.get('cron')
        if isinstance(crons, dict):
            crons = [crons]
        elif not isinstance(crons, list):
            crons = []
        crons = [
            {
                'name': c.get('name'),
                'schedule': c.get('schedule'),
                'command': c.get('command'),
            }
            for c in crons
            if isinstance(c, dict)
        ]

        ports = cls._normalize_ports(svc.get('ports'), prefix, errors)
        bootstrap = cls._normalize_bootstrap(svc.get('bootstrap'))
        image = svc.get('image')
        registry = svc.get('registry')
        host_requirements = cls._normalize_host_requirements(svc.get('hostRequirements')
                                                             or svc.get('host_requirements'))
        containers = cls._normalize_containers(svc, prefix, errors)

        # Dockerfile build: the service is built from a Dockerfile in the
        # repository (repo root = build context). Exactly one image source per
        # service — a declared path excludes BYO `image` and `containers`.
        dockerfile_path = cls._alias(svc, 'dockerfilePath', 'dockerfile_path')
        if dockerfile_path:
            if kind == 'database':
                errors.append(f'{prefix}: `dockerfilePath` does not apply to database services')
            if image:
                errors.append(f'{prefix}: `dockerfilePath` and `image` are mutually exclusive '
                              '— build from source or bring an image, not both')
            if containers:
                errors.append(f'{prefix}: `dockerfilePath` cannot be combined with `containers` '
                              '— unit containers declare ready-made images')
            if not cls._safe_relative_path(dockerfile_path):
                errors.append(f'{prefix}: `dockerfilePath` must be a relative path inside the '
                              'repository (no leading `/`, no `..`)')

        # Monorepo auto-deploy controls (Render's rootDir / buildFilter).
        from app.services.deploy_control_service import validate_build_filter, validate_root_dir
        root_dir, root_err = validate_root_dir(cls._alias(svc, 'rootDir', 'root_dir'))
        if root_err:
            errors.append(f'{prefix}: `rootDir` must be a relative path inside the repository')
        raw_filter = cls._alias(svc, 'buildFilter', 'build_filter')
        build_filter, filter_err = validate_build_filter(raw_filter)
        if filter_err:
            errors.append(f'{prefix}: `buildFilter` is invalid ({filter_err})')

        return {
            'name': name,
            'type': stype,
            'kind': kind,
            'root_dir': root_dir,
            'build_filter': build_filter,
            'app_type': APP_TYPE_MAP.get(stype) if kind == 'app' else None,
            'db_engine': DB_ENGINE_MAP.get(stype) if kind == 'database' else None,
            'runtime': svc.get('runtime'),
            'build_command': cls._alias(svc, 'buildCommand', 'build_command'),
            'start_command': cls._alias(svc, 'startCommand', 'start_command'),
            'dockerfile_path': dockerfile_path or None,
            'port': svc.get('port'),
            'ports': ports,
            'bootstrap': bootstrap,
            'image': image,
            'registry': registry,
            'host_requirements': host_requirements,
            'containers': containers,
            'healthcheck_path': cls._alias(svc, 'healthCheckPath', 'healthcheck_path'),
            'auto_deploy': bool(cls._alias(svc, 'autoDeploy', 'auto_deploy', default=False)),
            'engine_version': cls._stringify(svc.get('version')),
            'server': svc.get('server') or None,
            'cpu': svc.get('cpu'),
            'memory': svc.get('memory'),
            'env_vars': env_vars,
            'disks': disks,
            'crons': crons,
        }

    @classmethod
    def _normalize_ports(cls, raw: Any, prefix: str, errors: List[str]) -> List[Dict[str, Any]]:
        """Typed L4 publishes. Legacy scalar ``port`` is unaffected —
        it keeps its HTTP/nginx semantics. ``ports`` are raw tcp/udp publishes
        (the appliance escape hatch): NULL/[] means "no raw ports declared".
        """
        if not isinstance(raw, list):
            return []
        ports: List[Dict[str, Any]] = []
        seen: set = set()
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                errors.append(f'{prefix}/ports[{i}]: must be a mapping')
                continue
            host_port = entry.get('port')
            if not isinstance(host_port, int):
                errors.append(f'{prefix}/ports[{i}]: `port` is required')
                continue
            container_port = cls._alias(entry, 'containerPort', 'container_port') or host_port
            protocol = (entry.get('protocol') or 'tcp').lower()
            expose = (entry.get('expose') or 'public').lower()
            key = (host_port, protocol)
            if key in seen:
                errors.append(f'{prefix}/ports[{i}]: duplicate publish {host_port}/{protocol}')
                continue
            seen.add(key)
            ports.append({
                'host_port': host_port,
                'container_port': container_port,
                'protocol': protocol,
                'expose': expose,
            })
        return ports

    @classmethod
    def _normalize_bootstrap(cls, raw: Any) -> Optional[Dict[str, Any]]:
        """First-boot bootstrap: ``{command, timeoutSeconds?}``."""
        if not isinstance(raw, dict):
            return None
        command = raw.get('command')
        if not command:
            return None
        timeout = cls._alias(raw, 'timeoutSeconds', 'timeout_seconds')
        return {
            'command': command,
            'timeout_seconds': int(timeout) if timeout else None,
        }

    @classmethod
    def _normalize_host_requirements(cls, raw: Any) -> Optional[Dict[str, Any]]:
        """hostRequirements: privileged/capAdd/sysctls/devices/kernelModules."""
        if not isinstance(raw, dict):
            return None
        hr = {
            'privileged': bool(raw.get('privileged')),
            'cap_add': list(cls._alias(raw, 'capAdd', 'cap_add') or []),
            'sysctls': dict(raw.get('sysctls') or {}),
            'devices': list(raw.get('devices') or []),
            'kernel_modules': list(cls._alias(raw, 'kernelModules', 'kernel_modules') or []),
        }
        # collapse to None when nothing is actually requested
        if (not hr['privileged'] and not hr['cap_add'] and not hr['sysctls']
                and not hr['devices'] and not hr['kernel_modules']):
            return None
        return hr

    @classmethod
    def _normalize_healthcheck(cls, raw: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        cmd = raw.get('cmd')
        http_path = cls._alias(raw, 'httpPath', 'http_path')
        if not cmd and not http_path:
            return None
        return {
            'cmd': cmd,
            'http_path': http_path,
            'interval': raw.get('interval'),
            'timeout': raw.get('timeout'),
            'retries': raw.get('retries'),
        }

    @classmethod
    def _normalize_depends_on(cls, raw: Any) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not isinstance(raw, list):
            return out
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get('service'):
                continue
            out.append({'service': entry['service'],
                        'condition': entry.get('condition') or 'started'})
        return out

    @classmethod
    def _normalize_containers(cls, svc: Dict[str, Any], prefix: str,
                              errors: List[str]) -> List[Dict[str, Any]]:
        """A multi-container unit. ``containers`` is a
        mapping of name -> container spec, rendered as ONE compose project.

        Mutually exclusive with the buildpack keys; dependsOn must reference a
        sibling container and must be acyclic.
        """
        raw = svc.get('containers')
        if not isinstance(raw, dict) or not raw:
            return []

        # mutual exclusion with the single-image build path
        for key in ('buildCommand', 'build_command', 'startCommand', 'start_command',
                    'runtime'):
            if svc.get(key):
                errors.append(f'{prefix}: `containers` cannot be combined with `{key}` '
                              f'— a unit declares its own container images')
                break

        containers: List[Dict[str, Any]] = []
        names = list(raw.keys())
        for cname in names:
            spec = raw[cname] or {}
            cprefix = f'{prefix}/containers/{cname}'
            containers.append({
                'name': cname,
                'image': spec.get('image'),
                'registry': spec.get('registry'),
                'ports': cls._normalize_ports(spec.get('ports'), cprefix, errors),
                'disks': [cls._normalize_disk(d) for d in (spec.get('disks') or [])],
                'env_vars': [cls._normalize_env_var(v, f'{cprefix}/env[{i}]', errors)
                             for i, v in enumerate(spec.get('envVars')
                                                   or spec.get('env_vars') or [])],
                'bootstrap': cls._normalize_bootstrap(spec.get('bootstrap')),
                'host_requirements': cls._normalize_host_requirements(
                    spec.get('hostRequirements') or spec.get('host_requirements')),
                'health_check': cls._normalize_healthcheck(
                    spec.get('healthCheck') or spec.get('health_check')),
                'depends_on': cls._normalize_depends_on(
                    spec.get('dependsOn') or spec.get('depends_on')),
            })

        name_set = set(names)
        graph: Dict[str, List[str]] = {}
        for c in containers:
            deps = []
            for d in c['depends_on']:
                if d['service'] not in name_set:
                    errors.append(f'{prefix}/containers/{c["name"]}: dependsOn unknown '
                                  f'container `{d["service"]}`')
                else:
                    deps.append(d['service'])
            graph[c['name']] = deps

        cycle = cls._first_cycle(graph)
        if cycle:
            errors.append(f'{prefix}: dependsOn cycle detected ({" -> ".join(cycle)})')

        return containers

    @staticmethod
    def _first_cycle(graph: Dict[str, List[str]]) -> Optional[List[str]]:
        """DFS back-edge detection; returns a representative cycle path or None."""
        WHITE, GREY, BLACK = 0, 1, 2
        color = {n: WHITE for n in graph}
        stack: List[str] = []

        def visit(node: str) -> Optional[List[str]]:
            color[node] = GREY
            stack.append(node)
            for nxt in graph.get(node, []):
                if color.get(nxt) == GREY:
                    return stack[stack.index(nxt):] + [nxt]
                if color.get(nxt) == WHITE:
                    found = visit(nxt)
                    if found:
                        return found
            stack.pop()
            color[node] = BLACK
            return None

        for n in graph:
            if color[n] == WHITE:
                found = visit(n)
                if found:
                    return found
        return None

    @classmethod
    def _normalize_env_var(cls, var: Dict[str, Any], where: str, errors: List[str]) -> Dict[str, Any]:
        key = var.get('key')
        if key and not _KEY_RE.match(key):
            errors.append(f'{where}: invalid env key `{key}`')

        from_secret = cls._alias(var, 'fromSecret', 'from_secret')
        from_service = cls._alias(var, 'fromService', 'from_service')
        from_server = cls._alias(var, 'fromServer', 'from_server')
        has_value = 'value' in var and var.get('value') is not None
        generate = bool(var.get('generate'))

        sources = [s for s in (has_value, bool(from_secret), bool(from_service),
                               generate, bool(from_server)) if s]
        if len(sources) > 1:
            errors.append(
                f'{where}: env `{key}` must declare exactly one of '
                'value/fromSecret/fromService/fromServer/generate'
            )

        source = 'placeholder'
        service_ref = None
        server_ref = None
        if has_value:
            source = 'value'
        elif from_secret:
            source = 'secret'
        elif generate:
            source = 'generate'
        elif isinstance(from_service, dict):
            source = 'service'
            service_ref = {
                'name': from_service.get('name'),
                'property': from_service.get('property'),
            }
        elif isinstance(from_server, dict):
            source = 'server'
            server_ref = {'property': from_server.get('property') or 'publicIp'}

        return {
            'key': key,
            'source': source,
            'value': var.get('value') if has_value else None,
            'secret_name': from_secret or None,
            'service_ref': service_ref,
            'server_ref': server_ref,
            'secret': source in ('secret', 'generate') or bool(var.get('secret')),
        }

    @classmethod
    def _normalize_disk(cls, disk: Dict[str, Any]) -> Dict[str, Any]:
        backup = disk.get('backup') if isinstance(disk.get('backup'), dict) else None
        return {
            'name': disk.get('name'),
            'mount_path': cls._alias(disk, 'mountPath', 'mount_path'),
            'size': cls._stringify(disk.get('size')),
            'backup': cls._normalize_backup(backup) if backup else None,
        }

    @staticmethod
    def _normalize_backup(backup: Dict[str, Any]) -> Dict[str, Any]:
        return {
            'schedule': backup.get('schedule') or 'daily',
            'retain': backup.get('retain') or 7,
        }

    @classmethod
    def _normalize_domain(cls, dom: Dict[str, Any]) -> Dict[str, Any]:
        ssl = dom.get('ssl')
        if isinstance(ssl, bool):
            ssl_mode = 'auto' if ssl else 'off'
        elif ssl in ('auto', 'on'):
            ssl_mode = 'auto'
        elif ssl == 'off':
            ssl_mode = 'off'
        else:
            ssl_mode = 'auto'  # default best-effort
        return {
            'host': dom.get('host'),
            'service': dom.get('service') or None,
            'ssl': ssl_mode,
        }

    # -- wizard/summary -----------------------------------------------------

    @classmethod
    def summarize(cls, normalized: Dict[str, Any]) -> Dict[str, Any]:
        """A compact, UI-friendly summary of a normalized manifest."""
        services = normalized.get('services', [])
        env_required = []
        for svc in services:
            for var in svc['env_vars']:
                if var['source'] in ('secret', 'placeholder'):
                    env_required.append({'service': svc['name'], 'key': var['key'],
                                         'source': var['source']})
        return {
            'version': 1,
            'server': normalized.get('server'),
            'project': normalized.get('project'),
            'service_count': len(services),
            'services': [
                {
                    'name': s['name'],
                    'type': s['type'],
                    'kind': s['kind'],
                    'port': s.get('port'),
                    'auto_deploy': s['auto_deploy'],
                    'disk_count': len(s['disks']),
                    'env_count': len(s['env_vars']),
                }
                for s in services
            ],
            'databases': [s['name'] for s in services if s['kind'] == 'database'],
            'domains': [{'host': d['host'], 'service': d['service'], 'ssl': d['ssl']}
                        for d in normalized['domains']],
            'env_required': env_required,
            'recipe': {
                'capabilities': list(normalized.get('capabilities') or []),
                'requirements': dict(normalized.get('requires') or {}),
                'configure_steps': len(normalized.get('configure') or []),
                'verify_steps': len(normalized.get('verify') or []),
                'handoffs': [
                    {'id': step['id'], 'title': step['title'],
                     'ttl_seconds': step.get('ttl_seconds')}
                    for step in normalized.get('configure') or []
                    if step.get('type') == 'handoff'
                ],
            },
        }

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _safe_relative_path(path: Any) -> bool:
        """True for a repo-relative path with no traversal — rejects absolute
        paths (POSIX or drive-letter) and any `..` segment."""
        if not isinstance(path, str) or not path.strip():
            return False
        p = path.replace('\\', '/')
        if p.startswith('/') or re.match(r'^[A-Za-z]:', p):
            return False
        return not any(part == '..' for part in p.split('/'))

    @staticmethod
    def _alias(obj: Dict[str, Any], camel: str, snake: str, default: Any = None) -> Any:
        if camel in obj and obj[camel] is not None:
            return obj[camel]
        if snake in obj and obj[snake] is not None:
            return obj[snake]
        return default

    @staticmethod
    def _stringify(value: Any) -> Optional[str]:
        if value is None:
            return None
        return str(value)
