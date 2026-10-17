from datetime import datetime, timedelta
from sqlalchemy.orm import validates
from werkzeug.security import generate_password_hash, check_password_hash
from app import db
from app.models.json_column_mixin import JsonColumnMixin
import json
import secrets


class User(JsonColumnMixin, db.Model):
    __tablename__ = 'users'

    # Role constants
    ROLE_ADMIN = 'admin'
    ROLE_DEVELOPER = 'developer'
    ROLE_VIEWER = 'viewer'
    VALID_ROLES = [ROLE_ADMIN, ROLE_DEVELOPER, ROLE_VIEWER]

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=True)
    auth_provider = db.Column(db.String(50), default='local', index=True)  # local, google, github, oidc, saml
    role = db.Column(db.String(20), default='developer')  # 'admin', 'developer', 'viewer'
    permissions = db.Column(db.Text, nullable=True)  # JSON per-feature read/write flags
    is_active = db.Column(db.Boolean, default=True)
    auth_version = db.Column(db.String(32), nullable=False,
                             default=lambda: secrets.token_hex(16), server_default='0')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login_at = db.Column(db.DateTime, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    # Hosting plan (migration 112). NULL = the panel default plan; admins are
    # never limited. See app/services/plan_quota_service.py.
    plan_id = db.Column(db.Integer, db.ForeignKey('plans.id'), nullable=True, index=True)

    @validates('email')
    def _normalize_email(self, key, email):
        """Store emails in lowercase for consistent matching."""
        return email.lower() if isinstance(email, str) else email

    # Account lockout fields
    failed_login_count = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)

    # Two-Factor Authentication fields
    totp_secret = db.Column(db.String(32), nullable=True)  # Base32 encoded secret
    totp_enabled = db.Column(db.Boolean, default=False)
    backup_codes = db.Column(db.Text, nullable=True)  # JSON array of hashed backup codes
    totp_confirmed_at = db.Column(db.DateTime, nullable=True)  # When 2FA was enabled

    # Sidebar preferences: { preset: 'full'|'web'|'email'|'devops'|'minimal'|'custom', hiddenItems: [...] }
    sidebar_config = db.Column(db.Text, nullable=True)

    # Preferred UI language as a BCP-47 language code. NULL means
    # "follow the panel default", which is a genuinely different state from an
    # explicit choice of English -- the same distinction ThemeContext draws
    # between an inherited skin and a picked one.
    language = db.Column(db.String(10), nullable=True)

    # Per-user Setup Health item snoozes: JSON { item_key: iso_expiry }.
    # Panel-item snoozes live in a SettingsService map instead.
    setup_snoozes = db.Column(db.Text, nullable=True)

    # Relationships
    applications = db.relationship('Application', backref='owner', lazy='dynamic')

    # Lockout durations (progressive: 5 min, 15 min, 1 hour)
    LOCKOUT_DURATIONS = [5, 15, 60]
    MAX_FAILED_ATTEMPTS = 5

    # Per-feature permission system
    PERMISSION_FEATURES = [
        'applications', 'databases', 'docker', 'domains', 'files',
        'monitoring', 'backups', 'security', 'email', 'git', 'cron',
        'terminal', 'users', 'settings', 'servers'
    ]

    ROLE_PERMISSION_TEMPLATES = {
        'admin': {f: {'read': True, 'write': True} for f in PERMISSION_FEATURES},
        'developer': {
            'applications': {'read': True, 'write': True},
            'databases': {'read': True, 'write': True},
            'docker': {'read': True, 'write': True},
            'domains': {'read': True, 'write': True},
            'files': {'read': True, 'write': True},
            'email': {'read': True, 'write': True},
            'git': {'read': True, 'write': True},
            'cron': {'read': True, 'write': True},
            'monitoring': {'read': True, 'write': False},
            'backups': {'read': True, 'write': False},
            'security': {'read': True, 'write': False},
            'terminal': {'read': True, 'write': False},
            'servers': {'read': True, 'write': False},
            'users': {'read': False, 'write': False},
            'settings': {'read': False, 'write': False},
        },
        'viewer': {
            'applications': {'read': True, 'write': False},
            'databases': {'read': True, 'write': False},
            'docker': {'read': True, 'write': False},
            'domains': {'read': True, 'write': False},
            'files': {'read': True, 'write': False},
            'email': {'read': True, 'write': False},
            'git': {'read': True, 'write': False},
            'cron': {'read': True, 'write': False},
            'monitoring': {'read': True, 'write': False},
            'backups': {'read': True, 'write': False},
            'security': {'read': True, 'write': False},
            'terminal': {'read': False, 'write': False},
            'users': {'read': False, 'write': False},
            'settings': {'read': False, 'write': False},
            'servers': {'read': True, 'write': False},
        },
    }

    @property
    def is_locked(self):
        """Check if account is currently locked."""
        if self.locked_until is None:
            return False
        return datetime.utcnow() < self.locked_until

    def record_failed_login(self):
        """Record a failed login attempt and lock account if threshold reached."""
        self.failed_login_count += 1
        if self.failed_login_count >= self.MAX_FAILED_ATTEMPTS:
            # Calculate lockout duration based on how many times they've been locked
            lockout_index = min(
                (self.failed_login_count - self.MAX_FAILED_ATTEMPTS) // self.MAX_FAILED_ATTEMPTS,
                len(self.LOCKOUT_DURATIONS) - 1
            )
            lockout_minutes = self.LOCKOUT_DURATIONS[lockout_index]
            self.locked_until = datetime.utcnow() + timedelta(minutes=lockout_minutes)

    def reset_failed_login(self):
        """Reset failed login count after successful login."""
        self.failed_login_count = 0
        self.locked_until = None

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        self.revoke_sessions()

    @validates('is_active')
    def _invalidate_disabled_sessions(self, key, active):
        if not active:
            self.revoke_sessions()
        return active

    def revoke_sessions(self):
        """Invalidate access/refresh/MFA tokens and unused login links."""
        self.auth_version = secrets.token_hex(16)
        if self.id is not None:
            from app.models.login_link import LoginLink
            LoginLink.query.filter(
                (LoginLink.user_id == self.id) | (LoginLink.created_by_id == self.id)
            ).delete(synchronize_session=False)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        """Check if user has admin role."""
        return self.role == self.ROLE_ADMIN

    @property
    def is_developer(self):
        """Check if user has developer role or higher."""
        return self.role in [self.ROLE_ADMIN, self.ROLE_DEVELOPER]

    @property
    def is_viewer(self):
        """Check if user has viewer role or higher (all roles)."""
        return self.role in self.VALID_ROLES

    def has_role(self, *roles):
        """Check if user has any of the specified roles."""
        return self.role in roles

    @property
    def has_password(self):
        return self.password_hash is not None

    def get_permissions(self):
        """Return resolved permissions: custom if set, otherwise role template."""
        if self.role == self.ROLE_ADMIN:
            return self.ROLE_PERMISSION_TEMPLATES['admin']
        custom = self._json_read('permissions', None)
        if custom is not None:
            return custom
        return self.ROLE_PERMISSION_TEMPLATES.get(self.role, {})

    def set_permissions(self, perms_dict):
        """Store custom permissions as JSON."""
        self.permissions = json.dumps(perms_dict) if perms_dict else None

    def has_permission(self, feature, level='read'):
        """Check if user has a specific permission. Admin always returns True."""
        if self.role == self.ROLE_ADMIN:
            return True
        perms = self.get_permissions()
        feature_perms = perms.get(feature, {})
        return feature_perms.get(level, False)

    def get_sidebar_config(self):
        """Return sidebar config dict, or default."""
        return self._json_read('sidebar_config',
                               {'preset': 'full', 'hiddenItems': []})

    def set_sidebar_config(self, config):
        """Store sidebar config as JSON."""
        self._json_write('sidebar_config', config)

    def to_dict(self):
        return {
            'id': self.id,
            'email': self.email,
            'username': self.username,
            'role': self.role,
            'permissions': self.get_permissions(),
            'is_active': self.is_active,
            'totp_enabled': self.totp_enabled,
            'auth_provider': self.auth_provider or 'local',
            'has_password': self.has_password,
            'passkey_enabled': self.passkeys.filter_by(is_active=True).count() > 0,
            'sidebar_config': self.get_sidebar_config(),
            'language': self.language,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
            'last_login_at': self.last_login_at.isoformat() if self.last_login_at else None,
            'created_by': self.created_by,
            'plan_id': self.plan_id,
            'is_admin': self.is_admin
        }

    def get_backup_codes(self):
        """Get the list of backup code hashes."""
        return self._json_read('backup_codes', [])

    def set_backup_codes(self, codes_hashes):
        """Store backup code hashes."""
        self.backup_codes = json.dumps(codes_hashes)

    def use_backup_code(self, code_hash):
        """Remove a used backup code."""
        codes = self.get_backup_codes()
        if code_hash in codes:
            codes.remove(code_hash)
            self.set_backup_codes(codes)
            return True
        return False

    def __repr__(self):
        return f'<User {self.username}>'
