"""Per-app monorepo build filter and repository root directory.

``root_dir`` is a path inside the repository; ``build_filter`` is JSON
``{"paths": [...], "ignored_paths": [...]}``. Both NULL = every push deploys.
"""
from alembic import op
import sqlalchemy as sa

revision = '111_app_build_filter'
down_revision = '110_drop_workflow_schedules_job'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    existing = {c['name'] for c in inspector.get_columns('applications')}
    # Add-if-missing: the pre-alembic schema sync may already have added them.
    if 'root_dir' not in existing:
        op.add_column('applications', sa.Column('root_dir', sa.String(500), nullable=True))
    if 'build_filter' not in existing:
        op.add_column('applications', sa.Column('build_filter', sa.Text, nullable=True))


def downgrade():
    with op.batch_alter_table('applications') as batch:
        batch.drop_column('build_filter')
        batch.drop_column('root_dir')
