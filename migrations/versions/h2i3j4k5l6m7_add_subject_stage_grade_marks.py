"""Add stage, grade_id, total_marks, pass_marks to subjects

Revision ID: h2i3j4k5l6m7
Revises: g1h2i3j4k5l6
Create Date: 2026-05-07
"""

from alembic import op
import sqlalchemy as sa

revision = 'h2i3j4k5l6m7'
down_revision = 'g1h2i3j4k5l6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('subjects') as batch_op:
        batch_op.add_column(sa.Column('stage', sa.String(50), nullable=True))
        batch_op.add_column(sa.Column('grade_id', sa.Integer,
                                      sa.ForeignKey('grades.id'), nullable=True))
        batch_op.add_column(sa.Column('total_marks', sa.Numeric(8, 2), nullable=True))
        batch_op.add_column(sa.Column('pass_marks',  sa.Numeric(8, 2), nullable=True))
        batch_op.create_index('ix_subjects_grade_id', ['grade_id'])


def downgrade():
    with op.batch_alter_table('subjects') as batch_op:
        batch_op.drop_index('ix_subjects_grade_id')
        batch_op.drop_column('pass_marks')
        batch_op.drop_column('total_marks')
        batch_op.drop_column('grade_id')
        batch_op.drop_column('stage')
