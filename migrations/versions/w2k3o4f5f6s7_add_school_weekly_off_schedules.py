"""add school_weekly_off_schedules (effective-dated weekly days off per audience)

ONE new table: school_weekly_off_schedules.

  school_id       → schools.id ON DELETE CASCADE (NOT NULL — no global rows)
  audience        'students' | 'employees'
  off_days        '' (explicitly none) or canonical "d,d" with d in 0..6,
                  Python weekday numbering (Mon=0 … Sun=6) — same as
                  schools.weekly_off_days
  effective_from  first date the row applies to
  UNIQUE (school_id, audience, effective_from)

PURELY ADDITIVE. The table starts EMPTY and nothing is backfilled: a school
with no rows keeps resolving its weekly days off from the legacy shared
schools.weekly_off_days for both audiences — exactly the current behaviour.
schools.weekly_off_days is not modified.

Downgrade drops the table; the application then falls back to
schools.weekly_off_days for both audiences.  No attendance, payroll, holiday or
school rows are touched in either direction.

Revision ID: w2k3o4f5f6s7
Revises: a1u2d3h4o5l6
"""
from alembic import op
import sqlalchemy as sa


revision = 'w2k3o4f5f6s7'
down_revision = 'a1u2d3h4o5l6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'school_weekly_off_schedules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('audience', sa.String(length=20), nullable=False),
        sa.Column('off_days', sa.String(length=20), nullable=False,
                  server_default=''),
        sa.Column('effective_from', sa.Date(), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ['school_id'], ['schools.id'],
            name='fk_weekly_off_school_id', ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['created_by'], ['users.id'],
            name='fk_weekly_off_created_by', ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('school_id', 'audience', 'effective_from',
                            name='uq_weekly_off_school_audience_from'),
        sa.CheckConstraint("audience IN ('students', 'employees')",
                           name='ck_weekly_off_audience'),
    )
    op.create_index('ix_school_weekly_off_schedules_school_id',
                    'school_weekly_off_schedules', ['school_id'])
    if op.get_bind().dialect.name == 'postgresql':
        # Canonical serialisation only: empty, or 1–7 comma-separated digits 0..6.
        op.create_check_constraint(
            'ck_weekly_off_days_format',
            'school_weekly_off_schedules',
            "off_days ~ '^([0-6](,[0-6]){0,6})?$'",
        )


def downgrade():
    op.drop_index('ix_school_weekly_off_schedules_school_id',
                  table_name='school_weekly_off_schedules')
    op.drop_table('school_weekly_off_schedules')
