"""Add homework table

Revision ID: x8y9z0a1b2c3
Revises: w7x8y9z0a1b2
Create Date: 2026-05-29

"""
from alembic import op
import sqlalchemy as sa

revision = 'x8y9z0a1b2c3'
down_revision = 'w7x8y9z0a1b2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'homework',
        sa.Column('id',               sa.Integer(),     nullable=False),
        sa.Column('school_id',        sa.Integer(),     nullable=False),
        sa.Column('academic_year_id', sa.Integer(),     nullable=False),
        sa.Column('teacher_id',       sa.Integer(),     nullable=True),
        sa.Column('subject_id',       sa.Integer(),     nullable=True),
        sa.Column('section_id',       sa.Integer(),     nullable=True),
        sa.Column('title',            sa.String(300),   nullable=False),
        sa.Column('description',      sa.Text(),        nullable=True),
        sa.Column('publish_date',     sa.Date(),        nullable=False),
        sa.Column('due_date',         sa.Date(),        nullable=False),
        sa.Column('attachment_path',  sa.String(500),   nullable=True),
        sa.Column('attachment_type',  sa.String(20),    nullable=True),
        sa.Column('is_active',        sa.Boolean(),     nullable=False, server_default='1'),
        sa.Column('created_at',       sa.DateTime(),    nullable=True),
        sa.Column('updated_at',       sa.DateTime(),    nullable=True),

        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['school_id'],        ['schools.id'],       ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['teacher_id'],       ['employees.id'],     ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['subject_id'],       ['subjects.id'],      ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['section_id'],       ['sections.id'],      ondelete='SET NULL'),
    )

    op.create_index('ix_homework_school_id',        'homework', ['school_id'])
    op.create_index('ix_homework_academic_year_id', 'homework', ['academic_year_id'])
    op.create_index('ix_homework_teacher_id',       'homework', ['teacher_id'])
    op.create_index('ix_homework_subject_id',       'homework', ['subject_id'])
    op.create_index('ix_homework_section_id',       'homework', ['section_id'])


def downgrade():
    op.drop_index('ix_homework_section_id',       table_name='homework')
    op.drop_index('ix_homework_subject_id',       table_name='homework')
    op.drop_index('ix_homework_teacher_id',       table_name='homework')
    op.drop_index('ix_homework_academic_year_id', table_name='homework')
    op.drop_index('ix_homework_school_id',        table_name='homework')
    op.drop_table('homework')
