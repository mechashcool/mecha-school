"""Add student_middle_records table (السجلات الوسطية)

Revision ID: m1n2d3l4e5f6
Revises: j2k3l4m5n6o7
Create Date: 2026-10-02
"""
from alembic import op
import sqlalchemy as sa


revision = 'm1n2d3l4e5f6'
down_revision = 'j2k3l4m5n6o7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'student_middle_records',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), nullable=False),
        sa.Column('record_number', sa.String(length=80), nullable=True),
        sa.Column('page_number', sa.String(length=40), nullable=True),
        sa.Column('father_name', sa.String(length=200), nullable=True),
        sa.Column('grandfather_name', sa.String(length=200), nullable=True),
        sa.Column('great_grandfather_name', sa.String(length=200), nullable=True),
        sa.Column('years_failed', sa.String(length=100), nullable=True),
        sa.Column('snap_full_name', sa.String(length=200), nullable=False),
        sa.Column('snap_student_number', sa.String(length=40), nullable=True),
        sa.Column('snap_stage', sa.String(length=50), nullable=True),
        sa.Column('snap_grade_name', sa.String(length=100), nullable=True),
        sa.Column('snap_section_name', sa.String(length=50), nullable=True),
        sa.Column('snap_year_name', sa.String(length=50), nullable=True),
        sa.Column('snap_gender', sa.String(length=10), nullable=True),
        sa.Column('snap_date_of_birth', sa.Date(), nullable=True),
        sa.Column('snap_phone', sa.String(length=30), nullable=True),
        sa.Column('snap_address', sa.Text(), nullable=True),
        sa.Column('snap_status', sa.String(length=20), nullable=True),
        sa.Column('snap_enrollment_date', sa.Date(), nullable=True),
        sa.Column('snap_guardian_name', sa.String(length=200), nullable=True),
        sa.Column('snap_guardian_phone', sa.String(length=30), nullable=True),
        sa.Column('snap_guardian_relation', sa.String(length=50), nullable=True),
        sa.Column('school_name', sa.String(length=200), nullable=True),
        sa.Column('school_name_ar', sa.String(length=200), nullable=True),
        sa.Column('previous_school', sa.String(length=200), nullable=True),
        sa.Column('admission_date', sa.Date(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('subject_grades', sa.JSON(), nullable=False),
        sa.Column('total_score', sa.Float(), nullable=True),
        sa.Column('first_round_result', sa.String(length=100), nullable=True),
        sa.Column('second_round_result', sa.String(length=100), nullable=True),
        sa.Column('result_notes', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id']),
        sa.ForeignKeyConstraint(['student_id'], ['students.id']),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('school_id', 'student_id', 'academic_year_id',
                    name='uq_middle_record_school_student_year'),
    )
    op.create_index('ix_student_middle_records_school_id', 'student_middle_records', ['school_id'])
    op.create_index('ix_student_middle_records_student_id', 'student_middle_records', ['student_id'])
    op.create_index('ix_student_middle_records_academic_year_id', 'student_middle_records', ['academic_year_id'])


def downgrade():
    op.drop_index('ix_student_middle_records_academic_year_id', table_name='student_middle_records')
    op.drop_index('ix_student_middle_records_student_id', table_name='student_middle_records')
    op.drop_index('ix_student_middle_records_school_id', table_name='student_middle_records')
    op.drop_table('student_middle_records')
