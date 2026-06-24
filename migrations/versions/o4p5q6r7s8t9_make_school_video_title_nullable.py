"""Make school_videos.title nullable

Background
----------
The title field for school board media (SchoolVideo) was NOT NULL.
Making it optional so users can upload images or videos without providing a title.

Revision ID: o4p5q6r7s8t9
Revises: n3o4p5q6r7s8
Create Date: 2026-06-24
"""

from alembic import op
import sqlalchemy as sa

revision = 'o4p5q6r7s8t9'
down_revision = 'n3o4p5q6r7s8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('school_videos') as batch:
        batch.alter_column(
            'title',
            existing_type=sa.String(200),
            nullable=True,
        )


def downgrade():
    op.execute("UPDATE school_videos SET title = '' WHERE title IS NULL")
    with op.batch_alter_table('school_videos') as batch:
        batch.alter_column(
            'title',
            existing_type=sa.String(200),
            nullable=False,
        )
