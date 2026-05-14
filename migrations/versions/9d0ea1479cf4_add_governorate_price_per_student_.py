"""add_governorate_price_per_student_school_billing

Revision ID: 9d0ea1479cf4
Revises: f7a8b9c0d1e2
Create Date: 2026-05-06 20:45:48.847054

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9d0ea1479cf4'
down_revision = 'f7a8b9c0d1e2'
branch_labels = None
depends_on = None


def upgrade():
    # New school_billing table for super-admin subscription billing
    op.create_table('school_billing',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('amount_due', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('amount_paid', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('description', sa.String(length=400), nullable=True),
        sa.Column('billing_type', sa.String(length=30), nullable=False),
        sa.Column('due_date', sa.Date(), nullable=True),
        sa.Column('payment_date', sa.Date(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_school_billing_school_id', 'school_billing', ['school_id'], unique=False)

    # New columns on schools table
    with op.batch_alter_table('schools', schema=None) as batch_op:
        batch_op.add_column(sa.Column('governorate', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('price_per_student', sa.Numeric(precision=12, scale=2), nullable=True))
        batch_op.create_index('ix_schools_governorate', ['governorate'], unique=False)


def downgrade():
    with op.batch_alter_table('schools', schema=None) as batch_op:
        batch_op.drop_index('ix_schools_governorate')
        batch_op.drop_column('price_per_student')
        batch_op.drop_column('governorate')

    op.drop_index('ix_school_billing_school_id', table_name='school_billing')
    op.drop_table('school_billing')
