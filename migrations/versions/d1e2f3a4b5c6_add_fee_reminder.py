"""Add fee reminder settings to schools and fee_reminder_logs table

Revision ID: d1e2f3a4b5c6
Revises: c2d3e4f5a6b7
Create Date: 2026-05-30

PostgreSQL/Supabase-safe strategy
──────────────────────────────────
ADD COLUMN with a server_default on a large table can acquire a long lock and
hit Supabase's statement_timeout.  We instead:
  1. Add each column as nullable with NO DEFAULT  (instant metadata-only change)
  2. UPDATE existing rows to backfill the default  (fast — schools table is tiny)
Idempotency: each column and the table are checked before being created/added,
so re-running after a partial failure is safe.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


# ── helpers ───────────────────────────────────────────────────────────────────

def _col_exists(table: str, col: str) -> bool:
    bind = op.get_bind()
    return col in {c['name'] for c in inspect(bind).get_columns(table)}


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    return inspect(bind).has_table(table)


def _index_exists(index: str, table: str) -> bool:
    bind = op.get_bind()
    return any(ix['name'] == index for ix in inspect(bind).get_indexes(table))


# ── revision metadata ─────────────────────────────────────────────────────────

revision = 'd1e2f3a4b5c6'
down_revision = 'c2d3e4f5a6b7'
branch_labels = None
depends_on = None


def upgrade():
    # ── schools: add three new columns ───────────────────────────────────────
    # Add each column WITHOUT a DEFAULT so PostgreSQL performs an instant
    # metadata-only operation (no table rewrite, no prolonged lock).
    # Then UPDATE to set the application-level default for existing rows.

    if not _col_exists('schools', 'fee_reminder_enabled'):
        op.add_column('schools',
            sa.Column('fee_reminder_enabled', sa.Boolean(), nullable=True))
        op.execute(text(
            "UPDATE schools SET fee_reminder_enabled = false "
            "WHERE fee_reminder_enabled IS NULL"
        ))

    if not _col_exists('schools', 'fee_reminder_before_value'):
        op.add_column('schools',
            sa.Column('fee_reminder_before_value', sa.Integer(), nullable=True))
        op.execute(text(
            "UPDATE schools SET fee_reminder_before_value = 3 "
            "WHERE fee_reminder_before_value IS NULL"
        ))

    if not _col_exists('schools', 'fee_reminder_before_unit'):
        op.add_column('schools',
            sa.Column('fee_reminder_before_unit', sa.String(10), nullable=True))
        op.execute(text(
            "UPDATE schools SET fee_reminder_before_unit = 'days' "
            "WHERE fee_reminder_before_unit IS NULL"
        ))

    # ── fee_reminder_logs ─────────────────────────────────────────────────────
    if not _table_exists('fee_reminder_logs'):
        op.create_table(
            'fee_reminder_logs',
            sa.Column('id',               sa.Integer(),    nullable=False),
            sa.Column('school_id',        sa.Integer(),    nullable=False),
            sa.Column('academic_year_id', sa.Integer(),    nullable=True),
            sa.Column('student_id',       sa.Integer(),    nullable=False),
            sa.Column('installment_id',   sa.Integer(),    nullable=False),
            sa.Column('parent_user_id',   sa.Integer(),    nullable=False),
            sa.Column('reminder_value',   sa.Integer(),    nullable=False),
            sa.Column('reminder_unit',    sa.String(10),   nullable=False),
            sa.Column('due_date',         sa.Date(),       nullable=False),
            sa.Column('sent_at',          sa.DateTime(),   nullable=True),
            sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id']),
            sa.ForeignKeyConstraint(['installment_id'],   ['fee_installments.id']),
            sa.ForeignKeyConstraint(['parent_user_id'],   ['users.id']),
            sa.ForeignKeyConstraint(['school_id'],        ['schools.id']),
            sa.ForeignKeyConstraint(['student_id'],       ['students.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint(
                'installment_id', 'parent_user_id', 'reminder_value', 'reminder_unit',
                name='uq_fee_reminder_log',
            ),
        )

    if not _index_exists('ix_fee_reminder_logs_school_id', 'fee_reminder_logs'):
        op.create_index('ix_fee_reminder_logs_school_id',
                        'fee_reminder_logs', ['school_id'])

    if not _index_exists('ix_fee_reminder_logs_student_id', 'fee_reminder_logs'):
        op.create_index('ix_fee_reminder_logs_student_id',
                        'fee_reminder_logs', ['student_id'])

    if not _index_exists('ix_fee_reminder_logs_installment_id', 'fee_reminder_logs'):
        op.create_index('ix_fee_reminder_logs_installment_id',
                        'fee_reminder_logs', ['installment_id'])


def downgrade():
    op.drop_index('ix_fee_reminder_logs_installment_id', table_name='fee_reminder_logs')
    op.drop_index('ix_fee_reminder_logs_student_id',     table_name='fee_reminder_logs')
    op.drop_index('ix_fee_reminder_logs_school_id',      table_name='fee_reminder_logs')
    op.drop_table('fee_reminder_logs')
    op.drop_column('schools', 'fee_reminder_before_unit')
    op.drop_column('schools', 'fee_reminder_before_value')
    op.drop_column('schools', 'fee_reminder_enabled')
