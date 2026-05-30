"""Add school_modules table

Revision ID: o9p0q1r2s3t4
Revises: n8o9p0q1r2s3
Create Date: 2026-05-25

Creates:
  school_modules — one row per (school_id, module_key) feature flag.

No data migration needed:
  Existing schools have no rows in school_modules.  The application treats
  "no rows for a school" as "all modules enabled" for backward compatibility,
  so the current demo school and any other existing school continue to work
  exactly as before.
"""
from alembic import op
import sqlalchemy as sa


revision      = 'o9p0q1r2s3t4'
down_revision = 'n8o9p0q1r2s3'
branch_labels = None
depends_on    = None


def upgrade():
    op.create_table(
        'school_modules',
        sa.Column('id',         sa.Integer(),    nullable=False),
        sa.Column('school_id',  sa.Integer(),    nullable=False),
        sa.Column('module_key', sa.String(50),   nullable=False),
        sa.Column('is_enabled', sa.Boolean(),    nullable=False,
                  server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(),   nullable=True),
        sa.Column('updated_at', sa.DateTime(),   nullable=True),
        sa.ForeignKeyConstraint(
            ['school_id'], ['schools.id'],
            name='fk_school_modules_school_id',
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('school_id', 'module_key', name='uq_school_module'),
    )
    op.create_index('ix_school_modules_school_id', 'school_modules', ['school_id'])


def downgrade():
    op.drop_index('ix_school_modules_school_id', table_name='school_modules')
    op.drop_table('school_modules')
