"""Add source and created_by_user_id to leave_requests and employee_leave_requests

Revision ID: k0l1m2n3o4p5
Revises: j9k0l1m2n3o4
Create Date: 2026-06-14

Backward-compatible: adds two nullable columns to each leave table only.
Existing rows receive NULL for both fields; application code treats NULL source
as the original mobile/parent or mobile/employee submission path.
No existing data is modified.
"""
from alembic import op
import sqlalchemy as sa

revision = 'k0l1m2n3o4p5'
down_revision = 'j9k0l1m2n3o4'
branch_labels = None
depends_on = None


def upgrade():
    # ── leave_requests (parent/student leave) ─────────────────────────────────
    op.add_column('leave_requests',
        sa.Column('source', sa.String(length=20), nullable=True))
    op.add_column('leave_requests',
        sa.Column('created_by_user_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_leave_requests_created_by_user_id',
        'leave_requests', 'users',
        ['created_by_user_id'], ['id'],
    )
    # Allow admin-created leave requests to have no parent (parent_id=NULL).
    # Existing rows already have valid non-null values; only new admin-created
    # rows will use NULL. parent_id=NULL is disambiguated by source='admin'.
    op.alter_column('leave_requests', 'parent_id',
                    existing_type=sa.Integer(), nullable=True)

    # ── employee_leave_requests ───────────────────────────────────────────────
    op.add_column('employee_leave_requests',
        sa.Column('source', sa.String(length=20), nullable=True))
    op.add_column('employee_leave_requests',
        sa.Column('created_by_user_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_employee_leave_requests_created_by_user_id',
        'employee_leave_requests', 'users',
        ['created_by_user_id'], ['id'],
    )


def downgrade():
    op.drop_constraint('fk_employee_leave_requests_created_by_user_id',
                       'employee_leave_requests', type_='foreignkey')
    op.drop_column('employee_leave_requests', 'created_by_user_id')
    op.drop_column('employee_leave_requests', 'source')

    op.drop_constraint('fk_leave_requests_created_by_user_id',
                       'leave_requests', type_='foreignkey')
    op.drop_column('leave_requests', 'created_by_user_id')
    op.drop_column('leave_requests', 'source')
    # NOTE: restoring NOT NULL will fail if any admin-created rows exist with
    # parent_id=NULL. Those rows must be removed or back-filled before downgrading.
    op.alter_column('leave_requests', 'parent_id',
                    existing_type=sa.Integer(), nullable=False)
