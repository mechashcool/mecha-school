"""Add mobile_module_views table for per-user badge-module last-viewed timestamps

Revision ID: l1m2n3o4p5q6
Revises: k0l1m2n3o4p5
Create Date: 2026-06-16

Adds the mobile_module_views table used by the mobile badge-count endpoint
(GET /api/mobile/v1/me/badge-counts) and the mark-module-viewed endpoint
(POST /api/mobile/v1/me/mark-module-viewed/<module>).

One row per (user_id, module) stores the timestamp of when the authenticated
mobile user last opened that section of the app.  Counts of new records after
that timestamp are returned as badge numbers.  No existing data is modified.
"""
from alembic import op
import sqlalchemy as sa

revision = 'l1m2n3o4p5q6'
down_revision = 'k0l1m2n3o4p5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'mobile_module_views',
        sa.Column('id',             sa.Integer(),     nullable=False),
        sa.Column('user_id',        sa.Integer(),     nullable=False),
        sa.Column('school_id',      sa.Integer(),     nullable=False),
        sa.Column('module',         sa.String(50),    nullable=False),
        sa.Column('last_viewed_at', sa.DateTime(),    nullable=False),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'],
            name='fk_mobile_module_views_user_id',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['school_id'], ['schools.id'],
            name='fk_mobile_module_views_school_id',
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'module', name='uq_mobile_module_view'),
    )
    op.create_index(
        'ix_mobile_module_views_user_id',
        'mobile_module_views',
        ['user_id'],
    )
    op.create_index(
        'ix_mobile_module_views_school_id',
        'mobile_module_views',
        ['school_id'],
    )


def downgrade():
    op.drop_index('ix_mobile_module_views_school_id',
                  table_name='mobile_module_views')
    op.drop_index('ix_mobile_module_views_user_id',
                  table_name='mobile_module_views')
    op.drop_table('mobile_module_views')
