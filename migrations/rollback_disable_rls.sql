-- ============================================================
-- EMERGENCY ROLLBACK: Disable RLS on all public schema tables
-- ============================================================
--
-- Use ONLY if the Flask application breaks after migration
-- p5q6r7s8t9u0 (enable_rls_on_all_public_tables).
--
-- This script is safe to run in the Supabase SQL Editor.
-- It does NOT delete data, drop columns, or modify schema.
-- It does NOT remove any RLS policies (if any were later added).
--
-- After running this script, also run:
--   flask db downgrade o4p5q6r7s8t9
-- to revert the Alembic version tracking.
--
-- ============================================================

DO $$
DECLARE
    _tbl TEXT;
BEGIN
    FOR _tbl IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename
    LOOP
        EXECUTE format(
            'ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY',
            _tbl
        );
    END LOOP;
END $$;

-- Verify rollback
SELECT
    relname         AS table_name,
    relrowsecurity  AS rls_enabled
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind = 'r'
ORDER BY relname;
