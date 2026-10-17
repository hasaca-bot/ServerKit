"""Hosting plans and per-tenant quotas.

The single seam every create path (panel API, MCP, manifests, previews) asks
before it adds a billable/limited resource:

    from app.services.plan_quota_service import PlanQuotaService
    PlanQuotaService.check(user, 'apps')            # raises QuotaExceededError
    PlanQuotaService.check(user, 'databases', add=2)
    PlanQuotaService.limits_for(user)               # dict of the user's limits
    PlanQuotaService.usage_for(user)                # dict of current usage
    PlanQuotaService.check_resources(user, cpu, memory_mb)  # per-service caps

Resource kinds: 'apps', 'databases', 'cron_jobs', 'custom_domains',
'previews', 'servers', 'mcp_keys'. Admins are never limited.

A user's plan is ``User.plan_id``; NULL means the panel default plan, the
``default_plan_slug`` setting (``hobby`` unless changed). Usage counts only
what the user OWNS: an app shared with them through a grant counts against
its owner.
"""

import json
import logging
import os
import re
from datetime import datetime

from app.exceptions import ApplicationError

logger = logging.getLogger(__name__)


class QuotaExceededError(ApplicationError):
    status_code = 402
    code = 'quota_exceeded'
    default_message = 'Plan limit reached'


RESOURCE_KINDS = (
    'apps', 'databases', 'cron_jobs', 'custom_domains', 'previews', 'servers', 'mcp_keys',
)

DEFAULT_PLAN_SETTING = 'default_plan_slug'
FALLBACK_PLAN_SLUG = 'hobby'

_MEMORY_RE = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*([bkmg]?)\s*$', re.IGNORECASE)
_MEMORY_FACTOR_MB = {'b': 1 / (1024 * 1024), 'k': 1 / 1024, 'm': 1, 'g': 1024, '': 1 / (1024 * 1024)}


def memory_to_mb(value):
    """'512m' / '2g' / '1048576' (bytes, docker's unitless meaning) -> MB float, or None."""
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _MEMORY_RE.match(str(value))
    if not match:
        return None
    return float(match.group(1)) * _MEMORY_FACTOR_MB[match.group(2).lower()]


def _is_unlimited(user):
    return user is None or bool(getattr(user, 'is_admin', False))


