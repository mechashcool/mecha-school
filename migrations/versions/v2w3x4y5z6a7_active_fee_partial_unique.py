"""allow re-adding a fee after cancellation (partial-unique active-fee index)

Requirement: a cancelled fee is preserved as history but must NOT block creating
a new active fee for the same (student, fee_type, academic_year). The original
``uq_fee_record_student_type_year`` UNIQUE constraint forbade *any* second row
for that triple — cancelled or not — so it is replaced by a PARTIAL unique index
that constrains only ACTIVE rows (``cancelled_at IS NULL``). Cancelled rows are
excluded from the index, so unlimited cancelled history may coexist with exactly
one active fee.

Safe / additive to data: no fee rows are modified, deleted, cancelled, or merged.
On installs whose original constraint had already degraded to the non-unique
``ix_..._nonunique`` fallback (pre-existing duplicate operational data), we drop
that too.

The index is ALWAYS created UNIQUE — there is no non-unique fallback. The
database must always enforce "at most one ACTIVE fee per (student, fee_type,
academic_year)". If unexpected ACTIVE duplicates already exist the migration
ABORTS before changing anything, listing the exact conflicting
(student_id, fee_type_id, academic_year_id) combinations so an operator can
resolve them (cancel or remove the extra active fee) and re-run. Cancelled
historical duplicates are excluded from the index and remain allowed.

Revision ID: v2w3x4y5z6a7
Revises: u1v2w3x4y5z6
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'v2w3x4y5z6a7'
down_revision = 'u1v2w3x4y5z6'
branch_labels = None
depends_on = None


_ACTIVE_IDX = 'uq_fee_record_active_student_type_year'
_OLD_UQ = 'uq_fee_record_student_type_year'
_OLD_IX_FALLBACK = 'ix_fee_record_student_type_year_nonunique'
_PARTIAL_WHERE = sa.text('cancelled_at IS NULL')


def _insp():
    return sa.inspect(op.get_bind())


def _uq_names(table):
    try:
        return {uq['name'] for uq in _insp().get_unique_constraints(table)}
    except Exception:
        return set()


def _index_names(table):
    try:
        return {ix['name'] for ix in _insp().get_indexes(table)}
    except Exception:
        return set()


def _active_duplicate_conflicts():
    """Return every (student_id, fee_type_id, academic_year_id, n) group that
    already has more than one ACTIVE (``cancelled_at IS NULL``) fee record.

    Empty list means the data is clean and the UNIQUE index can be created.
    Cancelled rows are excluded, so historical duplicates are never reported and
    remain allowed. Read-only — this inspects data and modifies nothing."""
    rows = op.get_bind().execute(sa.text("""
        SELECT student_id, fee_type_id, academic_year_id, COUNT(*) AS n
        FROM fee_records
        WHERE cancelled_at IS NULL
        GROUP BY student_id, fee_type_id, academic_year_id
        HAVING COUNT(*) > 1
        ORDER BY student_id, fee_type_id, academic_year_id
    """)).fetchall()
    return [tuple(r) for r in rows]


def upgrade():
    # 0) Pre-flight — refuse to proceed if ACTIVE duplicates already exist.
    #    This runs BEFORE dropping the old constraint, so an abort leaves the
    #    schema completely untouched regardless of transactional-DDL behaviour.
    #    A non-unique fallback is NEVER created: the DB must always enforce one
    #    active fee per (student, fee_type, academic_year). No rows are deleted,
    #    cancelled, or merged — the operator resolves the conflicts and re-runs.
    conflicts = _active_duplicate_conflicts()
    if conflicts:
        details = '\n'.join(
            f'  - student_id={s}, fee_type_id={ft}, academic_year_id={ay} '
            f'({n} active rows)'
            for (s, ft, ay, n) in conflicts
        )
        raise RuntimeError(
            f'Aborting migration {revision}: cannot create the partial UNIQUE '
            f'index {_ACTIVE_IDX!r} because {len(conflicts)} '
            '(student_id, fee_type_id, academic_year_id) combination(s) already '
            'have more than one ACTIVE (cancelled_at IS NULL) fee record. '
            'This migration refuses to create a non-unique index and does NOT '
            'delete, cancel, or merge any rows. Resolve each conflict (cancel or '
            'remove the extra active fee so only one remains) and re-run the '
            'migration. Cancelled historical duplicates are allowed and are not '
            'listed here.\nConflicting groups:\n' + details
        )

    # 1) Drop the old blanket uniqueness (constraint OR degraded fallback index).
    if _OLD_UQ in _uq_names('fee_records'):
        op.drop_constraint(_OLD_UQ, 'fee_records', type_='unique')
    # Some databases expose a unique CONSTRAINT as an index of the same name.
    if _OLD_UQ in _index_names('fee_records'):
        try:
            op.drop_index(_OLD_UQ, table_name='fee_records')
        except Exception:
            pass
    if _OLD_IX_FALLBACK in _index_names('fee_records'):
        op.drop_index(_OLD_IX_FALLBACK, table_name='fee_records')

    # 2) Create the partial index scoped to active (non-cancelled) fees.
    #    ALWAYS unique — the pre-flight above guarantees the data is clean.
    if _ACTIVE_IDX not in _index_names('fee_records'):
        op.create_index(
            _ACTIVE_IDX,
            'fee_records',
            ['student_id', 'fee_type_id', 'academic_year_id'],
            unique=True,
            postgresql_where=_PARTIAL_WHERE,
        )


def downgrade():
    # Drop the partial index and restore the original blanket unique constraint
    # (best-effort — only when no duplicate triples exist across ALL rows).
    if _ACTIVE_IDX in _index_names('fee_records'):
        op.drop_index(_ACTIVE_IDX, table_name='fee_records')

    dup = op.get_bind().execute(sa.text("""
        SELECT 1
        FROM (
            SELECT student_id, fee_type_id, academic_year_id, COUNT(*) AS n
            FROM fee_records
            GROUP BY student_id, fee_type_id, academic_year_id
            HAVING COUNT(*) > 1
        ) d
        LIMIT 1
    """)).fetchone()
    if dup is None and _OLD_UQ not in _uq_names('fee_records'):
        op.create_unique_constraint(
            _OLD_UQ, 'fee_records',
            ['student_id', 'fee_type_id', 'academic_year_id'],
        )
