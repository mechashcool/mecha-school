"""add school_student_form_config table

Revision ID: u5v6w7x8y9z0
Revises: t4u5v6w7x8y9
Create Date: 2026-05-27
"""
from alembic import op
import sqlalchemy as sa

revision = 'u5v6w7x8y9z0'
down_revision = 't4u5v6w7x8y9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'school_student_form_config',
        sa.Column('id',              sa.Integer(),  nullable=False),
        sa.Column('school_id',       sa.Integer(),  nullable=False),
        sa.Column('hidden_sections', sa.JSON(),     nullable=True),
        sa.Column('hidden_fields',   sa.JSON(),     nullable=True),
        sa.Column('required_fields', sa.JSON(),     nullable=True),
        sa.Column('created_at',      sa.DateTime(), nullable=True),
        sa.Column('updated_at',      sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('school_id'),
    )
    op.create_index('ix_school_student_form_config_school_id',
                    'school_student_form_config', ['school_id'], unique=True)


def downgrade():
    op.drop_index('ix_school_student_form_config_school_id',
                  table_name='school_student_form_config')
    op.drop_table('school_student_form_config')
