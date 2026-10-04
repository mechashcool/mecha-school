"""Enforce one investor account per school

Revision ID: p3q4r5s6t7u8
Revises: o2p3q4r5s6t7
Create Date: 2026-10-05
"""
from alembic import op


revision = 'p3q4r5s6t7u8'
down_revision = 'o2p3q4r5s6t7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        'uq_investor_school_access_school',
        'investor_school_access',
        ['school_id'],
    )


def downgrade():
    op.drop_constraint(
        'uq_investor_school_access_school',
        'investor_school_access',
        type_='unique',
    )
