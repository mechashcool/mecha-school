"""add residential areas

Adds the per-school residential areas (مناطق السكن) feature:
  * residential_areas table               — school-scoped lookup list of areas
  * students.residential_area_id (nullable FK) — optional area link per student

Purely additive and default-safe:
  * existing students keep residential_area_id = NULL and remain fully valid;
  * the existing free-text students.address column is NOT touched, migrated,
    or rewritten in any way;
  * no existing rows are modified.

Revision ID: t8u9v0w1x2y3
Revises: s7t8u9v0w1x2
Create Date: 2026-07-19
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 't8u9v0w1x2y3'
down_revision = 's7t8u9v0w1x2'
branch_labels = None
depends_on = None


def upgrade():
    # 1) residential_areas — one row per area, always owned by exactly one school.
    op.create_table(
        'residential_areas',
        sa.Column('id',         sa.Integer(), nullable=False),
        sa.Column('school_id',  sa.Integer(), nullable=False),
        sa.Column('name',       sa.String(length=200), nullable=False),
        sa.Column('is_active',  sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('school_id', 'name', name='uq_residential_area_school_name'),
    )
    op.create_index('ix_residential_areas_school_id',
                    'residential_areas', ['school_id'], unique=False)

    # 2) students.residential_area_id — optional area link (nullable).
    with op.batch_alter_table('students', schema=None) as batch_op:
        batch_op.add_column(sa.Column('residential_area_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_students_residential_area_id',
                              ['residential_area_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_students_residential_area_id', 'residential_areas',
            ['residential_area_id'], ['id'],
        )


def downgrade():
    with op.batch_alter_table('students', schema=None) as batch_op:
        batch_op.drop_constraint('fk_students_residential_area_id', type_='foreignkey')
        batch_op.drop_index('ix_students_residential_area_id')
        batch_op.drop_column('residential_area_id')

    op.drop_index('ix_residential_areas_school_id', table_name='residential_areas')
    op.drop_table('residential_areas')
