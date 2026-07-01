"""Enable Row-Level Security on all public schema tables

Background
----------
Supabase Security Advisor raises two warnings for this project:

  rls_disabled_in_public    — every table in the public schema has RLS off
  sensitive_columns_exposed — sensitive columns (password_hash, salary, bank
                              account, PII) are in tables without RLS

Root cause
----------
All 80 tables in the public schema have relrowsecurity=false.  Supabase
exposes every table via its auto-generated PostgREST REST API.  With RLS
disabled, any request using the anon or authenticated Supabase role would
return all rows from every table, bypassing all application-level isolation.

Safety analysis
---------------
Flask connects as the postgres user through the Supabase connection pooler.
The postgres role has BYPASSRLS=true (confirmed via pg_roles query before
this migration was written).  Enabling RLS (without FORCE) does not affect
any query run by a user whose role has BYPASSRLS=true.

Effect of this migration
------------------------
  postgres role (Flask)       — BYPASSRLS=true → unaffected, all queries work
  anon role (Supabase REST)   — BYPASSRLS=false, no policies → denied
  authenticated role          — BYPASSRLS=false, no policies → denied

No policies are added.  The default-deny behaviour when RLS is on with no
policies is exactly what we want: block direct Supabase REST/Realtime access
while preserving Flask backend functionality unchanged.

FORCE ROW LEVEL SECURITY is NOT used — that would subject even BYPASSRLS
users to policies and would break Flask.

Tables covered
--------------
All 80 tables currently present in the public schema (discovered by the
DO block at migration time, so future tables are not retroactively covered;
a follow-up migration must be run after any new table is added if it is
added outside the normal Flask create-table path).

Revision ID: p5q6r7s8t9u0
Revises: o4p5q6r7s8t9
Create Date: 2026-07-01
"""

from alembic import op

revision = 'p5q6r7s8t9u0'
down_revision = 'o4p5q6r7s8t9'
branch_labels = None
depends_on = None


def upgrade():
    # Enable RLS on every table currently in the public schema.
    # The postgres user has BYPASSRLS=true so Flask is unaffected.
    # anon / authenticated roles have no policies → denied by default.
    # FORCE ROW LEVEL SECURITY is intentionally NOT used.
    op.execute("""
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
                    'ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',
                    _tbl
                );
            END LOOP;
        END $$;
    """)


def downgrade():
    # Rollback: disable RLS on every table in the public schema.
    # Use only if the application breaks after upgrade.
    # Does NOT delete any data or policies.
    op.execute("""
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
    """)
