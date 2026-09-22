"""add institute study-group target to homework

Institute-group-targeted homework. An institute organises teaching as
subject -> study group -> instructor and its students carry no section, so an
institute assignment cannot use homework.section_id.

PURELY ADDITIVE:

  * one new nullable column  homework.institute_group_id
  * one composite foreign key  (institute_group_id, school_id)
        -> institute_study_groups (id, school_id)
  * one covering index

No existing row is read, modified, backfilled or deleted. No existing column
changes type or nullability. Every pre-existing homework row keeps
institute_group_id IS NULL and therefore keeps behaving exactly as before:
section_id remains its one and only target.

NO CHECK CONSTRAINT IS ADDED. A rule such as "exactly one of section_id /
institute_group_id is NOT NULL" would be violated by legitimate legacy rows:
homework.section_id is ON DELETE SET NULL against sections, so a historical row
whose section was deleted already has BOTH columns NULL and must stay readable.
The one-target rule is enforced in the homework routes instead, where a new row
is created.

ON DELETE SET NULL (institute_group_id) uses the PostgreSQL 15+ column-list
form. A bare SET NULL on a composite FK nulls EVERY column of the key,
including the NOT NULL homework.school_id, which would turn study-group
deletion into a constraint error. The column list is load-bearing, not
stylistic. It is the same form already used by
fk_institute_group_instructor_school in revision k1n2s3t4g5r6. Verified against
the isolated local instance (PostgreSQL 18.3); production is PostgreSQL 17.6.

Study groups are never hard-deleted through the interface — is_active is
toggled — so this path only ever runs during an explicit full-school teardown,
where the homework rows themselves are removed moments later.

Revision ID: h7w8i9g0r1p2
Revises: k1n2s3t4g5r6
Create Date: 2026-09-22
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'h7w8i9g0r1p2'
# Explicitly the committed head. The repository also carries an UNRELATED and
# currently untracked sync-foundation revision (s1y2n3c0f0a1) on its own branch;
# this migration deliberately does not merge, rebase or depend on it. Apply with
# `flask db upgrade h7w8i9g0r1p2`, never `upgrade head`, while that second head
# exists.
down_revision = 'k1n2s3t4g5r6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('homework',
                  sa.Column('institute_group_id', sa.Integer(), nullable=True))
    op.create_index('ix_homework_institute_group_id', 'homework',
                    ['institute_group_id'])
    op.create_index('ix_homework_school_year_group', 'homework',
                    ['school_id', 'academic_year_id', 'institute_group_id'])

    # Same-school ownership of the study group, rejected by PostgreSQL itself:
    # a homework row of school A can never point at a group of school B.
    # The parent key uq_institute_group_id_school was created in k1n2s3t4g5r6.
    op.execute(
        'ALTER TABLE homework '
        'ADD CONSTRAINT fk_homework_institute_group_school '
        'FOREIGN KEY (institute_group_id, school_id) '
        'REFERENCES institute_study_groups (id, school_id) '
        'ON DELETE SET NULL (institute_group_id)'
    )


def downgrade():
    op.drop_constraint('fk_homework_institute_group_school', 'homework',
                       type_='foreignkey')
    op.drop_index('ix_homework_school_year_group', table_name='homework')
    op.drop_index('ix_homework_institute_group_id', table_name='homework')
    op.drop_column('homework', 'institute_group_id')
