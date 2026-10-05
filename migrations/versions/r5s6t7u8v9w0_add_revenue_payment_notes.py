"""add revenues.notes (per-payment-transaction note)

ONE new nullable column: revenues.notes TEXT.

Each accepted fee payment writes one Revenue row per installment it touched
(stage_installment_payment), all tagged with the same [TXN:op_ref]. That row is
the only per-transaction record, so the note typed in the payment modal is now
stored on it — previously it was written to FeeInstallment.notes, where every
later payment overwrote the earlier note. Revenue.description is NOT used: it is
machine-parsed (receipt number + TXN tag) by receipts, refunds and the
payment-date filter and must never carry free text.

PURELY ADDITIVE. No default, no NOT NULL, no backfill, no UPDATE: every existing
row reads NULL. FeeInstallment.notes is not touched — historical notes stay
where they are and are displayed as a legacy payment note.

Locking (PostgreSQL): ADD COLUMN without a default is a catalog-only change — no
table rewrite and no row scan — but it needs a brief ACCESS EXCLUSIVE lock on
revenues. lock_timeout makes the migration fail fast (nothing applied, safe to
retry) instead of queueing behind a long transaction.

Downgrade drops the column (also catalog-only) and with it any per-payment
notes recorded since the upgrade.

Revision ID: r5s6t7u8v9w0
Revises: q4r5s6t7u8v9
Create Date: 2026-10-06
"""
from alembic import op
import sqlalchemy as sa


revision = 'r5s6t7u8v9w0'
down_revision = 'q4r5s6t7u8v9'
branch_labels = None
depends_on = None


def _lock_timeout():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade():
    _lock_timeout()
    op.add_column('revenues', sa.Column('notes', sa.Text(), nullable=True))


def downgrade():
    _lock_timeout()
    op.drop_column('revenues', 'notes')
