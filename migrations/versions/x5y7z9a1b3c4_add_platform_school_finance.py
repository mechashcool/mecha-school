"""Add isolated annual platform school finance tables.

Revision ID: x5y7z9a1b3c4
Revises: w4x6y8z0a2b3
Create Date: 2026-11-10
"""
from alembic import op
import sqlalchemy as sa

revision = 'x5y7z9a1b3c4'
down_revision = 'w4x6y8z0a2b3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('annual_school_agreements',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('school_id', sa.Integer(), sa.ForeignKey('schools.id'), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), sa.ForeignKey('academic_years.id'), nullable=False),
        sa.Column('revision_number', sa.Integer(), nullable=False),
        sa.Column('annual_price_per_student', sa.BigInteger(), nullable=False),
        sa.Column('effective_date', sa.Date(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('approved_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('approved_at', sa.DateTime()), sa.Column('notes', sa.Text()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('annual_price_per_student > 0', name='ck_annual_agreement_price_positive'),
        sa.UniqueConstraint('school_id', 'academic_year_id', 'revision_number', name='uq_annual_agreement_revision'))
    op.create_index('uq_annual_agreement_active', 'annual_school_agreements',
                    ['school_id', 'academic_year_id'], unique=True,
                    postgresql_where=sa.text("status = 'active'"))

    op.create_table('platform_school_invoices',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('school_id', sa.Integer(), sa.ForeignKey('schools.id'), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), sa.ForeignKey('academic_years.id'), nullable=False),
        sa.Column('agreement_id', sa.Integer(), sa.ForeignKey('annual_school_agreements.id'), nullable=False),
        sa.Column('invoice_number', sa.String(50), nullable=False, unique=True),
        sa.Column('submission_key', sa.String(64), nullable=False, unique=True),
        sa.Column('invoice_type', sa.String(30), nullable=False),
        sa.Column('issue_date', sa.Date(), nullable=False), sa.Column('due_date', sa.Date()),
        sa.Column('approved_student_count', sa.Integer(), nullable=False),
        sa.Column('unit_price_snapshot', sa.BigInteger(), nullable=False),
        sa.Column('total_amount', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('approved_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('approved_at', sa.DateTime()), sa.Column('notes', sa.Text()),
        sa.Column('voided_at', sa.DateTime()),
        sa.Column('voided_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('void_reason', sa.Text()), sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint("invoice_type IN ('annual_base', 'additional_students')", name='ck_platform_invoice_type'),
        sa.CheckConstraint('approved_student_count > 0', name='ck_platform_invoice_students_positive'),
        sa.CheckConstraint('unit_price_snapshot > 0 AND total_amount > 0', name='ck_platform_invoice_amount_positive'))
    op.create_index('ix_platform_invoice_school_year', 'platform_school_invoices', ['school_id', 'academic_year_id'])

    op.create_table('platform_invoice_student_coverage',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('invoice_id', sa.Integer(), sa.ForeignKey('platform_school_invoices.id'), nullable=False),
        sa.Column('school_id', sa.Integer(), sa.ForeignKey('schools.id'), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), sa.ForeignKey('academic_years.id'), nullable=False),
        sa.Column('student_id', sa.Integer(), sa.ForeignKey('students.id'), nullable=False),
        sa.Column('student_code_snapshot', sa.String(40), nullable=False),
        sa.Column('student_name_snapshot', sa.String(200), nullable=False),
        sa.Column('unit_price_snapshot', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_index('uq_platform_effective_student_coverage', 'platform_invoice_student_coverage',
                    ['school_id', 'academic_year_id', 'student_id'], unique=True,
                    postgresql_where=sa.text("status = 'active'"))
    op.create_index('ix_platform_coverage_invoice', 'platform_invoice_student_coverage', ['invoice_id'])

    op.create_table('platform_school_payments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('school_id', sa.Integer(), sa.ForeignKey('schools.id'), nullable=False),
        sa.Column('amount', sa.BigInteger(), nullable=False), sa.Column('payment_date', sa.Date(), nullable=False),
        sa.Column('payment_method', sa.String(30), nullable=False),
        sa.Column('receipt_number', sa.String(50), nullable=False, unique=True),
        sa.Column('submission_key', sa.String(64), nullable=False, unique=True),
        sa.Column('transaction_reference', sa.String(120)), sa.Column('received_by', sa.String(200)),
        sa.Column('recorded_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('covered_student_count', sa.Integer()), sa.Column('notes', sa.Text()),
        sa.Column('status', sa.String(20), nullable=False), sa.Column('reversed_at', sa.DateTime()),
        sa.Column('reversed_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('reversal_reason', sa.Text()), sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('amount > 0', name='ck_platform_payment_amount_positive'),
        sa.CheckConstraint('covered_student_count IS NULL OR covered_student_count >= 0', name='ck_platform_payment_student_count'))
    op.create_index('ix_platform_payment_school_date', 'platform_school_payments', ['school_id', 'payment_date'])

    op.create_table('platform_payment_allocations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('payment_id', sa.Integer(), sa.ForeignKey('platform_school_payments.id'), nullable=False),
        sa.Column('invoice_id', sa.Integer(), sa.ForeignKey('platform_school_invoices.id'), nullable=False),
        sa.Column('allocated_amount', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('allocated_amount > 0', name='ck_platform_allocation_positive'),
        sa.UniqueConstraint('payment_id', 'invoice_id', name='uq_platform_payment_invoice'))
    op.create_index('ix_platform_allocation_invoice', 'platform_payment_allocations', ['invoice_id'])


def downgrade():
    op.drop_table('platform_payment_allocations')
    op.drop_table('platform_school_payments')
    op.drop_table('platform_invoice_student_coverage')
    op.drop_table('platform_school_invoices')
    op.drop_table('annual_school_agreements')
