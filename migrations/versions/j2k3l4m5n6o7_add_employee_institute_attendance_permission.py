"""add employee institute attendance recording permission

Revision ID: j2k3l4m5n6o7
Revises: h8e9m1p2s3t4
"""
from alembic import op
import sqlalchemy as sa


revision = 'j2k3l4m5n6o7'
down_revision = 'h8e9m1p2s3t4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'employees',
        sa.Column('can_record_institute_attendance', sa.Boolean(),
                  nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column('employees', 'can_record_institute_attendance')
