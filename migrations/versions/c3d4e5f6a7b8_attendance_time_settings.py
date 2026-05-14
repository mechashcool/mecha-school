"""add attendance time settings to school_settings

Revision ID: c3d4e5f6a7b8
Revises: a1b2c3d4e5f6
Create Date: 2026-04-29 10:00:00.000000

Adds three nullable Time columns to school_settings:
  - att_start_time        : when school opens
  - att_late_threshold    : check-in at/after this → 'late'
  - att_absence_threshold : no check-in by this → automated 'absent'
"""
from alembic import op
import sqlalchemy as sa


revision = 'c3d4e5f6a7b8'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('school_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('att_start_time',        sa.Time(), nullable=True))
        batch_op.add_column(sa.Column('att_late_threshold',    sa.Time(), nullable=True))
        batch_op.add_column(sa.Column('att_absence_threshold', sa.Time(), nullable=True))


def downgrade():
    with op.batch_alter_table('school_settings', schema=None) as batch_op:
        batch_op.drop_column('att_absence_threshold')
        batch_op.drop_column('att_late_threshold')
        batch_op.drop_column('att_start_time')
