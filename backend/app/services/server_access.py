"""Tenant ownership of servers (hosting roadmap P0/P2, AI-managed VPS).

Admins see and operate every server. A non-admin sees and operates only the
servers they own (``Server.owner_id``), and the workspace rule in
``WorkspaceService.can_access_server`` still narrows that further. REST
routes, Socket.IO rooms and the MCP layer all ask here.
"""

from app.exceptions import not_found


class ServerAccess:

    @staticmethod
    def is_owner(user, server):
        return (user is not None and server is not None
                and server.owner_id is not None and server.owner_id == user.id)

    @classmethod
    def can_access(cls, user, server):
        if user is None or server is None:
            return False
        if user.is_admin:
            return True
        return cls.is_owner(user, server)

    @classmethod
    def scoped_servers(cls, user, query=None):
        """Narrow a Server query (default: all servers) to what *user* may see."""
        from app.models.server import Server
        query = Server.query if query is None else query
        if user is None:
            return query.filter(False)
        if user.is_admin:
            return query
        return query.filter(Server.owner_id == user.id)

    @classmethod
    def get_owned_server_or_404(cls, user, server_id):
        """The server when *user* may operate it; NotFoundError otherwise, so a
        tenant cannot tell another tenant's server from a missing one."""
        from app.services.workspace_service import WorkspaceService
        server = WorkspaceService.get_accessible_server(user, server_id)
        if server is None:
            raise not_found('server')
        return server


def enforce_server_view_arg():
    """``before_request`` hook for blueprints with ``<server_id>`` routes: a
    non-admin may reach only servers they own; anything else is a 404.
    Unauthenticated requests pass through so each route's own decorator
    answers them (agent-signed routes keep working)."""
    from flask import g, request
    server_id = (request.view_args or {}).get('server_id')
    if not server_id:
        return None
    from flask_jwt_extended import verify_jwt_in_request
    from app.middleware.rbac import get_current_user
    user = getattr(g, 'api_key_user', None)
    if user is None:
        try:
            verify_jwt_in_request(optional=True)
        except Exception:
            return None  # the route's decorator renders the auth error
        user = get_current_user()
    if user is None or user.is_admin:
        return None
    from app.models.server import Server
    server = Server.query.get(server_id)
    if server is None:
        return None  # the route answers a missing server in its own words
    if ServerAccess.can_access(user, server):
        return None
    if not user.is_developer:
        # Most server routes gate on the developer role, and this hook runs
        # before that gate: answer a viewer with the gate's 403.
        from app.exceptions import permission_denied
        raise permission_denied()
    raise not_found('server')
