"""Add extra_fields_json to student_registration_records

Revision ID: a8b9c0d1e2f3
Revises: b2c3d4e5f6a7
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'a8b9c0d1e2f3'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('student_registration_records') as batch_op:
        batch_op.add_column(sa.Column('extra_fields_json', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('student_registration_records') as batch_op:
        batch_op.drop_column('extra_fields_json')
