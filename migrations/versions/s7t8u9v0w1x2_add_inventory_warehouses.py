"""Add multi-warehouse support to the inventory module

Adds:
  * inventory_warehouses table       — physical stock locations per school
                                        (school-scoped only, persists across years)
  * inventory_item_stocks table      — per-(item, warehouse) quantity/reorder
                                        threshold; the source of truth for stock
  * inventory_movements.warehouse_id / to_warehouse_id (nullable, to_warehouse_id
                                        used only for 'transfer' movements)
  * inventory_counts.warehouse_id    (nullable)

inventory_items.current_quantity/minimum_quantity are kept as-is (not dropped)
and are now maintained by the application as an aggregate across
inventory_item_stocks rows, so existing reports/filters keep working unchanged.

Backfill: every school with existing inventory_items gets one default
warehouse ('المخزن الرئيسي'), every existing item gets one stock row in that
warehouse carrying its current quantity/minimum threshold forward exactly,
and historical movements/counts are pointed at that same default warehouse.
Nothing is lost or duplicated.

Revision ID: s7t8u9v0w1x2
Revises: q6r7s8t9u0v1
Create Date: 2026-07-18
"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = 's7t8u9v0w1x2'
down_revision = 'q6r7s8t9u0v1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'inventory_warehouses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('school_id', 'name', name='uq_inventory_warehouse_school_name'),
    )
    op.create_index(op.f('ix_inventory_warehouses_school_id'), 'inventory_warehouses', ['school_id'])
    op.create_index(op.f('ix_inventory_warehouses_is_active'), 'inventory_warehouses', ['is_active'])

    op.create_table(
        'inventory_item_stocks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('warehouse_id', sa.Integer(), nullable=False),
        sa.Column('quantity', sa.Numeric(12, 2), nullable=False),
        sa.Column('minimum_quantity', sa.Numeric(12, 2), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id']),
        sa.ForeignKeyConstraint(['item_id'], ['inventory_items.id']),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id']),
        sa.ForeignKeyConstraint(['warehouse_id'], ['inventory_warehouses.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('item_id', 'warehouse_id', name='uq_inventory_item_stock_item_warehouse'),
    )
    op.create_index(op.f('ix_inventory_item_stocks_academic_year_id'), 'inventory_item_stocks', ['academic_year_id'])
    op.create_index(op.f('ix_inventory_item_stocks_item_id'), 'inventory_item_stocks', ['item_id'])
    op.create_index(op.f('ix_inventory_item_stocks_school_id'), 'inventory_item_stocks', ['school_id'])
    op.create_index(op.f('ix_inventory_item_stocks_warehouse_id'), 'inventory_item_stocks', ['warehouse_id'])

    with op.batch_alter_table('inventory_movements', schema=None) as batch_op:
        batch_op.add_column(sa.Column('warehouse_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('to_warehouse_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_inventory_movements_warehouse_id', ['warehouse_id'])
        batch_op.create_index('ix_inventory_movements_to_warehouse_id', ['to_warehouse_id'])
        batch_op.create_foreign_key('fk_inventory_movements_warehouse_id',
                                    'inventory_warehouses', ['warehouse_id'], ['id'])
        batch_op.create_foreign_key('fk_inventory_movements_to_warehouse_id',
                                    'inventory_warehouses', ['to_warehouse_id'], ['id'])

    with op.batch_alter_table('inventory_counts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('warehouse_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_inventory_counts_warehouse_id', ['warehouse_id'])
        batch_op.create_foreign_key('fk_inventory_counts_warehouse_id',
                                    'inventory_warehouses', ['warehouse_id'], ['id'])

    # ── Backfill: preserve every existing item's quantity/threshold by moving
    #    it into a per-school default warehouse; point historical movements
    #    and counts at that same warehouse so nothing is left ambiguous. ──────
    conn = op.get_bind()
    now = datetime.utcnow()

    conn.execute(sa.text("""
        INSERT INTO inventory_warehouses (school_id, name, description, is_active, is_default, created_at, updated_at)
        SELECT DISTINCT school_id, 'المخزن الرئيسي', NULL, true, true, :now, :now
        FROM inventory_items
    """), {'now': now})

    conn.execute(sa.text("""
        INSERT INTO inventory_item_stocks
            (school_id, academic_year_id, item_id, warehouse_id, quantity, minimum_quantity, created_at, updated_at)
        SELECT i.school_id, i.academic_year_id, i.id, w.id, i.current_quantity, i.minimum_quantity, :now, :now
        FROM inventory_items i
        JOIN inventory_warehouses w ON w.school_id = i.school_id AND w.is_default = true
    """), {'now': now})

    conn.execute(sa.text("""
        UPDATE inventory_movements m
        SET warehouse_id = w.id
        FROM inventory_warehouses w
        WHERE w.school_id = m.school_id AND w.is_default = true AND m.warehouse_id IS NULL
    """))

    conn.execute(sa.text("""
        UPDATE inventory_counts c
        SET warehouse_id = w.id
        FROM inventory_warehouses w
        WHERE w.school_id = c.school_id AND w.is_default = true AND c.warehouse_id IS NULL
    """))


def downgrade():
    with op.batch_alter_table('inventory_counts', schema=None) as batch_op:
        batch_op.drop_constraint('fk_inventory_counts_warehouse_id', type_='foreignkey')
        batch_op.drop_index('ix_inventory_counts_warehouse_id')
        batch_op.drop_column('warehouse_id')

    with op.batch_alter_table('inventory_movements', schema=None) as batch_op:
        batch_op.drop_constraint('fk_inventory_movements_to_warehouse_id', type_='foreignkey')
        batch_op.drop_constraint('fk_inventory_movements_warehouse_id', type_='foreignkey')
        batch_op.drop_index('ix_inventory_movements_to_warehouse_id')
        batch_op.drop_index('ix_inventory_movements_warehouse_id')
        batch_op.drop_column('to_warehouse_id')
        batch_op.drop_column('warehouse_id')

    op.drop_index(op.f('ix_inventory_item_stocks_warehouse_id'), table_name='inventory_item_stocks')
    op.drop_index(op.f('ix_inventory_item_stocks_school_id'), table_name='inventory_item_stocks')
    op.drop_index(op.f('ix_inventory_item_stocks_item_id'), table_name='inventory_item_stocks')
    op.drop_index(op.f('ix_inventory_item_stocks_academic_year_id'), table_name='inventory_item_stocks')
    op.drop_table('inventory_item_stocks')

    op.drop_index(op.f('ix_inventory_warehouses_is_active'), table_name='inventory_warehouses')
    op.drop_index(op.f('ix_inventory_warehouses_school_id'), table_name='inventory_warehouses')
    op.drop_table('inventory_warehouses')
