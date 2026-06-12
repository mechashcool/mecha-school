"""
Rate limiting helper.

Wraps Flask-Limiter so the rest of the codebase can apply limits without
caring whether the package is installed yet. If Flask-Limiter is unavailable
the module degrades to a no-op (decorators pass through, init_app does
nothing), so the application keeps booting and the limits simply become
inactive until the dependency is installed.

Storage defaults to in-memory (per worker process). For multi-worker /
multi-instance production deployments configure RATELIMIT_STORAGE_URI to a
shared backend such as Redis so limits are enforced globally.
"""
import os

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[],  # only explicitly-decorated routes are limited
        storage_uri=os.environ.get('RATELIMIT_STORAGE_URI', 'memory://'),
        headers_enabled=True,
    )
    RATE_LIMITING_AVAILABLE = True

except Exception:  # pragma: no cover - exercised only when dep is missing
    RATE_LIMITING_AVAILABLE = False

    class _NoopLimiter:
        """Stand-in used when Flask-Limiter is not installed."""

        def init_app(self, app):
            return None

        def limit(self, *args, **kwargs):
            def decorator(f):
                return f
            return decorator

        def exempt(self, f):
            return f

    limiter = _NoopLimiter()


# Shared limit strings so login surfaces stay consistent.
LOGIN_RATE_LIMIT = os.environ.get('LOGIN_RATE_LIMIT', '10 per minute; 60 per hour')
