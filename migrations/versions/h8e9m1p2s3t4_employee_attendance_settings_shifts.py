"""employee attendance settings + employee shifts (Phase 1 — config only)

Separates EMPLOYEE attendance configuration from the student att_* settings and
adds employee shifts. No attendance row, student column, student shift or
calculation is touched.

Schema:
  schools / school_settings  — six new columns each:
      emp_att_start_time, emp_att_late_threshold,
      emp_att_absence_threshold, emp_att_departure_time   (nullable Time)
      emp_enable_attendance_shifts  (NOT NULL, server_default false)
      emp_shift_absent_after_time   (nullable Time)
  employee_attendance_shifts — new table (school-scoped, uq on school+name)
  employees.shift_id         — nullable FK + index

COPY-FORWARD (behaviour preservation): employees currently read
School.att_late_threshold and School.att_departure_time directly, so those two
values are copied into their emp_* equivalents, and att_start_time is copied for
configuration parity. After this migration employee behaviour is IDENTICAL to
before; the values can then be edited per audience.

att_absence_threshold is DELIBERATELY NOT COPIED. Employee absence has no
cutoff policy today, so copying one would silently introduce a new policy and
change which days count as absent (and therefore payroll deductions).
emp_att_absence_threshold stays NULL = "not configured" and fails closed.

No employee is assigned a shift: emp_enable_attendance_shifts starts false and
employees.shift_id starts NULL for every existing row.

Locking (PostgreSQL): ADD COLUMN nullable without default is catalog-only. The
boolean carries a constant server_default, which PostgreSQL 11+ also applies
without a table rewrite. lock_timeout makes the migration fail fast (nothing
applied, safe to retry).

Downgrade refuses — and changes nothing — while any employee still holds a
shift assignment; it never discards data to make itself fit.

Revision ID: h8e9m1p2s3t4
Revises: g7p5l1v3l0c2
"""
from alembic import op
import sqlalchemy as sa


revision = 'h8e9m1p2s3t4'
down_revision = 'g7p5l1v3l0c2'
branch_labels = None
depends_on = None

# Tables that carry the student att_* block and therefore get the emp_* block.
# school_settings is the single-tenant / fresh-install fallback used when no
# School row resolves, so it must expose the same attribute names.
_SETTINGS_TABLES = ('schools', 'school_settings')

_TIME_COLUMNS = (
    'emp_att_start_time',
    'emp_att_late_threshold',
    'emp_att_absence_threshold',
    'emp_att_departure_time',
    'emp_shift_absent_after_time',
)

# Student source → employee destination. att_absence_threshold is absent ON
# PURPOSE — see the module docstring.
_COPY_FORWARD = (
    ('att_start_time',     'emp_att_start_time'),
    ('att_late_threshold', 'emp_att_late_threshold'),
    ('att_departure_time', 'emp_att_departure_time'),
)


def _lock_timeout():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade():
    _lock_timeout()

    # ── 1. Settings columns on both tables ───────────────────────────────────
    for table in _SETTINGS_TABLES:
        for name in _TIME_COLUMNS:
            op.add_column(table, sa.Column(name, sa.Time(), nullable=True))
        op.add_column(table, sa.Column(
            'emp_enable_attendance_shifts', sa.Boolean(),
            nullable=False, server_default=sa.false()))

    # ── 2. Copy-forward so employee behaviour is unchanged on deploy ─────────
    # Only the three values employee code already reads (plus start time for
    # parity). NULL sources stay NULL; no time is invented.
    for table in _SETTINGS_TABLES:
        assignments = ', '.join(f'{dst} = {src}' for src, dst in _COPY_FORWARD)
        op.execute(sa.text(f'UPDATE {table} SET {assignments}'))

    # ── 3. Employee shifts table ─────────────────────────────────────────────
    op.create_table(
        'employee_attendance_shifts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('start_time', sa.Time(), nullable=False),
        sa.Column('late_after_time', sa.Time(), nullable=True),
        sa.Column('dismissal_time', sa.Time(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                name='fk_emp_shift_school',
                                ondelete='CASCADE'),
        sa.UniqueConstraint('school_id', 'name', name='uq_emp_shift_school_name'),
    )
    op.create_index('ix_employee_attendance_shifts_school_id',
                    'employee_attendance_shifts', ['school_id'])

    # ── 4. Direct employee → shift assignment (starts NULL for everyone) ─────
    op.add_column('employees', sa.Column('shift_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_employees_shift', 'employees',
                          'employee_attendance_shifts', ['shift_id'], ['id'])
    op.create_index('ix_employees_shift_id', 'employees', ['shift_id'])


def downgrade():
    _lock_timeout()

    assigned = op.get_bind().execute(sa.text(
        'SELECT COUNT(*) FROM employees WHERE shift_id IS NOT NULL')).scalar()
    if assigned:
        raise RuntimeError(
            f'Refusing to drop employee shift support: {assigned} employee(s) are '
            f'assigned to an employee shift. No data was changed; clear those '
            f'assignments deliberately before downgrading.')

    op.drop_index('ix_employees_shift_id', table_name='employees')
    op.drop_constraint('fk_employees_shift', 'employees', type_='foreignkey')
    op.drop_column('employees', 'shift_id')

    op.drop_index('ix_employee_attendance_shifts_school_id',
                  table_name='employee_attendance_shifts')
    op.drop_table('employee_attendance_shifts')

    for table in _SETTINGS_TABLES:
        op.drop_column(table, 'emp_enable_attendance_shifts')
        for name in reversed(_TIME_COLUMNS):
            op.drop_column(table, name)
