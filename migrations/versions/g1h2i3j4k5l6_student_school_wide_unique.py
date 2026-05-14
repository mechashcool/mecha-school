"""Student unique constraint: school-wide (no academic_year_id)

Students now persist across academic years — their student_id must be
unique per school, not per school+year.

This migration:
  1. Detects and skips if duplicates exist (school_id, student_id must be
     checked before running — duplicates indicate year-duplication that
     should be cleaned up first).
  2. Drops  uq_student_school_year_student_id  (school + year + student_id).
  3. Creates uq_student_school_student_id       (school + student_id).

Revision ID: g1h2i3j4k5l6
Revises: a2b3c4d5e6f7
Create Date: 2026-05-07
"""

from alembic import op
import sqlalchemy as sa

revision = 'g1h2i3j4k5l6'
down_revision = 'a2b3c4d5e6f7'
branch_labels = None
depends_on = None


def _insp():
    return sa.inspect(op.get_bind())


def _uq_names(table):
    return {uq['name'] for uq in _insp().get_unique_constraints(table)}


def _drop_uq_if_exists(name, table):
    if name in _uq_names(table):
        op.drop_constraint(name, table, type_='unique')


def _create_uq_if_missing(name, table, cols):
    if name not in _uq_names(table):
        op.create_unique_constraint(name, table, cols)


def upgrade():
    conn = op.get_bind()

    # Safety check: abort if there are (school_id, student_id) duplicates.
    # Duplicates mean the same student was re-entered in multiple years —
    # these rows must be merged/removed before tightening the constraint.
    dup_count = conn.execute(sa.text("""
        SELECT COUNT(*) FROM (
            SELECT school_id, student_id
            FROM students
            GROUP BY school_id, student_id
            HAVING COUNT(*) > 1
        ) AS dups
    """)).scalar()

    if dup_count and dup_count > 0:
        raise Exception(
            f"Cannot apply migration: {dup_count} duplicate (school_id, student_id) "
            "pair(s) found in the students table. "
            "Deduplicate the rows first (keep the most-recent record per student, "
            "then re-run the migration)."
        )

    _drop_uq_if_exists('uq_student_school_year_student_id', 'students')
    _create_uq_if_missing('uq_student_school_student_id', 'students',
                          ['school_id', 'student_id'])


def downgrade():
    _drop_uq_if_exists('uq_student_school_student_id', 'students')
    _create_uq_if_missing('uq_student_school_year_student_id', 'students',
                          ['school_id', 'academic_year_id', 'student_id'])
