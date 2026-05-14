"""add manage_attendance_settings permission

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-04-29 11:00:00.000000

Inserts the 'manage_attendance_settings' permission into the permissions table.
This permission gates the /admin/attendance-settings route and sidebar link,
allowing it to be delegated to non-admin staff roles via Role Management.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column
from datetime import datetime


revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


permissions_table = table(
    'permissions',
    column('name', sa.String),
    column('label', sa.String),
    column('category', sa.String),
    column('created_at', sa.DateTime),
)


def upgrade():
    # Insert only if the permission does not already exist (idempotent)
    conn = op.get_bind()
    exists = conn.execute(
        sa.text("SELECT 1 FROM permissions WHERE name = 'manage_attendance_settings' LIMIT 1")
    ).fetchone()
    if not exists:
        op.bulk_insert(permissions_table, [
            {
                'name':       'manage_attendance_settings',
                'label':      'إدارة إعدادات الحضور والغياب',
                'category':   'attendance',
                'created_at': datetime.utcnow(),
            }
        ])


def downgrade():
    op.execute(
        sa.text("DELETE FROM permissions WHERE name = 'manage_attendance_settings'")
    )
