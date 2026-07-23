"""Make the fee-duplicate trigger cancellation-aware (the real blocker).

Root cause of "cannot re-add a fee after cancellation"
------------------------------------------------------
The rejection was NOT a stale blanket UNIQUE constraint/index (none exists on
``fee_records``) and NOT the intended partial index
``uq_fee_record_active_student_type_year`` (``WHERE cancelled_at IS NULL``),
which correctly excludes cancelled rows.

It was the BEFORE INSERT/UPDATE trigger ``trg_prevent_fee_record_duplicate``
(function ``prevent_fee_record_duplicate()``, added in ``c9d0e1f2a3b4`` and only
search-path-hardened, never re-logicked, in ``q6r7s8t9u0v1``). Its EXISTS check
matched on (student_id, fee_type_id, academic_year_id) ALONE — it never looked at
``cancelled_at`` — and raised ``RAISE EXCEPTION ... USING ERRCODE = '23505'``.
psycopg2/SQLAlchemy map SQLSTATE 23505 to ``IntegrityError``, so inserting a
replacement fee while a CANCELLED matching fee still existed as history was
rejected exactly like a unique violation.

A trigger is invisible to ``get_unique_constraints`` / ``get_indexes`` inspection,
which is why direct schema inspection found no offending constraint yet the INSERT
still failed — the previous "stale blanket unique" diagnosis inspected the wrong
kind of object.

Fix
---
Redefine the function so its check mirrors the partial index exactly: only an
ACTIVE (``cancelled_at IS NULL``) existing row conflicts, and only when the NEW
row is itself ACTIVE. Cancelled rows neither block nor are blocked, so a
replacement fee can be created after cancellation while a genuinely active
duplicate is still rejected. The trigger is kept (defence-in-depth alongside the
partial index) with its clearer message.

The ``SET search_path = public, pg_temp`` hardening from ``q6r7s8t9u0v1`` is
re-applied inline, because ``CREATE OR REPLACE FUNCTION`` without a SET clause
would drop the previously-configured proconfig and re-open the Supabase
"Function Search Path Mutable" advisory.

No fee row is created, modified, cancelled, or deleted. Only the trigger function
body changes.

Revision ID: y5z6a7b8c9d0
Revises: x4y5z6a7b8c9
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa


revision = 'y5z6a7b8c9d0'
down_revision = 'x4y5z6a7b8c9'
branch_labels = None
depends_on = None


# Cancellation-aware guard: mirrors uq_fee_record_active_student_type_year.
_FN_CANCEL_AWARE = """
CREATE OR REPLACE FUNCTION prevent_fee_record_duplicate()
RETURNS trigger AS $$
DECLARE
    should_check boolean := false;
BEGIN
    -- A cancelled row is preserved history and never conflicts with anything.
    IF NEW.cancelled_at IS NOT NULL THEN
        RETURN NEW;
    END IF;

    IF TG_OP = 'INSERT' THEN
        should_check := true;
    ELSIF NEW.student_id IS DISTINCT FROM OLD.student_id
       OR NEW.fee_type_id IS DISTINCT FROM OLD.fee_type_id
       OR NEW.academic_year_id IS DISTINCT FROM OLD.academic_year_id
       OR (OLD.cancelled_at IS NOT NULL AND NEW.cancelled_at IS NULL) THEN
        should_check := true;
    END IF;

    IF should_check AND EXISTS (
        SELECT 1
        FROM fee_records fr
        WHERE fr.student_id = NEW.student_id
          AND fr.fee_type_id = NEW.fee_type_id
          AND fr.academic_year_id = NEW.academic_year_id
          AND fr.cancelled_at IS NULL
          AND fr.id <> COALESCE(NEW.id, -1)
    ) THEN
        RAISE EXCEPTION
            'duplicate active fee record for student %, fee type %, academic year %',
            NEW.student_id, NEW.fee_type_id, NEW.academic_year_id
            USING ERRCODE = '23505';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
SET search_path = public, pg_temp;
"""


# Original blanket guard from c9d0e1f2a3b4 (ignores cancelled_at), re-applying the
# q6r7s8t9u0v1 search_path hardening so downgrade restores the last-known state.
_FN_BLANKET = """
CREATE OR REPLACE FUNCTION prevent_fee_record_duplicate()
RETURNS trigger AS $$
DECLARE
    should_check boolean := false;
BEGIN
    IF TG_OP = 'INSERT' THEN
        should_check := true;
    ELSIF NEW.student_id IS DISTINCT FROM OLD.student_id
       OR NEW.fee_type_id IS DISTINCT FROM OLD.fee_type_id
       OR NEW.academic_year_id IS DISTINCT FROM OLD.academic_year_id THEN
        should_check := true;
    END IF;

    IF should_check AND EXISTS (
        SELECT 1
        FROM fee_records fr
        WHERE fr.student_id = NEW.student_id
          AND fr.fee_type_id = NEW.fee_type_id
          AND fr.academic_year_id = NEW.academic_year_id
          AND fr.id <> COALESCE(NEW.id, -1)
    ) THEN
        RAISE EXCEPTION
            'duplicate fee record for student %, fee type %, academic year %',
            NEW.student_id, NEW.fee_type_id, NEW.academic_year_id
            USING ERRCODE = '23505';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
SET search_path = public, pg_temp;
"""


def upgrade():
    # Redefine the function body only. The existing trigger
    # (trg_prevent_fee_record_duplicate) keeps pointing at it, so no trigger
    # DDL is needed and its BEFORE INSERT OR UPDATE OF ... binding is unchanged.
    op.execute(sa.text(_FN_CANCEL_AWARE))


def downgrade():
    op.execute(sa.text(_FN_BLANKET))
