"""Add size field to inventory items

Adds a nullable `size` column to inventory_items for clothing/uniform stock
tracking. Each clothing item + size combination is stored as a separate row,
keeping existing non-clothing items unchanged (size = NULL).

Revision ID: g6h7i8j9k0l1
Revises: f5a6b7c8d9e0
Create Date: 2026-06-10
"""
from alembic import op
import sqlalchemy as sa


revision = 'g6h7i8j9k0l1'
down_revision = 'f5a6b7c8d9e0'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('inventory_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('size', sa.String(length=20), nullable=True))


def downgrade():
    with op.batch_alter_table('inventory_items', schema=None) as batch_op:
        batch_op.drop_column('size')
