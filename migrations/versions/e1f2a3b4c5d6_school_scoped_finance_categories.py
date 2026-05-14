"""School-scope finance categories.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-05-03
"""
from alembic import op
import sqlalchemy as sa


revision = 'e1f2a3b4c5d6'
down_revision = 'd0e1f2a3b4c5'
branch_labels = None
depends_on = None


def _default_school_id(conn):
    row = conn.execute(sa.text(
        "SELECT id FROM schools ORDER BY id LIMIT 1"
    )).first()
    return row[0] if row else None


def _backfill_categories(conn, table_name, record_table, extra_columns=None):
    extra_columns = extra_columns or []
    default_school_id = _default_school_id(conn)

    categories = conn.execute(sa.text(
        f"SELECT id, name{''.join(', ' + c for c in extra_columns)} "
        f"FROM {table_name} ORDER BY id"
    )).mappings().all()

    for category in categories:
        school_ids = conn.execute(sa.text(
            f"SELECT DISTINCT school_id FROM {record_table} "
            "WHERE category_id = :category_id AND school_id IS NOT NULL "
            "ORDER BY school_id"
        ), {'category_id': category['id']}).scalars().all()

        if not school_ids:
            if default_school_id:
                conn.execute(sa.text(
                    f"UPDATE {table_name} SET school_id = :school_id WHERE id = :id"
                ), {'school_id': default_school_id, 'id': category['id']})
            continue

        first_school_id = school_ids[0]
        conn.execute(sa.text(
            f"UPDATE {table_name} SET school_id = :school_id WHERE id = :id"
        ), {'school_id': first_school_id, 'id': category['id']})

        for school_id in school_ids[1:]:
            extra_names = ''.join(f", {column}" for column in extra_columns)
            extra_values = ''.join(f", :{column}" for column in extra_columns)
            params = {
                'name': category['name'],
                'school_id': school_id,
                **{column: category[column] for column in extra_columns},
            }
            new_category_id = conn.execute(sa.text(
                f"INSERT INTO {table_name} (name, school_id{extra_names}) "
                f"VALUES (:name, :school_id{extra_values}) RETURNING id"
            ), params).scalar_one()

            conn.execute(sa.text(
                f"UPDATE {record_table} SET category_id = :new_category_id "
                "WHERE category_id = :old_category_id AND school_id = :school_id"
            ), {
                'new_category_id': new_category_id,
                'old_category_id': category['id'],
                'school_id': school_id,
            })


def _merge_duplicate_categories(conn, table_name, record_table):
    duplicate_names = conn.execute(sa.text(
        f"SELECT name, MIN(id) AS keep_id FROM {table_name} "
        "GROUP BY name HAVING COUNT(*) > 1"
    )).mappings().all()

    for duplicate in duplicate_names:
        duplicate_ids = conn.execute(sa.text(
            f"SELECT id FROM {table_name} "
            "WHERE name = :name AND id <> :keep_id ORDER BY id"
        ), {
            'name': duplicate['name'],
            'keep_id': duplicate['keep_id'],
        }).scalars().all()

        for duplicate_id in duplicate_ids:
            conn.execute(sa.text(
                f"UPDATE {record_table} SET category_id = :keep_id "
                "WHERE category_id = :duplicate_id"
            ), {
                'keep_id': duplicate['keep_id'],
                'duplicate_id': duplicate_id,
            })
            conn.execute(sa.text(
                f"DELETE FROM {table_name} WHERE id = :duplicate_id"
            ), {'duplicate_id': duplicate_id})


def upgrade():
    conn = op.get_bind()

    op.add_column('revenue_categories',
                  sa.Column('school_id', sa.Integer(), nullable=True))
    op.add_column('expense_categories',
                  sa.Column('school_id', sa.Integer(), nullable=True))

    # Drop the old global uniqueness so the same category name can exist in
    # different schools after we split shared historical category rows.
    conn.execute(sa.text(
        "ALTER TABLE revenue_categories DROP CONSTRAINT IF EXISTS revenue_categories_name_key"
    ))
    conn.execute(sa.text(
        "ALTER TABLE expense_categories DROP CONSTRAINT IF EXISTS expense_categories_name_key"
    ))

    _backfill_categories(conn, 'revenue_categories', 'revenues')
    _backfill_categories(conn, 'expense_categories', 'expenses',
                         extra_columns=['is_system'])

    op.create_foreign_key('fk_revenue_categories_school_id',
                          'revenue_categories', 'schools',
                          ['school_id'], ['id'])
    op.create_foreign_key('fk_expense_categories_school_id',
                          'expense_categories', 'schools',
                          ['school_id'], ['id'])
    op.create_index('ix_revenue_categories_school_id',
                    'revenue_categories', ['school_id'])
    op.create_index('ix_expense_categories_school_id',
                    'expense_categories', ['school_id'])
    op.create_unique_constraint('uq_revenue_category_school_name',
                                'revenue_categories', ['school_id', 'name'])
    op.create_unique_constraint('uq_expense_category_school_name',
                                'expense_categories', ['school_id', 'name'])

    op.alter_column('revenue_categories', 'school_id',
                    existing_type=sa.Integer(), nullable=False)
    op.alter_column('expense_categories', 'school_id',
                    existing_type=sa.Integer(), nullable=False)


def downgrade():
    conn = op.get_bind()

    _merge_duplicate_categories(conn, 'revenue_categories', 'revenues')
    _merge_duplicate_categories(conn, 'expense_categories', 'expenses')

    op.drop_constraint('uq_expense_category_school_name',
                       'expense_categories', type_='unique')
    op.drop_constraint('uq_revenue_category_school_name',
                       'revenue_categories', type_='unique')
    op.drop_index('ix_expense_categories_school_id',
                  table_name='expense_categories')
    op.drop_index('ix_revenue_categories_school_id',
                  table_name='revenue_categories')
    op.drop_constraint('fk_expense_categories_school_id',
                       'expense_categories', type_='foreignkey')
    op.drop_constraint('fk_revenue_categories_school_id',
                       'revenue_categories', type_='foreignkey')
    op.drop_column('expense_categories', 'school_id')
    op.drop_column('revenue_categories', 'school_id')

    op.create_unique_constraint('expense_categories_name_key',
                                'expense_categories', ['name'])
    op.create_unique_constraint('revenue_categories_name_key',
                                'revenue_categories', ['name'])
