"""Redesign fee reminder: slot-based daily recurring reminders

Revision ID: n3o4p5q6r7s8
Revises: m2n3o4p5q6r7
Create Date: 2026-06-20

Summary of changes
──────────────────
schools table:
  + fee_reminder_days_before  Integer  days before due date to begin reminders (default 3)
  + fee_reminder_per_day      Integer  reminder slots per day 1–6 (default 1)
  (fee_reminder_before_value and fee_reminder_before_unit are kept for DB compatibility
   but are no longer used by application code.)

fee_reminder_logs table:
  + reminder_date  Date     nullable — local school date the slot fired
  + slot_index     Integer  nullable — 0-based daily slot index
  ~ reminder_value — altered to nullable (was NOT NULL, now unused by new code)
  ~ reminder_unit  — altered to nullable (was NOT NULL, now unused by new code)
  - uq_fee_reminder_log  (dropped — keyed on reminder_value/unit, no longer meaningful)
  + uq_fee_reminder_log_v2  on (installment_id, parent_user_id, reminder_date, slot_index)

PostgreSQL / Supabase safety
────────────────────────────
Each ALTER is guarded by an existence check so re-running after a partial
failure is safe.  ADD COLUMN is done without a server-side DEFAULT (instant
metadata-only op on PostgreSQL ≥12), followed by a separate UPDATE to
backfill defaults on existing rows.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


# ── helpers ───────────────────────────────────────────────────────────────────

def _col_exists(table: str, col: str) -> bool:
    return col in {c['name'] for c in inspect(op.get_bind()).get_columns(table)}


def _table_exists(table: str) -> bool:
    return inspect(op.get_bind()).has_table(table)


def _constraint_exists(table: str, name: str) -> bool:
    try:
        return any(
            c.get('name') == name
            for c in inspect(op.get_bind()).get_unique_constraints(table)
        )
    except Exception:
        return False


# ── revision metadata ─────────────────────────────────────────────────────────

revision      = 'n3o4p5q6r7s8'
down_revision = 'm2n3o4p5q6r7'
branch_labels = None
depends_on    = None


def upgrade():
    # ── schools: add two new settings columns ─────────────────────────────────

    if not _col_exists('schools', 'fee_reminder_days_before'):
        op.add_column('schools',
            sa.Column('fee_reminder_days_before', sa.Integer(), nullable=True))
        # Seed from the legacy value where the unit was 'days'; otherwise default 3.
        op.execute(text(
            "UPDATE schools "
            "SET fee_reminder_days_before = "
            "  CASE "
            "    WHEN fee_reminder_before_unit = 'days' "
            "         AND fee_reminder_before_value IS NOT NULL "
            "    THEN fee_reminder_before_value "
            "    ELSE 3 "
            "  END "
            "WHERE fee_reminder_days_before IS NULL"
        ))

    if not _col_exists('schools', 'fee_reminder_per_day'):
        op.add_column('schools',
            sa.Column('fee_reminder_per_day', sa.Integer(), nullable=True))
        op.execute(text(
            "UPDATE schools SET fee_reminder_per_day = 1 "
            "WHERE fee_reminder_per_day IS NULL"
        ))

    # ── fee_reminder_logs: evolve schema ─────────────────────────────────────

    if not _table_exists('fee_reminder_logs'):
        # Table does not exist yet — create it with the full v2 schema.
        op.create_table(
            'fee_reminder_logs',
            sa.Column('id',               sa.Integer(),  nullable=False),
            sa.Column('school_id',        sa.Integer(),  nullable=False),
            sa.Column('academic_year_id', sa.Integer(),  nullable=True),
            sa.Column('student_id',       sa.Integer(),  nullable=False),
            sa.Column('installment_id',   sa.Integer(),  nullable=False),
            sa.Column('parent_user_id',   sa.Integer(),  nullable=False),
            sa.Column('reminder_date',    sa.Date(),     nullable=True),
            sa.Column('slot_index',       sa.Integer(),  nullable=True),
            sa.Column('due_date',         sa.Date(),     nullable=False),
            sa.Column('sent_at',          sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id']),
            sa.ForeignKeyConstraint(['installment_id'],   ['fee_installments.id']),
            sa.ForeignKeyConstraint(['parent_user_id'],   ['users.id']),
            sa.ForeignKeyConstraint(['school_id'],        ['schools.id']),
            sa.ForeignKeyConstraint(['student_id'],       ['students.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint(
                'installment_id', 'parent_user_id', 'reminder_date', 'slot_index',
                name='uq_fee_reminder_log_v2',
            ),
        )
        op.create_index('ix_fee_reminder_logs_school_id',
                        'fee_reminder_logs', ['school_id'])
        op.create_index('ix_fee_reminder_logs_student_id',
                        'fee_reminder_logs', ['student_id'])
        op.create_index('ix_fee_reminder_logs_installment_id',
                        'fee_reminder_logs', ['installment_id'])
        return  # nothing more to do

    # Table exists — evolve it in place.

    # 1. Make legacy columns nullable so new INSERTs can omit them.
    if _col_exists('fee_reminder_logs', 'reminder_value'):
        op.alter_column('fee_reminder_logs', 'reminder_value',
                        existing_type=sa.Integer(), nullable=True)

    if _col_exists('fee_reminder_logs', 'reminder_unit'):
        op.alter_column('fee_reminder_logs', 'reminder_unit',
                        existing_type=sa.String(10), nullable=True)

    # 2. Add new slot-tracking columns.
    if not _col_exists('fee_reminder_logs', 'reminder_date'):
        op.add_column('fee_reminder_logs',
            sa.Column('reminder_date', sa.Date(), nullable=True))

    if not _col_exists('fee_reminder_logs', 'slot_index'):
        op.add_column('fee_reminder_logs',
            sa.Column('slot_index', sa.Integer(), nullable=True))

    # 3. Drop the old unique constraint (keyed on reminder_value/reminder_unit).
    if _constraint_exists('fee_reminder_logs', 'uq_fee_reminder_log'):
        op.drop_constraint('uq_fee_reminder_log',
                           'fee_reminder_logs', type_='unique')

    # 4. Add the new unique constraint.
    if not _constraint_exists('fee_reminder_logs', 'uq_fee_reminder_log_v2'):
        op.create_unique_constraint(
            'uq_fee_reminder_log_v2',
            'fee_reminder_logs',
            ['installment_id', 'parent_user_id', 'reminder_date', 'slot_index'],
        )


def downgrade():
    # Restore original constraint
    if not _constraint_exists('fee_reminder_logs', 'uq_fee_reminder_log'):
        op.create_unique_constraint(
            'uq_fee_reminder_log',
            'fee_reminder_logs',
            ['installment_id', 'parent_user_id', 'reminder_value', 'reminder_unit'],
        )
    if _constraint_exists('fee_reminder_logs', 'uq_fee_reminder_log_v2'):
        op.drop_constraint('uq_fee_reminder_log_v2',
                           'fee_reminder_logs', type_='unique')

    if _col_exists('fee_reminder_logs', 'slot_index'):
        op.drop_column('fee_reminder_logs', 'slot_index')
    if _col_exists('fee_reminder_logs', 'reminder_date'):
        op.drop_column('fee_reminder_logs', 'reminder_date')

    if _col_exists('fee_reminder_logs', 'reminder_unit'):
        op.alter_column('fee_reminder_logs', 'reminder_unit',
                        existing_type=sa.String(10), nullable=False)
    if _col_exists('fee_reminder_logs', 'reminder_value'):
        op.alter_column('fee_reminder_logs', 'reminder_value',
                        existing_type=sa.Integer(), nullable=False)

    if _col_exists('schools', 'fee_reminder_per_day'):
        op.drop_column('schools', 'fee_reminder_per_day')
    if _col_exists('schools', 'fee_reminder_days_before'):
        op.drop_column('schools', 'fee_reminder_days_before')
