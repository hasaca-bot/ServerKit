"""Mask credentials that ride in a URL path before the path is logged or stored.

``/api/v1/mcp/k/<key>`` carries a user's API key for MCP clients that cannot
send headers (claude.ai custom connectors). Every sink that records a request
path — the audit log, API analytics, access logs — runs it through
``mask_path`` first.
"""
import logging
import re

_MCP_KEY_PATH = re.compile(r'(/api/v1/mcp/k/)[^/?#\s"]+')


def mask_path(path):
    if not path or '/mcp/k/' not in str(path):
        return path
    return _MCP_KEY_PATH.sub(r'\1<key>', str(path))


class SecretPathFilter(logging.Filter):
    """Rewrites path keys inside access-log records (werkzeug / gunicorn)."""

    def filter(self, record):
        try:
            if isinstance(record.msg, str) and '/mcp/k/' in record.msg:
                record.msg = mask_path(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(mask_path(a) if isinstance(a, str) else a for a in record.args)
            elif isinstance(record.args, dict):
                # In place: gunicorn passes a dict subclass with lookup defaults.
                for k, a in list(record.args.items()):
                    if isinstance(a, str) and '/mcp/k/' in a:
                        record.args[k] = mask_path(a)
        except Exception:  # noqa: BLE001 - a log filter must never drop a line
            pass
        return True


def install_log_filters():
    for name in ('werkzeug', 'gunicorn.access'):
        logger = logging.getLogger(name)
        if not any(isinstance(f, SecretPathFilter) for f in logger.filters):
            logger.addFilter(SecretPathFilter())
