"""API analytics middleware for logging request metrics."""
import time
import threading
import logging
from flask import g, request

from app.utils.secret_paths import mask_path

logger = logging.getLogger(__name__)

# Buffer for batch inserts
_log_buffer = []
_buffer_lock = threading.Lock()


def register_api_analytics(app):
    """Register before/after request handlers for API analytics."""

    @app.before_request
    def record_request_start():
        """Record request start time."""
        if request.path.startswith('/api/'):
            g.request_start_time = time.time()

    @app.after_request
    def record_request_metrics(response):
        """Log API request metrics."""
        if not request.path.startswith('/api/'):
            return response

        start_time = getattr(g, 'request_start_time', None)
        if start_time is None:
            return response

        elapsed_ms = (time.time() - start_time) * 1000

        api_key = getattr(g, 'api_key', None)
        api_key_user = getattr(g, 'api_key_user', None)

        # Get user_id from API key or JWT
        user_id = None
        if api_key_user:
            user_id = api_key_user.id
        else:
            try:
                from flask_jwt_extended import get_jwt_identity
                user_id = get_jwt_identity()
            except Exception:
                pass

        from app.utils.client_ip import get_client_ip
        ip_address = get_client_ip()

        log_entry = {
            'api_key_id': api_key.id if api_key else None,
            'user_id': user_id,
            'method': request.method,
            'endpoint': mask_path(request.path),
            'blueprint': request.blueprints[0] if request.blueprints else None,
            'status_code': response.status_code,
            'response_time_ms': round(elapsed_ms, 2),
            'ip_address': ip_address,
            'user_agent': (request.headers.get('User-Agent') or '')[:500],
            'request_size': request.content_length or 0,
            'response_size': response.content_length or 0,
        }

        with _buffer_lock:
            _log_buffer.append(log_entry)

        return response


def start_analytics_flush_thread(app):
    """Start background thread to flush analytics buffer to DB.

    Skipped under testing: the app fixture is function-scoped, so an
    unconditional per-create_app() flusher would leak one immortal writer per
    test and race the suite's shared database (mirrors the queue-bus consumers).
    """
    if app.config.get('ENV') == 'testing' or app.config.get('TESTING'):
        return None

    def flush_loop():
        while True:
            time.sleep(5)
            try:
                _flush_buffer(app)
            except Exception as e:
                logger.error(f'Analytics flush error: {e}')

    thread = threading.Thread(
        target=flush_loop,
        daemon=True,
        name='api-analytics-flush'
    )
    thread.start()
    return thread


def _flush_buffer(app):
    """Flush buffered log entries to the database."""
    with _buffer_lock:
        if not _log_buffer:
            return
        entries = _log_buffer.copy()
        _log_buffer.clear()

    with app.app_context():
        from app import db
        from app.models.api_usage import ApiUsageLog

        for entry in entries:
            log = ApiUsageLog(**entry)
            db.session.add(log)

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f'Failed to flush analytics: {e}')
