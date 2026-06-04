"""add school buildings feature

Adds the optional building-based data-access feature:
  * schools.enable_buildings           — per-school feature flag (default false)
  * school_buildings table             — branches/buildings inside one school
  * students.building_id (nullable FK)  — optional building assignment
  * user_building_access table         — restricts a user to specific buildings

All fields are nullable / default-safe so existing schools and existing rows
keep working exactly as before (feature OFF = current behaviour).

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4d5e6f7a8b9'
down_revision = 'b3c4d5e6f7a8'
branch_labels = None
depends_on = None


def upgrade():
    # 1) School feature flag — default false so existing schools are unchanged.
    with op.batch_alter_table('schools', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('enable_buildings', sa.Boolean(),
                      nullable=False, server_default=sa.false())
        )

    # 2) school_buildings — one row per building/branch inside a school.
    op.create_table(
        'school_buildings',
        sa.Column('id',          sa.Integer(), nullable=False),
        sa.Column('school_id',   sa.Integer(), nullable=False),
        sa.Column('name',        sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_active',   sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at',  sa.DateTime(), nullable=True),
        sa.Column('updated_at',  sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('school_id', 'name', name='uq_building_school_name'),
    )
    op.create_index('ix_school_buildings_school_id',
                    'school_buildings', ['school_id'], unique=False)

    # 3) students.building_id — optional building assignment (nullable).
    with op.batch_alter_table('students', schema=None) as batch_op:
        batch_op.add_column(sa.Column('building_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_students_building_id', ['building_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_students_building_id', 'school_buildings',
            ['building_id'], ['id'],
        )

    # 4) user_building_access — restricts a user to specific buildings.
    op.create_table(
        'user_building_access',
        sa.Column('id',          sa.Integer(), nullable=False),
        sa.Column('school_id',   sa.Integer(), nullable=False),
        sa.Column('user_id',     sa.Integer(), nullable=False),
        sa.Column('building_id', sa.Integer(), nullable=False),
        sa.Column('created_at',  sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['school_id'],   ['schools.id'],          ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'],     ['users.id'],            ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['building_id'], ['school_buildings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'building_id', name='uq_user_building'),
    )
    op.create_index('ix_user_building_access_school_id',
                    'user_building_access', ['school_id'], unique=False)
    op.create_index('ix_user_building_access_user_id',
                    'user_building_access', ['user_id'], unique=False)
    op.create_index('ix_user_building_access_building_id',
                    'user_building_access', ['building_id'], unique=False)


def downgrade():
    op.drop_index('ix_user_building_access_building_id', table_name='user_building_access')
    op.drop_index('ix_user_building_access_user_id', table_name='user_building_access')
    op.drop_index('ix_user_building_access_school_id', table_name='user_building_access')
    op.drop_table('user_building_access')

    with op.batch_alter_table('students', schema=None) as batch_op:
        batch_op.drop_constraint('fk_students_building_id', type_='foreignkey')
        batch_op.drop_index('ix_students_building_id')
        batch_op.drop_column('building_id')

    op.drop_index('ix_school_buildings_school_id', table_name='school_buildings')
    op.drop_table('school_buildings')

    with op.batch_alter_table('schools', schema=None) as batch_op:
        batch_op.drop_column('enable_buildings')
