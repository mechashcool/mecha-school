"""add school_holidays.applies_to (attendance audience of a calendar holiday)

ONE new column: school_holidays.applies_to VARCHAR(20) NOT NULL
SERVER DEFAULT 'both', plus CHECK (applies_to IN ('both','students','employees')).

PURELY ADDITIVE. No UPDATE and no backfill statement: every existing row reads
'both' through the server default, which is exactly the current behaviour (a
holiday exempts students AND employees).  Old application code that does not
know the column keeps inserting rows that receive 'both' from the default.

Locking (PostgreSQL 11+): ADD COLUMN with a constant non-volatile default is a
catalog-only change (no table rewrite).  The CHECK constraint validates the
existing rows, all of which are 'both'.  lock_timeout makes the migration fail
fast (nothing applied, safe to retry) instead of queueing behind a long
transaction.

Downgrade drops the constraint and the column: every holiday reverts to
applying to both audiences — the pre-feature behaviour.  No attendance or
payroll rows are touched in either direction.

Revision ID: a1u2d3h4o5l6
Revises: e5m6p7d8s9p0
"""
from alembic import op
import sqlalchemy as sa


revision = 'a1u2d3h4o5l6'
down_revision = 'e5m6p7d8s9p0'
branch_labels = None
depends_on = None


def _lock_timeout():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade():
    _lock_timeout()
    op.add_column(
        'school_holidays',
        sa.Column('applies_to', sa.String(length=20), nullable=False,
                  server_default='both'),
    )
    op.create_check_constraint(
        'ck_school_holidays_applies_to',
        'school_holidays',
        "applies_to IN ('both', 'students', 'employees')",
    )


def downgrade():
    _lock_timeout()
    op.drop_constraint('ck_school_holidays_applies_to', 'school_holidays',
                       type_='check')
    op.drop_column('school_holidays', 'applies_to')
