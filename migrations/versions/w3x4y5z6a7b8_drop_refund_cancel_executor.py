"""remove the executor identity from refund / cancellation records

The refunds & cancellations feature must no longer store WHO performed an
installment refund or a full-fee cancellation. Only the fact, reason, timestamp,
student, fee, installment, amount, receipt, operation reference and status are
kept. This drops the two executor columns (and their foreign keys):

  * fee_refund_events.performed_by   — who performed the refund / cancellation
  * fee_records.cancelled_by         — who cancelled the fee

Purely a data-model reduction. No fee, installment, revenue, refund-event, or
cancellation-reason row is deleted or otherwise modified; cancelled fees remain
preserved as history, and every financial figure/link is untouched. Permissions
are unchanged — only authorized users may still perform these operations.

Revision ID: w3x4y5z6a7b8
Revises: v2w3x4y5z6a7
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'w3x4y5z6a7b8'
down_revision = 'v2w3x4y5z6a7'
branch_labels = None
depends_on = None


def _fk_names_for(insp, table, column):
    return [fk['name'] for fk in insp.get_foreign_keys(table)
            if column in (fk.get('constrained_columns') or []) and fk.get('name')]


def _columns(insp, table):
    return {c['name'] for c in insp.get_columns(table)}


def upgrade():
    insp = sa.inspect(op.get_bind())

    # 1) fee_refund_events.performed_by — the FK was created inline (auto-named),
    #    so discover and drop it, then drop the column.
    ev_fks = _fk_names_for(insp, 'fee_refund_events', 'performed_by')
    with op.batch_alter_table('fee_refund_events', schema=None) as batch_op:
        for name in ev_fks:
            batch_op.drop_constraint(name, type_='foreignkey')
        if 'performed_by' in _columns(insp, 'fee_refund_events'):
            batch_op.drop_column('performed_by')

    # 2) fee_records.cancelled_by — named FK from the creating migration.
    fr_fks = _fk_names_for(insp, 'fee_records', 'cancelled_by')
    with op.batch_alter_table('fee_records', schema=None) as batch_op:
        for name in fr_fks:
            batch_op.drop_constraint(name, type_='foreignkey')
        if 'cancelled_by' in _columns(insp, 'fee_records'):
            batch_op.drop_column('cancelled_by')


def downgrade():
    # Re-add the columns (nullable — the removed attribution cannot be restored)
    # and their foreign keys. No data is back-filled.
    insp = sa.inspect(op.get_bind())
    if 'cancelled_by' not in _columns(insp, 'fee_records'):
        with op.batch_alter_table('fee_records', schema=None) as batch_op:
            batch_op.add_column(sa.Column('cancelled_by', sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                'fk_fee_records_cancelled_by', 'users', ['cancelled_by'], ['id'])
    if 'performed_by' not in _columns(insp, 'fee_refund_events'):
        with op.batch_alter_table('fee_refund_events', schema=None) as batch_op:
            batch_op.add_column(sa.Column('performed_by', sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                'fk_fee_refund_events_performed_by', 'users', ['performed_by'], ['id'])
