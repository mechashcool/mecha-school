"""Split super-admin and school-admin roles

Revision ID: f7a8b9c0d1e2
Revises: e1f2a3b4c5d6
Create Date: 2026-05-04
"""

from alembic import op
import sqlalchemy as sa


revision = 'f7a8b9c0d1e2'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    conn.execute(sa.text("""
        INSERT INTO roles (name, label, description, is_admin, created_at)
        SELECT 'super_admin', 'Super Admin',
               'Full system owner access across all schools.',
               TRUE, NOW()
        WHERE NOT EXISTS (SELECT 1 FROM roles WHERE name = 'super_admin')
    """))
    conn.execute(sa.text("""
        INSERT INTO roles (name, label, description, is_admin, created_at)
        SELECT 'school_admin', 'School Manager',
               'School-level manager scoped to one school.',
               FALSE, NOW()
        WHERE NOT EXISTS (SELECT 1 FROM roles WHERE name = 'school_admin')
    """))

    conn.execute(sa.text("""
        UPDATE users
        SET role_id = (SELECT id FROM roles WHERE name = 'super_admin')
        WHERE role_id = (SELECT id FROM roles WHERE name = 'admin')
          AND school_id IS NULL
    """))
    conn.execute(sa.text("""
        UPDATE users
        SET role_id = (SELECT id FROM roles WHERE name = 'school_admin')
        WHERE role_id = (SELECT id FROM roles WHERE name = 'admin')
          AND school_id IS NOT NULL
    """))

    # Repair any partially migrated/bad data so role name and tenant shape agree.
    conn.execute(sa.text("""
        UPDATE users
        SET role_id = (SELECT id FROM roles WHERE name = 'school_admin')
        WHERE role_id = (SELECT id FROM roles WHERE name = 'super_admin')
          AND school_id IS NOT NULL
    """))
    conn.execute(sa.text("""
        UPDATE users
        SET role_id = (SELECT id FROM roles WHERE name = 'super_admin'),
            school_id = NULL
        WHERE username = 'admin'
    """))

    conn.execute(sa.text("""
        UPDATE roles
        SET is_admin = FALSE,
            description = COALESCE(description, 'Legacy compatibility role.')
        WHERE name = 'admin'
    """))
    conn.execute(sa.text("""
        UPDATE roles SET is_admin = TRUE WHERE name = 'super_admin'
    """))
    conn.execute(sa.text("""
        UPDATE roles SET is_admin = FALSE WHERE name = 'school_admin'
    """))


def downgrade():
    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE users
        SET role_id = (SELECT id FROM roles WHERE name = 'admin')
        WHERE role_id IN (
            SELECT id FROM roles WHERE name IN ('super_admin', 'school_admin')
        )
          AND EXISTS (SELECT 1 FROM roles WHERE name = 'admin')
    """))
    conn.execute(sa.text("""
        UPDATE roles SET is_admin = TRUE WHERE name = 'admin'
    """))
