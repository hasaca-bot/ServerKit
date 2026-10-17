"""API key scope enforcement middleware.

This complements RBAC: RBAC governs *who* the user is (role/permissions),
whereas API-key scopes govern *what a given key is allowed to do*. The two are
orthogonal — a key is always owned by a user, and the user's RBAC still applies
on top of the key's scopes.

The ``require_scope`` decorator is a **pass-through for JWT/session requests**:
only requests authenticated with an ``X-API-Key`` header (i.e. ``g.api_key`` is
set by :mod:`app.middleware.api_key_auth`) are scope-checked. JWT users are
governed by RBAC decorators instead. This lets the same endpoint be safely
shared between the web UI (JWT) and programmatic clients (API keys).
"""
from functools import wraps

from flask import current_app, g, jsonify, request


# ---------------------------------------------------------------------------
# Canonical scope catalog
# ---------------------------------------------------------------------------
# Each entry: {key, label, group, description}. ``'*'`` (full access) is handled
# implicitly by ApiKey.has_scope() and surfaced in the UI as a master toggle, so
# it is not listed as a row here — see FULL_ACCESS_SCOPE.
FULL_ACCESS_SCOPE = '*'

SCOPES = [
    # Coarse-grained
    {'key': 'read', 'label': 'Read (all)', 'group': 'General',
     'description': 'Read-only access across all resources.'},
    {'key': 'write', 'label': 'Write (all)', 'group': 'General',
     'description': 'Create and modify access across all resources.'},

    # Applications
    {'key': 'apps:read', 'label': 'View applications', 'group': 'Applications',
     'description': 'List and inspect managed applications.'},
    {'key': 'apps:write', 'label': 'Manage applications', 'group': 'Applications',
     'description': 'Create, update, start/stop, and delete applications.'},
    {'key': 'apps:deploy', 'label': 'Deploy applications', 'group': 'Applications',
     'description': 'Trigger builds and deployments.'},

    # Databases
    {'key': 'databases:read', 'label': 'View databases', 'group': 'Databases',
     'description': 'List databases and inspect schemas.'},
    {'key': 'databases:write', 'label': 'Manage databases', 'group': 'Databases',
     'description': 'Create, alter, and drop databases and users.'},

    # Domains
    {'key': 'domains:read', 'label': 'View domains', 'group': 'Domains',
     'description': 'List domains and SSL status.'},
    {'key': 'domains:write', 'label': 'Manage domains', 'group': 'Domains',
     'description': 'Attach domains and manage certificates.'},

    # DNS
    {'key': 'dns:read', 'label': 'View DNS records', 'group': 'DNS',
     'description': 'List DNS zones and records.'},
    {'key': 'dns:write', 'label': 'Manage DNS records', 'group': 'DNS',
     'description': 'Create, update, and delete DNS records.'},

    # Backups
    {'key': 'backups:read', 'label': 'View backups', 'group': 'Backups',
     'description': 'List backup policies and runs.'},
    {'key': 'backups:write', 'label': 'Manage backups', 'group': 'Backups',
     'description': 'Create policies, trigger backups, and restore.'},

    # Servers
    {'key': 'servers:read', 'label': 'View servers', 'group': 'Servers',
     'description': 'List servers, agents, and metrics.'},
    {'key': 'servers:admin', 'label': 'Administer servers', 'group': 'Servers',
     'description': 'Run privileged server and agent operations.'},

    # Secrets
    {'key': 'secrets:read', 'label': 'Reveal secrets', 'group': 'Secrets',
     'description': 'Read unmasked secret values in API responses.'},

    # MCP
    {'key': 'mcp', 'label': 'MCP server', 'group': 'MCP',
     'description': 'Use the MCP endpoint (/api/v1/mcp) for AI assistants. Tools act only '
                    'on your own services; your role decides read-only or write.'},
]

# Set of valid catalog keys (excludes the implicit full-access scope).
SCOPE_KEYS = {entry['key'] for entry in SCOPES}


def enforce_request_scope(api_key):
    """Fail closed for restricted keys when a route has no scope policy.

    Role checks alone cannot constrain an administrator-owned read-only key.
    Legacy empty scope lists, like '*', retain their existing full-access
    meaning; both still pass through the route's authentication/RBAC policy.
    """
    assigned = api_key.get_scopes()
    if not assigned or FULL_ACCESS_SCOPE in assigned:
        return None
    view = current_app.view_functions.get(request.endpoint)
    required = getattr(view, '_sk_api_scopes', ()) if view else ()
    if not required:
        return jsonify({'error': 'This endpoint does not allow restricted API keys'}), 403
    for scope in required:
        if not api_key.has_scope(scope):
            return jsonify({'error': f'Insufficient API key scope: {scope}'}), 403
    return None


def require_scope(*scopes):
    """Enforce that an API-key request carries ALL of ``scopes``.

    Semantics:
      * If the request is **API-key authenticated** (``g.api_key`` is set), the
        key must satisfy every listed scope via ``ApiKey.has_scope()`` (which
        honors ``'*'`` full access and ``'resource:*'`` wildcards). The first
        unsatisfied scope yields ``403``.
      * If the request is **not** API-key authenticated (JWT/session user), the
        decorator is a pass-through — those users are governed by RBAC.

    This decorator does not itself require authentication; compose it with an
    auth decorator (``@jwt_required()``, ``@auth_required()``, etc.) when a route
    needs to be authenticated.

    Usage::

        @bp.route('/things')
        @auth_required()
        @require_scope('apps:read')
        def list_things():
            ...
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            api_key = getattr(g, 'api_key', None)
            if api_key is not None:
                for scope in scopes:
                    if not api_key.has_scope(scope):
                        return jsonify({
                            'error': f'Insufficient API key scope: {scope}'
                        }), 403
            # JWT/session requests pass through (governed by RBAC).
            return fn(*args, **kwargs)
        # functools.wraps preserves this metadata through outer RBAC wrappers.
        # Merge nested declarations: a later decorator may only add constraints.
        wrapper._sk_api_scopes = tuple(dict.fromkeys(
            (*getattr(fn, '_sk_api_scopes', ()), *scopes)))
        return wrapper
    return decorator
