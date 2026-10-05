"""add schools.fee_receipt_footer (fee-receipt-only per-school footer)

ONE new nullable column: schools.fee_receipt_footer TEXT.

PURELY ADDITIVE. No default, no NOT NULL, no backfill, no UPDATE: every existing
row reads NULL and the fee receipt simply renders no school footer line.

schools.receipt_footer is NOT touched and NOT copied. It stays exactly as it is
and keeps feeding the class-schedule PDF (app/utils/pdf_gen.py:generate_schedule_pdf)
unchanged — the fee receipt deliberately reads the new column only, so the two
documents can never share a footer again.

Locking (PostgreSQL): ADD COLUMN without a default is a catalog-only change — no
table rewrite and no row scan — but it needs a brief ACCESS EXCLUSIVE lock on
schools. lock_timeout makes the migration fail fast (nothing applied, safe to
retry) instead of queueing behind a long transaction.

Downgrade drops the column (also catalog-only); receipt_footer is unaffected.

Revision ID: q4r5s6t7u8v9
Revises: p3q4r5s6t7u8
Create Date: 2026-10-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'q4r5s6t7u8v9'
down_revision = 'p3q4r5s6t7u8'
branch_labels = None
depends_on = None


def _lock_timeout():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade():
    _lock_timeout()
    op.add_column('schools',
                  sa.Column('fee_receipt_footer', sa.Text(), nullable=True))


def downgrade():
    _lock_timeout()
    op.drop_column('schools', 'fee_receipt_footer')
