"""update default timezone to Asia/Baghdad

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-04-30 00:00:00.000000

Changes the school_settings.timezone column default from 'Asia/Riyadh' to
'Asia/Baghdad'. Existing rows still holding the old default are updated as
well (both timezones are UTC+3, so no recorded times change in value).
"""
from alembic import op
import sqlalchemy as sa


revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    # Change the column default for new rows
    op.alter_column(
        'school_settings', 'timezone',
        existing_type=sa.String(50),
        server_default='Asia/Baghdad',
    )
    # Update any rows that still carry the old default value
    op.execute(
        sa.text(
            "UPDATE school_settings SET timezone = 'Asia/Baghdad' "
            "WHERE timezone = 'Asia/Riyadh' OR timezone IS NULL"
        )
    )


def downgrade():
    op.alter_column(
        'school_settings', 'timezone',
        existing_type=sa.String(50),
        server_default='Asia/Riyadh',
    )
    op.execute(
        sa.text(
            "UPDATE school_settings SET timezone = 'Asia/Riyadh' "
            "WHERE timezone = 'Asia/Baghdad'"
        )
    )
