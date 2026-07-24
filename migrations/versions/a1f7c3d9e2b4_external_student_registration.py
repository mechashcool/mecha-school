"""External (public) student registration: school link columns + request tables

Adds the optional per-school external-registration feature:

* schools.external_registration_enabled  — feature toggle (default False)
* schools.registration_token_hash        — sha256(raw token) for public lookup
* schools.registration_token_encrypted   — Fernet-encrypted raw token (recovery)
* schools.registration_token_created_at

* student_registration_requests           — public intake applications
* student_registration_request_documents  — documents attached before a Student exists

All additions are new/nullable with server defaults, so existing schools and rows
are unaffected and the feature is OFF by default.

Revision ID: a1f7c3d9e2b4
Revises: y5z6a7b8c9d0
Create Date: 2026-07-23
"""

from alembic import op
import sqlalchemy as sa


revision = 'a1f7c3d9e2b4'
down_revision = 'y5z6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    # ── School: external-registration columns ─────────────────────────────────
    with op.batch_alter_table('schools') as batch:
        batch.add_column(sa.Column(
            'external_registration_enabled', sa.Boolean(),
            nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column(
            'registration_token_hash', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column(
            'registration_token_encrypted', sa.Text(), nullable=True))
        batch.add_column(sa.Column(
            'registration_token_created_at', sa.DateTime(), nullable=True))
        batch.create_unique_constraint(
            'uq_schools_registration_token_hash', ['registration_token_hash'])

    # ── student_registration_requests ─────────────────────────────────────────
    op.create_table(
        'student_registration_requests',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('school_id', sa.Integer(),
                  sa.ForeignKey('schools.id'), nullable=False, index=True),
        sa.Column('academic_year_id', sa.Integer(),
                  sa.ForeignKey('academic_years.id'), nullable=False, index=True),
        sa.Column('desired_grade_id', sa.Integer(),
                  sa.ForeignKey('grades.id'), nullable=False, index=True),

        sa.Column('full_name', sa.String(length=200), nullable=False),
        sa.Column('date_of_birth', sa.Date(), nullable=True),
        sa.Column('gender', sa.String(length=10), nullable=True),
        sa.Column('nationality', sa.String(length=80), nullable=True),
        sa.Column('address', sa.Text(), nullable=True),
        sa.Column('phone', sa.String(length=30), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('student_photo_path', sa.String(length=255), nullable=True),

        sa.Column('guardian_name', sa.String(length=200), nullable=True),
        sa.Column('guardian_phone', sa.String(length=30), nullable=True),
        sa.Column('guardian_email', sa.String(length=180), nullable=True),
        sa.Column('guardian_relation', sa.String(length=50), nullable=True),

        sa.Column('status', sa.String(length=20), nullable=False,
                  server_default='pending', index=True),
        sa.Column('tracking_token_hash', sa.String(length=64), nullable=False),
        sa.Column('submission_nonce', sa.String(length=64), nullable=True),
        sa.Column('rejection_reason', sa.Text(), nullable=True),
        sa.Column('internal_notes', sa.Text(), nullable=True),
        sa.Column('submission_ip', sa.String(length=64), nullable=True),

        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('reviewed_by', sa.Integer(),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('approved_student_id', sa.Integer(),
                  sa.ForeignKey('students.id'), nullable=True),
        sa.Column('linked_parent_id', sa.Integer(),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('parent_account_created', sa.Boolean(),
                  server_default=sa.false()),

        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),

        sa.CheckConstraint("status IN ('pending','approved','rejected')",
                           name='ck_reg_request_status'),
        sa.UniqueConstraint('tracking_token_hash',
                            name='uq_reg_request_tracking_token_hash'),
        sa.UniqueConstraint('school_id', 'submission_nonce',
                            name='uq_reg_request_school_nonce'),
    )
    op.create_index('ix_reg_request_school_status',
                    'student_registration_requests', ['school_id', 'status'])
    op.create_index('ix_student_registration_requests_tracking_token_hash',
                    'student_registration_requests', ['tracking_token_hash'])

    # ── student_registration_request_documents ────────────────────────────────
    op.create_table(
        'student_registration_request_documents',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('request_id', sa.Integer(),
                  sa.ForeignKey('student_registration_requests.id',
                                ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('school_id', sa.Integer(),
                  sa.ForeignKey('schools.id'), nullable=False, index=True),
        sa.Column('document_type', sa.String(length=100), nullable=False),
        sa.Column('file_path', sa.String(length=255), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table('student_registration_request_documents')
    op.drop_index('ix_student_registration_requests_tracking_token_hash',
                  table_name='student_registration_requests')
    op.drop_index('ix_reg_request_school_status',
                  table_name='student_registration_requests')
    op.drop_table('student_registration_requests')

    with op.batch_alter_table('schools') as batch:
        batch.drop_constraint('uq_schools_registration_token_hash', type_='unique')
        batch.drop_column('registration_token_created_at')
        batch.drop_column('registration_token_encrypted')
        batch.drop_column('registration_token_hash')
        batch.drop_column('external_registration_enabled')
