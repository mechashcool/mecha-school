"""Add mobile_device_tokens table

Stores FCM device tokens registered by the Flutter mobile app.
Supports multiple tokens per user (one per physical device) with
platform, device_name, is_active, and last_seen_at tracking.

The existing User.device_token column is left untouched — it is
kept in sync by the /auth/register-device endpoint so the legacy
notification service keeps working without changes.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a0
Create Date: 2026-05-30
"""

from alembic import op
import sqlalchemy as sa

revision      = 'c2d3e4f5a6b7'
down_revision = 'b1c2d3e4f5a0'
branch_labels = None
depends_on    = None


def upgrade():
    op.create_table(
        'mobile_device_tokens',
        sa.Column('id',          sa.Integer(),     nullable=False),
        sa.Column('user_id',     sa.Integer(),     nullable=False),
        sa.Column('school_id',   sa.Integer(),     nullable=False),
        sa.Column('fcm_token',   sa.String(512),   nullable=False),
        sa.Column('platform',    sa.String(20),    nullable=False, server_default='android'),
        sa.Column('device_name', sa.String(200),   nullable=True),
        sa.Column('is_active',   sa.Boolean(),     nullable=False, server_default=sa.true()),
        sa.Column('created_at',  sa.DateTime(),    nullable=True),
        sa.Column('last_seen_at', sa.DateTime(),   nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('fcm_token', name='uq_mobile_device_token_fcm'),
        sa.ForeignKeyConstraint(['user_id'],   ['users.id'],   ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'], ondelete='CASCADE'),
    )
    op.create_index('ix_mobile_device_tokens_user_id',   'mobile_device_tokens', ['user_id'])
    op.create_index('ix_mobile_device_tokens_school_id', 'mobile_device_tokens', ['school_id'])


def downgrade():
    op.drop_index('ix_mobile_device_tokens_school_id', table_name='mobile_device_tokens')
    op.drop_index('ix_mobile_device_tokens_user_id',   table_name='mobile_device_tokens')
    op.drop_table('mobile_device_tokens')