class PlanQuotaService:

    # ------------------------------------------------------------------ plans

    @staticmethod
    def ensure_default_plans():
        """Seed hobby/pro/team when the table is empty (migration 112 seeds
        real installs; create_all-built databases start empty)."""
        from app import db
        from app.models.plan import Plan, DEFAULT_PLANS
        if Plan.query.first() is not None:
            return
        for spec in DEFAULT_PLANS:
            db.session.add(Plan(**spec))
        db.session.commit()

    @classmethod
    def list_plans(cls):
        from app.models.plan import Plan
        cls.ensure_default_plans()
        return Plan.query.order_by(Plan.id).all()

    @classmethod
    def get_plan(cls, slug):
        from app.models.plan import Plan
        cls.ensure_default_plans()
        return Plan.query.filter_by(slug=slug).first()

    @classmethod
    def default_plan(cls):
        from app.services.settings_service import SettingsService
        slug = SettingsService.get(DEFAULT_PLAN_SETTING, FALLBACK_PLAN_SLUG) or FALLBACK_PLAN_SLUG
        return cls.get_plan(slug) or cls.get_plan(FALLBACK_PLAN_SLUG)

    @classmethod
    def plan_for(cls, user):
        """The Plan that limits *user*: their assigned plan, else the default.
        None for admins (never limited) and anonymous callers."""
        if _is_unlimited(user):
            return None
        from app.models.plan import Plan
        if getattr(user, 'plan_id', None):
            plan = Plan.query.get(user.plan_id)
            if plan is not None:
                return plan
        return cls.default_plan()

    @staticmethod
    def update_plan(plan, data):
        """Apply admin edits to *plan*. Limits accept a non-negative number or
        null (unlimited). Raises ValidationError on bad input."""
        from app import db
        from app.exceptions import ValidationError
        from app.models.plan import Plan
        for field in Plan.EDITABLE_FIELDS:
            if field not in data:
                continue
            value = data[field]
            if field == 'name':
                value = (value or '').strip()
                if not value:
                    raise ValidationError('Plan name cannot be empty')
            elif value is not None:
                try:
                    value = float(value) if field == 'max_cpu' else int(value)
                except (TypeError, ValueError):
                    raise ValidationError(f'{field} must be a number or null')
                if value < 0:
                    raise ValidationError(f'{field} cannot be negative')
            setattr(plan, field, value)
        db.session.commit()
        return plan

    @classmethod
    def assign_plan(cls, user, slug):
        """Assign plan *slug* to *user*; None/'' clears it back to the default."""
        from app import db
        from app.exceptions import ValidationError
        if slug in (None, ''):
            user.plan_id = None
        else:
            plan = cls.get_plan(slug)
            if plan is None:
                raise ValidationError(f'Unknown plan: {slug}')
            user.plan_id = plan.id
        db.session.commit()
        return user

    # ------------------------------------------------------------- contract

    @classmethod
    def limits_for(cls, user):
        """Return {kind: limit} for the user's plan; None means unlimited."""
        plan = cls.plan_for(user)
        if plan is None:
            return {kind: None for kind in RESOURCE_KINDS}
        return plan.limits()

    @classmethod
    def usage_for(cls, user):
        """Return {kind: current count} for resources the user owns."""
        usage = {kind: 0 for kind in RESOURCE_KINDS}
        if user is None or getattr(user, 'id', None) is None:
            return usage
        from app.models import Application
        from app.services import database_engine_service

        apps = Application.query_active().filter_by(user_id=user.id).all()
        app_ids = [a.id for a in apps]
        engine_ids = set()
        for app in apps:
            try:
                if database_engine_service.is_engine_app(app):
                    engine_ids.add(app.id)
            except Exception:  # template lookup is best-effort
                pass
        usage['apps'] = len(apps) - len(engine_ids)
        usage['databases'] = len(engine_ids) + cls._managed_db_count(app_ids)
        usage['cron_jobs'] = cls._cron_count(app_ids)
        usage['custom_domains'] = cls._custom_domain_count(app_ids)
        usage['previews'] = cls._preview_count(app_ids)
        usage['servers'] = cls._server_count(user.id)
        usage['mcp_keys'] = cls._api_key_count(user.id)
        return usage

    @classmethod
    def check(cls, user, kind, add=1):
        """Raise QuotaExceededError if adding `add` of `kind` would exceed the plan."""
        if kind not in RESOURCE_KINDS:
            raise ValueError(f'unknown quota kind: {kind}')
        if user is None or getattr(user, 'is_admin', False):
            return
        limit = cls.limits_for(user).get(kind)
        if limit is None:
            return
        used = cls.usage_for(user).get(kind, 0)
        if used + add > limit:
            raise QuotaExceededError(
                f'Your plan allows {limit} {kind.replace("_", " ")}; you have {used}.',
                details={'kind': kind, 'limit': limit, 'used': used},
            )

    @classmethod
    def check_resources(cls, user, cpu=None, memory_mb=None):
        """Raise QuotaExceededError when a per-service CPU (cores) or memory
        (MB) value exceeds the plan cap. None means "not being set"."""
        plan = cls.plan_for(user)
        if plan is None:
            return
        if cpu is not None and plan.max_cpu is not None and float(cpu) > plan.max_cpu:
            raise QuotaExceededError(
                f'Your plan allows at most {plan.max_cpu:g} CPU per service.',
                details={'kind': 'cpu', 'limit': plan.max_cpu, 'requested': float(cpu)},
            )
        if (memory_mb is not None and plan.max_memory_mb is not None
                and float(memory_mb) > plan.max_memory_mb):
            raise QuotaExceededError(
                f'Your plan allows at most {plan.max_memory_mb} MB memory per service.',
                details={'kind': 'memory_mb', 'limit': plan.max_memory_mb,
                         'requested': float(memory_mb)},
            )

    @classmethod
    def summary_for(cls, user):
        """Plan, limits and usage in one payload (GET /api/v1/plan, MCP get_usage)."""
        plan = cls.plan_for(user)
        return {
            'unlimited': plan is None,
            'plan': plan.to_dict() if plan else None,
            'limits': cls.limits_for(user),
            'usage': cls.usage_for(user),
        }

    @classmethod
    def apply_plan_to_new_app(cls, user, app):
        """Stamp the owner's plan defaults on a freshly created app: CPU/memory
        caps when the app has none, and the idle-sleep policy. Best-effort."""
        plan = cls.plan_for(user)
        if plan is None or app is None or app.id is None:
            return
        try:
            from app import db
            changed = False
            if plan.max_cpu is not None and not app.cpu_limit:
                app.cpu_limit = f'{plan.max_cpu:g}'
                changed = True
            if plan.max_memory_mb is not None and not app.memory_limit:
                app.memory_limit = f'{plan.max_memory_mb}m'
                changed = True
            if changed:
                db.session.commit()
            if plan.sleep_after_minutes:
                from app.services.container_sleep_service import ContainerSleepService
                ContainerSleepService.set_policy(
                    app.id, enabled=True, idle_timeout_minutes=plan.sleep_after_minutes)
        except Exception as exc:
            logger.warning('applying plan defaults to app %s failed: %s', app.id, exc)

    # ------------------------------------------------------------ counters

    @staticmethod
    def _managed_db_count(app_ids):
        if not app_ids:
            return 0
        from app.models import ManagedDatabase
        return ManagedDatabase.query.filter(
            ManagedDatabase.owner_application_id.in_(app_ids)).count()

    @staticmethod
    def _cron_count(app_ids):
        if not app_ids:
            return 0
        from app.services import cron_service
        path = getattr(cron_service, 'JOBS_FILE', None)
        if not path or not os.path.exists(path):
            return 0
        try:
            with open(path, 'r') as handle:
                jobs = (json.load(handle) or {}).get('jobs') or {}
        except (OSError, ValueError):
            return 0
        wanted = {str(i) for i in app_ids}
        return sum(1 for job in jobs.values()
                   if isinstance(job, dict) and str(job.get('application_id')) in wanted)

    @staticmethod
    def _custom_domain_count(app_ids):
        if not app_ids:
            return 0
        from app.models import Domain
        from app.services.site_domain_service import SiteDomainService
        names = [d.name for d in Domain.query_active().filter(
            Domain.application_id.in_(app_ids)).all()]
        return sum(1 for name in names if not SiteDomainService.covers(name))

    @staticmethod
    def _preview_count(app_ids):
        if not app_ids:
            return 0
        from app.models import ApplicationPreview
        return ApplicationPreview.query.filter(
            ApplicationPreview.application_id.in_(app_ids),
            ApplicationPreview.deleted_at.is_(None),
            ApplicationPreview.status != ApplicationPreview.STATUS_DESTROYED,
        ).count()

    @staticmethod
    def _server_count(user_id):
        from app.models import Server
        return Server.query.filter_by(owner_id=user_id).count()

    @staticmethod
    def _api_key_count(user_id):
        from app.models import ApiKey
        now = datetime.utcnow()
        from app import db
        return ApiKey.query.filter(
            ApiKey.user_id == user_id,
            ApiKey.is_active.is_(True),
            ApiKey.revoked_at.is_(None),
            db.or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now),
        ).count()


def is_custom_domain(host):
    """True when *host* is not under any registered managed base domain."""
    from app.services.site_domain_service import SiteDomainService
    return not SiteDomainService.covers(host)


def quota_owner(caller, app):
    """Whose plan a change to *app* counts against: None (unlimited) when the
    caller is an admin, otherwise the app's owner."""
    if caller is not None and getattr(caller, 'is_admin', False):
        return None
    if app is None:
        return caller
    from app.models import User
    return User.query.get(app.user_id) if app.user_id else caller
