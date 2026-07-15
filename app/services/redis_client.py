"""Optional shared Redis client (P3).

Design contract — read before adding a caller:
  * Redis is OPTIONAL. `get_redis()` returns None when REDIS_URL is unset, the
    `redis` package is missing, or the server is unreachable — it NEVER raises.
    Every caller must have a non-Redis fallback path (thread pool, in-process
    cache, per-worker limit); core academic/financial/attendance operations
    must never depend on a Redis round trip.
  * All keys MUST be built with `key()` so they carry the REDIS_KEY_PREFIX
    namespace, and any key whose value differs per school/user/role/year MUST
    embed every one of those isolation dimensions — same rule as the
    in-process caches. (The durable push queue stores jobs, not per-tenant
    values: ownership lives inside each job's payload and is re-verified by
    the executing task's explicit filters.)
  * Failed connections are retried at most every _RETRY_COOLDOWN seconds so a
    Redis outage costs one cheap timestamp check per call, not a blocking
    connect per request.
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger('mecha.redis')

_RETRY_COOLDOWN = 30  # seconds between reconnect attempts while Redis is down

_lock = threading.Lock()
_client = None
_last_failure = 0.0


def _url() -> str:
    # Read via Flask config when available (respects testing overrides),
    # falling back to the environment for non-app contexts.
    try:
        from flask import current_app
        if current_app:
            return current_app.config.get('REDIS_URL') or ''
    except Exception:
        pass
    return os.environ.get('REDIS_URL', '')


def redis_configured() -> bool:
    return bool(_url())


def key(*parts) -> str:
    prefix = os.environ.get('REDIS_KEY_PREFIX', 'mecha')
    return ':'.join([prefix] + [str(p) for p in parts])


def get_redis():
    """Return a live, pinged Redis client or None. Never raises."""
    global _client, _last_failure
    url = _url()
    if not url:
        return None
    if _client is not None:
        return _client
    if time.time() - _last_failure < _RETRY_COOLDOWN:
        return None
    with _lock:
        if _client is not None:
            return _client
        if time.time() - _last_failure < _RETRY_COOLDOWN:
            return None
        try:
            import redis as _redis
        except ImportError:
            log.warning('[redis] REDIS_URL is set but the redis package is not '
                        'installed — Redis features disabled')
            _last_failure = time.time()
            return None
        try:
            client = _redis.Redis.from_url(
                url,
                socket_connect_timeout=3,
                # Must stay ABOVE the longest blocking-pop timeout used by the
                # durable queue consumer (5 s), or every idle BRPOP would raise.
                socket_timeout=15,
                health_check_interval=30,
                decode_responses=True,
            )
            client.ping()
            _client = client
            log.info('[redis] connected')
            return _client
        except Exception as exc:
            _last_failure = time.time()
            log.warning('[redis] unavailable (%s) — falling back to in-process '
                        'behaviour; retry in %ss', type(exc).__name__, _RETRY_COOLDOWN)
            return None


def mark_failed() -> None:
    """Callers that hit a dead connection report it here so the next
    get_redis() re-establishes instead of reusing a broken client."""
    global _client, _last_failure
    with _lock:
        _client = None
        _last_failure = time.time()
