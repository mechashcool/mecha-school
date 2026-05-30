"""Add DeviceEmployeeMapping table and extend EmployeeAttendance

Revision ID: r2s3t4u5v6w7
Revises: q1r2s3t4u5v6
Create Date: 2026-05-27

Changes:
  device_employee_mappings — new table: maps device enrollment numbers to employees
                             (mirrors device_student_mappings for employee AI Face support)
  employee_attendance      — add source VARCHAR(30) nullable
                           — add device_id INT nullable FK → attendance_devices(id) SET NULL
"""
from alembic import op
import sqlalchemy as sa


revision      = 'r2s3t4u5v6w7'
down_revision = 'q1r2s3t4u5v6'
branch_labels = None
depends_on    = None


def upgrade():
    # ── device_employee_mappings ──────────────────────────────────────────────
    op.create_table(
        'device_employee_mappings',
        sa.Column('id',            sa.Integer(),     nullable=False),
        sa.Column('school_id',     sa.Integer(),     nullable=False),
        sa.Column('device_id',     sa.Integer(),     nullable=False),
        sa.Column('employee_id',   sa.Integer(),     nullable=False),
        sa.Column('enrollment_no', sa.String(50),    nullable=False),
        sa.Column('is_active',     sa.Boolean(),     nullable=False, server_default='1'),
        sa.Column('created_at',    sa.DateTime(),    nullable=True),
        sa.ForeignKeyConstraint(['device_id'],   ['attendance_devices.id'],
                                name='fk_dem_device',   ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['employee_id'], ['employees.id'],
                                name='fk_dem_employee', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['school_id'],   ['schools.id'],
                                name='fk_dem_school'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('device_id', 'enrollment_no',
                            name='uq_device_employee_enrollid'),
    )
    op.create_index('ix_device_employee_mappings_school_id',
                    'device_employee_mappings', ['school_id'])
    op.create_index('ix_device_employee_mappings_device_id',
                    'device_employee_mappings', ['device_id'])
    op.create_index('ix_device_employee_mappings_employee_id',
                    'device_employee_mappings', ['employee_id'])

    # ── employee_attendance additions ─────────────────────────────────────────
    op.add_column('employee_attendance',
                  sa.Column('source', sa.String(30), nullable=True))
    op.add_column('employee_attendance',
                  sa.Column('device_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_ea_device', 'employee_attendance',
        'attendance_devices', ['device_id'], ['id'],
        ondelete='SET NULL',
    )


def downgrade():
    op.drop_constraint('fk_ea_device', 'employee_attendance', type_='foreignkey')
    op.drop_column('employee_attendance', 'device_id')
    op.drop_column('employee_attendance', 'source')

    op.drop_index('ix_device_employee_mappings_employee_id',
                  table_name='device_employee_mappings')
    op.drop_index('ix_device_employee_mappings_device_id',
                  table_name='device_employee_mappings')
    op.drop_index('ix_device_employee_mappings_school_id',
                  table_name='device_employee_mappings')
    op.drop_table('device_employee_mappings')
