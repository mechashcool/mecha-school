"""Add stage column to grades table

Revision ID: i3j4k5l6m7n8
Revises: h2i3j4k5l6m7
Create Date: 2026-05-07
"""

from alembic import op
import sqlalchemy as sa

revision = 'i3j4k5l6m7n8'
down_revision = 'h2i3j4k5l6m7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('grades') as batch_op:
        batch_op.add_column(sa.Column('stage', sa.String(50), nullable=True))

    # Backfill stage based on grade name keywords
    op.execute("""
        UPDATE grades SET stage = 'ابتدائية'
        WHERE name LIKE '%ابتدائي%'
    """)
    op.execute("""
        UPDATE grades SET stage = 'متوسطة'
        WHERE name LIKE '%متوسط%'
    """)
    op.execute("""
        UPDATE grades SET stage = 'إعدادية'
        WHERE name LIKE '%إعدادي%'
    """)


def downgrade():
    with op.batch_alter_table('grades') as batch_op:
        batch_op.drop_column('stage')
