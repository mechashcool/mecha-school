"""add per-school educational stages

Adds one nullable column:

  * schools.educational_stages (VARCHAR(120) NULL)

Purely additive and non-destructive:
  * NO UPDATE / backfill of any existing schools row. Every existing school
    keeps educational_stages IS NULL, which the application reads as LEGACY
    mode: its grades, sections, subjects and external-registration behaviour
    stay exactly as they are today and are never filtered or provisioned by
    stage. A stage is never inferred from existing data.
  * No grade, section, subject, student or academic row is created, renamed,
    moved or deleted by this migration.
  * No index, constraint, default or server_default is added — a NULL value is
    the meaningful "not configured" state and must stay distinguishable from an
    empty string.
  * Downgrade drops only this column; nothing else is touched.

Revision ID: h1e2d3u4s5t6
Revises: g9s1h2a0b1c2
Create Date: 2026-09-15
"""
from alembic import op
import sqlalchemy as sa


revision = 'h1e2d3u4s5t6'
down_revision = 'g9s1h2a0b1c2'
branch_labels = None
depends_on = None


TABLE = 'schools'
COLUMN = 'educational_stages'


def _has_column() -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return COLUMN in {c['name'] for c in inspector.get_columns(TABLE)}


def upgrade():
    # Idempotent: re-running against a database that already has the column is
    # a no-op rather than an error.
    if not _has_column():
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=120),
                                       nullable=True))


def downgrade():
    if _has_column():
        op.drop_column(TABLE, COLUMN)
