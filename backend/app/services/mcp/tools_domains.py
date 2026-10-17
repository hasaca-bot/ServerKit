"""Domains of your sites, and PR previews."""
import re

from app.services.mcp.errors import ToolError
from app.services.mcp.registry import tool
from app.services.mcp import validation as v
from app.services.mcp.context import check_quota, get_site
from app.services.mcp.tools_sites import site_schema

_DOMAIN_RE = re.compile(r'^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$')
DOMAIN_PROP = {'type': 'string', '_required': True, 'description': 'A host name such as "shop.example.com".'}


def _host(value):
    host = value.strip().lower().rstrip('.') if isinstance(value, str) else ''
    if not _DOMAIN_RE.match(host):
        raise ToolError('domain: a host name such as "shop.example.com" (no scheme, port or path)')
    return host


@tool('list_domains', 'Host names your site answers on (primary first) and their HTTPS state.', site_schema())
def list_domains(ctx, args):
    from app.models.domain import Domain
    v.no_extra(args, ('site',))
    app = get_site(ctx, args.get('site'))
    rows = (Domain.query_active().filter_by(application_id=app.id)
            .order_by(Domain.is_primary.desc(), Domain.name).all())
    return {'site': app.name, 'domains': [
        {'name': d.name, 'primary': bool(d.is_primary), 'ssl': bool(d.ssl_enabled)} for d in rows]}


@tool('add_domain', 'Point one of YOUR custom domains at your site (create its DNS record yourself: a CNAME to '
                    'the site\'s address, or an A record to the server). Counts against the custom-domain plan limit.',
      site_schema(domain=dict(DOMAIN_PROP)), write=True)
def add_domain(ctx, args):
    from app import db
    from app.models.domain import Domain
    from app.services.site_domain_service import SiteDomainService
    v.no_extra(args, ('site', 'domain'))
    host = _host(args.get('domain'))
    app = get_site(ctx, args.get('site'), access='write')
    if SiteDomainService.covers(host) or host == (SiteDomainService.panel_host() or ''):
        raise ToolError(f'{host} is managed by this panel; your site already has https://{app.name}.'
                        f'{SiteDomainService.base_domain()}')
    if Domain.query_active().filter_by(name=host).first() is not None:
        raise ToolError(f'{host} is already in use')
    if app.app_type == 'docker' and not app.port:
        raise ToolError('the site has no port yet; publish it with "port" first')
    check_quota(ctx, 'custom_domains')
    db.session.add(Domain(name=host, is_primary=False, application_id=app.id))
    db.session.commit()
    result = SiteDomainService.write_app_vhost(app) or {}
    payload = {'site': app.name, 'domain': host, 'added': True}
    if result.get('warning'):
        payload['warning'] = v.redact(result['warning'])
    return payload


@tool('remove_domain', 'Stop your site answering on one of its custom domains.',
      site_schema(domain=dict(DOMAIN_PROP)), write=True)
def remove_domain(ctx, args):
    from app import db
    from app.models.domain import Domain
    from app.services.site_domain_service import SiteDomainService
    v.no_extra(args, ('site', 'domain'))
    host = _host(args.get('domain'))
    app = get_site(ctx, args.get('site'), access='write')
    domain = Domain.query_active().filter_by(name=host, application_id=app.id).first()
    if domain is None:
        raise ToolError(f'{host} is not a domain of {app.name}')
    if SiteDomainService.covers(host):
        raise ToolError(f'{host} is the site\'s own address; delete the site instead')
    domain.soft_delete(user_id=ctx.user.id)
    db.session.commit()
    result = SiteDomainService.write_app_vhost(app) or {}
    payload = {'site': app.name, 'domain': host, 'removed': True}
    if result.get('warning'):
        payload['warning'] = v.redact(result['warning'])
    return payload


@tool('list_previews', 'Pull-request preview environments of your site (enabled from the panel).', site_schema())
def list_previews(ctx, args):
    from app.models.application_preview import ApplicationPreview
    v.no_extra(args, ('site',))
    app = get_site(ctx, args.get('site'))
    rows = (ApplicationPreview.query.filter_by(application_id=app.id)
            .filter(ApplicationPreview.deleted_at.is_(None))
            .order_by(ApplicationPreview.pr_number.desc()).limit(50).all())
    return {'site': app.name, 'previews': [
        {'pr': p.pr_number, 'title': (p.pr_title or '')[:200], 'branch': p.branch, 'status': p.status,
         'url': f'https://{p.domain}' if p.domain else None,
         'expires_at': p.expires_at.isoformat() if p.expires_at else None} for p in rows]}
