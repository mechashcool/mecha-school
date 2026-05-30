"""Add device_scope to attendance_devices

Revision ID: s3t4u5v6w7x8
Revises: r2s3t4u5v6w7
Create Date: 2026-05-27

Changes:
  attendance_devices — add device_scope VARCHAR(20) NOT NULL DEFAULT 'students'
  Existing rows get scope='students' to preserve current student-attendance behavior.
"""
from alembic import op
import sqlalchemy as sa


revision      = 's3t4u5v6w7x8'
down_revision = 'r2s3t4u5v6w7'
branch_labels = None
depends_on    = None


def upgrade():
    op.add_column(
        'attendance_devices',
        sa.Column('device_scope', sa.String(20), nullable=False,
                  server_default='students'),
    )


def downgrade():
    op.drop_column('attendance_devices', 'device_scope')
