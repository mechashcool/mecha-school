"""Add school board tables: school_videos, school_announcements, school_content_reads

Revision ID: f2g3h4i5j6k7
Revises: d1e2f3a4b5c6
Create Date: 2026-06-01

PostgreSQL/Supabase-safe strategy
──────────────────────────────────
All three tables are created new (no existing rows to backfill), so we use
nullable columns and no server_default — instant metadata-only operations.
Idempotency: every table, index, and constraint is checked before creation
so re-running after a partial failure is safe.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


# ── helpers ───────────────────────────────────────────────────────────────────

def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    return inspect(bind).has_table(table)


def _index_exists(index: str, table: str) -> bool:
    bind = op.get_bind()
    return any(ix['name'] == index for ix in inspect(bind).get_indexes(table))


# ── revision metadata ─────────────────────────────────────────────────────────

revision      = 'f2g3h4i5j6k7'
down_revision = 'e2f3a4b5c6d7'
branch_labels = None
depends_on    = None


def upgrade():
    # ── school_videos ─────────────────────────────────────────────────────────
    if not _table_exists('school_videos'):
        op.create_table(
            'school_videos',
            sa.Column('id',            sa.Integer(),   nullable=False),
            sa.Column('school_id',     sa.Integer(),   nullable=False),
            sa.Column('title',         sa.String(200), nullable=False),
            sa.Column('description',   sa.Text(),      nullable=True),
            sa.Column('video_url',     sa.String(500), nullable=False),
            sa.Column('thumbnail_url', sa.String(500), nullable=True),
            sa.Column('audience',      sa.String(20),  nullable=True),
            sa.Column('is_featured',   sa.Boolean(),   nullable=True),
            sa.Column('is_active',     sa.Boolean(),   nullable=True),
            sa.Column('publish_at',    sa.DateTime(),  nullable=True),
            sa.Column('expires_at',    sa.DateTime(),  nullable=True),
            sa.Column('created_by',    sa.Integer(),   nullable=True),
            sa.Column('created_at',    sa.DateTime(),  nullable=True),
            sa.Column('updated_at',    sa.DateTime(),  nullable=True),
            sa.ForeignKeyConstraint(['created_by'], ['users.id']),
            sa.ForeignKeyConstraint(['school_id'],  ['schools.id']),
            sa.PrimaryKeyConstraint('id'),
        )

    if not _index_exists('ix_school_videos_school_id', 'school_videos'):
        op.create_index('ix_school_videos_school_id',  'school_videos', ['school_id'])
    if not _index_exists('ix_school_videos_created_at', 'school_videos'):
        op.create_index('ix_school_videos_created_at', 'school_videos', ['created_at'])

    # ── school_announcements ──────────────────────────────────────────────────
    if not _table_exists('school_announcements'):
        op.create_table(
            'school_announcements',
            sa.Column('id',            sa.Integer(),   nullable=False),
            sa.Column('school_id',     sa.Integer(),   nullable=False),
            sa.Column('title',         sa.String(200), nullable=False),
            sa.Column('body',          sa.Text(),      nullable=False),
            sa.Column('media_url',     sa.String(500), nullable=True),
            sa.Column('media_type',    sa.String(20),  nullable=True),
            sa.Column('thumbnail_url', sa.String(500), nullable=True),
            sa.Column('audience',      sa.String(20),  nullable=True),
            sa.Column('is_featured',   sa.Boolean(),   nullable=True),
            sa.Column('is_active',     sa.Boolean(),   nullable=True),
            sa.Column('publish_at',    sa.DateTime(),  nullable=True),
            sa.Column('expires_at',    sa.DateTime(),  nullable=True),
            sa.Column('created_by',    sa.Integer(),   nullable=True),
            sa.Column('created_at',    sa.DateTime(),  nullable=True),
            sa.Column('updated_at',    sa.DateTime(),  nullable=True),
            sa.ForeignKeyConstraint(['created_by'], ['users.id']),
            sa.ForeignKeyConstraint(['school_id'],  ['schools.id']),
            sa.PrimaryKeyConstraint('id'),
        )

    if not _index_exists('ix_school_announcements_school_id', 'school_announcements'):
        op.create_index('ix_school_announcements_school_id',
                        'school_announcements', ['school_id'])
    if not _index_exists('ix_school_announcements_created_at', 'school_announcements'):
        op.create_index('ix_school_announcements_created_at',
                        'school_announcements', ['created_at'])

    # ── school_content_reads ──────────────────────────────────────────────────
    if not _table_exists('school_content_reads'):
        op.create_table(
            'school_content_reads',
            sa.Column('id',           sa.Integer(),  nullable=False),
            sa.Column('school_id',    sa.Integer(),  nullable=False),
            sa.Column('user_id',      sa.Integer(),  nullable=False),
            sa.Column('content_type', sa.String(20), nullable=False),
            sa.Column('content_id',   sa.Integer(),  nullable=False),
            sa.Column('read_at',      sa.DateTime(), nullable=True),
            sa.Column('created_at',   sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['school_id'], ['schools.id']),
            sa.ForeignKeyConstraint(['user_id'],   ['users.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('user_id', 'content_type', 'content_id',
                                name='uq_school_content_read'),
        )

    if not _index_exists('ix_school_content_reads_school_id', 'school_content_reads'):
        op.create_index('ix_school_content_reads_school_id',
                        'school_content_reads', ['school_id'])
    if not _index_exists('ix_school_content_reads_user_id', 'school_content_reads'):
        op.create_index('ix_school_content_reads_user_id',
                        'school_content_reads', ['user_id'])
    if not _index_exists('ix_school_content_reads_content_id', 'school_content_reads'):
        op.create_index('ix_school_content_reads_content_id',
                        'school_content_reads', ['content_id'])


def downgrade():
    op.drop_index('ix_school_content_reads_content_id', table_name='school_content_reads')
    op.drop_index('ix_school_content_reads_user_id',    table_name='school_content_reads')
    op.drop_index('ix_school_content_reads_school_id',  table_name='school_content_reads')
    op.drop_table('school_content_reads')
    op.drop_index('ix_school_announcements_created_at', table_name='school_announcements')
    op.drop_index('ix_school_announcements_school_id',  table_name='school_announcements')
    op.drop_table('school_announcements')
    op.drop_index('ix_school_videos_created_at', table_name='school_videos')
    op.drop_index('ix_school_videos_school_id',  table_name='school_videos')
    op.drop_table('school_videos')
