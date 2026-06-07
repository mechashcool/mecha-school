"""add grade shift_id for grade-level shift assignment

Adds Grade.shift_id so schools without sections can assign an attendance
shift directly to a grade/class.  Section.shift_id still takes priority;
this is purely a fallback that get_student_shift() uses when a student's
section has no shift assigned.

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-06-06
"""
from alembic import op
import sqlalchemy as sa


revision = 'e4f5a6b7c8d9'
down_revision = 'd3e4f5a6b7c8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('grades', schema=None) as batch_op:
        batch_op.add_column(sa.Column('shift_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_grades_shift_id', 'attendance_shifts',
            ['shift_id'], ['id'],
        )


def downgrade():
    with op.batch_alter_table('grades', schema=None) as batch_op:
        batch_op.drop_constraint('fk_grades_shift_id', type_='foreignkey')
        batch_op.drop_column('shift_id')
