"""Add employee_leave_requests (teacher leave requests)

Revision ID: j9k0l1m2n3o4
Revises: i8j9k0l1m2n3
Create Date: 2026-06-13

Backward-compatible: adds a brand-new table only. Does not alter or touch the
parent/student ``leave_requests`` table or any existing data.
"""
from alembic import op
import sqlalchemy as sa

revision = 'j9k0l1m2n3o4'
down_revision = 'i8j9k0l1m2n3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'employee_leave_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), nullable=True),
        sa.Column('leave_type', sa.String(length=30), nullable=False),
        sa.Column('from_date', sa.Date(), nullable=False),
        sa.Column('to_date', sa.Date(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('attachment_path', sa.String(length=500), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('admin_response', sa.Text(), nullable=True),
        sa.Column('rejection_reason', sa.Text(), nullable=True),
        sa.Column('reviewed_by', sa.Integer(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['employee_id'], ['employees.id']),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id']),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id']),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_employee_leave_requests_employee_id'),
                    'employee_leave_requests', ['employee_id'], unique=False)
    op.create_index(op.f('ix_employee_leave_requests_school_id'),
                    'employee_leave_requests', ['school_id'], unique=False)
    op.create_index(op.f('ix_employee_leave_requests_academic_year_id'),
                    'employee_leave_requests', ['academic_year_id'], unique=False)
    op.create_index(op.f('ix_employee_leave_requests_status'),
                    'employee_leave_requests', ['status'], unique=False)
    op.create_index(op.f('ix_employee_leave_requests_created_at'),
                    'employee_leave_requests', ['created_at'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_employee_leave_requests_created_at'),
                  table_name='employee_leave_requests')
    op.drop_index(op.f('ix_employee_leave_requests_status'),
                  table_name='employee_leave_requests')
    op.drop_index(op.f('ix_employee_leave_requests_academic_year_id'),
                  table_name='employee_leave_requests')
    op.drop_index(op.f('ix_employee_leave_requests_school_id'),
                  table_name='employee_leave_requests')
    op.drop_index(op.f('ix_employee_leave_requests_employee_id'),
                  table_name='employee_leave_requests')
    op.drop_table('employee_leave_requests')
