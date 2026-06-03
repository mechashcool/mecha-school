"""Add student_registration_records table (سجل قيد الطالب)

Revision ID: b2c3d4e5f6a7
Revises: f2g3h4i5j6k7
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'b2c3d4e5f6a7'
down_revision = 'f2g3h4i5j6k7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'student_registration_records',
        sa.Column('id',        sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),

        # Student snapshot
        sa.Column('snap_full_name',       sa.String(200), nullable=False),
        sa.Column('snap_student_number',  sa.String(40),  nullable=True),
        sa.Column('snap_gender',          sa.String(10),  nullable=True),
        sa.Column('snap_date_of_birth',   sa.Date(),      nullable=True),
        sa.Column('snap_nationality',     sa.String(80),  nullable=True),
        sa.Column('snap_address',         sa.Text(),      nullable=True),
        sa.Column('snap_phone',           sa.String(30),  nullable=True),
        sa.Column('snap_status',          sa.String(20),  nullable=True),
        sa.Column('snap_enrollment_date', sa.Date(),      nullable=True),

        # Guardian snapshot
        sa.Column('snap_guardian_name',     sa.String(200), nullable=True),
        sa.Column('snap_guardian_phone',    sa.String(30),  nullable=True),
        sa.Column('snap_guardian_email',    sa.String(180), nullable=True),
        sa.Column('snap_guardian_relation', sa.String(50),  nullable=True),
        sa.Column('snap_guardian_address',  sa.Text(),      nullable=True),

        # Academic placement snapshot
        sa.Column('snap_school_name',    sa.String(200), nullable=True),
        sa.Column('snap_school_name_ar', sa.String(200), nullable=True),
        sa.Column('snap_year_name',      sa.String(50),  nullable=True),
        sa.Column('snap_grade_name',     sa.String(100), nullable=True),
        sa.Column('snap_stage',          sa.String(50),  nullable=True),
        sa.Column('snap_section_name',   sa.String(50),  nullable=True),

        # Admission info
        sa.Column('admission_date',  sa.Date(),        nullable=True),
        sa.Column('document_number', sa.String(100),   nullable=True),
        sa.Column('previous_school', sa.String(200),   nullable=True),
        sa.Column('transfer_reason', sa.Text(),        nullable=True),
        sa.Column('admission_notes', sa.Text(),        nullable=True),

        # Document checklist
        sa.Column('has_birth_cert',       sa.Boolean(), server_default='false', nullable=False),
        sa.Column('has_id_card',          sa.Boolean(), server_default='false', nullable=False),
        sa.Column('has_prev_certificate', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('has_photo',            sa.Boolean(), server_default='false', nullable=False),
        sa.Column('document_notes',       sa.Text(),    nullable=True),

        # Academic history (JSON)
        sa.Column('academic_history_json', sa.Text(), nullable=True),

        # Notes & signatures
        sa.Column('general_notes',    sa.Text(),        nullable=True),
        sa.Column('signature_admin',  sa.String(200),   nullable=True),
        sa.Column('signature_parent', sa.String(200),   nullable=True),

        # Audit
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),

        # Constraints
        sa.ForeignKeyConstraint(['school_id'],  ['schools.id']),
        sa.ForeignKeyConstraint(['student_id'], ['students.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('school_id', 'student_id',
                            name='uq_registration_record_school_student'),
    )
    op.create_index('ix_student_registration_records_school_id',
                    'student_registration_records', ['school_id'])
    op.create_index('ix_student_registration_records_student_id',
                    'student_registration_records', ['student_id'])


def downgrade():
    op.drop_index('ix_student_registration_records_student_id',
                  table_name='student_registration_records')
    op.drop_index('ix_student_registration_records_school_id',
                  table_name='student_registration_records')
    op.drop_table('student_registration_records')
