"""Add school calendar: weekly_off_days on schools + school_holidays table

Revision ID: q1r2s3t4u5v6
Revises: p0q1r2s3t4u5
Create Date: 2026-05-27

Changes:
  schools          — add weekly_off_days VARCHAR(20) (nullable)
  school_holidays  — new table for school-specific and global holiday ranges

No data migration needed: NULL weekly_off_days = no weekly holidays configured;
empty school_holidays table = no holidays defined (auto-absence runs every day).
"""
from alembic import op
import sqlalchemy as sa


revision      = 'q1r2s3t4u5v6'
down_revision = 'p0q1r2s3t4u5'
branch_labels = None
depends_on    = None


def upgrade():
    # ── weekly_off_days column on schools ────────────────────────────────────
    op.add_column(
        'schools',
        sa.Column('weekly_off_days', sa.String(20), nullable=True),
    )

    # ── school_holidays table ─────────────────────────────────────────────────
    op.create_table(
        'school_holidays',
        sa.Column('id',               sa.Integer(),    nullable=False),
        sa.Column('school_id',        sa.Integer(),    nullable=True),
        sa.Column('academic_year_id', sa.Integer(),    nullable=True),
        sa.Column('name',             sa.String(200),  nullable=False),
        sa.Column('start_date',       sa.Date(),       nullable=False),
        sa.Column('end_date',         sa.Date(),       nullable=False),
        sa.Column('holiday_type',     sa.String(20),   nullable=False,
                  server_default='official'),
        sa.Column('notes',            sa.Text(),       nullable=True),
        sa.Column('is_active',        sa.Boolean(),    nullable=False,
                  server_default=sa.text('true')),
        sa.Column('created_by',       sa.Integer(),    nullable=True),
        sa.Column('created_at',       sa.DateTime(),   nullable=True),
        sa.Column('updated_at',       sa.DateTime(),   nullable=True),
        sa.ForeignKeyConstraint(
            ['school_id'], ['schools.id'],
            name='fk_school_holidays_school_id',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['academic_year_id'], ['academic_years.id'],
            name='fk_school_holidays_academic_year_id',
            ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['created_by'], ['users.id'],
            name='fk_school_holidays_created_by',
            ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_school_holidays_school_id',  'school_holidays', ['school_id'])
    op.create_index('ix_school_holidays_start_date', 'school_holidays', ['start_date'])
    op.create_index('ix_school_holidays_academic_year_id', 'school_holidays', ['academic_year_id'])


def downgrade():
    op.drop_index('ix_school_holidays_academic_year_id', table_name='school_holidays')
    op.drop_index('ix_school_holidays_start_date',  table_name='school_holidays')
    op.drop_index('ix_school_holidays_school_id',   table_name='school_holidays')
    op.drop_table('school_holidays')
    op.drop_column('schools', 'weekly_off_days')
