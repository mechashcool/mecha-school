"""force-remove any blanket unique on fee_records (student, fee_type, year)

Corrective / idempotent migration.

Symptom it fixes: after a fee is fully cancelled it disappears from the active
Fees list, yet re-adding the same (student, fee_type, academic_year) is still
rejected as an active duplicate. The application layer is correct — it only
blocks when a NON-cancelled matching fee exists — so the rejection can only come
from a stale DATABASE-level BLANKET uniqueness (a UNIQUE constraint or a
non-partial UNIQUE index over the three columns) that predates, or survived, the
partial-index migration ``v2w3x4y5z6a7`` (e.g. it was stamped without actually
dropping the old constraint, or the table was created from an older model).

This migration deterministically removes ANY blanket uniqueness over exactly
(student_id, fee_type_id, academic_year_id) — whatever its name — while
preserving the intended PARTIAL unique index
``uq_fee_record_active_student_type_year`` (``WHERE cancelled_at IS NULL``),
which it (re)creates if missing. It never deletes, cancels, or modifies any fee
row; cancelled historical duplicates remain intact and allowed. If genuine
ACTIVE duplicates already exist it ABORTS with a diagnostic rather than creating
a non-unique index.

Revision ID: x4y5z6a7b8c9
Revises: w3x4y5z6a7b8
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'x4y5z6a7b8c9'
down_revision = 'w3x4y5z6a7b8'
branch_labels = None
depends_on = None


_ACTIVE_IDX = 'uq_fee_record_active_student_type_year'
_TRIPLE = {'student_id', 'fee_type_id', 'academic_year_id'}
_PARTIAL_WHERE = sa.text('cancelled_at IS NULL')


def _triple(cols):
    return set(cols or []) == _TRIPLE


def _active_duplicate_conflicts():
    """(student_id, fee_type_id, academic_year_id, n) groups with >1 ACTIVE fee.
    Empty when clean. Read-only; cancelled rows are excluded and never reported."""
    return [tuple(r) for r in op.get_bind().execute(sa.text("""
        SELECT student_id, fee_type_id, academic_year_id, COUNT(*) AS n
        FROM fee_records
        WHERE cancelled_at IS NULL
        GROUP BY student_id, fee_type_id, academic_year_id
        HAVING COUNT(*) > 1
        ORDER BY student_id, fee_type_id, academic_year_id
    """)).fetchall()]


def upgrade():
    # 1) Drop every UNIQUE CONSTRAINT over the exact triple (constraints are never
    #    partial, so any such constraint blocks a replacement fee).
    insp = sa.inspect(op.get_bind())
    for uc in insp.get_unique_constraints('fee_records'):
        if _triple(uc.get('column_names')):
            op.drop_constraint(uc['name'], 'fee_records', type_='unique')

    # 2) Drop every NON-PARTIAL UNIQUE INDEX over the exact triple. The intended
    #    partial index is preserved — recognised by its canonical name OR by a
    #    partial predicate — so it is never dropped here.
    insp = sa.inspect(op.get_bind())
    for ix in insp.get_indexes('fee_records'):
        if not ix.get('unique') or not _triple(ix.get('column_names')):
            continue
        has_partial = bool((ix.get('dialect_options') or {}).get('postgresql_where'))
        if ix['name'] == _ACTIVE_IDX or has_partial:
            continue
        op.drop_index(ix['name'], table_name='fee_records')

    # 3) Ensure the partial UNIQUE index exists (ALWAYS unique — never a
    #    non-unique fallback). Abort with a diagnostic if active duplicates exist.
    insp = sa.inspect(op.get_bind())
    if _ACTIVE_IDX not in {ix['name'] for ix in insp.get_indexes('fee_records')}:
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
                'have more than one ACTIVE (cancelled_at IS NULL) fee record. No '
                'row is deleted, cancelled, or merged — resolve each conflict so '
                'only one active fee remains and re-run.\nConflicting groups:\n'
                + details
            )
        op.create_index(
            _ACTIVE_IDX,
            'fee_records',
            ['student_id', 'fee_type_id', 'academic_year_id'],
            unique=True,
            postgresql_where=_PARTIAL_WHERE,
        )


def downgrade():
    # Intentional no-op: this is a corrective migration whose only effect is to
    # guarantee the correct partial-unique state established by v2w3x4y5z6a7.
    # Re-introducing a blanket unique constraint here would re-break replacement
    # fees after cancellation, so nothing is reversed.
    pass
