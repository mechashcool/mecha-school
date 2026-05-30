"""Make students.date_of_birth and students.gender nullable

Background
----------
The Student model originally declared date_of_birth and gender as NOT NULL.
The configurable student-form system (SchoolStudentFormConfig) lets a Super Admin
hide or make either field optional per school.  When a field is hidden the form
does not submit a value, so the backend sends None — which previously caused a
psycopg2.errors.NotNullViolation (500 error).

This migration drops the NOT NULL constraint on both columns so that hidden or
optional fields can be stored as NULL without errors.

Revision ID: z0a1b2c3d4e5
Revises: y9z0a1b2c3d4
Create Date: 2026-05-29
"""

from alembic import op
import sqlalchemy as sa

revision = 'z0a1b2c3d4e5'
down_revision = 'y9z0a1b2c3d4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('students') as batch:
        batch.alter_column(
            'date_of_birth',
            existing_type=sa.Date(),
            nullable=True,
        )
        batch.alter_column(
            'gender',
            existing_type=sa.String(10),
            nullable=True,
        )


def downgrade():
    # Re-applying NOT NULL would fail if any rows already contain NULL values.
    # Fill NULLs with placeholders before tightening the constraint.
    op.execute(
        "UPDATE students SET date_of_birth = '2000-01-01' WHERE date_of_birth IS NULL"
    )
    op.execute(
        "UPDATE students SET gender = 'unknown' WHERE gender IS NULL"
    )
    with op.batch_alter_table('students') as batch:
        batch.alter_column(
            'date_of_birth',
            existing_type=sa.Date(),
            nullable=False,
        )
        batch.alter_column(
            'gender',
            existing_type=sa.String(10),
            nullable=False,
        )
