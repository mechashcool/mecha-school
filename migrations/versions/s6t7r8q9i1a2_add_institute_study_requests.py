"""add institute study requests ("أرسل درسي") — tables only

Two new tables for the institute parent → administration lesson-image request:

  institute_study_requests        — one row per request (note, status
                                    pending/viewed, admin_note, image_count).
  institute_study_request_images  — one METADATA row per stored image
                                    (file_path, mime, size_bytes, sort_order).
                                    The image bytes live in Supabase Storage
                                    (bucket `uploads`), never in PostgreSQL.

PURELY ADDITIVE. Both tables are created empty. No existing table, column,
constraint, index or row is read, altered, backfilled or deleted. The student
composite FK reuses the existing UNIQUE students(id, school_id)
(uq_student_id_school, revision k1n2s3t4g5r6).

SAME-SCHOOL BY CONSTRAINT. The request references (student id, school_id) and
each image references (request id, school_id) through composite FKs, so
PostgreSQL rejects a mixed-school row. Both are RESTRICT: nothing cascades, so
image rows (and their file_path values) are always removed explicitly.

Indexes are limited to the two list paths, the image detail path and the
file-access owner lookup (UNIQUE file_path).

Revision ID: s6t7r8q9i1a2
Revises: r5s6t7u8v9w0
Create Date: 2026-10-16
"""
from alembic import op
import sqlalchemy as sa


revision = 's6t7r8q9i1a2'
down_revision = 'r5s6t7u8v9w0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'institute_study_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('academic_year_id', sa.Integer(), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('parent_id', sa.Integer(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False,
                  server_default='pending'),
        sa.Column('admin_note', sa.Text(), nullable=True),
        sa.Column('viewed_by', sa.Integer(), nullable=True),
        sa.Column('viewed_at', sa.DateTime(), nullable=True),
        sa.Column('image_count', sa.SmallInteger(), nullable=False,
                  server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                name='fk_inst_study_request_school'),
        sa.ForeignKeyConstraint(['academic_year_id'], ['academic_years.id'],
                                name='fk_inst_study_request_academic_year'),
        sa.ForeignKeyConstraint(['parent_id'], ['users.id'],
                                name='fk_inst_study_request_parent'),
        sa.ForeignKeyConstraint(['viewed_by'], ['users.id'],
                                name='fk_inst_study_request_viewed_by'),
        sa.ForeignKeyConstraint(
            ['student_id', 'school_id'], ['students.id', 'students.school_id'],
            name='fk_inst_study_request_student_school', ondelete='RESTRICT'),
        sa.CheckConstraint("status IN ('pending', 'viewed')",
                           name='ck_inst_study_request_status'),
        sa.CheckConstraint('image_count >= 0',
                           name='ck_inst_study_request_image_count'),
        sa.UniqueConstraint('id', 'school_id',
                            name='uq_inst_study_request_id_school'),
    )
    op.create_index('ix_institute_study_requests_academic_year_id',
                    'institute_study_requests', ['academic_year_id'])
    op.create_index('ix_inst_study_request_school_status_created',
                    'institute_study_requests',
                    ['school_id', 'status', 'created_at'])
    op.create_index('ix_inst_study_request_student_created',
                    'institute_study_requests', ['student_id', 'created_at'])

    op.create_table(
        'institute_study_request_images',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('request_id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('file_path', sa.String(length=500), nullable=False),
        sa.Column('mime', sa.String(length=50), nullable=True),
        sa.Column('size_bytes', sa.Integer(), nullable=True),
        sa.Column('sort_order', sa.SmallInteger(), nullable=False,
                  server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                name='fk_inst_study_request_image_school'),
        sa.ForeignKeyConstraint(
            ['request_id', 'school_id'],
            ['institute_study_requests.id', 'institute_study_requests.school_id'],
            name='fk_inst_study_request_image_request_school', ondelete='RESTRICT'),
        sa.UniqueConstraint('file_path',
                            name='uq_inst_study_request_image_file_path'),
    )
    op.create_index('ix_inst_study_request_image_request_sort',
                    'institute_study_request_images', ['request_id', 'sort_order'])


def downgrade():
    # Only the two tables this revision created; nothing else is touched.
    op.drop_index('ix_inst_study_request_image_request_sort',
                  table_name='institute_study_request_images')
    op.drop_table('institute_study_request_images')
    op.drop_index('ix_inst_study_request_student_created',
                  table_name='institute_study_requests')
    op.drop_index('ix_inst_study_request_school_status_created',
                  table_name='institute_study_requests')
    op.drop_index('ix_institute_study_requests_academic_year_id',
                  table_name='institute_study_requests')
    op.drop_table('institute_study_requests')
