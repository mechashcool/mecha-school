"""add institute study-group target to exams

Institute-group-targeted exams. An institute organises teaching as
subject -> study group -> instructor and its students carry no section, so an
institute exam cannot use exams.section_id.

CHANGES

  1. exams.section_id           NOT NULL -> NULL
  2. exams.institute_group_id   new, nullable
  3. two indexes
  4. composite FK (institute_group_id, school_id)
         -> institute_study_groups (id, school_id)   ON DELETE RESTRICT
  5. CHECK ck_exam_single_target  (added NOT VALID, then validated)

NO DATA IS READ, MODIFIED, BACKFILLED OR DELETED. No existing row changes. No
existing exam or exam_results row is rewritten, retargeted or reinterpreted.

Dropping NOT NULL is a catalogue-only operation in PostgreSQL: it does not
rewrite the table and does not touch a single row. It is the one relaxation
this feature needs, and the CHECK below immediately re-tightens it so a SCHOOL
exam still cannot exist without a section.

ck_exam_single_target: exactly one target, never both and never neither.
Every pre-existing row satisfies it BY CONSTRUCTION — section_id was NOT NULL
until this revision, so every legacy row has a section, and institute_group_id
does not exist yet so it is NULL for all of them. The constraint is still added
NOT VALID and validated in a second statement: ADD CONSTRAINT ... NOT VALID
takes only a brief ACCESS EXCLUSIVE lock without scanning, and VALIDATE
CONSTRAINT then scans under SHARE UPDATE EXCLUSIVE, which does not block reads
or writes. That keeps the deployment safe on a large exams table.

ON DELETE RESTRICT is deliberate and is NOT a cascade: an exam, and therefore
every ExamResult hanging off it, must never be deleted or silently detached
because a study group was removed. A group carrying exams simply cannot be
deleted. app/utils/school_cleanup.py already removes ExamResult and Exam well
before InstituteStudyGroup, so a full-school teardown is unaffected.

Verified against the isolated local instance (PostgreSQL 18.3) before being
written here; production is PostgreSQL 17.6. Not applied to production.

Revision ID: e3x4m5g6r7p8
Revises: h7w8i9g0r1p2
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e3x4m5g6r7p8'
# h7w8i9g0r1p2 was verified to be COMMITTED (present in `git ls-files`) and the
# single head of the tracked migration graph. The repository also carries an
# UNRELATED and still untracked sync-foundation revision (s1y2n3c0f0a1) on its
# own branch; this migration does not merge, rebase, reference or depend on it.
# Apply with `flask db upgrade e3x4m5g6r7p8`, never `upgrade head`, while that
# second head exists in the working tree.
down_revision = 'h7w8i9g0r1p2'
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. Allow a section-less (institute) exam ─────────────────────────────
    # Catalogue-only: no table rewrite, no row touched.
    op.alter_column('exams', 'section_id', existing_type=sa.Integer(),
                    nullable=True)

    # ── 2. The institute target ──────────────────────────────────────────────
    op.add_column('exams',
                  sa.Column('institute_group_id', sa.Integer(), nullable=True))
    op.create_index('ix_exams_institute_group_id', 'exams',
                    ['institute_group_id'])
    op.create_index('ix_exam_school_year_group', 'exams',
                    ['school_id', 'academic_year_id', 'institute_group_id'])

    # ── 3. Same-school ownership of the group, enforced by PostgreSQL ────────
    op.execute(
        'ALTER TABLE exams '
        'ADD CONSTRAINT fk_exam_institute_group_school '
        'FOREIGN KEY (institute_group_id, school_id) '
        'REFERENCES institute_study_groups (id, school_id) '
        'ON DELETE RESTRICT'
    )

    # ── 4. Exactly one target ────────────────────────────────────────────────
    # NOT VALID first (no scan, brief lock), then validated (no read/write block).
    op.execute(
        'ALTER TABLE exams '
        'ADD CONSTRAINT ck_exam_single_target CHECK ('
        '(section_id IS NOT NULL AND institute_group_id IS NULL) OR '
        '(section_id IS NULL AND institute_group_id IS NOT NULL)'
        ') NOT VALID'
    )
    op.execute('ALTER TABLE exams VALIDATE CONSTRAINT ck_exam_single_target')


def downgrade():
    op.execute('ALTER TABLE exams DROP CONSTRAINT ck_exam_single_target')
    op.drop_constraint('fk_exam_institute_group_school', 'exams',
                       type_='foreignkey')
    op.drop_index('ix_exam_school_year_group', table_name='exams')
    op.drop_index('ix_exams_institute_group_id', table_name='exams')
    op.drop_column('exams', 'institute_group_id')
    # Restoring NOT NULL fails if any institute exam still exists. That is
    # intended: the downgrade must not silently discard an institute exam's
    # only target. Remove or retarget those rows deliberately before
    # downgrading.
    op.alter_column('exams', 'section_id', existing_type=sa.Integer(),
                    nullable=False)
