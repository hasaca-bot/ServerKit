"""Which repository a caller may build an app from (tenant isolation).

Like Render: a non-admin deploys only repositories reachable through THEIR OWN
source connection. Raw git URLs, another user's connection and local paths
are admin-only. Every create/clone path (the REST routes, the MCP layer)
resolves its source here.
"""

from app.exceptions import PermissionDeniedError, ValidationError


def resolve_clone_source(user, *, source_connection_id=None, repository_full_name='',
                         repo_url=''):
    """Return ``{'clone_url', 'public_url', 'connection_id'}`` for *user*.

    Admins keep today's behaviour: a source connection when given, otherwise
    the raw ``repo_url``. Non-admins must name one of their own connections and
    a repository that connection grants; a raw URL is refused.
    """
    from app.services.source_connection_service import SourceConnectionService

    repo_url = (repo_url or '').strip()
    full_name = (repository_full_name or '').strip()
    is_admin = bool(user is not None and user.is_admin)

    if not source_connection_id:
        if not is_admin:
            raise PermissionDeniedError(
                'Connect a Git provider and pick one of its repositories; '
                'deploying from a raw git URL is limited to administrators.')
        if not repo_url:
            raise ValidationError('repo_url is required')
        return {'clone_url': repo_url, 'public_url': repo_url, 'connection_id': None}

    if repo_url and not is_admin:
        raise PermissionDeniedError('Pass a repository of your connection, not a git URL.')
    try:
        connection_id = int(source_connection_id)
    except (TypeError, ValueError):
        raise ValidationError('source_connection_id must be a number')
    if not full_name:
        raise ValidationError('repository_full_name is required')

    # get_authenticated_clone_url only ever loads the caller's own connection
    # (filter by user_id), so another user's id answers "not found".
    try:
        source = SourceConnectionService.get_authenticated_clone_url(
            user_id=user.id, connection_id=connection_id, full_name=full_name)
    except ValueError as exc:
        raise ValidationError(str(exc))
    if not is_admin and not SourceConnectionService.connection_can_reach(
            user.id, connection_id, full_name):
        raise PermissionDeniedError(
            'That repository is not available through your Git connection.')
    return {'clone_url': source['clone_url'], 'public_url': source['public_url'],
            'connection_id': connection_id}


def require_no_local_path(user, path):
    """Local filesystem sources are admin-only."""
    if path and not (user is not None and user.is_admin):
        raise PermissionDeniedError('Only an administrator can build from a server path.')
