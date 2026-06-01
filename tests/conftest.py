"""
pytest configuration — runs before any test collection.

Safety guard: abort immediately if DATABASE_URL appears to point at a
production or shared remote database.  This prevents automated tests from
creating or mutating real school data on Render/Supabase/Neon.
"""
import os
import sys

_PROD_INDICATORS = (
    'render.com',
    'supabase.co',
    'supabase.io',
    'pooler.supabase.com',
    'neon.tech',
    '.fl0.io',
)


def pytest_configure(config):
    db_url = os.environ.get('DATABASE_URL', '')
    if any(indicator in db_url for indicator in _PROD_INDICATORS):
        sys.stderr.write(
            '\n\n'
            '╔══════════════════════════════════════════════════════════════╗\n'
            '║  BLOCKED: DATABASE_URL points to a PRODUCTION database.     ║\n'
            '║  Running tests against production would corrupt live data.  ║\n'
            '║  Unset DATABASE_URL or set it to a local/test database.     ║\n'
            '╚══════════════════════════════════════════════════════════════╝\n\n'
        )
        sys.exit(1)
