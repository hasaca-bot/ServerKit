"""Hosting plans and per-tenant quotas.

The single seam every create path (panel API, MCP, manifests, previews) asks
before it adds a billable/limited resource:

    from app.services.plan_quota_service import PlanQuotaService
    PlanQuotaService.check(user, 'apps')            # raises QuotaExceededError
    PlanQuotaService.check(user, 'databases', add=2)
    PlanQuotaService.limits_for(user)               # dict of the user's limits
    PlanQuotaService.usage_for(user)                # dict of current usage

Resource kinds: 'apps', 'databases', 'cron_jobs', 'custom_domains',
'previews', 'servers', 'mcp_keys'. Admins are never limited.

This is the contract stub: it allows everything until plans are wired in.
"""

from app.exceptions import ApplicationError


class QuotaExceededError(ApplicationError):
    status_code = 402
    code = 'quota_exceeded'
    default_message = 'Plan limit reached'


RESOURCE_KINDS = (
    'apps', 'databases', 'cron_jobs', 'custom_domains', 'previews', 'servers', 'mcp_keys',
)


class PlanQuotaService:

    @staticmethod
    def limits_for(user):
        """Return {kind: limit} for the user's plan; None means unlimited."""
        return {kind: None for kind in RESOURCE_KINDS}

    @staticmethod
    def usage_for(user):
        """Return {kind: current count} for resources the user owns."""
        return {kind: 0 for kind in RESOURCE_KINDS}

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
