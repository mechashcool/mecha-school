"""Make user email optional

Revision ID: k5l6m7n8o9p0
Revises: j4k5l6m7n8o9
Create Date: 2026-05-22
"""

from alembic import op
import sqlalchemy as sa


revision = 'k5l6m7n8o9p0'
down_revision = 'j4k5l6m7n8o9'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        'users',
        'email',
        existing_type=sa.String(length=180),
        nullable=True,
    )


def downgrade():
    op.alter_column(
        'users',
        'email',
        existing_type=sa.String(length=180),
        nullable=False,
    )
