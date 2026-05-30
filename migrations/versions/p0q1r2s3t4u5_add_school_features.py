"""Add school_features table

Revision ID: p0q1r2s3t4u5
Revises: o9p0q1r2s3t4
Create Date: 2026-05-25

Creates:
  school_features — one row per (school_id, feature_key) granular feature flag.

No data migration needed:
  "No rows for a school" = "all features enabled" (backward compatibility).
  Existing schools continue working without any data changes.
"""
from alembic import op
import sqlalchemy as sa


revision      = 'p0q1r2s3t4u5'
down_revision = 'o9p0q1r2s3t4'
branch_labels = None
depends_on    = None


def upgrade():
    op.create_table(
        'school_features',
        sa.Column('id',          sa.Integer(),    nullable=False),
        sa.Column('school_id',   sa.Integer(),    nullable=False),
        sa.Column('feature_key', sa.String(100),  nullable=False),
        sa.Column('is_enabled',  sa.Boolean(),    nullable=False,
                  server_default=sa.text('true')),
        sa.Column('created_at',  sa.DateTime(),   nullable=True),
        sa.Column('updated_at',  sa.DateTime(),   nullable=True),
        sa.ForeignKeyConstraint(
            ['school_id'], ['schools.id'],
            name='fk_school_features_school_id',
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('school_id', 'feature_key', name='uq_school_feature'),
    )
    op.create_index('ix_school_features_school_id', 'school_features', ['school_id'])


def downgrade():
    op.drop_index('ix_school_features_school_id', table_name='school_features')
    op.drop_table('school_features')
