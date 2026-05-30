"""Add employee absence limit settings to schools table."""
import sqlalchemy as sa
from alembic import op

revision = 't4u5v6w7x8y9'
down_revision = 's3t4u5v6w7x8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('schools') as batch_op:
        batch_op.add_column(sa.Column('emp_absence_limit', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('emp_absence_period',
                                       sa.String(length=20), nullable=True,
                                       server_default='monthly'))
        batch_op.add_column(sa.Column('emp_absence_alert_enabled',
                                       sa.Boolean(), nullable=True,
                                       server_default='true'))


def downgrade():
    with op.batch_alter_table('schools') as batch_op:
        batch_op.drop_column('emp_absence_alert_enabled')
        batch_op.drop_column('emp_absence_period')
        batch_op.drop_column('emp_absence_limit')
