"""add per-shift employee automatic-absence time

Revision ID: n1p2s3a4b5c6
Revises: m1n2d3l4e5f6
"""
from alembic import op
import sqlalchemy as sa


revision = 'n1p2s3a4b5c6'
down_revision = 'm1n2d3l4e5f6'
branch_labels = None
depends_on = None


def _lock_timeout():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade():
    _lock_timeout()
    op.add_column(
        'employee_attendance_shifts',
        sa.Column('absent_after_time', sa.Time(), nullable=True),
    )

    # Preserve the exact pre-migration behavior for every existing shift.
    # A NULL school cutoff deliberately remains NULL (fail closed).
    op.execute(sa.text("""
        UPDATE employee_attendance_shifts
        SET absent_after_time = (
            SELECT schools.emp_shift_absent_after_time
            FROM schools
            WHERE schools.id = employee_attendance_shifts.school_id
        )
    """))


def downgrade():
    _lock_timeout()
    op.drop_column('employee_attendance_shifts', 'absent_after_time')
