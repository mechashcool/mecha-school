"""add school-wide shift auto-absence cutoff

Adds a single school-level automatic-absence time shared by every attendance
shift of that school:

  * schools.shift_absent_after_time (TIME NULL)

Purely additive:
  * Nothing is backfilled.  Every school starts NULL, which means "not
    configured" and makes shift auto-absence fail closed (no absence records
    are created) until an administrator explicitly chooses a value.  A value is
    deliberately NOT derived from the existing per-shift values, because
    schools have divergent per-shift cutoffs and silently picking one would
    create wrong absences and unsendable parent notifications.
  * schools.att_absence_threshold is untouched — unified (non-shift) mode keeps
    working exactly as before.
  * attendance_shifts.absent_after_time is untouched — the column and all
    existing values are retained for rollback/audit.  It simply stops being
    read for the automatic-absence decision.
  * No student_attendance row is read, rewritten or deleted.
  * No attendance_shifts row is created, merged or deleted.

Revision ID: g9s1h2a0b1c2
Revises: s1y2n3c0f0a1
Create Date: 2026-09-13
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'g9s1h2a0b1c2'
down_revision = 's1y2n3c0f0a1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('schools', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('shift_absent_after_time', sa.Time(), nullable=True)
        )


def downgrade():
    # Drops only the column added above.  att_absence_threshold,
    # attendance_shifts.absent_after_time and all attendance history are
    # untouched, so reverting restores the previous per-shift behaviour with no
    # data loss beyond the newly configured global cutoffs.
    with op.batch_alter_table('schools', schema=None) as batch_op:
        batch_op.drop_column('shift_absent_after_time')
