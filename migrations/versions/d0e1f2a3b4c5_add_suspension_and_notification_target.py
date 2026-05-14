"""Add StudentSuspension table and Notification.target_user_id.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-05-02
"""
from alembic import op
import sqlalchemy as sa


revision = 'd0e1f2a3b4c5'
down_revision = 'c9d0e1f2a3b4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'notifications',
        sa.Column('target_user_id', sa.Integer(),
                  sa.ForeignKey('users.id'), nullable=True),
    )
    op.create_index('ix_notifications_target_user_id',
                    'notifications', ['target_user_id'])

    op.create_table(
        'student_suspensions',
        sa.Column('id',               sa.Integer(), primary_key=True),
        sa.Column('student_id',       sa.Integer(),
                  sa.ForeignKey('students.id', ondelete='CASCADE'), nullable=False),
        sa.Column('school_id',        sa.Integer(),
                  sa.ForeignKey('schools.id'), nullable=False),
        sa.Column('academic_year_id', sa.Integer(),
                  sa.ForeignKey('academic_years.id'), nullable=False),
        sa.Column('start_date',  sa.Date(), nullable=False),
        sa.Column('end_date',    sa.Date(), nullable=False),
        sa.Column('reason',      sa.Text(), nullable=True),
        sa.Column('created_by',  sa.Integer(),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at',  sa.DateTime(), nullable=True),
    )
    op.create_index('ix_student_suspensions_school_id',
                    'student_suspensions', ['school_id'])
    op.create_index('ix_student_suspensions_academic_year_id',
                    'student_suspensions', ['academic_year_id'])


def downgrade():
    op.drop_index('ix_student_suspensions_academic_year_id',
                  table_name='student_suspensions')
    op.drop_index('ix_student_suspensions_school_id',
                  table_name='student_suspensions')
    op.drop_table('student_suspensions')
    op.drop_index('ix_notifications_target_user_id',
                  table_name='notifications')
    op.drop_column('notifications', 'target_user_id')
