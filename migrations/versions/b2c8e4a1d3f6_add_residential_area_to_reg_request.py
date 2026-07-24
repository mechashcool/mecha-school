"""Add residential_area_id to student_registration_requests

The public registration form reuses the internal Add Student residential-area
selector, so the intake request must carry the (optional, same-school,
server-validated) residential_area_id that is copied onto the Student at approval.

Nullable + FK only; existing rows are unaffected.

Revision ID: b2c8e4a1d3f6
Revises: a1f7c3d9e2b4
Create Date: 2026-07-25
"""

from alembic import op
import sqlalchemy as sa


revision = 'b2c8e4a1d3f6'
down_revision = 'a1f7c3d9e2b4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('student_registration_requests') as batch:
        batch.add_column(sa.Column('residential_area_id', sa.Integer(), nullable=True))
        batch.create_foreign_key(
            'fk_reg_request_residential_area',
            'residential_areas', ['residential_area_id'], ['id'])
    op.create_index('ix_student_registration_requests_residential_area_id',
                    'student_registration_requests', ['residential_area_id'])


def downgrade():
    op.drop_index('ix_student_registration_requests_residential_area_id',
                  table_name='student_registration_requests')
    with op.batch_alter_table('student_registration_requests') as batch:
        batch.drop_constraint('fk_reg_request_residential_area', type_='foreignkey')
        batch.drop_column('residential_area_id')
