"""Add feature_packages table and school.package_id FK

Revision ID: v6w7x8y9z0a1
Revises: u5v6w7x8y9z0
Create Date: 2026-05-27

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'v6w7x8y9z0a1'
down_revision = 'u5v6w7x8y9z0'
branch_labels = None
depends_on = None


def upgrade():
    # Create feature_packages table first (referenced by schools)
    op.create_table(
        'feature_packages',
        sa.Column('id',          sa.Integer(),     nullable=False),
        sa.Column('name',        sa.String(150),   nullable=False),
        sa.Column('description', sa.Text(),        nullable=True),
        sa.Column('is_active',   sa.Boolean(),     nullable=False, server_default='1'),
        sa.Column('config',      sa.JSON(),        nullable=True),
        sa.Column('created_at',  sa.DateTime(),    nullable=True),
        sa.Column('updated_at',  sa.DateTime(),    nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    # Add package_id FK to schools (nullable, SET NULL on delete)
    with op.batch_alter_table('schools', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('package_id', sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            'fk_schools_package_id',
            'feature_packages',
            ['package_id'], ['id'],
            ondelete='SET NULL',
        )


def downgrade():
    with op.batch_alter_table('schools', schema=None) as batch_op:
        batch_op.drop_constraint('fk_schools_package_id', type_='foreignkey')
        batch_op.drop_column('package_id')

    op.drop_table('feature_packages')
