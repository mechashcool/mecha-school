"""transport driver identity + trip lifecycle (live tracking Phase 1)

Three changes, nothing else:
  1. transport_routes.driver_employee_id — nullable FK → employees.id
     (ON DELETE SET NULL) + index. NULL for every existing route: legacy routes
     keep using driver_name / driver_phone exactly as before. No backfill and
     no matching by name/phone.
  2. transport_trips — start/end lifecycle rows (no location columns), with a
     partial unique index allowing at most ONE active trip per route.
  3. The built-in `driver` role row (no permissions), inserted only if missing.

Locking (PostgreSQL): ADD COLUMN (nullable, no default) is catalog-only; the
FK validation scans transport_routes, where every value is NULL. lock_timeout
makes the migration fail fast (nothing applied, safe to retry).

Downgrade refuses — and changes nothing — while any trip, linked route or
driver account exists; it never deletes data to make itself fit.

Revision ID: d4r1v3r5t6p7
Revises: r7g2d0n1u2l3
"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = 'd4r1v3r5t6p7'
down_revision = 'r7g2d0n1u2l3'
branch_labels = None
depends_on = None


def _lock_timeout():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade():
    _lock_timeout()

    # 1. Route → driver Employee
    op.add_column('transport_routes',
                  sa.Column('driver_employee_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_transport_routes_driver_employee_id',
                          'transport_routes', 'employees',
                          ['driver_employee_id'], ['id'], ondelete='SET NULL')
    op.create_index('ix_transport_routes_driver_employee_id',
                    'transport_routes', ['driver_employee_id'])

    # 2. Trip lifecycle
    op.create_table(
        'transport_trips',
        sa.Column('id',                 sa.Integer(),    nullable=False),
        sa.Column('school_id',          sa.Integer(),    nullable=False),
        sa.Column('route_id',           sa.Integer(),    nullable=False),
        sa.Column('driver_employee_id', sa.Integer(),    nullable=False),
        sa.Column('status',             sa.String(20),   nullable=False,
                  server_default='active'),
        sa.Column('started_at',         sa.DateTime(),   nullable=False),
        sa.Column('ended_at',           sa.DateTime(),   nullable=True),
        sa.Column('created_at',         sa.DateTime(),   nullable=True),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['route_id'], ['transport_routes.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['driver_employee_id'], ['employees.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_transport_trips_school_id', 'transport_trips', ['school_id'])
    op.create_index('ix_transport_trips_route_id', 'transport_trips', ['route_id'])
    op.create_index('ix_transport_trips_driver_employee_id', 'transport_trips',
                    ['driver_employee_id'])
    op.create_index('uq_transport_trip_active_route', 'transport_trips', ['route_id'],
                    unique=True, postgresql_where=sa.text("status = 'active'"))

    # 3. Built-in driver role (idempotent; no permissions)
    conn = op.get_bind()
    exists = conn.execute(
        sa.text("SELECT 1 FROM roles WHERE name = 'driver' LIMIT 1")).fetchone()
    if not exists:
        roles_t = sa.table(
            'roles',
            sa.column('name', sa.String), sa.column('label', sa.String),
            sa.column('description', sa.Text), sa.column('is_admin', sa.Boolean),
            sa.column('created_at', sa.DateTime),
        )
        op.bulk_insert(roles_t, [{
            'name':        'driver',
            'label':       'سائق',
            'description': 'سائق خط نقل — يستخدم تطبيق الهاتف فقط لعرض خطوطه وبدء/إنهاء الرحلة',
            'is_admin':    False,
            'created_at':  datetime.utcnow(),
        }])


def downgrade():
    _lock_timeout()
    conn = op.get_bind()
    trips = conn.execute(sa.text('SELECT COUNT(*) FROM transport_trips')).scalar()
    linked = conn.execute(sa.text(
        'SELECT COUNT(*) FROM transport_routes WHERE driver_employee_id IS NOT NULL')).scalar()
    drivers = conn.execute(sa.text(
        "SELECT COUNT(*) FROM users u JOIN roles r ON r.id = u.role_id "
        "WHERE r.name = 'driver'")).scalar()
    if trips or linked or drivers:
        raise RuntimeError(
            f'Refusing to downgrade transport driver/trips: {trips} trip(s), '
            f'{linked} linked route(s), {drivers} driver account(s) exist. '
            f'No data was changed; resolve them deliberately before downgrading.')

    op.drop_index('uq_transport_trip_active_route', table_name='transport_trips')
    op.drop_index('ix_transport_trips_driver_employee_id', table_name='transport_trips')
    op.drop_index('ix_transport_trips_route_id', table_name='transport_trips')
    op.drop_index('ix_transport_trips_school_id', table_name='transport_trips')
    op.drop_table('transport_trips')

    op.drop_index('ix_transport_routes_driver_employee_id', table_name='transport_routes')
    op.drop_constraint('fk_transport_routes_driver_employee_id', 'transport_routes',
                       type_='foreignkey')
    op.drop_column('transport_routes', 'driver_employee_id')

    # The role row is removed only when nothing references it (checked above
    # for users; role_permissions / role_schools rows are cleared first).
    conn.execute(sa.text(
        "DELETE FROM role_permissions WHERE role_id IN "
        "(SELECT id FROM roles WHERE name = 'driver')"))
    conn.execute(sa.text(
        "DELETE FROM role_schools WHERE role_id IN "
        "(SELECT id FROM roles WHERE name = 'driver')"))
    conn.execute(sa.text("DELETE FROM roles WHERE name = 'driver'"))
