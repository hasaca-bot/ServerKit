"""Hosting plans: per-tenant resource limits.

A user's plan is ``User.plan_id``; NULL means the panel default plan
(setting ``default_plan_slug``, ``hobby`` unless changed). Admins are never
limited. Limits are read through ``app.services.plan_quota_service``.
"""
from datetime import datetime

from app import db


# Seeded by migration 112 and lazily by PlanQuotaService when the table is
# empty (test databases are built with create_all, not migrations). Keep the
# two in step.
DEFAULT_PLANS = (
    {'slug': 'hobby', 'name': 'Hobby', 'max_apps': 3, 'max_databases': 1,
     'max_cron_jobs': 2, 'max_custom_domains': 0, 'max_previews': 0,
     'max_servers': 0, 'max_mcp_keys': 1, 'max_cpu': 0.25,
     'max_memory_mb': 256, 'sleep_after_minutes': 15},
    {'slug': 'pro', 'name': 'Pro', 'max_apps': 25, 'max_databases': 10,
     'max_cron_jobs': 20, 'max_custom_domains': 10, 'max_previews': 5,
     'max_servers': 1, 'max_mcp_keys': 5, 'max_cpu': 2.0,
     'max_memory_mb': 4096, 'sleep_after_minutes': None},
    {'slug': 'team', 'name': 'Team', 'max_apps': 100, 'max_databases': 30,
     'max_cron_jobs': 50, 'max_custom_domains': 50, 'max_previews': 20,
     'max_servers': 5, 'max_mcp_keys': 20, 'max_cpu': 4.0,
     'max_memory_mb': 8192, 'sleep_after_minutes': None},
)


class Plan(db.Model):
    __tablename__ = 'plans'

    # Count limits, keyed by PlanQuotaService resource kind.
    COUNT_LIMITS = {
        'apps': 'max_apps',
        'databases': 'max_databases',
        'cron_jobs': 'max_cron_jobs',
        'custom_domains': 'max_custom_domains',
        'previews': 'max_previews',
        'servers': 'max_servers',
        'mcp_keys': 'max_mcp_keys',
    }
    EDITABLE_FIELDS = ('name', *COUNT_LIMITS.values(), 'max_cpu', 'max_memory_mb',
                       'sleep_after_minutes')

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(40), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    # NULL on any limit = unlimited.
    max_apps = db.Column(db.Integer, nullable=True)
    max_databases = db.Column(db.Integer, nullable=True)
    max_cron_jobs = db.Column(db.Integer, nullable=True)
    max_custom_domains = db.Column(db.Integer, nullable=True)
    max_previews = db.Column(db.Integer, nullable=True)
    max_servers = db.Column(db.Integer, nullable=True)
    max_mcp_keys = db.Column(db.Integer, nullable=True)
    max_cpu = db.Column(db.Float, nullable=True)          # cores per service
    max_memory_mb = db.Column(db.Integer, nullable=True)  # MB per service
    sleep_after_minutes = db.Column(db.Integer, nullable=True)  # NULL = never sleeps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def limits(self):
        return {kind: getattr(self, col) for kind, col in self.COUNT_LIMITS.items()}

    def to_dict(self):
        return {
            'id': self.id,
            'slug': self.slug,
            'name': self.name,
            'limits': self.limits(),
            'max_cpu': self.max_cpu,
            'max_memory_mb': self.max_memory_mb,
            'sleep_after_minutes': self.sleep_after_minutes,
        }

    def __repr__(self):
        return f'<Plan {self.slug}>'
