"""add institute suspension group scope

Two new tables that let an INSTITUTE suspension apply to all study groups or
to selected ones only:

  institute_suspension_scopes  — at most one row per student_suspensions row
                                 (UNIQUE suspension_id), carrying
                                 applies_to_all_groups.
  institute_suspension_groups  — the selected groups of a selected-groups scope.

PURELY ADDITIVE. Both tables are created empty. No existing table, column,
constraint, index or row is read, altered, backfilled or deleted; in particular
student_suspensions and every school suspension are untouched.

BACKWARD COMPATIBLE BY ABSENCE. A suspension with NO scope row means "all
groups" — exactly how every existing institute suspension already behaves — so
nothing needs backfilling and no production behaviour changes on upgrade.

SAME-INSTITUTE BY CONSTRAINT. Group rows reference (scope id, school_id) and
(group id, school_id) through composite FKs, so PostgreSQL rejects any link
between a scope and another institute's group.

ON DELETE CASCADE from student_suspensions (and from scope to group rows), so
the existing delete route, the raw student delete and school cleanup remove the
scope without any code change. Group FK is RESTRICT, like enrollments.

Revision ID: g5s6c0p1e2a3
Revises: n0tb0x1a2b3c
Create Date: 2026-09-26
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'g5s6c0p1e2a3'
# n0tb0x1a2b3c is the head of the TRACKED migration graph. The untracked
# sync-foundation revision (s1y2n3c0f0a1) is a separate head and is neither
# referenced nor merged here. Apply with `flask db upgrade g5s6c0p1e2a3`, never
# `upgrade head`, while that second head exists in the working tree.
down_revision = 'n0tb0x1a2b3c'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'institute_suspension_scopes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('suspension_id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('applies_to_all_groups', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['suspension_id'], ['student_suspensions.id'],
                                name='fk_institute_susp_scope_suspension',
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                name='fk_institute_susp_scope_school'),
        sa.UniqueConstraint('suspension_id',
                            name='uq_institute_susp_scope_suspension'),
        sa.UniqueConstraint('id', 'school_id',
                            name='uq_institute_susp_scope_id_school'),
    )
    op.create_index('ix_institute_suspension_scopes_school_id',
                    'institute_suspension_scopes', ['school_id'])

    op.create_table(
        'institute_suspension_groups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('scope_id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(
            ['scope_id', 'school_id'],
            ['institute_suspension_scopes.id',
             'institute_suspension_scopes.school_id'],
            name='fk_institute_susp_group_scope_school', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['group_id', 'school_id'],
            ['institute_study_groups.id', 'institute_study_groups.school_id'],
            name='fk_institute_susp_group_group_school', ondelete='RESTRICT'),
        sa.UniqueConstraint('scope_id', 'group_id',
                            name='uq_institute_susp_group'),
    )
    op.create_index('ix_institute_suspension_groups_school_id',
                    'institute_suspension_groups', ['school_id'])
    op.create_index('ix_institute_suspension_groups_group_id',
                    'institute_suspension_groups', ['group_id'])


def downgrade():
    # Only the two tables this revision created; nothing else is touched.
    op.drop_index('ix_institute_suspension_groups_group_id',
                  table_name='institute_suspension_groups')
    op.drop_index('ix_institute_suspension_groups_school_id',
                  table_name='institute_suspension_groups')
    op.drop_table('institute_suspension_groups')
    op.drop_index('ix_institute_suspension_scopes_school_id',
                  table_name='institute_suspension_scopes')
    op.drop_table('institute_suspension_scopes')
