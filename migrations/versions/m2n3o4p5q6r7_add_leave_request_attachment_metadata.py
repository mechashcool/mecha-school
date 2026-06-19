"""Add attachment metadata columns to leave_requests

Revision ID: m2n3o4p5q6r7
Revises: l1m2n3o4p5q6
Create Date: 2026-06-19

Adds three nullable columns to leave_requests to store file metadata
when an attachment is uploaded alongside a leave request from the
parent mobile app:

  attachment_name  VARCHAR(255) – original client filename (display only)
  attachment_mime  VARCHAR(100) – server-validated MIME type
  attachment_size  INTEGER      – file size in bytes

All three default to NULL so existing rows and the web-portal upload
path (which already writes only attachment_path) are unaffected.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm2n3o4p5q6r7'
down_revision = 'l1m2n3o4p5q6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('leave_requests',
        sa.Column('attachment_name', sa.String(255), nullable=True))
    op.add_column('leave_requests',
        sa.Column('attachment_mime', sa.String(100), nullable=True))
    op.add_column('leave_requests',
        sa.Column('attachment_size', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('leave_requests', 'attachment_size')
    op.drop_column('leave_requests', 'attachment_mime')
    op.drop_column('leave_requests', 'attachment_name')
