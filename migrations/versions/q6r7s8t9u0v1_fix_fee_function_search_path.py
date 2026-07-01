"""Fix mutable search_path on prevent_fee_record_duplicate trigger function.

Background
----------
Supabase Security Advisor raises a 'Function Search Path Mutable' warning for
public.prevent_fee_record_duplicate().  The function was created without an
explicit SET search_path clause, so PostgreSQL inherits the caller's session
search_path at runtime.  A session that manipulates its own search_path could
in theory redirect unqualified table references inside the function to a
different schema — a search-path injection risk.

Fix
---
ALTER FUNCTION ... SET search_path = public, pg_temp

This sets the search_path as a stable function-level configuration parameter.
pg_temp is appended per PostgreSQL best practice so that temporary-table
references (if any are ever used in future) remain safe; it cannot shadow
permanent objects because permanent objects are resolved first.

What is NOT changed
-------------------
* Function body (logic is identical).
* Function signature / argument list (none).
* Return type (trigger).
* Language (plpgsql).
* Trigger definition or behaviour.
* fee_records table or data.
* Any other database object.

Revision ID: q6r7s8t9u0v1
Revises: o4p5q6r7s8t9
Create Date: 2026-07-01

Note on down_revision
---------------------
Originally set to p5q6r7s8t9u0 (the RLS migration) but that file was never
committed to git and therefore does not exist on the VPS.  Corrected to
o4p5q6r7s8t9, which is the actual last committed migration head.
"""
from alembic import op
import sqlalchemy as sa


revision = 'q6r7s8t9u0v1'
down_revision = 'o4p5q6r7s8t9'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text(
        "ALTER FUNCTION public.prevent_fee_record_duplicate()"
        " SET search_path = public, pg_temp;"
    ))


def downgrade():
    op.execute(sa.text(
        "ALTER FUNCTION public.prevent_fee_record_duplicate()"
        " RESET search_path;"
    ))
