"""allow an attendance shift to have no lateness cutoff (institutes)

Relaxes NOT NULL on two columns of ``attendance_shifts``:

  * late_after_time   — the shift's lateness cutoff. NULL now means "lateness is
    switched off for this shift", which is what an institute selects by leaving
    the field blank. School forms still require it, so a school can never
    produce a NULL here.
  * absent_after_time — LEGACY column, never read for any behaviour (see
    School.shift_absent_after_time). It is relaxed only because the shift-create
    path derives it from late_after_time; with lateness blank there is no honest
    value to store, and inventing one would fabricate a cutoff.

Purely permissive and non-destructive:
  * NO UPDATE, backfill or bulk change of any row. Every existing shift keeps
    the exact times it has today.
  * Widening a constraint only: rows valid before are still valid after.
  * No column is added, dropped, renamed or re-typed; no index or default
    changes; no other table is touched.
  * Downgrade restores NOT NULL, but ONLY when no NULL values exist. If any do,
    it aborts with a clear message instead of inventing times to satisfy the
    constraint — the operator must decide what those shifts should contain.

Revision ID: j1s2h3l4t5n6
Revises: i1n2s3t4t5y6
Create Date: 2026-09-18
"""
from alembic import op
import sqlalchemy as sa


revision = 'j1s2h3l4t5n6'
down_revision = 'i1n2s3t4t5y6'
branch_labels = None
depends_on = None


TABLE = 'attendance_shifts'
COLUMNS = ('late_after_time', 'absent_after_time')


def _nullable_map() -> dict:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {c['name']: c['nullable'] for c in inspector.get_columns(TABLE)}


def upgrade():
    current = _nullable_map()
    for column in COLUMNS:
        # Idempotent: already-nullable columns are left alone.
        if current.get(column) is False:
            op.alter_column(TABLE, column, existing_type=sa.Time(),
                            nullable=True)


def downgrade():
    bind = op.get_bind()
    current = _nullable_map()
    for column in COLUMNS:
        if current.get(column) is not True:
            continue
        nulls = bind.execute(sa.text(
            f'SELECT COUNT(*) FROM {TABLE} WHERE {column} IS NULL'
        )).scalar()
        if nulls:
            raise RuntimeError(
                f'Cannot restore NOT NULL on {TABLE}.{column}: {nulls} row(s) '
                f'hold NULL. Set a real value for those shifts first — this '
                f'migration will not invent a cutoff time.'
            )
        op.alter_column(TABLE, column, existing_type=sa.Time(), nullable=False)
