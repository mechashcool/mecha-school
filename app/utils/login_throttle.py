"""
Progressive login throttle – web authentication flow.

Tracks failed login attempts per (IP, username) pair and imposes escalating
lockout periods to resist brute-force attacks:

    attempts 1–5  →  no lockout
    attempt  6    →  2 minutes
    attempt  7    →  5 minutes
    attempt  8    →  10 minutes
    attempt  9    →  30 minutes
    attempt  10+  →  2 hours

Counters auto-expire after 24 hours so legitimate users are never permanently
locked out.

Storage: uses Redis when RATELIMIT_STORAGE_URI is a redis:// or rediss:// URI
(the same backend that Flask-Limiter uses), so limits are shared across all
Gunicorn worker processes. Falls back to a per-process in-memory dict if Redis
is unavailable — limits then apply per-worker only, which is acceptable for
single-worker deployments or development.

On any Redis error the functions fail open (no lockout applied) to prevent a
Redis outage from blocking all logins.
"""
import hashlib
import logging
import os
import threading
import time
from typing import Tuple

_log = logging.getLogger('mecha.login_throttle')

# ── Lockout schedule ─────────────────────────────────────────────────────────
# Sorted descending so the first matching threshold is the most restrictive.
_SCHEDULE = [
    (10, 7200),  # >= 10 attempts → 2 hours
    (9,  1800),  # 9  attempts    → 30 minutes
    (8,   600),  # 8  attempts    → 10 minutes
    (7,   300),  # 7  attempts    → 5 minutes
    (6,   120),  # 6  attempts    → 2 minutes
    # 1–5 → no lockout
]

# Counter keys auto-expire after this many seconds to ensure eventual cleanup.
_COUNTER_TTL = 24 * 3600  # 24 hours


def _lockout_seconds(count: int) -> int:
    """Return the lockout duration in seconds for the given failure count."""
    for threshold, seconds in _SCHEDULE:
        if count >= threshold:
            return seconds
    return 0


# ── Key construction ─────────────────────────────────────────────────────────

def _make_keys(ip: str, username: str) -> Tuple[str, str]:
    """
    Return (counter_key, lock_key) for an (IP, username) pair.

    The username is hashed so raw credentials are never written to Redis,
    even though Redis is server-side only.
    """
    ukey = hashlib.sha256(username.lower().encode()).hexdigest()[:20]
    base = f'login_fail:{ip}:{ukey}'
    return f'{base}:c', f'{base}:l'


# ── Redis backend ─────────────────────────────────────────────────────────────

_redis_client = None
_redis_initialized = False
_redis_init_lock = threading.Lock()


def _want_redis() -> bool:
    uri = os.environ.get('RATELIMIT_STORAGE_URI', '')
    return uri.startswith(('redis://', 'rediss://'))


