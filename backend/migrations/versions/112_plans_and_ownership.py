"""Hosting plans, users.plan_id and servers.owner_id.

``plans`` holds per-tenant limits (seeded hobby/pro/team). ``users.plan_id``
NULL means the panel default plan. ``servers.owner_id`` makes servers
tenant-owned; existing rows are backfilled from ``registered_by`` so whoever
added a server keeps it.
"""
from alembic import op
import sqlalchemy as sa

revision = '112_plans_and_ownership'
down_revision = '111_app_build_filter'
branch_labels = None
depends_on = None


SEED = (
    # slug, name, apps, dbs, cron, domains, previews, servers, mcp_keys, cpu, mem, sleep
    ('hobby', 'Hobby', 3, 1, 2, 0, 0, 0, 1, 0.25, 256, 15),
    ('pro', 'Pro', 25, 10, 20, 10, 5, 1, 5, 2.0, 4096, None),
    ('team', 'Team', 100, 30, 50, 50, 20, 5, 20, 4.0, 8192, None),
)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if 'plans' not in tables:
        op.create_table(
            'plans',
            sa.Column('id', sa.Integer, primary_key=True),
            sa.Column('slug', sa.String(40), nullable=False),
            sa.Column('name', sa.String(100), nullable=False),
            sa.Column('max_apps', sa.Integer, nullable=True),
            sa.Column('max_databases', sa.Integer, nullable=True),
            sa.Column('max_cron_jobs', sa.Integer, nullable=True),
            sa.Column('max_custom_domains', sa.Integer, nullable=True),
            sa.Column('max_previews', sa.Integer, nullable=True),
            sa.Column('max_servers', sa.Integer, nullable=True),
            sa.Column('max_mcp_keys', sa.Integer, nullable=True),
            sa.Column('max_cpu', sa.Float, nullable=True),
            sa.Column('max_memory_mb', sa.Integer, nullable=True),
            sa.Column('sleep_after_minutes', sa.Integer, nullable=True),
            sa.Column('created_at', sa.DateTime, nullable=True),
            sa.Column('updated_at', sa.DateTime, nullable=True),
        )
        op.create_index('ix_plans_slug', 'plans', ['slug'], unique=True)

    plans = sa.table(
        'plans',
        sa.column('slug', sa.String), sa.column('name', sa.String),
        sa.column('max_apps', sa.Integer), sa.column('max_databases', sa.Integer),
        sa.column('max_cron_jobs', sa.Integer), sa.column('max_custom_domains', sa.Integer),
        sa.column('max_previews', sa.Integer), sa.column('max_servers', sa.Integer),
        sa.column('max_mcp_keys', sa.Integer), sa.column('max_cpu', sa.Float),
        sa.column('max_memory_mb', sa.Integer), sa.column('sleep_after_minutes', sa.Integer),
    )
    existing = {row[0] for row in bind.execute(sa.text('SELECT slug FROM plans'))}
    rows = [
        dict(zip(('slug', 'name', 'max_apps', 'max_databases', 'max_cron_jobs',
                  'max_custom_domains', 'max_previews', 'max_servers', 'max_mcp_keys',
                  'max_cpu', 'max_memory_mb', 'sleep_after_minutes'), seed))
        for seed in SEED if seed[0] not in existing
    ]
    if rows:
        op.bulk_insert(plans, rows)

    user_cols = {c['name'] for c in inspector.get_columns('users')}
    if 'plan_id' not in user_cols:
        with op.batch_alter_table('users') as batch:
            batch.add_column(sa.Column('plan_id', sa.Integer, nullable=True))
            batch.create_index('ix_users_plan_id', ['plan_id'])
            batch.create_foreign_key('fk_users_plan_id', 'plans', ['plan_id'], ['id'])

    server_cols = {c['name'] for c in inspector.get_columns('servers')}
    if 'owner_id' not in server_cols:
        with op.batch_alter_table('servers') as batch:
            batch.add_column(sa.Column('owner_id', sa.Integer, nullable=True))
            batch.create_index('ix_servers_owner_id', ['owner_id'])
            batch.create_foreign_key('fk_servers_owner_id', 'users', ['owner_id'], ['id'])
        op.execute('UPDATE servers SET owner_id = registered_by WHERE owner_id IS NULL')


def downgrade():
    with op.batch_alter_table('servers') as batch:
        batch.drop_constraint('fk_servers_owner_id', type_='foreignkey')
        batch.drop_index('ix_servers_owner_id')
        batch.drop_column('owner_id')
    with op.batch_alter_table('users') as batch:
        batch.drop_constraint('fk_users_plan_id', type_='foreignkey')
        batch.drop_index('ix_users_plan_id')
        batch.drop_column('plan_id')
    op.drop_index('ix_plans_slug', table_name='plans')
    op.drop_table('plans')
