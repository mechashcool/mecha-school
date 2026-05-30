"""add school_module_configs table

Revision ID: n8o9p0q1r2s3
Revises: v6w7x8y9z0a1
Create Date: 2026-05-27

"""
from alembic import op
import sqlalchemy as sa

revision = 'w7x8y9z0a1b2'
down_revision = 'v6w7x8y9z0a1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'school_module_configs',
        sa.Column('id',         sa.Integer(),     nullable=False),
        sa.Column('school_id',  sa.Integer(),     nullable=False),
        sa.Column('module_key', sa.String(50),    nullable=False),
        sa.Column('config',     sa.JSON(),        nullable=True),
        sa.Column('updated_at', sa.DateTime(),    nullable=True),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('school_id', 'module_key', name='uq_school_module_config'),
    )
    op.create_index('ix_school_module_configs_school_id',
                    'school_module_configs', ['school_id'])


def downgrade():
    op.drop_index('ix_school_module_configs_school_id',
                  table_name='school_module_configs')
    op.drop_table('school_module_configs')
