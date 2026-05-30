"""Add parent complaints and leave requests

Revision ID: l6m7n8o9p0q1
Revises: k5l6m7n8o9p0
Create Date: 2026-05-22
"""

from alembic import op
import sqlalchemy as sa


revision = 'l6m7n8o9p0q1'
down_revision = 'k5l6m7n8o9p0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'complaints',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('parent_id', sa.Integer(), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('complaint_type', sa.String(length=30), nullable=False),
        sa.Column('details', sa.Text(), nullable=False),
        sa.Column('attachment_path', sa.String(length=500), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('manager_reply', sa.Text(), nullable=True),
        sa.Column('replied_by', sa.Integer(), nullable=True),
        sa.Column('replied_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id']),
        sa.ForeignKeyConstraint(['parent_id'], ['users.id']),
        sa.ForeignKeyConstraint(['replied_by'], ['users.id']),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id']),
        sa.ForeignKeyConstraint(['student_id'], ['students.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_complaints_academic_year_id'), 'complaints', ['academic_year_id'], unique=False)
    op.create_index(op.f('ix_complaints_created_at'), 'complaints', ['created_at'], unique=False)
    op.create_index(op.f('ix_complaints_parent_id'), 'complaints', ['parent_id'], unique=False)
    op.create_index(op.f('ix_complaints_school_id'), 'complaints', ['school_id'], unique=False)
    op.create_index(op.f('ix_complaints_status'), 'complaints', ['status'], unique=False)
    op.create_index(op.f('ix_complaints_student_id'), 'complaints', ['student_id'], unique=False)

    op.create_table(
        'leave_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('parent_id', sa.Integer(), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), nullable=False),
        sa.Column('leave_type', sa.String(length=30), nullable=False),
        sa.Column('from_date', sa.Date(), nullable=False),
        sa.Column('to_date', sa.Date(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('attachment_path', sa.String(length=500), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('manager_note', sa.Text(), nullable=True),
        sa.Column('reviewed_by', sa.Integer(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id']),
        sa.ForeignKeyConstraint(['parent_id'], ['users.id']),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id']),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id']),
        sa.ForeignKeyConstraint(['student_id'], ['students.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_leave_requests_academic_year_id'), 'leave_requests', ['academic_year_id'], unique=False)
    op.create_index(op.f('ix_leave_requests_created_at'), 'leave_requests', ['created_at'], unique=False)
    op.create_index(op.f('ix_leave_requests_parent_id'), 'leave_requests', ['parent_id'], unique=False)
    op.create_index(op.f('ix_leave_requests_school_id'), 'leave_requests', ['school_id'], unique=False)
    op.create_index(op.f('ix_leave_requests_status'), 'leave_requests', ['status'], unique=False)
    op.create_index(op.f('ix_leave_requests_student_id'), 'leave_requests', ['student_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_leave_requests_student_id'), table_name='leave_requests')
    op.drop_index(op.f('ix_leave_requests_status'), table_name='leave_requests')
    op.drop_index(op.f('ix_leave_requests_school_id'), table_name='leave_requests')
    op.drop_index(op.f('ix_leave_requests_parent_id'), table_name='leave_requests')
    op.drop_index(op.f('ix_leave_requests_created_at'), table_name='leave_requests')
    op.drop_index(op.f('ix_leave_requests_academic_year_id'), table_name='leave_requests')
    op.drop_table('leave_requests')

    op.drop_index(op.f('ix_complaints_student_id'), table_name='complaints')
    op.drop_index(op.f('ix_complaints_status'), table_name='complaints')
    op.drop_index(op.f('ix_complaints_school_id'), table_name='complaints')
    op.drop_index(op.f('ix_complaints_parent_id'), table_name='complaints')
    op.drop_index(op.f('ix_complaints_created_at'), table_name='complaints')
    op.drop_index(op.f('ix_complaints_academic_year_id'), table_name='complaints')
    op.drop_table('complaints')
