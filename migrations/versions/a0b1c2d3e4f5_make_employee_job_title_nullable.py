"""Make employees.job_title nullable

Background
----------
The Employee model originally declared job_title as NOT NULL.
The configurable employee-form system (SchoolModuleConfig) lets a Super Admin
hide or make the field optional per school.  When the field is hidden the form
does not submit a value, so the backend sends None — which previously caused a
database NOT NULL violation.

This migration drops the NOT NULL constraint so hidden or optional job_title
can be stored as NULL without errors.

Revision ID: a0b1c2d3e4f5
Revises: z0a1b2c3d4e5
Create Date: 2026-05-29
"""

from alembic import op
import sqlalchemy as sa

revision = 'a0b1c2d3e4f5'
down_revision = 'z0a1b2c3d4e5'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('employees') as batch:
        batch.alter_column(
            'job_title',
            existing_type=sa.String(150),
            nullable=True,
        )


def downgrade():
    op.execute("UPDATE employees SET job_title = '' WHERE job_title IS NULL")
    with op.batch_alter_table('employees') as batch:
        batch.alter_column(
            'job_title',
            existing_type=sa.String(150),
            nullable=False,
        )
