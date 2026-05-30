"""Widen student_id/employee_id columns and remove stale global unique indexes

Background
----------
The original schema had globally-unique indexes on students.student_id and
employees.employee_id.  Migration b8c9d0e1f2a3 attempted to replace them
with non-unique indexes, but depending on how the DB was initialised the
old unique index may still be present under the same or a similar name.

If a stale global unique index remains, the school-aware code generator can
fail even for a brand-new school because it only queries per-school rows
(finding zero), generates suffix 000001, and then hits the global unique
constraint at INSERT time.

This migration:
  1. Drops any unique index/constraint on students.student_id that covers
     only that single column (not the composite school-scoped ones).
  2. Drops any unique index/constraint on employees.employee_id that covers
     only that single column.
  3. Recreates both as plain (non-unique) indexes if they went missing.
  4. Widens both columns from VARCHAR(20) → VARCHAR(40) so that longer school
     codes (up to the 20-char maximum) fit without overflow.

Revision ID: y9z0a1b2c3d4
Revises: x8y9z0a1b2c3
Create Date: 2026-05-29
"""

from alembic import op
import sqlalchemy as sa

revision = 'y9z0a1b2c3d4'
down_revision = 'x8y9z0a1b2c3'
branch_labels = None
depends_on = None


# ── helpers ──────────────────────────────────────────────────────────────────

def _insp():
    return sa.inspect(op.get_bind())


def _index_names(table):
    return {idx['name']: idx for idx in _insp().get_indexes(table)}


def _uq_names(table):
    return {uq['name']: uq for uq in _insp().get_unique_constraints(table)}


def _drop_single_col_unique_indexes(table, column):
    """
    Drop every unique index/constraint on *table* that covers *only* the
    given *column* (i.e. global unique — not a composite key).
    """
    conn = op.get_bind()
    dialect = conn.dialect.name

    # Drop pure-unique constraints (ALTERed via op.create_unique_constraint)
    for name, uq in list(_uq_names(table).items()):
        cols = [c for c in uq.get('column_names', [])]
        if cols == [column]:
            op.drop_constraint(name, table, type_='unique')

    # Drop unique indexes (created via op.create_index(..., unique=True))
    for name, idx in list(_index_names(table).items()):
        cols = [c for c in idx.get('column_names', [])]
        if cols == [column] and idx.get('unique'):
            if dialect == 'postgresql':
                conn.execute(sa.text(f'DROP INDEX IF EXISTS "{name}"'))
            else:
                op.drop_index(name, table_name=table)


def _ensure_plain_index(name, table, columns):
    """Create a non-unique index if it doesn't already exist."""
    existing = _index_names(table)
    if name not in existing:
        op.create_index(name, table, columns, unique=False)


def _widen_column(table, column, new_type):
    """ALTER COLUMN to a wider type only if current length is smaller."""
    cols = {c['name']: c for c in _insp().get_columns(table)}
    if column not in cols:
        return
    current = cols[column]['type']
    try:
        current_len = current.length
    except AttributeError:
        return
    if current_len is not None and current_len < new_type.length:
        with op.batch_alter_table(table) as batch:
            batch.alter_column(column, type_=new_type, existing_nullable=False)


# ── upgrade ───────────────────────────────────────────────────────────────────

def upgrade():
    # 1. Remove any stale global-unique index on students.student_id
    _drop_single_col_unique_indexes('students', 'student_id')

    # 2. Ensure the plain lookup index still exists
    _ensure_plain_index('ix_students_student_id', 'students', ['student_id'])

    # 3. Remove any stale global-unique index on employees.employee_id
    _drop_single_col_unique_indexes('employees', 'employee_id')

    # 4. Ensure the plain lookup index still exists
    _ensure_plain_index('ix_employees_employee_id', 'employees', ['employee_id'])

    # 5. Widen student_id: VARCHAR(20) → VARCHAR(40)
    _widen_column('students', 'student_id', sa.String(40))

    # 6. Widen employee_id: VARCHAR(20) → VARCHAR(40)
    _widen_column('employees', 'employee_id', sa.String(40))


# ── downgrade ─────────────────────────────────────────────────────────────────

def downgrade():
    # Narrowing columns back is intentionally a no-op: shrinking VARCHAR can
    # truncate data.  The unique indexes are also not restored because they
    # caused the bug this migration was written to fix.
    pass
