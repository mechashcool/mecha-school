"""add institute instructor (teacher) attendance per lesson

ONE new table: institute_instructor_attendance — one explicit status per
(institute lesson session, teacher).

PURELY ADDITIVE. No existing table, column, constraint or row is read, altered,
backfilled or deleted. employee_attendance is untouched, so payroll deductions,
AI Face device attendance, the leave sync, the daily employee reports and every
school-type institution behave exactly as before.

WHY NOT employee_attendance: it carries UNIQUE (employee_id, date) — one row per
employee per DAY. An institute teacher may teach several lessons on the same
day, each with its own status (present at 16:00, absent at 17:00), which that
table cannot represent without changing the meaning of a column that payroll
and the device integration already depend on.

HISTORY: employee_id is the teacher snapshot written at record time. It is not
re-derived from institute_study_groups.instructor_id later, so reassigning a
group never rewrites recorded attendance.

role accepts 'scheduled' and 'substitute' now so a later substitute-teacher
action needs no further migration; the application writes 'scheduled' only.

ISOLATION is enforced by PostgreSQL through composite foreign keys, the same
pattern every institute table uses:
  * (session_id,  school_id) -> institute_attendance_sessions (id, school_id)
                                [uq_institute_session_id_school]
  * (employee_id, school_id) -> employees (id, school_id)
                                [uq_employee_id_school]
Both ON DELETE RESTRICT, like institute_attendance_records: recorded history is
never cascaded away. The only teardown path is the explicit full-school
cleanup, which deletes this table before sessions and employees.

UNIQUE (session_id, employee_id) makes a retried or concurrent save idempotent.

Locking: CREATE TABLE plus two foreign keys. Adding the FKs takes a brief
SHARE ROW EXCLUSIVE lock on institute_attendance_sessions and employees (reads
continue; writes to those two tables wait for the statement). The new table is
empty, so validation is immediate.

Rollback: downgrade drops only this table (and its indexes/constraints).

Verified against the isolated local test instance (PostgreSQL 18). Not applied
to production.

Revision ID: i7n8s9t0r1a2
Revises: g5s6c0p1e2a3
Create Date: 2026-09-26
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'i7n8s9t0r1a2'
# g5s6c0p1e2a3 is the single COMMITTED head (identical migrations/ tree on this
# branch and origin/main) and the revision of the approved test database. The
# working tree also carries the UNTRACKED sync-foundation revision
# s1y2n3c0f0a1, which branches from c3d9f5b2a7e1 and is its own head; this
# migration does not merge, rebase, reference or depend on it. The number of
# heads is unchanged (this revision replaces g5s6c0p1e2a3 as the tip of the
# mainline). Apply with `upgrade i7n8s9t0r1a2`, never `upgrade head`, while
# that second head exists in the working tree.
down_revision = 'g5s6c0p1e2a3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'institute_instructor_attendance',
        sa.Column('id',          sa.Integer(), nullable=False),
        sa.Column('school_id',   sa.Integer(), nullable=False),
        sa.Column('session_id',  sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('role',        sa.String(length=20), nullable=False,
                  server_default='scheduled'),
        sa.Column('status',      sa.String(length=20), nullable=False),
        sa.Column('source',      sa.String(length=20), nullable=False),
        sa.Column('recorded_by', sa.Integer(), nullable=True),
        sa.Column('recorded_at', sa.DateTime(), nullable=False),
        sa.Column('notes',       sa.Text(), nullable=True),
        sa.Column('created_at',  sa.DateTime(), nullable=True),
        sa.Column('updated_at',  sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                name='fk_institute_instr_att_school'),
        sa.ForeignKeyConstraint(['recorded_by'], ['users.id'],
                                name='fk_institute_instr_att_recorded_by'),
        sa.ForeignKeyConstraint(
            ['session_id', 'school_id'],
            ['institute_attendance_sessions.id',
             'institute_attendance_sessions.school_id'],
            name='fk_institute_instr_att_session_school', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(
            ['employee_id', 'school_id'],
            ['employees.id', 'employees.school_id'],
            name='fk_institute_instr_att_employee_school', ondelete='RESTRICT'),
        sa.UniqueConstraint('session_id', 'employee_id',
                            name='uq_institute_instr_att_session_employee'),
        sa.CheckConstraint("role IN ('scheduled', 'substitute')",
                           name='ck_institute_instr_att_role'),
        sa.CheckConstraint(
            "status IN ('present', 'absent', 'late', 'excused')",
            name='ck_institute_instr_att_status'),
        sa.CheckConstraint(
            "source IN ('manual_admin', 'manual_instructor', 'card', 'device')",
            name='ck_institute_instr_att_source'),
    )
    op.create_index('ix_institute_instructor_attendance_school_id',
                    'institute_instructor_attendance', ['school_id'])
    op.create_index('ix_institute_instructor_attendance_session_id',
                    'institute_instructor_attendance', ['session_id'])
    op.create_index('ix_institute_instructor_attendance_employee_id',
                    'institute_instructor_attendance', ['employee_id'])
    op.create_index('ix_institute_instr_att_school_employee',
                    'institute_instructor_attendance',
                    ['school_id', 'employee_id'])


def downgrade():
    # Only the table this revision created; nothing else is touched.
    op.drop_index('ix_institute_instr_att_school_employee',
                  table_name='institute_instructor_attendance')
    op.drop_index('ix_institute_instructor_attendance_employee_id',
                  table_name='institute_instructor_attendance')
    op.drop_index('ix_institute_instructor_attendance_session_id',
                  table_name='institute_instructor_attendance')
    op.drop_index('ix_institute_instructor_attendance_school_id',
                  table_name='institute_instructor_attendance')
    op.drop_table('institute_instructor_attendance')
