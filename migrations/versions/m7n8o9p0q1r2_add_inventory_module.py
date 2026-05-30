"""Add inventory module

Revision ID: m7n8o9p0q1r2
Revises: l6m7n8o9p0q1
Create Date: 2026-05-22
"""

from alembic import op
import sqlalchemy as sa


revision = 'm7n8o9p0q1r2'
down_revision = 'l6m7n8o9p0q1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'inventory_categories',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id']),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('school_id', 'academic_year_id', 'name',
                            name='uq_inventory_category_school_year_name'),
    )
    op.create_index(op.f('ix_inventory_categories_academic_year_id'), 'inventory_categories', ['academic_year_id'])
    op.create_index(op.f('ix_inventory_categories_school_id'), 'inventory_categories', ['school_id'])

    op.create_table(
        'inventory_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('item_code', sa.String(length=80), nullable=True),
        sa.Column('unit', sa.String(length=40), nullable=False),
        sa.Column('current_quantity', sa.Numeric(12, 2), nullable=False),
        sa.Column('minimum_quantity', sa.Numeric(12, 2), nullable=False),
        sa.Column('purchase_price', sa.Numeric(12, 2), nullable=True),
        sa.Column('supplier', sa.String(length=200), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('image_path', sa.String(length=500), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id']),
        sa.ForeignKeyConstraint(['category_id'], ['inventory_categories.id']),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('school_id', 'academic_year_id', 'item_code',
                            name='uq_inventory_item_school_year_code'),
    )
    op.create_index(op.f('ix_inventory_items_academic_year_id'), 'inventory_items', ['academic_year_id'])
    op.create_index(op.f('ix_inventory_items_category_id'), 'inventory_items', ['category_id'])
    op.create_index(op.f('ix_inventory_items_is_active'), 'inventory_items', ['is_active'])
    op.create_index(op.f('ix_inventory_items_school_id'), 'inventory_items', ['school_id'])

    op.create_table(
        'inventory_movements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('movement_type', sa.String(length=20), nullable=False),
        sa.Column('reason', sa.String(length=60), nullable=False),
        sa.Column('quantity', sa.Numeric(12, 2), nullable=False),
        sa.Column('movement_date', sa.Date(), nullable=False),
        sa.Column('recipient', sa.String(length=200), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('attachment_path', sa.String(length=500), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.ForeignKeyConstraint(['item_id'], ['inventory_items.id']),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_inventory_movements_academic_year_id'), 'inventory_movements', ['academic_year_id'])
    op.create_index(op.f('ix_inventory_movements_item_id'), 'inventory_movements', ['item_id'])
    op.create_index(op.f('ix_inventory_movements_movement_date'), 'inventory_movements', ['movement_date'])
    op.create_index(op.f('ix_inventory_movements_school_id'), 'inventory_movements', ['school_id'])

    op.create_table(
        'inventory_counts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('system_quantity', sa.Numeric(12, 2), nullable=False),
        sa.Column('actual_quantity', sa.Numeric(12, 2), nullable=False),
        sa.Column('difference', sa.Numeric(12, 2), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('reason', sa.String(length=200), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('counted_by', sa.Integer(), nullable=True),
        sa.Column('count_date', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id']),
        sa.ForeignKeyConstraint(['counted_by'], ['users.id']),
        sa.ForeignKeyConstraint(['item_id'], ['inventory_items.id']),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_inventory_counts_academic_year_id'), 'inventory_counts', ['academic_year_id'])
    op.create_index(op.f('ix_inventory_counts_count_date'), 'inventory_counts', ['count_date'])
    op.create_index(op.f('ix_inventory_counts_item_id'), 'inventory_counts', ['item_id'])
    op.create_index(op.f('ix_inventory_counts_school_id'), 'inventory_counts', ['school_id'])
    op.create_index(op.f('ix_inventory_counts_status'), 'inventory_counts', ['status'])


def downgrade():
    op.drop_index(op.f('ix_inventory_counts_status'), table_name='inventory_counts')
    op.drop_index(op.f('ix_inventory_counts_school_id'), table_name='inventory_counts')
    op.drop_index(op.f('ix_inventory_counts_item_id'), table_name='inventory_counts')
    op.drop_index(op.f('ix_inventory_counts_count_date'), table_name='inventory_counts')
    op.drop_index(op.f('ix_inventory_counts_academic_year_id'), table_name='inventory_counts')
    op.drop_table('inventory_counts')

    op.drop_index(op.f('ix_inventory_movements_school_id'), table_name='inventory_movements')
    op.drop_index(op.f('ix_inventory_movements_movement_date'), table_name='inventory_movements')
    op.drop_index(op.f('ix_inventory_movements_item_id'), table_name='inventory_movements')
    op.drop_index(op.f('ix_inventory_movements_academic_year_id'), table_name='inventory_movements')
    op.drop_table('inventory_movements')

    op.drop_index(op.f('ix_inventory_items_school_id'), table_name='inventory_items')
    op.drop_index(op.f('ix_inventory_items_is_active'), table_name='inventory_items')
    op.drop_index(op.f('ix_inventory_items_category_id'), table_name='inventory_items')
    op.drop_index(op.f('ix_inventory_items_academic_year_id'), table_name='inventory_items')
    op.drop_table('inventory_items')

    op.drop_index(op.f('ix_inventory_categories_school_id'), table_name='inventory_categories')
    op.drop_index(op.f('ix_inventory_categories_academic_year_id'), table_name='inventory_categories')
    op.drop_table('inventory_categories')
