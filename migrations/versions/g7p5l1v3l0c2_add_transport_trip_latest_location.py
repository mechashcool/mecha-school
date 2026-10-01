"""latest GPS location on transport_trips (live tracking Phase 2)

ONE change: five nullable columns on transport_trips holding the LATEST
location of the trip only — no history table, no backfill, no index (they are
rewritten every few seconds and never filtered on):

  latitude, longitude, location_accuracy  — double precision
  location_recorded_at                    — device-supplied time (metadata)
  location_updated_at                     — server receive time (freshness)

Locking (PostgreSQL): ADD COLUMN nullable without default is catalog-only (no
table rewrite). lock_timeout makes the migration fail fast (nothing applied,
safe to retry).

Downgrade refuses — and changes nothing — while any trip still holds a
location; it never discards data to make itself fit.

Revision ID: g7p5l1v3l0c2
Revises: d4r1v3r5t6p7
"""
from alembic import op
import sqlalchemy as sa


revision = 'g7p5l1v3l0c2'
down_revision = 'd4r1v3r5t6p7'
branch_labels = None
depends_on = None

_COLUMNS = (
    ('latitude', sa.Float()),
    ('longitude', sa.Float()),
    ('location_accuracy', sa.Float()),
    ('location_recorded_at', sa.DateTime()),
    ('location_updated_at', sa.DateTime()),
)


def _lock_timeout():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade():
    _lock_timeout()
    for name, type_ in _COLUMNS:
        op.add_column('transport_trips', sa.Column(name, type_, nullable=True))


def downgrade():
    _lock_timeout()
    located = op.get_bind().execute(sa.text(
        'SELECT COUNT(*) FROM transport_trips '
        'WHERE latitude IS NOT NULL OR location_updated_at IS NOT NULL')).scalar()
    if located:
        raise RuntimeError(
            f'Refusing to drop transport_trips location columns: {located} trip(s) '
            f'hold a location. No data was changed; clear them deliberately '
            f'before downgrading.')
    for name, _ in reversed(_COLUMNS):
        op.drop_column('transport_trips', name)
