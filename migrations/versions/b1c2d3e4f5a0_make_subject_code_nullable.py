"""Make subjects.code nullable

Background
----------
The Subject model originally declared code as NOT NULL.
The configurable subjects form system (SchoolModuleConfig) lets a Super Admin
hide or make the code field optional per school.  When the field is hidden the
form does not submit a value, so the backend stores None — which previously
caused a database NOT NULL violation.

This migration drops the NOT NULL constraint so hidden or optional subject
codes can be stored as NULL without errors.

Revision ID: b1c2d3e4f5a0
Revises: a0b1c2d3e4f5
Create Date: 2026-05-29
"""

from alembic import op
import sqlalchemy as sa

revision = 'b1c2d3e4f5a0'
down_revision = 'a0b1c2d3e4f5'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('subjects') as batch:
        batch.alter_column(
            'code',
            existing_type=sa.String(20),
            nullable=True,
        )


def downgrade():
    op.execute("UPDATE subjects SET code = '' WHERE code IS NULL")
    with op.batch_alter_table('subjects') as batch:
        batch.alter_column(
            'code',
            existing_type=sa.String(20),
            nullable=False,
        )
