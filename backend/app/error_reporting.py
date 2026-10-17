"""One place that turns an *unexpected* exception into a recorded, correlated 500.

The global ``@app.errorhandler(500)`` logs the crash, records it into the
centralized error log behind ``/monitoring/errors``, and answers with the
documented JSON body carrying a request id. A route that catches ``Exception``
itself never reaches that handler, so its crash is logged nowhere and is
invisible on the errors page — the failure looks handled precisely because
someone handled it.

That is why this module exists rather than only the handler: the recording side
is importable, so a route that must keep answering JSON in-band can still put
its crash on the record.

Use it only for genuinely unexpected failures. An expected, caller-actionable
failure should raise one of ``app.exceptions``' typed errors instead — those
are not crashes and must not fill the error log.

    from app.error_reporting import unexpected_response

    try:
        return jsonify(SomeService.run())
    except Exception as exc:            # noqa: BLE001
        return unexpected_response(exc)

Prefer deleting the ``try``/``except`` entirely where nothing in it does real
cleanup: letting the exception reach the global handler produces the same
response through the same recorder, with one less thing to keep in step.
"""

import traceback as _traceback

from flask import current_app, request

from app.middleware.request_id import get_request_id
from app.utils.secret_paths import mask_path


def record_unexpected(exc, *, request_id=None):
    """Log `exc` and record it into the centralized error log. Never raises.

    Returns the request id the crash was recorded under so a caller can hand it
    to the client — an error the user can quote is worth far more than the
    exception text this replaces.
    """
    request_id = request_id or get_request_id(create=True)

    try:
        current_app.logger.exception(
            'Unhandled exception on %s %s [request_id=%s]',
            request.method, mask_path(request.path), request_id, exc_info=exc,
        )
    except Exception:  # noqa: BLE001 - reporting must never mask the crash
        pass

    # Roll back BEFORE recording. This shares the app-context-scoped session
    # with the code that just crashed, so a DB-caused failure leaves it in a
    # failed transaction and record_error's first query would raise
    # PendingRollbackError — swallowed by its own contract, which is how
    # database-caused crashes silently never reach the error log.
    try:
        from app import db
        db.session.rollback()
    except Exception:  # noqa: BLE001
        pass

    try:
        from app.services import error_log_service
        from app.middleware.rbac import get_current_user

        user = None
        try:
            user = get_current_user()
        except Exception:  # noqa: BLE001 - anonymous crashes still count
            pass

        error_log_service.record_error(
            source='backend',
            exception_type=type(exc).__name__,
            message=str(exc),
            traceback=''.join(
                _traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
            endpoint=getattr(request, 'path', None),
            method=getattr(request, 'method', None),
            user_id=user.id if user else None,
            context={'request_id': request_id},
        )
    except Exception:  # noqa: BLE001
        pass

    return request_id


def unexpected_error_body(request_id):
    """The documented crash body — identical to the global 500 handler's."""
    return {
        'error': 'Internal server error',
        'status': 500,
        'code': 'internal_error',
        'request_id': request_id,
    }


def unexpected_response(exc):
    """Record `exc` and return the standard ``(body, 500)`` for a crash.

    The body deliberately does NOT carry ``str(exc)``. An unexpected exception's
    text is written for whoever reads the log, not for the caller: it routinely
    contains connection strings, absolute paths, and SQL. The request id is the
    part the caller can act on.
    """
    return unexpected_error_body(record_unexpected(exc)), 500
