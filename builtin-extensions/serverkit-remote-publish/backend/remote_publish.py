"""Remote Publish & MCP — panel side.

The publish gateway itself (REST + MCP for AIs outside this server) is the
`serverkit-gateway` systemd service; this blueprint is how the owner manages
it from the panel, plus per-service auto-deploy switches.

Every route is admin-only. The gateway key is never returned by the status
route; revealing it is an explicit, audited action.
"""
import os
import secrets
import subprocess

from flask import Blueprint, jsonify, request

from app.middleware.rbac import admin_required, get_current_user
from app.models import Application
from app.models.audit_log import AuditLog
from app.services.audit_service import AuditService
from app.services.git_service import GitService

remote_publish_bp = Blueprint('remote_publish', __name__)

UNIT = 'serverkit-gateway'
TOKEN_FILE = '/etc/serverkit-gateway/token'
PUBLIC_URL_FILE = '/etc/serverkit-gateway/public_url'  # written by windows/gateway/install.sh


def _public_url():
    """Public gateway address: $SERVERKIT_GATEWAY_URL, else the file install.sh writes
    from the config's domain + gateway label; empty until the gateway is installed."""
    url = os.environ.get('SERVERKIT_GATEWAY_URL', '')
    if not url:
        try:
            with open(PUBLIC_URL_FILE, encoding='utf-8') as f:
                url = f.read().strip()
        except OSError:
            url = ''
    return url.rstrip('/')



def _systemctl(*args):
    try:
        return subprocess.run(['systemctl', *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(args, 1, '', str(e))


def _installed():
    return _systemctl('cat', UNIT).returncode == 0


def _read_token():
    try:
        with open(TOKEN_FILE, encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def _audit(action, details):
    user = get_current_user()
    AuditService.log(action=AuditLog.ACTION_SETTINGS_UPDATE, user_id=user.id if user else None,
                     target_type='remote_publish', target_id=action, details=details)


@remote_publish_bp.route('/status', methods=['GET'])
@admin_required
def status():
    token = _read_token()
    return jsonify({
        'installed': _installed(),
        'active': _systemctl('is-active', UNIT).stdout.strip() == 'active',
        'enabled': _systemctl('is-enabled', UNIT).stdout.strip() == 'enabled',
        'url': _public_url(),
        'mcp_url': f'{_public_url()}/mcp',
        'token_set': bool(token),
        'token_hint': f'{token[:4]}…' if token else None,
    })


@remote_publish_bp.route('/token/reveal', methods=['POST'])
@admin_required
def reveal_token():
    token = _read_token()
    if not token:
        return jsonify({'error': 'No key has been generated yet'}), 404
    _audit('token.reveal', {'note': 'gateway key shown in the panel'})
    return jsonify({'token': token})


@remote_publish_bp.route('/token/rotate', methods=['POST'])
@admin_required
def rotate_token():
    if not _installed():
        return jsonify({'error': 'The gateway service is not installed on this server'}), 400
    os.makedirs(os.path.dirname(TOKEN_FILE), mode=0o700, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_FILE + '.new', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(token)
    os.replace(TOKEN_FILE + '.new', TOKEN_FILE)
    _audit('token.rotate', {'note': 'gateway key replaced; the old key stopped working'})
    return jsonify({'token': token})


@remote_publish_bp.route('/service/<action>', methods=['POST'])
@admin_required
def control(action):
    if action not in ('start', 'stop'):
        return jsonify({'error': 'action must be start or stop'}), 400
    if not _installed():
        return jsonify({'error': 'The gateway service is not installed on this server'}), 400
    verb = ['enable', '--now'] if action == 'start' else ['disable', '--now']
    proc = _systemctl(*verb, UNIT)
    if proc.returncode != 0:
        return jsonify({'error': proc.stderr.strip() or 'systemctl failed'}), 500
    _audit(f'service.{action}', {'unit': UNIT})
    return status()


def _service_row(app):
    cfg = GitService.get_app_config(app.id) or {}
    domains = [d for d in (app.domains or []) if not getattr(d, 'deleted_at', None)]
    primary = next((d for d in domains if d.is_primary), domains[0] if domains else None)
    return {
        'id': app.id,
        'name': app.name,
        'status': app.status,
        'url': (('https://' if primary.ssl_enabled else 'http://') + primary.name) if primary else None,
        'git_connected': bool(cfg),
        'auto_deploy': bool(cfg.get('auto_deploy')) if cfg else False,
        'branch': cfg.get('branch'),
        'last_deploy': cfg.get('last_deploy'),
    }


@remote_publish_bp.route('/auto-deploy', methods=['GET'])
@admin_required
def list_auto_deploy():
    apps = Application.query.filter_by(server_id=None).order_by(Application.name).all()
    rows = [_service_row(a) for a in apps if not getattr(a, 'deleted_at', None)]
    return jsonify({'services': rows})


@remote_publish_bp.route('/auto-deploy/<int:app_id>', methods=['GET'])
@admin_required
def get_auto_deploy(app_id):
    app = Application.query.get(app_id)
    if app is None:
        return jsonify({'error': 'Service not found'}), 404
    return jsonify(_service_row(app))


@remote_publish_bp.route('/auto-deploy/<int:app_id>', methods=['PUT'])
@admin_required
def set_auto_deploy(app_id):
    """Flip only the auto_deploy flag of an existing Git deployment, keeping
    its webhook token, scripts and history (configure_deployment would
    regenerate them)."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get('enabled'), bool):
        return jsonify({'error': 'enabled must be true or false'}), 400
    app = Application.query.get(app_id)
    if app is None:
        return jsonify({'error': 'Service not found'}), 404
    config = GitService.get_config()
    entry = config.get('apps', {}).get(str(app_id))
    if not entry:
        return jsonify({'error': 'This service has no Git deployment to switch'}), 400
    entry['auto_deploy'] = data['enabled']
    GitService.save_config(config)
    _audit('auto_deploy', {'app_id': app_id, 'app': app.name, 'enabled': data['enabled']})
    return jsonify(_service_row(app))
