"""Add exam_time and duration_minutes to exams

Revision ID: i8j9k0l1m2n3
Revises: h7i8j9k0l1m2
Create Date: 2026-06-13
"""
from alembic import op
import sqlalchemy as sa

revision = 'i8j9k0l1m2n3'
down_revision = 'h7i8j9k0l1m2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('exams', schema=None) as batch_op:
        batch_op.add_column(sa.Column('exam_time', sa.Time(), nullable=True))
        batch_op.add_column(sa.Column('duration_minutes', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('exams', schema=None) as batch_op:
        batch_op.drop_column('duration_minutes')
        batch_op.drop_column('exam_time')
