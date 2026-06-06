"""add attendance shifts feature

Adds optional two-shift (morning/afternoon) attendance scheduling:
  * schools.enable_attendance_shifts  — per-school flag (default false)
  * attendance_shifts table           — shift definitions with time windows
  * sections.shift_id (nullable FK)   — assigns a section to a shift
  * student_attendance.shift_id (nullable FK) — records which shift for reporting

All fields are nullable / default-safe so existing schools and existing rows
keep working exactly as before (feature OFF = current behaviour).

Revision ID: d3e4f5a6b7c8
Revises: c4d5e6f7a8b9
Create Date: 2026-06-06
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd3e4f5a6b7c8'
down_revision = 'c4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade():
    # 1) School feature flag — default false so existing schools are unchanged.
    with op.batch_alter_table('schools', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('enable_attendance_shifts', sa.Boolean(),
                      nullable=False, server_default=sa.false())
        )

    # 2) attendance_shifts — one row per shift per school.
    op.create_table(
        'attendance_shifts',
        sa.Column('id',                sa.Integer(),     nullable=False),
        sa.Column('school_id',         sa.Integer(),     nullable=False),
        sa.Column('name',              sa.String(100),   nullable=False),
        sa.Column('start_time',        sa.Time(),        nullable=False),
        sa.Column('late_after_time',   sa.Time(),        nullable=False),
        sa.Column('absent_after_time', sa.Time(),        nullable=False),
        sa.Column('dismissal_time',    sa.Time(),        nullable=True),
        sa.Column('is_active',         sa.Boolean(),     nullable=False, server_default=sa.true()),
        sa.Column('created_at',        sa.DateTime(),    nullable=True),
        sa.Column('updated_at',        sa.DateTime(),    nullable=True),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('school_id', 'name', name='uq_shift_school_name'),
    )
    op.create_index('ix_attendance_shifts_school_id',
                    'attendance_shifts', ['school_id'], unique=False)

    # 3) sections.shift_id — optional shift assignment (nullable).
    with op.batch_alter_table('sections', schema=None) as batch_op:
        batch_op.add_column(sa.Column('shift_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_sections_shift_id', 'attendance_shifts',
            ['shift_id'], ['id'],
        )

    # 4) student_attendance.shift_id — records shift for reporting (nullable).
    with op.batch_alter_table('student_attendance', schema=None) as batch_op:
        batch_op.add_column(sa.Column('shift_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_student_attendance_shift_id', 'attendance_shifts',
            ['shift_id'], ['id'],
        )


def downgrade():
    with op.batch_alter_table('student_attendance', schema=None) as batch_op:
        batch_op.drop_constraint('fk_student_attendance_shift_id', type_='foreignkey')
        batch_op.drop_column('shift_id')

    with op.batch_alter_table('sections', schema=None) as batch_op:
        batch_op.drop_constraint('fk_sections_shift_id', type_='foreignkey')
        batch_op.drop_column('shift_id')

    op.drop_index('ix_attendance_shifts_school_id', table_name='attendance_shifts')
    op.drop_table('attendance_shifts')

    with op.batch_alter_table('schools', schema=None) as batch_op:
        batch_op.drop_column('enable_attendance_shifts')
