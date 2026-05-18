"""add transport routes and student_transport tables

Revision ID: j4k5l6m7n8o9
Revises: i3j4k5l6m7n8
Create Date: 2026-05-18 00:00:00.000000

Creates:
  - transport_routes — one row per school bus/van route
  - student_transport — links students to routes with subscription status
  - manage_transport permission
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column
from datetime import datetime


revision = 'j4k5l6m7n8o9'
down_revision = 'i3j4k5l6m7n8'
branch_labels = None
depends_on = None


def upgrade():
    # ── transport_routes ──────────────────────────────────────────────────────
    op.create_table(
        'transport_routes',
        sa.Column('id',             sa.Integer(),     nullable=False),
        sa.Column('school_id',      sa.Integer(),     nullable=False),
        sa.Column('name',           sa.String(150),   nullable=False),
        sa.Column('route_number',   sa.String(30),    nullable=True),
        sa.Column('driver_name',    sa.String(200),   nullable=False),
        sa.Column('driver_phone',   sa.String(30),    nullable=False),
        sa.Column('supervisor',     sa.String(200),   nullable=True),
        sa.Column('vehicle_type',   sa.String(80),    nullable=False),
        sa.Column('vehicle_number', sa.String(30),    nullable=False),
        sa.Column('capacity',       sa.Integer(),     nullable=False, server_default='1'),
        sa.Column('status',         sa.String(20),    nullable=False, server_default='active'),
        sa.Column('created_at',     sa.DateTime(),    nullable=True),
        sa.Column('updated_at',     sa.DateTime(),    nullable=True),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('school_id', 'name', name='uq_transport_route_school_name'),
    )
    op.create_index('ix_transport_routes_school_id', 'transport_routes', ['school_id'])

    # ── student_transport ─────────────────────────────────────────────────────
    op.create_table(
        'student_transport',
        sa.Column('id',         sa.Integer(),   nullable=False),
        sa.Column('school_id',  sa.Integer(),   nullable=False),
        sa.Column('route_id',   sa.Integer(),   nullable=False),
        sa.Column('student_id', sa.Integer(),   nullable=False),
        sa.Column('status',     sa.String(20),  nullable=False, server_default='active'),
        sa.Column('start_date', sa.Date(),      nullable=True),
        sa.Column('notes',      sa.Text(),      nullable=True),
        sa.Column('created_at', sa.DateTime(),  nullable=True),
        sa.ForeignKeyConstraint(['school_id'],  ['schools.id'],          ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['route_id'],   ['transport_routes.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['student_id'], ['students.id'],         ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('route_id', 'student_id', name='uq_student_transport_route'),
    )
    op.create_index('ix_student_transport_school_id', 'student_transport', ['school_id'])
    op.create_index('ix_student_transport_route_id',  'student_transport', ['route_id'])
    op.create_index('ix_student_transport_student_id','student_transport', ['student_id'])

    # ── manage_transport permission ───────────────────────────────────────────
    permissions_t = table(
        'permissions',
        column('name',       sa.String),
        column('label',      sa.String),
        column('category',   sa.String),
        column('created_at', sa.DateTime),
    )
    conn = op.get_bind()
    exists = conn.execute(
        sa.text("SELECT 1 FROM permissions WHERE name = 'manage_transport' LIMIT 1")
    ).fetchone()
    if not exists:
        op.bulk_insert(permissions_t, [{
            'name':       'manage_transport',
            'label':      'إدارة خطوط النقل',
            'category':   'النقل',
            'created_at': datetime.utcnow(),
        }])


def downgrade():
    op.execute(sa.text("DELETE FROM permissions WHERE name = 'manage_transport'"))
    op.drop_index('ix_student_transport_student_id', table_name='student_transport')
    op.drop_index('ix_student_transport_route_id',   table_name='student_transport')
    op.drop_index('ix_student_transport_school_id',  table_name='student_transport')
    op.drop_table('student_transport')
    op.drop_index('ix_transport_routes_school_id', table_name='transport_routes')
    op.drop_table('transport_routes')
