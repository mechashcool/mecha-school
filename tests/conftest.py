"""
pytest configuration — runs before any test collection.

Safety guard: abort immediately if the database the tests would use appears to
point at a production or shared remote database.  This prevents automated tests
from creating or mutating real school data on Render/Supabase/Neon.

IMPORTANT: the guard must inspect the *effective* database URL, not just the
raw shell environment.  The project's ``config/settings.py`` calls
``load_dotenv()`` at import time, so a production ``DATABASE_URL`` placed in
``.env`` only becomes visible *after* the env file is loaded.  An earlier
version of this guard read ``os.environ['DATABASE_URL']`` before ``.env`` was
loaded, saw an empty value, and allowed tests to run against production.  We
now load ``.env`` ourselves and also check the resolved ``SQLALCHEMY_DATABASE_URI``
of every config the tests might build with.
"""
import os
import re

_PROD_INDICATORS = (
    'render.com',
    'supabase.co',
    'supabase.io',
    'pooler.supabase.com',
    'neon.tech',
    '.fl0.io',
)


def _mask(url: str) -> str:
    """Hide any credentials before printing the offending URL."""
    return re.sub(r'://([^:/@]+):[^@/]+@', r'://\1:***@', url or '')


def _effective_db_urls() -> list:
    """Collect every DB URL the test session could realistically use.

    Mirrors the app's own startup: load ``.env`` first (so a production
    ``DATABASE_URL`` in the env file becomes visible), then read the resolved
    ``SQLALCHEMY_DATABASE_URI`` from each config class the tests build with via
    ``create_app(...)``.
    """
    urls = []

    # Load .env exactly like config/settings.py does, so a production URL placed
    # there is detected even though it is not an explicit shell variable yet.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    urls.append(os.environ.get('DATABASE_URL', ''))

    # Inspect the resolved URIs of the configs the test suite actually uses.
    # Importing config.settings also triggers its own load_dotenv() — harmless
    # and consistent with the application factory.
    try:
        from config.settings import config as _config
        for name in ('default', 'development', 'testing', 'production'):
            cfg = _config.get(name)
            uri = getattr(cfg, 'SQLALCHEMY_DATABASE_URI', None)
            if uri:
                urls.append(uri)
    except Exception:
        pass

    return urls


def pytest_configure(config):
    offending = next(
        (u for u in _effective_db_urls()
         if any(indicator in (u or '') for indicator in _PROD_INDICATORS)),
        None,
    )
    if offending:
        import pytest
        message = (
            '\n'
            '╔══════════════════════════════════════════════════════════════╗\n'
            '║  BLOCKED: the test database URL points to PRODUCTION.       ║\n'
            '║  Running tests against production would corrupt live data.  ║\n'
            '║  Point DATABASE_URL (or .env) at a local/test database.     ║\n'
            '╚══════════════════════════════════════════════════════════════╝\n'
            f'  Offending URL: {_mask(offending)}\n'
        )
        # pytest.exit aborts the whole session cleanly at configure time —
        # before collection imports any test module or opens a DB connection.
        pytest.exit(message, returncode=2)
