"""add institute group weekly schedules and manual attendance

Institute groups meet on a RECURRING weekly pattern. This revision stores those
patterns as RULES and stores attendance per MATERIALIZED SESSION.

  1. institute_group_schedules      — recurring weekly slot (day + start + end)
  2. institute_attendance_sessions  — one materialized occurrence, with a
                                      historical date/time/instructor snapshot
  3. institute_attendance_records   — one explicit status per (session, student)

PURELY ADDITIVE. Three new tables. No existing table, column, constraint or row
is read, altered, backfilled or deleted. student_attendance is untouched, so
school-section attendance and the face/card device integration behave exactly
as before.

WHY NOT student_attendance: it carries UNIQUE (student_id, date) — one row per
student per DAY. An institute student may legitimately attend two different
groups on the same day, so reusing that table would make the second group's
attendance impossible to store. A per-session table is a requirement, not a
preference.

NO SCHEDULER, NO PRE-GENERATION. Occurrences are computed from the rules for
display and a session row is inserted only when a human opens or records
attendance. Passing a scheduled time therefore cannot mark anybody absent:
there is no process that would write such a row.

CONCURRENCY / IDEMPOTENCY is enforced by two unique constraints rather than by
pre-insert SELECTs:
  * uq_institute_session_group_date_start          one session per occurrence
  * uq_institute_attendance_session_student        one result per student
Two simultaneous submissions race at the database; the loser gets an
IntegrityError and recovers by re-selecting the winning row.

ON DELETE RESTRICT on every reference into groups, sessions and students: an
exam-style dependency guard, so attendance history can never be silently
orphaned or cascaded away. schedule_id is the one exception — ON DELETE SET
NULL — so deleting a weekly rule never deletes recorded history; the session
keeps its own date/time snapshot.

Verified against the isolated local instance (PostgreSQL 18.3); production is
PostgreSQL 17.6. Not applied to production.

Revision ID: a9t8n9d0s1c2
Revises: e3x4m5g6r7p8
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a9t8n9d0s1c2'
# e3x4m5g6r7p8 was verified COMMITTED (present in `git ls-files`) and the single
# head of the tracked migration graph. The repository also carries an UNRELATED
# and still untracked sync-foundation revision (s1y2n3c0f0a1) on its own branch;
# this migration does not merge, rebase, reference or depend on it. Apply with
# `flask db upgrade a9t8n9d0s1c2`, never `upgrade head`, while that second head
# exists in the working tree.
down_revision = 'e3x4m5g6r7p8'
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. Recurring weekly slots ────────────────────────────────────────────
    op.create_table(
        'institute_group_schedules',
        sa.Column('id',               sa.Integer(), nullable=False),
        sa.Column('school_id',        sa.Integer(), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), nullable=False),
        sa.Column('group_id',         sa.Integer(), nullable=False),
        sa.Column('day_of_week',      sa.Integer(), nullable=False),
        sa.Column('start_time',       sa.Time(), nullable=False),
        sa.Column('end_time',         sa.Time(), nullable=False),
        sa.Column('is_active',        sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('created_at',       sa.DateTime(), nullable=True),
        sa.Column('updated_at',       sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                name='fk_institute_schedule_school'),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id'],
                                name='fk_institute_schedule_academic_year'),
        sa.UniqueConstraint('group_id', 'day_of_week', 'start_time',
                            name='uq_institute_schedule_group_day_start'),
        sa.CheckConstraint('start_time < end_time',
                           name='ck_institute_schedule_time_order'),
        sa.CheckConstraint('day_of_week >= 0 AND day_of_week <= 6',
                           name='ck_institute_schedule_day_range'),
    )
    op.create_index('ix_institute_group_schedules_school_id',
                    'institute_group_schedules', ['school_id'])
    op.create_index('ix_institute_group_schedules_academic_year_id',
                    'institute_group_schedules', ['academic_year_id'])
    op.create_index('ix_institute_group_schedules_group_id',
                    'institute_group_schedules', ['group_id'])
    op.create_index('ix_institute_schedule_group_active',
                    'institute_group_schedules', ['group_id', 'is_active'])
    op.create_foreign_key(
        'fk_institute_schedule_group_school', 'institute_group_schedules',
        'institute_study_groups', ['group_id', 'school_id'], ['id', 'school_id'],
        ondelete='RESTRICT',
    )

    # ── 2. Materialized occurrences ──────────────────────────────────────────
    op.create_table(
        'institute_attendance_sessions',
        sa.Column('id',               sa.Integer(), nullable=False),
        sa.Column('school_id',        sa.Integer(), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), nullable=False),
        sa.Column('group_id',         sa.Integer(), nullable=False),
        sa.Column('schedule_id',      sa.Integer(), nullable=True),
        sa.Column('session_date',     sa.Date(), nullable=False),
        sa.Column('start_time',       sa.Time(), nullable=False),
        sa.Column('end_time',         sa.Time(), nullable=False),
        sa.Column('instructor_id',    sa.Integer(), nullable=True),
        sa.Column('status',           sa.String(length=20), nullable=False,
                  server_default='not_recorded'),
        sa.Column('source',           sa.String(length=20), nullable=True),
        sa.Column('recorded_by',      sa.Integer(), nullable=True),
        sa.Column('recorded_at',      sa.DateTime(), nullable=True),
        sa.Column('created_at',       sa.DateTime(), nullable=True),
        sa.Column('updated_at',       sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                name='fk_institute_session_school'),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id'],
                                name='fk_institute_session_academic_year'),
        # Deleting a weekly rule must NEVER delete recorded history.
        sa.ForeignKeyConstraint(['schedule_id'], ['institute_group_schedules.id'],
                                name='fk_institute_session_schedule',
                                ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['recorded_by'], ['users.id'],
                                name='fk_institute_session_recorded_by'),
        sa.UniqueConstraint('group_id', 'session_date', 'start_time',
                            name='uq_institute_session_group_date_start'),
        # Parent key for the attendance rows' composite FK.
        sa.UniqueConstraint('id', 'school_id',
                            name='uq_institute_session_id_school'),
        sa.CheckConstraint('start_time < end_time',
                           name='ck_institute_session_time_order'),
        sa.CheckConstraint("status IN ('not_recorded', 'recorded')",
                           name='ck_institute_session_status'),
        sa.CheckConstraint(
            "source IS NULL OR source IN "
            "('manual_admin', 'manual_instructor', 'card', 'device')",
            name='ck_institute_session_source'),
        sa.CheckConstraint(
            "status = 'not_recorded' OR "
            "(source IS NOT NULL AND recorded_at IS NOT NULL)",
            name='ck_institute_session_recorded_fields'),
    )
    op.create_index('ix_institute_attendance_sessions_school_id',
                    'institute_attendance_sessions', ['school_id'])
    op.create_index('ix_institute_attendance_sessions_academic_year_id',
                    'institute_attendance_sessions', ['academic_year_id'])
    op.create_index('ix_institute_attendance_sessions_group_id',
                    'institute_attendance_sessions', ['group_id'])
    op.create_index('ix_institute_attendance_sessions_schedule_id',
                    'institute_attendance_sessions', ['schedule_id'])
    op.create_index('ix_institute_attendance_sessions_instructor_id',
                    'institute_attendance_sessions', ['instructor_id'])
    op.create_index('ix_institute_session_school_date',
                    'institute_attendance_sessions',
                    ['school_id', 'session_date'])
    op.create_index('ix_institute_session_group_date',
                    'institute_attendance_sessions',
                    ['group_id', 'session_date'])
    op.create_foreign_key(
        'fk_institute_session_group_school', 'institute_attendance_sessions',
        'institute_study_groups', ['group_id', 'school_id'], ['id', 'school_id'],
        ondelete='RESTRICT',
    )

    # ── 3. One explicit status per (session, student) ────────────────────────
    op.create_table(
        'institute_attendance_records',
        sa.Column('id',          sa.Integer(), nullable=False),
        sa.Column('school_id',   sa.Integer(), nullable=False),
        sa.Column('session_id',  sa.Integer(), nullable=False),
        sa.Column('student_id',  sa.Integer(), nullable=False),
        sa.Column('status',      sa.String(length=20), nullable=False),
        sa.Column('source',      sa.String(length=20), nullable=False),
        sa.Column('recorded_by', sa.Integer(), nullable=True),
        sa.Column('recorded_at', sa.DateTime(), nullable=False),
        sa.Column('notes',       sa.Text(), nullable=True),
        sa.Column('created_at',  sa.DateTime(), nullable=True),
        sa.Column('updated_at',  sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                name='fk_institute_attendance_school'),
        sa.ForeignKeyConstraint(['recorded_by'], ['users.id'],
                                name='fk_institute_attendance_recorded_by'),
        sa.UniqueConstraint('session_id', 'student_id',
                            name='uq_institute_attendance_session_student'),
        sa.CheckConstraint(
            "status IN ('present', 'absent', 'late', 'excused')",
            name='ck_institute_attendance_status'),
        sa.CheckConstraint(
            "source IN ('manual_admin', 'manual_instructor', 'card', 'device')",
            name='ck_institute_attendance_source'),
    )
    op.create_index('ix_institute_attendance_records_school_id',
                    'institute_attendance_records', ['school_id'])
    op.create_index('ix_institute_attendance_records_session_id',
                    'institute_attendance_records', ['session_id'])
    op.create_index('ix_institute_attendance_records_student_id',
                    'institute_attendance_records', ['student_id'])
    op.create_index('ix_institute_attendance_student_status',
                    'institute_attendance_records',
                    ['school_id', 'student_id', 'status'])
    op.create_foreign_key(
        'fk_institute_attendance_session_school',
        'institute_attendance_records', 'institute_attendance_sessions',
        ['session_id', 'school_id'], ['id', 'school_id'], ondelete='RESTRICT',
    )
    op.create_foreign_key(
        'fk_institute_attendance_student_school',
        'institute_attendance_records', 'students',
        ['student_id', 'school_id'], ['id', 'school_id'], ondelete='RESTRICT',
    )


def downgrade():
    # Reverse order: records reference sessions and students, sessions
    # reference schedules and groups.
    op.drop_constraint('fk_institute_attendance_student_school',
                       'institute_attendance_records', type_='foreignkey')
    op.drop_constraint('fk_institute_attendance_session_school',
                       'institute_attendance_records', type_='foreignkey')
    op.drop_index('ix_institute_attendance_student_status',
                  table_name='institute_attendance_records')
    op.drop_index('ix_institute_attendance_records_student_id',
                  table_name='institute_attendance_records')
    op.drop_index('ix_institute_attendance_records_session_id',
                  table_name='institute_attendance_records')
    op.drop_index('ix_institute_attendance_records_school_id',
                  table_name='institute_attendance_records')
    op.drop_table('institute_attendance_records')

    op.drop_constraint('fk_institute_session_group_school',
                       'institute_attendance_sessions', type_='foreignkey')
    op.drop_index('ix_institute_session_group_date',
                  table_name='institute_attendance_sessions')
    op.drop_index('ix_institute_session_school_date',
                  table_name='institute_attendance_sessions')
    op.drop_index('ix_institute_attendance_sessions_instructor_id',
                  table_name='institute_attendance_sessions')
    op.drop_index('ix_institute_attendance_sessions_schedule_id',
                  table_name='institute_attendance_sessions')
    op.drop_index('ix_institute_attendance_sessions_group_id',
                  table_name='institute_attendance_sessions')
    op.drop_index('ix_institute_attendance_sessions_academic_year_id',
                  table_name='institute_attendance_sessions')
    op.drop_index('ix_institute_attendance_sessions_school_id',
                  table_name='institute_attendance_sessions')
    op.drop_table('institute_attendance_sessions')

    op.drop_constraint('fk_institute_schedule_group_school',
                       'institute_group_schedules', type_='foreignkey')
    op.drop_index('ix_institute_schedule_group_active',
                  table_name='institute_group_schedules')
    op.drop_index('ix_institute_group_schedules_group_id',
                  table_name='institute_group_schedules')
    op.drop_index('ix_institute_group_schedules_academic_year_id',
                  table_name='institute_group_schedules')
    op.drop_index('ix_institute_group_schedules_school_id',
                  table_name='institute_group_schedules')
    op.drop_table('institute_group_schedules')
