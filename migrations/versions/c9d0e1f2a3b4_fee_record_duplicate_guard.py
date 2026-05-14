"""Add fee record duplicate guard.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-05-01
"""
from alembic import op
import sqlalchemy as sa


revision = 'c9d0e1f2a3b4'
down_revision = 'b8c9d0e1f2a3'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("""
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
        $$ LANGUAGE plpgsql;
    """))
    op.execute(sa.text("""
        DROP TRIGGER IF EXISTS trg_prevent_fee_record_duplicate ON fee_records;
        CREATE TRIGGER trg_prevent_fee_record_duplicate
        BEFORE INSERT OR UPDATE OF student_id, fee_type_id, academic_year_id
        ON fee_records
        FOR EACH ROW
        EXECUTE FUNCTION prevent_fee_record_duplicate();
    """))


def downgrade():
    op.execute(sa.text("""
        DROP TRIGGER IF EXISTS trg_prevent_fee_record_duplicate ON fee_records;
        DROP FUNCTION IF EXISTS prevent_fee_record_duplicate();
    """))