def _get_redis():
    """Return a Redis client, or None if Redis is unavailable."""
    global _redis_client, _redis_initialized
    if _redis_initialized:
        return _redis_client
    with _redis_init_lock:
        if _redis_initialized:
            return _redis_client
        uri = os.environ.get('RATELIMIT_STORAGE_URI', '')
        try:
            import redis as _r
            _redis_client = _r.from_url(
                uri,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            _redis_client.ping()
            # Log only the host:port, never credentials embedded in the URI.
            safe = uri.split('@')[-1].split('/')[0]
            _log.info('[login_throttle] Redis backend active (%s)', safe)
        except Exception as exc:
            _log.warning(
                '[login_throttle] Redis unavailable (%s); '
                'falling back to in-process memory store',
                type(exc).__name__,
            )
            _redis_client = None
        _redis_initialized = True
    return _redis_client


def _redis_check_lockout(ip: str, username: str) -> Tuple[bool, int]:
    try:
        r = _get_redis()
        if r is None:
            return False, 0
        _, lk = _make_keys(ip, username)
        ttl = r.ttl(lk)
        if ttl > 0:
            return True, int(ttl)
    except Exception as exc:
        _log.warning('[login_throttle] Redis error in check_lockout: %s', exc)
    return False, 0


def _redis_record_failure(ip: str, username: str) -> int:
    try:
        r = _get_redis()
        if r is None:
            return 0
        ck, lk = _make_keys(ip, username)
        # Atomically increment counter and refresh its TTL.
        pipe = r.pipeline()
        pipe.incr(ck)
        pipe.expire(ck, _COUNTER_TTL)
        count = pipe.execute()[0]
        lockout = _lockout_seconds(count)
        if lockout > 0:
            # Overwrite any existing (shorter) lock so the duration always
            # reflects the current attempt count, not the previous one.
            r.setex(lk, lockout, '1')
        return count
    except Exception as exc:
        _log.warning('[login_throttle] Redis error in record_failure: %s', exc)
    return 0


def _redis_reset(ip: str, username: str) -> None:
    try:
        r = _get_redis()
        if r is None:
            return
        ck, lk = _make_keys(ip, username)
        r.delete(ck, lk)
    except Exception as exc:
        _log.warning('[login_throttle] Redis error in reset: %s', exc)


# ── In-memory fallback ────────────────────────────────────────────────────────

_mem_store: dict = {}
_mem_lock = threading.Lock()


def _mem_check_lockout(ip: str, username: str) -> Tuple[bool, int]:
    _, lk = _make_keys(ip, username)
    with _mem_lock:
        until = _mem_store.get(lk, 0.0)
        remaining = until - time.time()
        if remaining > 0:
            return True, int(remaining)
    return False, 0


def _mem_record_failure(ip: str, username: str) -> int:
    ck, lk = _make_keys(ip, username)
    with _mem_lock:
        count = _mem_store.get(ck, 0) + 1
        _mem_store[ck] = count
        lockout = _lockout_seconds(count)
        if lockout > 0:
            _mem_store[lk] = time.time() + lockout
    return count


def _mem_reset(ip: str, username: str) -> None:
    ck, lk = _make_keys(ip, username)
    with _mem_lock:
        _mem_store.pop(ck, None)
        _mem_store.pop(lk, None)


# ── Public interface ──────────────────────────────────────────────────────────

def check_lockout(ip: str, username: str) -> Tuple[bool, int]:
    """
    Return (is_locked, remaining_seconds).

    Fails open on storage errors so a Redis outage cannot block all logins.
    """
    if _want_redis():
        return _redis_check_lockout(ip, username)
    return _mem_check_lockout(ip, username)


def record_failed_attempt(ip: str, username: str) -> int:
    """
    Increment the failure counter for (ip, username) and set a lockout if the
    counter crosses a threshold. Returns the updated attempt count.
    """
    if _want_redis():
        return _redis_record_failure(ip, username)
    return _mem_record_failure(ip, username)


def reset_attempts(ip: str, username: str) -> None:
    """Clear all failure state for (ip, username) after a successful login."""
    if _want_redis():
        _redis_reset(ip, username)
    else:
        _mem_reset(ip, username)


def format_wait_ar(seconds: int) -> str:
    """
    Return a human-readable Arabic wait-time phrase, including the preposition
    'بعد', so callers can embed it directly:

        f'يرجى المحاولة {format_wait_ar(300)}.'
        → 'يرجى المحاولة بعد 5 دقائق.'
    """
    if seconds < 60:
        return 'بعد أقل من دقيقة'
    minutes = (seconds + 59) // 60   # round up to nearest whole minute
    if minutes < 60:
        if minutes == 1:
            return 'بعد دقيقة'
        if minutes == 2:
            return 'بعد دقيقتين'
        if minutes <= 10:
            return f'بعد {minutes} دقائق'
        return f'بعد {minutes} دقيقة'
    hours = (minutes + 59) // 60     # round up to nearest whole hour
    if hours == 1:
        return 'بعد ساعة'
    if hours == 2:
        return 'بعد ساعتين'
    if hours <= 10:
        return f'بعد {hours} ساعات'
    return f'بعد {hours} ساعة'
