"""add grade_id to schedules (grade-based timetables)

Adds support for grade-based schedules alongside the existing section-based
ones, for schools that do not use sections:
  * schedules.grade_id  — nullable FK -> grades.id (+ index)
  * schedules.section_id — relaxed to NULLABLE (was NOT NULL)
  * new unique constraint uq_schedule_grade_subject_day_start

A schedule row targets EITHER a section (section_id set, grade_id NULL — the
original behaviour, fully preserved) OR a grade (grade_id set, section_id NULL).
All existing rows keep their section_id and get grade_id = NULL, so current
section-based schedules are unchanged.

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-06-09
"""
from alembic import op
import sqlalchemy as sa


revision = 'f5a6b7c8d9e0'
down_revision = 'e4f5a6b7c8d9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('schedules', schema=None) as batch_op:
        batch_op.add_column(sa.Column('grade_id', sa.Integer(), nullable=True))
        # Relax section_id so grade-based rows can leave it NULL.
        batch_op.alter_column('section_id',
                              existing_type=sa.Integer(),
                              nullable=True)
        batch_op.create_index('ix_schedules_grade_id', ['grade_id'], unique=False)
        batch_op.create_foreign_key('fk_schedules_grade_id', 'grades',
                                    ['grade_id'], ['id'])
        batch_op.create_unique_constraint(
            'uq_schedule_grade_subject_day_start',
            ['grade_id', 'subject_id', 'day_of_week', 'start_time'],
        )


def downgrade():
    with op.batch_alter_table('schedules', schema=None) as batch_op:
        batch_op.drop_constraint('uq_schedule_grade_subject_day_start', type_='unique')
        batch_op.drop_constraint('fk_schedules_grade_id', type_='foreignkey')
        batch_op.drop_index('ix_schedules_grade_id')
        batch_op.drop_column('grade_id')
        # Restore NOT NULL on section_id (only safe when no grade-based rows exist).
        batch_op.alter_column('section_id',
                              existing_type=sa.Integer(),
                              nullable=False)
