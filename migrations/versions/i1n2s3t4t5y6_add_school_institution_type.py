"""add per-institution type (school / institute)

Adds one nullable column:

  * schools.institution_type (VARCHAR(20) NULL)

Purely additive and non-destructive:
  * NO UPDATE / backfill of any existing schools row. Every existing row keeps
    institution_type IS NULL, which the application reads as the current school
    behaviour: student automatic absence keeps running exactly as it does today.
  * No server_default is added — NULL is the meaningful "not chosen" state and
    must stay distinguishable from an explicit 'school' selection.
  * No attendance, student, notification or settings row is created, updated or
    deleted by this migration. Historical absence records and the att_* cutoff
    columns are untouched, so switching an institution back to 'school' restores
    the previous behaviour from its existing settings.
  * Downgrade drops only this column; nothing else is touched.

Revision ID: i1n2s3t4t5y6
Revises: h1e2d3u4s5t6
Create Date: 2026-09-18
"""
from alembic import op
import sqlalchemy as sa


revision = 'i1n2s3t4t5y6'
down_revision = 'h1e2d3u4s5t6'
branch_labels = None
depends_on = None


TABLE = 'schools'
COLUMN = 'institution_type'


def _has_column() -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return COLUMN in {c['name'] for c in inspector.get_columns(TABLE)}


def upgrade():
    # Idempotent: re-running against a database that already has the column is
    # a no-op rather than an error.
    if not _has_column():
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=20),
                                       nullable=True))


def downgrade():
    if _has_column():
        op.drop_column(TABLE, COLUMN)
