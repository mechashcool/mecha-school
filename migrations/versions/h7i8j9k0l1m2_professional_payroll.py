"""Professional payroll module

Extends the salary system into a full payroll workflow:

  * employees           — payroll profile fields (salary_type, pay_method,
                          bank_account, salary_start_date, payroll_status)
  * salary_records      — snapshots, attendance breakdown counts, approval
                          audit columns, updated_at; legacy status 'pending'
                          is migrated to 'draft'
  * payroll_settings    — per-school payroll configuration (one row per school)
  * salary_components   — reusable allowance/deduction definitions
  * payroll_items       — per-record addition/deduction line items

Revision ID: h7i8j9k0l1m2
Revises: g6h7i8j9k0l1
Create Date: 2026-06-10
"""
from alembic import op
import sqlalchemy as sa


revision = 'h7i8j9k0l1m2'
down_revision = 'g6h7i8j9k0l1'
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. Employee payroll profile fields ────────────────────────────────────
    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.add_column(sa.Column('salary_type', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('pay_method', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('bank_account', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('salary_start_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('payroll_status', sa.String(length=20), nullable=True))

    # Sensible defaults for existing employees.
    op.execute("UPDATE employees SET salary_type = 'monthly' WHERE salary_type IS NULL")
    op.execute("UPDATE employees SET payroll_status = 'active' WHERE payroll_status IS NULL")

    # ── 2. SalaryRecord new columns ───────────────────────────────────────────
    with op.batch_alter_table('salary_records', schema=None) as batch_op:
        batch_op.add_column(sa.Column('employee_name_snapshot', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('job_title_snapshot', sa.String(length=150), nullable=True))
        batch_op.add_column(sa.Column('department_snapshot', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('absence_days', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('late_count', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('early_leave_count', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('approved_by', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('approved_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('cancelled_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True))
        batch_op.create_foreign_key(
            'fk_salary_records_approved_by_users', 'users', ['approved_by'], ['id']
        )

    # Migrate legacy 'pending' status → 'draft'; zero-out new count columns.
    op.execute("UPDATE salary_records SET status = 'draft' WHERE status = 'pending'")
    op.execute("UPDATE salary_records SET absence_days = 0 WHERE absence_days IS NULL")
    op.execute("UPDATE salary_records SET late_count = 0 WHERE late_count IS NULL")
    op.execute("UPDATE salary_records SET early_leave_count = 0 WHERE early_leave_count IS NULL")

    # ── 3. payroll_settings ───────────────────────────────────────────────────
    op.create_table(
        'payroll_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('payroll_calculation_day', sa.Integer(), nullable=True),
        sa.Column('default_payment_day', sa.Integer(), nullable=True),
        sa.Column('allow_edit_draft', sa.Boolean(), nullable=True),
        sa.Column('attendance_deduction_enabled', sa.Boolean(), nullable=True),
        sa.Column('absence_method', sa.String(length=20), nullable=True),
        sa.Column('absence_fixed_amount', sa.Numeric(12, 2), nullable=True),
        sa.Column('monthly_working_days', sa.Integer(), nullable=True),
        sa.Column('late_deduction_enabled', sa.Boolean(), nullable=True),
        sa.Column('late_method', sa.String(length=20), nullable=True),
        sa.Column('late_amount', sa.Numeric(12, 2), nullable=True),
        sa.Column('late_allowed_count', sa.Integer(), nullable=True),
        sa.Column('late_group_size', sa.Integer(), nullable=True),
        sa.Column('early_leave_deduction_enabled', sa.Boolean(), nullable=True),
        sa.Column('early_leave_amount', sa.Numeric(12, 2), nullable=True),
        sa.Column('unpaid_leave_deduction_enabled', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('school_id', name='uq_payroll_settings_school'),
    )
    op.create_index('ix_payroll_settings_school_id', 'payroll_settings',
                    ['school_id'], unique=False)

    # ── 4. salary_components ───────────────────────────────────────────────────
    op.create_table(
        'salary_components',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=150), nullable=False),
        sa.Column('component_type', sa.String(length=20), nullable=False),
        sa.Column('amount_type', sa.String(length=20), nullable=False),
        sa.Column('default_amount', sa.Numeric(12, 2), nullable=True),
        sa.Column('recurrence', sa.String(length=20), nullable=False),
        sa.Column('scope', sa.String(length=20), nullable=False),
        sa.Column('employee_id', sa.Integer(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['employee_id'], ['employees.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_salary_components_school_id', 'salary_components',
                    ['school_id'], unique=False)

    # ── 5. payroll_items ───────────────────────────────────────────────────────
    op.create_table(
        'payroll_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('salary_record_id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), nullable=False),
        sa.Column('component_id', sa.Integer(), nullable=True),
        sa.Column('name', sa.String(length=150), nullable=False),
        sa.Column('item_type', sa.String(length=20), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['salary_record_id'], ['salary_records.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id']),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id']),
        sa.ForeignKeyConstraint(['component_id'], ['salary_components.id'],
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_payroll_items_salary_record_id', 'payroll_items',
                    ['salary_record_id'], unique=False)
    op.create_index('ix_payroll_items_school_id', 'payroll_items',
                    ['school_id'], unique=False)
    op.create_index('ix_payroll_items_academic_year_id', 'payroll_items',
                    ['academic_year_id'], unique=False)

    # ── 6. Backfill legacy aggregate allowances/deductions into line items ──────
    # Existing salary_records carry allowances/deductions as plain totals with no
    # PayrollItem rows. Convert them to 'one_time' line items so the cached totals
    # stay consistent if the record is later edited (recompute sums line items).
    op.execute("""
        INSERT INTO payroll_items
            (salary_record_id, school_id, academic_year_id, name, item_type,
             amount, source, created_at)
        SELECT id, school_id, academic_year_id, 'بدلات', 'addition',
               allowances, 'one_time', CURRENT_TIMESTAMP
        FROM salary_records
        WHERE allowances IS NOT NULL AND allowances > 0
    """)
    op.execute("""
        INSERT INTO payroll_items
            (salary_record_id, school_id, academic_year_id, name, item_type,
             amount, source, created_at)
        SELECT id, school_id, academic_year_id, 'خصومات', 'deduction',
               deductions, 'one_time', CURRENT_TIMESTAMP
        FROM salary_records
        WHERE deductions IS NOT NULL AND deductions > 0
    """)


def downgrade():
    op.drop_index('ix_payroll_items_academic_year_id', table_name='payroll_items')
    op.drop_index('ix_payroll_items_school_id', table_name='payroll_items')
    op.drop_index('ix_payroll_items_salary_record_id', table_name='payroll_items')
    op.drop_table('payroll_items')

    op.drop_index('ix_salary_components_school_id', table_name='salary_components')
    op.drop_table('salary_components')

    op.drop_index('ix_payroll_settings_school_id', table_name='payroll_settings')
    op.drop_table('payroll_settings')

    with op.batch_alter_table('salary_records', schema=None) as batch_op:
        batch_op.drop_constraint('fk_salary_records_approved_by_users', type_='foreignkey')
        batch_op.drop_column('updated_at')
        batch_op.drop_column('cancelled_at')
        batch_op.drop_column('approved_at')
        batch_op.drop_column('approved_by')
        batch_op.drop_column('early_leave_count')
        batch_op.drop_column('late_count')
        batch_op.drop_column('absence_days')
        batch_op.drop_column('department_snapshot')
        batch_op.drop_column('job_title_snapshot')
        batch_op.drop_column('employee_name_snapshot')

    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.drop_column('payroll_status')
        batch_op.drop_column('salary_start_date')
        batch_op.drop_column('bank_account')
        batch_op.drop_column('pay_method')
        batch_op.drop_column('salary_type')
