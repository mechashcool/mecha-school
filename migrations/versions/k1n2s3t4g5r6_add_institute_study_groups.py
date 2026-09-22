"""add institute study groups and enrollments

Phase 1 of the institute study-group feature. Institutes organise teaching as
subject -> study group -> instructor instead of the school model
stage -> grade -> section.

  * institute_study_groups       — one named cohort of a subject, one instructor
  * institute_group_enrollments  — a student's membership of a group (history kept)

PURELY ADDITIVE. No existing row is read, modified, backfilled or deleted, and
no existing column changes type or nullability. Grade, Section,
students.section_id and teacher_subjects are untouched. Nothing in the school
code path reads either new table, so a school-type institution behaves exactly
as before.

THE ONE TOUCH TO EXISTING TABLES is three new UNIQUE (id, school_id)
constraints on subjects, employees and students. They are required as the
parent keys of the composite foreign keys below, which is what makes a
cross-school link *impossible at the database level* rather than merely
unchecked by application code. Each one is a new unique index over a column
pair whose left member is already the primary key, so it can never fail on
existing data and never rewrites a row. Verified against the isolated local
instance (PostgreSQL 18.3) before being written here; production is
PostgreSQL 17.6.

ON DELETE SET NULL (instructor_id) uses the PostgreSQL 15+ column-list form.
A bare SET NULL on a composite FK nulls EVERY column of the key, including the
NOT NULL school_id, which would turn employee deletion into a constraint error
instead of clearing the instructor. The column list is therefore load-bearing,
not a stylistic choice.

Revision ID: k1n2s3t4g5r6
Revises: d1o2c3s4d5e6
Create Date: 2026-09-22
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'k1n2s3t4g5r6'
# Explicitly the committed head. The repository also carries an UNRELATED and
# currently untracked sync-foundation revision (s1y2n3c0f0a1) that branched from
# c3d9f5b2a7e1; this migration deliberately does not merge, rebase or depend on
# it. Apply with `flask db upgrade k1n2s3t4g5r6`, never `upgrade head`, while
# that second head exists.
down_revision = 'd1o2c3s4d5e6'
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. Parent keys for the same-school composite FKs ─────────────────────
    # (id) is already unique via the primary key, so (id, school_id) is trivially
    # satisfied by every existing row. No data is touched.
    op.create_unique_constraint('uq_subject_id_school', 'subjects', ['id', 'school_id'])
    op.create_unique_constraint('uq_employee_id_school', 'employees', ['id', 'school_id'])
    op.create_unique_constraint('uq_student_id_school', 'students', ['id', 'school_id'])

    # ── 2. institute_study_groups ────────────────────────────────────────────
    op.create_table(
        'institute_study_groups',
        sa.Column('id',               sa.Integer(), nullable=False),
        sa.Column('school_id',        sa.Integer(), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), nullable=False),
        sa.Column('subject_id',       sa.Integer(), nullable=False),
        sa.Column('instructor_id',    sa.Integer(), nullable=True),
        sa.Column('name',             sa.String(length=150), nullable=False),
        sa.Column('start_date',       sa.Date(), nullable=True),
        sa.Column('end_date',         sa.Date(), nullable=True),
        sa.Column('is_active',        sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('created_at',       sa.DateTime(), nullable=True),
        sa.Column('updated_at',       sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                name='fk_institute_group_school'),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id'],
                                name='fk_institute_group_academic_year'),
        sa.UniqueConstraint('school_id', 'academic_year_id', 'subject_id', 'name',
                            name='uq_institute_group_school_year_subject_name'),
        # Parent key for institute_group_enrollments' composite FK.
        sa.UniqueConstraint('id', 'school_id', name='uq_institute_group_id_school'),
    )
    op.create_index('ix_institute_study_groups_school_id',
                    'institute_study_groups', ['school_id'])
    op.create_index('ix_institute_study_groups_academic_year_id',
                    'institute_study_groups', ['academic_year_id'])
    op.create_index('ix_institute_study_groups_subject_id',
                    'institute_study_groups', ['subject_id'])
    op.create_index('ix_institute_study_groups_instructor_id',
                    'institute_study_groups', ['instructor_id'])
    op.create_index('ix_institute_group_school_year_active',
                    'institute_study_groups',
                    ['school_id', 'academic_year_id', 'is_active'])

    # Same-school ownership of the subject: rejected by PostgreSQL, not by code.
    op.create_foreign_key(
        'fk_institute_group_subject_school', 'institute_study_groups', 'subjects',
        ['subject_id', 'school_id'], ['id', 'school_id'],
    )
    # Same-school ownership of the instructor. The column-list SET NULL clears
    # ONLY instructor_id when the employee row is deleted.
    op.execute(
        'ALTER TABLE institute_study_groups '
        'ADD CONSTRAINT fk_institute_group_instructor_school '
        'FOREIGN KEY (instructor_id, school_id) '
        'REFERENCES employees (id, school_id) '
        'ON DELETE SET NULL (instructor_id)'
    )

    # ── 3. institute_group_enrollments ───────────────────────────────────────
    op.create_table(
        'institute_group_enrollments',
        sa.Column('id',          sa.Integer(), nullable=False),
        sa.Column('school_id',   sa.Integer(), nullable=False),
        sa.Column('group_id',    sa.Integer(), nullable=False),
        sa.Column('student_id',  sa.Integer(), nullable=False),
        sa.Column('enrolled_at', sa.DateTime(), nullable=False),
        sa.Column('ended_at',    sa.DateTime(), nullable=True),
        sa.Column('status',      sa.String(length=20), nullable=False,
                  server_default='active'),
        sa.Column('created_at',  sa.DateTime(), nullable=True),
        sa.Column('updated_at',  sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                name='fk_institute_enrollment_school'),
        sa.ForeignKeyConstraint(
            ['group_id', 'school_id'],
            ['institute_study_groups.id', 'institute_study_groups.school_id'],
            name='fk_institute_enrollment_group_school', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(
            ['student_id', 'school_id'], ['students.id', 'students.school_id'],
            name='fk_institute_enrollment_student_school', ondelete='RESTRICT'),
        # An active row never carries ended_at; an ended row always does.
        sa.CheckConstraint(
            "(status = 'active' AND ended_at IS NULL) OR "
            "(status = 'ended' AND ended_at IS NOT NULL)",
            name='ck_institute_enrollment_status_ended_at'),
    )
    op.create_index('ix_institute_group_enrollments_school_id',
                    'institute_group_enrollments', ['school_id'])
    op.create_index('ix_institute_group_enrollments_group_id',
                    'institute_group_enrollments', ['group_id'])
    op.create_index('ix_institute_group_enrollments_student_id',
                    'institute_group_enrollments', ['student_id'])
    op.create_index('ix_institute_enrollment_school_student_status',
                    'institute_group_enrollments',
                    ['school_id', 'student_id', 'status'])
    op.create_index('ix_institute_enrollment_group_status',
                    'institute_group_enrollments', ['group_id', 'status'])
    # At most ONE active enrollment per (group, student). Partial: ended rows are
    # excluded, so a student may be re-enrolled later as a new historical row.
    op.create_index('uq_institute_enrollment_active',
                    'institute_group_enrollments', ['group_id', 'student_id'],
                    unique=True, postgresql_where=sa.text("status = 'active'"))


def downgrade():
    op.drop_index('uq_institute_enrollment_active',
                  table_name='institute_group_enrollments')
    op.drop_index('ix_institute_enrollment_group_status',
                  table_name='institute_group_enrollments')
    op.drop_index('ix_institute_enrollment_school_student_status',
                  table_name='institute_group_enrollments')
    op.drop_index('ix_institute_group_enrollments_student_id',
                  table_name='institute_group_enrollments')
    op.drop_index('ix_institute_group_enrollments_group_id',
                  table_name='institute_group_enrollments')
    op.drop_index('ix_institute_group_enrollments_school_id',
                  table_name='institute_group_enrollments')
    op.drop_table('institute_group_enrollments')

    op.drop_constraint('fk_institute_group_instructor_school',
                       'institute_study_groups', type_='foreignkey')
    op.drop_constraint('fk_institute_group_subject_school',
                       'institute_study_groups', type_='foreignkey')
    op.drop_index('ix_institute_group_school_year_active',
                  table_name='institute_study_groups')
    op.drop_index('ix_institute_study_groups_instructor_id',
                  table_name='institute_study_groups')
    op.drop_index('ix_institute_study_groups_subject_id',
                  table_name='institute_study_groups')
    op.drop_index('ix_institute_study_groups_academic_year_id',
                  table_name='institute_study_groups')
    op.drop_index('ix_institute_study_groups_school_id',
                  table_name='institute_study_groups')
    op.drop_table('institute_study_groups')

    op.drop_constraint('uq_student_id_school', 'students', type_='unique')
    op.drop_constraint('uq_employee_id_school', 'employees', type_='unique')
    op.drop_constraint('uq_subject_id_school', 'subjects', type_='unique')
