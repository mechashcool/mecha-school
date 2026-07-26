"""Add role_schools: per-school availability for custom roles

Introduces the role_schools association table so the Super Admin can choose,
per school, which custom (non built-in) roles that school may assign to its
users. Built-in system roles are never stored here and remain available to
every school unchanged.

Grandfathering: every existing custom role is linked to every existing school
so current users are not disrupted. The Super Admin can then add/remove roles
per school from the school-details page. Built-in roles are excluded from the
backfill by name.

Revision ID: c3d9f5b2a7e1
Revises: b2c8e4a1d3f6
Create Date: 2026-07-26
"""

from alembic import op
import sqlalchemy as sa


revision = 'c3d9f5b2a7e1'
down_revision = 'b2c8e4a1d3f6'
branch_labels = None
depends_on = None


# Kept as a literal so the migration is independent of application code and
# does not drift if the catalog changes later.
BUILTIN_ROLE_NAMES = (
    'super_admin', 'school_admin', 'admin', 'accountant', 'teacher',
    'hr', 'reception', 'parent', 'investor_viewer',
)


def upgrade():
    op.create_table(
        'role_schools',
        sa.Column('role_id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('role_id', 'school_id'),
    )

    # Grandfather existing custom roles → all existing schools (cartesian join,
    # restricted to non built-in roles). Built-in roles are handled in code and
    # never appear in this table.
    builtin = ', '.join(f"'{n}'" for n in BUILTIN_ROLE_NAMES)
    op.execute(
        f"""
        INSERT INTO role_schools (role_id, school_id)
        SELECT r.id, s.id
        FROM roles r
        CROSS JOIN schools s
        WHERE r.name NOT IN ({builtin})
        """
    )


def downgrade():
    op.drop_table('role_schools')
