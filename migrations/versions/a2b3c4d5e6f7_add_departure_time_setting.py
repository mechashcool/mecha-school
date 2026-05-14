"""Add att_departure_time to schools and school_settings.

Revision ID: a2b3c4d5e6f7
Revises: 9d0ea1479cf4
Create Date: 2026-05-07
"""
from alembic import op
import sqlalchemy as sa


revision = 'a2b3c4d5e6f7'
down_revision = '9d0ea1479cf4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('schools',
        sa.Column('att_departure_time', sa.Time(), nullable=True))
    op.add_column('school_settings',
        sa.Column('att_departure_time', sa.Time(), nullable=True))


def downgrade():
    op.drop_column('school_settings', 'att_departure_time')
    op.drop_column('schools', 'att_departure_time')
