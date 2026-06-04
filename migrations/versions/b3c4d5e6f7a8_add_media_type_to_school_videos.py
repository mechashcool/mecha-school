"""Add media_type to school_videos

Revision ID: b3c4d5e6f7a8
Revises: a8b9c0d1e2f3
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'b3c4d5e6f7a8'
down_revision = 'a8b9c0d1e2f3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('school_videos') as batch_op:
        batch_op.add_column(
            sa.Column('media_type', sa.String(20), nullable=False,
                      server_default='video')
        )


def downgrade():
    with op.batch_alter_table('school_videos') as batch_op:
        batch_op.drop_column('media_type')
