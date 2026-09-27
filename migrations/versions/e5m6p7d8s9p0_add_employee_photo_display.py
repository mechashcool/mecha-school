"""add employees.photo_display (display-only photo derivative reference)

ONE new nullable column: employees.photo_display VARCHAR(255).

PURELY ADDITIVE. No default, no NOT NULL, no backfill, no UPDATE: every
existing row reads NULL and keeps displaying employees.photo exactly as before.
employees.photo (the original upload and the AI Face source) is not touched.
The application writes photo_display only for NEW / replacement uploads.

Locking (PostgreSQL): ADD COLUMN without a default is a catalog-only change —
no table rewrite and no row scan — but it needs a brief ACCESS EXCLUSIVE lock
on employees. lock_timeout makes the migration fail fast (nothing applied, safe
to retry) instead of queueing behind a long transaction and stalling every
query on employees meanwhile.

Downgrade drops the column (also catalog-only); employees.photo is unaffected.

Revision ID: e5m6p7d8s9p0
Revises: j1s2d3p4h5o6
"""
from alembic import op
import sqlalchemy as sa


revision = 'e5m6p7d8s9p0'
down_revision = 'j1s2d3p4h5o6'
branch_labels = None
depends_on = None


def _lock_timeout():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade():
    _lock_timeout()
    op.add_column('employees',
                  sa.Column('photo_display', sa.String(length=255), nullable=True))


def downgrade():
    _lock_timeout()
    op.drop_column('employees', 'photo_display')
