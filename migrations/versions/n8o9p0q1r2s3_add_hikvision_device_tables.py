"""Add Hikvision attendance device tables

Revision ID: n8o9p0q1r2s3
Revises: m7n8o9p0q1r2
Create Date: 2026-05-23

Creates:
  attendance_devices       — one row per Hikvision reader per school
  device_event_logs        — raw access events from devices (dedup by device+serial_no)
  device_student_mappings  — numeric employeeNoString → student mapping per device
"""

from alembic import op
import sqlalchemy as sa


revision = 'n8o9p0q1r2s3'
down_revision = 'm7n8o9p0q1r2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'attendance_devices',
        sa.Column('id',               sa.Integer(),     nullable=False),
        sa.Column('school_id',        sa.Integer(),     nullable=False),
        sa.Column('academic_year_id', sa.Integer(),     nullable=True),
        sa.Column('name',             sa.String(150),   nullable=False),
        sa.Column('device_type',      sa.String(30),    nullable=False),
        sa.Column('ip_address',       sa.String(45),    nullable=False),
        sa.Column('port',             sa.Integer(),     nullable=False),
        sa.Column('username',         sa.String(80),    nullable=False),
        sa.Column('password',         sa.String(200),   nullable=False),
        sa.Column('device_sn',        sa.String(100),   nullable=False),
        sa.Column('is_active',        sa.Boolean(),     nullable=False),
        sa.Column('last_sync_at',     sa.DateTime(),    nullable=True),
        sa.Column('notes',            sa.Text(),        nullable=True),
        sa.Column('created_at',       sa.DateTime(),    nullable=True),
        sa.Column('updated_at',       sa.DateTime(),    nullable=True),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id']),
        sa.ForeignKeyConstraint(['school_id'],        ['schools.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_attendance_devices_school_id'),
                    'attendance_devices', ['school_id'])
    op.create_index(op.f('ix_attendance_devices_is_active'),
                    'attendance_devices', ['is_active'])

    op.create_table(
        'device_event_logs',
        sa.Column('id',                 sa.Integer(),    nullable=False),
        sa.Column('school_id',          sa.Integer(),    nullable=False),
        sa.Column('academic_year_id',   sa.Integer(),    nullable=True),
        sa.Column('device_id',          sa.Integer(),    nullable=False),
        sa.Column('serial_no',          sa.BigInteger(), nullable=False),
        sa.Column('employee_no_string', sa.String(50),   nullable=True),
        sa.Column('person_name',        sa.String(200),  nullable=True),
        sa.Column('event_time',         sa.DateTime(),   nullable=True),
        sa.Column('major',              sa.Integer(),    nullable=True),
        sa.Column('minor',              sa.Integer(),    nullable=True),
        sa.Column('verify_mode',        sa.String(80),   nullable=True),
        sa.Column('picture_url',        sa.String(500),  nullable=True),
        sa.Column('raw_json',           sa.Text(),       nullable=True),
        sa.Column('status',             sa.String(20),   nullable=False),
        sa.Column('error_message',      sa.Text(),       nullable=True),
        sa.Column('created_at',         sa.DateTime(),   nullable=True),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id']),
        sa.ForeignKeyConstraint(['device_id'],
                                ['attendance_devices.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('device_id', 'serial_no',
                            name='uq_device_event_log_device_serial'),
    )
    op.create_index(op.f('ix_device_event_logs_school_id'),
                    'device_event_logs', ['school_id'])
    op.create_index(op.f('ix_device_event_logs_device_id'),
                    'device_event_logs', ['device_id'])
    op.create_index(op.f('ix_device_event_logs_status'),
                    'device_event_logs', ['status'])

    op.create_table(
        'device_student_mappings',
        sa.Column('id',                 sa.Integer(),  nullable=False),
        sa.Column('school_id',          sa.Integer(),  nullable=False),
        sa.Column('device_id',          sa.Integer(),  nullable=False),
        sa.Column('employee_no_string', sa.String(50), nullable=False),
        sa.Column('student_id',         sa.Integer(),  nullable=False),
        sa.Column('is_active',          sa.Boolean(),  nullable=False),
        sa.Column('created_at',         sa.DateTime(), nullable=True),
        sa.Column('updated_at',         sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['device_id'],
                                ['attendance_devices.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id']),
        sa.ForeignKeyConstraint(['student_id'],
                                ['students.id'],
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('device_id', 'employee_no_string',
                            name='uq_device_student_mapping_device_empno'),
    )
    op.create_index(op.f('ix_device_student_mappings_school_id'),
                    'device_student_mappings', ['school_id'])
    op.create_index(op.f('ix_device_student_mappings_device_id'),
                    'device_student_mappings', ['device_id'])
    op.create_index(op.f('ix_device_student_mappings_student_id'),
                    'device_student_mappings', ['student_id'])


def downgrade():
    op.drop_index(op.f('ix_device_student_mappings_student_id'),
                  table_name='device_student_mappings')
    op.drop_index(op.f('ix_device_student_mappings_device_id'),
                  table_name='device_student_mappings')
    op.drop_index(op.f('ix_device_student_mappings_school_id'),
                  table_name='device_student_mappings')
    op.drop_table('device_student_mappings')

    op.drop_index(op.f('ix_device_event_logs_status'),
                  table_name='device_event_logs')
    op.drop_index(op.f('ix_device_event_logs_device_id'),
                  table_name='device_event_logs')
    op.drop_index(op.f('ix_device_event_logs_school_id'),
                  table_name='device_event_logs')
    op.drop_table('device_event_logs')

    op.drop_index(op.f('ix_attendance_devices_is_active'),
                  table_name='attendance_devices')
    op.drop_index(op.f('ix_attendance_devices_school_id'),
                  table_name='attendance_devices')
    op.drop_table('attendance_devices')
