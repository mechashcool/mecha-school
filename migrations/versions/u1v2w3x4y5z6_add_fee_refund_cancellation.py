"""add installment refund + full-fee cancellation

Adds the refund / cancellation workflow to the Fees module. Purely additive
and default-safe — no existing rows are modified, and all new columns are
nullable so every existing fee, installment, and revenue row stays valid:

  * fee_refund_events table            — one auditable refund/cancellation event
  * revenues.refunded_at / refund_event_id — marks a reversed revenue allocation
    (active revenue = refunded_at IS NULL)
  * fee_records.cancelled_at / cancelled_by / cancellation_reason — full-fee
    cancellation, preserved as history

Revision ID: u1v2w3x4y5z6
Revises: t8u9v0w1x2y3
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'u1v2w3x4y5z6'
down_revision = 't8u9v0w1x2y3'
branch_labels = None
depends_on = None


def upgrade():
    # 1) fee_refund_events — auditable refund / full-fee cancellation ledger.
    op.create_table(
        'fee_refund_events',
        sa.Column('id',               sa.Integer(), nullable=False),
        sa.Column('school_id',        sa.Integer(), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), nullable=False),
        sa.Column('student_id',       sa.Integer(), nullable=False),
        sa.Column('fee_record_id',    sa.Integer(), nullable=False),
        sa.Column('installment_id',   sa.Integer(), nullable=True),
        sa.Column('event_type',       sa.String(length=30), nullable=False),
        sa.Column('amount',           sa.Numeric(precision=12, scale=2),
                  nullable=False, server_default='0'),
        sa.Column('reason',           sa.Text(), nullable=False),
        sa.Column('op_refs',          sa.Text(), nullable=True),
        sa.Column('performed_by',     sa.Integer(), nullable=False),
        sa.Column('created_at',       sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['school_id'],        ['schools.id']),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id']),
        sa.ForeignKeyConstraint(['student_id'],       ['students.id']),
        sa.ForeignKeyConstraint(['fee_record_id'],    ['fee_records.id']),
        sa.ForeignKeyConstraint(['installment_id'],   ['fee_installments.id']),
        sa.ForeignKeyConstraint(['performed_by'],     ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_fee_refund_events_school_id',
                    'fee_refund_events', ['school_id'], unique=False)
    op.create_index('ix_fee_refund_events_academic_year_id',
                    'fee_refund_events', ['academic_year_id'], unique=False)
    op.create_index('ix_fee_refund_events_student_id',
                    'fee_refund_events', ['student_id'], unique=False)
    op.create_index('ix_fee_refund_events_fee_record_id',
                    'fee_refund_events', ['fee_record_id'], unique=False)
    op.create_index('ix_fee_refund_events_installment_id',
                    'fee_refund_events', ['installment_id'], unique=False)

    # 2) revenues — refund reversal marker (active revenue = refunded_at IS NULL).
    with op.batch_alter_table('revenues', schema=None) as batch_op:
        batch_op.add_column(sa.Column('refunded_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('refund_event_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_revenues_refund_event_id',
                              ['refund_event_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_revenues_refund_event_id', 'fee_refund_events',
            ['refund_event_id'], ['id'],
        )

    # 3) fee_records — full-fee cancellation (preserved as history).
    with op.batch_alter_table('fee_records', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cancelled_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('cancelled_by', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('cancellation_reason', sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            'fk_fee_records_cancelled_by', 'users',
            ['cancelled_by'], ['id'],
        )


def downgrade():
    with op.batch_alter_table('fee_records', schema=None) as batch_op:
        batch_op.drop_constraint('fk_fee_records_cancelled_by', type_='foreignkey')
        batch_op.drop_column('cancellation_reason')
        batch_op.drop_column('cancelled_by')
        batch_op.drop_column('cancelled_at')

    with op.batch_alter_table('revenues', schema=None) as batch_op:
        batch_op.drop_constraint('fk_revenues_refund_event_id', type_='foreignkey')
        batch_op.drop_index('ix_revenues_refund_event_id')
        batch_op.drop_column('refund_event_id')
        batch_op.drop_column('refunded_at')

    op.drop_index('ix_fee_refund_events_installment_id', table_name='fee_refund_events')
    op.drop_index('ix_fee_refund_events_fee_record_id', table_name='fee_refund_events')
    op.drop_index('ix_fee_refund_events_student_id', table_name='fee_refund_events')
    op.drop_index('ix_fee_refund_events_academic_year_id', table_name='fee_refund_events')
    op.drop_index('ix_fee_refund_events_school_id', table_name='fee_refund_events')
    op.drop_table('fee_refund_events')
