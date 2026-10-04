"""Add investor school access mappings

Revision ID: o2p3q4r5s6t7
Revises: n1p2s3a4b5c6
Create Date: 2026-10-04
"""
from alembic import op
import sqlalchemy as sa


revision = 'o2p3q4r5s6t7'
down_revision = 'n1p2s3a4b5c6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'investor_school_access',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('investor_user_id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(['investor_user_id'], ['users.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('investor_user_id', 'school_id',
                            name='uq_investor_school_access_user_school'),
    )
    op.create_index('ix_investor_school_access_investor_user_id',
                    'investor_school_access', ['investor_user_id'])
    op.create_index('ix_investor_school_access_school_id',
                    'investor_school_access', ['school_id'])

    op.execute(sa.text("""
        INSERT INTO investor_school_access (investor_user_id, school_id)
        SELECT u.id, u.school_id
        FROM users AS u
        JOIN roles AS r ON r.id = u.role_id
        WHERE r.name = 'investor_viewer' AND u.school_id IS NOT NULL
    """))


def downgrade():
    op.drop_index('ix_investor_school_access_school_id',
                  table_name='investor_school_access')
    op.drop_index('ix_investor_school_access_investor_user_id',
                  table_name='investor_school_access')
    op.drop_table('investor_school_access')
