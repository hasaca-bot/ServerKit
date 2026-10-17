"""Resolve an MCP caller's API key (Bearer header or path key)."""
from app import db
from app.services.api_key_service import ApiKeyService


def resolve_key(raw, ip):
    """The valid ApiKey for ``raw`` with its usage recorded, or None."""
    api_key = ApiKeyService.validate_key(raw)
    if api_key is None:
        return None
    api_key.record_usage(ip)
    db.session.commit()
    return api_key
