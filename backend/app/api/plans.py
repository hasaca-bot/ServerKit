"""Hosting plans: the caller's plan and usage, and admin plan management.

``GET /api/v1/plan`` is API-key capable on purpose (``auth_required``): the
MCP endpoint and API clients read a tenant's limits and usage with the same
key they deploy with. Everything else is admin-only.
"""
from flask import Blueprint, jsonify

from app.api._request import json_object
from app.exceptions import NotFoundError, ValidationError, not_found
from app.middleware.rbac import admin_required, auth_required, get_current_user
from app.services.plan_quota_service import DEFAULT_PLAN_SETTING, PlanQuotaService

plans_bp = Blueprint('plans', __name__)


@plans_bp.route('/plan', methods=['GET'])
@auth_required()
def get_my_plan():
    """The caller's plan, its limits and the caller's current usage."""
    return jsonify(PlanQuotaService.summary_for(get_current_user())), 200


@plans_bp.route('/plans', methods=['GET'])
@admin_required
def list_plans():
    default = PlanQuotaService.default_plan()
    return jsonify({
        'plans': [p.to_dict() for p in PlanQuotaService.list_plans()],
        'default_plan_slug': default.slug if default else None,
    }), 200


@plans_bp.route('/plans/<slug>', methods=['PUT'])
@admin_required
def update_plan(slug):
    plan = PlanQuotaService.get_plan(slug)
    if plan is None:
        raise NotFoundError('Plan not found')
    PlanQuotaService.update_plan(plan, json_object())
    return jsonify({'plan': plan.to_dict()}), 200


@plans_bp.route('/plans/settings/default', methods=['PUT'])
@admin_required
def set_default_plan():
    """Choose the plan that applies to users with none assigned."""
    from app.services.settings_service import SettingsService
    slug = (json_object().get('slug') or '').strip()
    if PlanQuotaService.get_plan(slug) is None:
        raise ValidationError(f'Unknown plan: {slug}')
    SettingsService.set(DEFAULT_PLAN_SETTING, slug, user_id=get_current_user().id)
    return jsonify({'default_plan_slug': slug}), 200


@plans_bp.route('/plans/users/<int:user_id>', methods=['PUT'])
@admin_required
def assign_user_plan(user_id):
    """Assign a plan to a user: ``{"plan": "<slug>"}``; null resets to the default."""
    from app.models import User
    user = User.query.get(user_id)
    if user is None:
        raise not_found('user')
    PlanQuotaService.assign_plan(user, json_object().get('plan'))
    return jsonify({'user_id': user.id, 'plan_id': user.plan_id,
                    **PlanQuotaService.summary_for(user)}), 200
