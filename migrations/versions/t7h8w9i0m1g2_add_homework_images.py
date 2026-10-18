"""add homework_images (image #2 / #3 of a school homework) — table only

New school homework may carry up to 3 images OR one PDF. Image #1 (or the PDF)
stays exactly where it always was, on homework.attachment_path /
homework.attachment_type, so every existing reader and older mobile build keeps
working unchanged. Only image #2 and #3 go into the new table:

  homework_images  — one METADATA row per extra stored image (file_path,
                     sort_order 1 = image #2, 2 = image #3). The bytes live in
                     the existing Storage, never in PostgreSQL.

PURELY ADDITIVE, NO BACKFILL. The new table is created empty. No existing
homework row, attachment_path or attachment_type value is read, copied,
rewritten or deleted; no stored file is touched.

SAME-SCHOOL BY CONSTRAINT. Each image references (homework id, school_id)
through a composite FK, so PostgreSQL rejects an image of school A pointing at
a homework of school B. PostgreSQL needs a UNIQUE key on the referenced
columns, so the only change to the existing homework table is
uq_homework_id_school on (id, school_id) — always satisfiable because id is
already the primary key. ON DELETE CASCADE: deleting a homework (web delete,
academic-year or school teardown) removes its image rows with it.

file_path is NOT unique on purpose: «جميع الشعب» stores each image once and
every generated homework row references the same object.
UNIQUE (homework_id, sort_order) + CHECK (sort_order IN (1, 2)) cap a homework
at 3 images in the database as well as in the routes.

Revision ID: t7h8w9i0m1g2
Revises: s6t7r8q9i1a2
Create Date: 2026-10-18
"""
from alembic import op
import sqlalchemy as sa


revision = 't7h8w9i0m1g2'
down_revision = 's6t7r8q9i1a2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint('uq_homework_id_school', 'homework',
                                ['id', 'school_id'])

    op.create_table(
        'homework_images',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('homework_id', sa.Integer(), nullable=False),
        sa.Column('school_id', sa.Integer(), nullable=False),
        sa.Column('file_path', sa.String(length=500), nullable=False),
        sa.Column('sort_order', sa.SmallInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(
            ['homework_id', 'school_id'], ['homework.id', 'homework.school_id'],
            name='fk_homework_image_homework_school', ondelete='CASCADE'),
        sa.UniqueConstraint('homework_id', 'sort_order',
                            name='uq_homework_image_homework_sort'),
        sa.CheckConstraint('sort_order IN (1, 2)',
                           name='ck_homework_image_sort_order'),
    )
    op.create_index('ix_homework_images_file_path', 'homework_images',
                    ['file_path'])


def downgrade():
    # Only what this revision created; homework rows and their attachment
    # columns are never touched. Extra images (#2/#3) recorded after the
    # upgrade are dropped with the table; image #1 stays on homework.
    op.drop_index('ix_homework_images_file_path', table_name='homework_images')
    op.drop_table('homework_images')
    op.drop_constraint('uq_homework_id_school', 'homework', type_='unique')
