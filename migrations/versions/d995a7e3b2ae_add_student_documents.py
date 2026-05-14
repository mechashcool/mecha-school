"""Add student documents table

Revision ID: d995a7e3b2ae
Revises: f5c78bef4cc4
Create Date: 2026-04-25 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'd995a7e3b2ae'
down_revision = 'f5c78bef4cc4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'student_documents',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('student_id', sa.Integer(), sa.ForeignKey('students.id'), nullable=False),
        sa.Column('document_type', sa.String(length=100), nullable=False),
        sa.Column('file_path', sa.String(length=255), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table('student_documents')
