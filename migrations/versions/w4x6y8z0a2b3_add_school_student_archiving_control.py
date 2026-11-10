"""Add per-school student archiving control.

Revision ID: w4x6y8z0a2b3
Revises: v3h5w7p9n1t2
Create Date: 2026-11-10
"""

from alembic import op
import sqlalchemy as sa


revision = 'w4x6y8z0a2b3'
down_revision = 'v3h5w7p9n1t2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'schools',
        sa.Column('archiving_enabled', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
    )


def downgrade():
    op.drop_column('schools', 'archiving_enabled')
