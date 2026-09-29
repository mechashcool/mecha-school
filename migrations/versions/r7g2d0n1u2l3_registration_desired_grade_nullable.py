"""allow student_registration_requests.desired_grade_id to be NULL

ONE change: student_registration_requests.desired_grade_id NOT NULL -> NULL.

Schema compatibility only. An institute's public registration form will stop
asking for a grade (placement happens at staff approval through institute
study groups), so its requests must be storable without one. Schools keep
requiring a grade in application code; nothing here changes their behaviour.

No UPDATE, no backfill, no placeholder grade: every existing row keeps its
current value. The foreign key to grades.id is unchanged (NULL is simply
"no grade").

Locking (PostgreSQL): DROP NOT NULL is a catalog-only change (no table rewrite,
no row scan). lock_timeout makes the migration fail fast (nothing applied, safe
to retry) instead of queueing behind a long transaction.

Downgrade restores NOT NULL ONLY when no row has a NULL desired_grade_id. If any
does, it raises and changes nothing — it never deletes, rewrites or invents
data to make the constraint fit.

Revision ID: r7g2d0n1u2l3
Revises: w2k3o4f5f6s7
"""
from alembic import op
import sqlalchemy as sa


revision = 'r7g2d0n1u2l3'
down_revision = 'w2k3o4f5f6s7'
branch_labels = None
depends_on = None


def _lock_timeout():
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade():
    _lock_timeout()
    op.alter_column('student_registration_requests', 'desired_grade_id',
                    existing_type=sa.Integer(), nullable=True)


def downgrade():
    _lock_timeout()
    null_rows = op.get_bind().execute(sa.text(
        'SELECT COUNT(*) FROM student_registration_requests '
        'WHERE desired_grade_id IS NULL')).scalar()
    if null_rows:
        raise RuntimeError(
            f'Refusing to restore NOT NULL on student_registration_requests.'
            f'desired_grade_id: {null_rows} row(s) have no grade. No data was '
            f'changed; resolve those rows deliberately before downgrading.')
    op.alter_column('student_registration_requests', 'desired_grade_id',
                    existing_type=sa.Integer(), nullable=False)
