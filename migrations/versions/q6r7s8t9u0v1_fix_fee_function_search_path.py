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
Revises: p5q6r7s8t9u0
Create Date: 2026-07-01
"""
from alembic import op
import sqlalchemy as sa


revision = 'q6r7s8t9u0v1'
down_revision = 'p5q6r7s8t9u0'
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
