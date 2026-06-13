"""Short-TTL in-process cache for sidebar badge counts.

Scope and guarantees:
  * Values are small integers only (counts) — never ORM objects.
  * Keys MUST embed every dimension the underlying query depends on
    (user id, school id / tenant selector, role) — see app/__init__.py.
  * TTL is short (45 s): a badge may lag reality by up to that long, which
    is the documented, accepted trade-off. Read/unread state in the database
    is never touched by this cache.
  * Thread-safe via a single lock; the loader runs OUTSIDE the lock so a
    slow query never blocks other threads' cache reads.
  * Multi-worker note (e.g. gunicorn on Render): each worker process has its
    own independent cache. Workers may briefly show counts that differ by a
    few seconds of staleness — acceptable for badges. No external service
    (Redis etc.) is introduced.
  * Memory bound: at most ``_MAX_ENTRIES`` keys. When full, expired entries
    are purged first, then oldest-inserted entries are evicted.
"""
import threading
import time

DEFAULT_TTL = 45  # seconds — within the approved 30–60 s window

_MAX_ENTRIES = 4096
_lock = threading.Lock()
_store: dict = {}   # key -> (expires_at_monotonic, value)


def get_or_set(key, loader, ttl: float = DEFAULT_TTL):
    """Return the cached value for ``key`` or compute, store and return it."""
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]

    value = loader()  # may raise — nothing is cached then

    with _lock:
        if len(_store) >= _MAX_ENTRIES:
            _evict(now)
        _store[key] = (now + ttl, value)
    return value


def _evict(now):
    """Purge expired entries; if still full, drop oldest-inserted entries."""
    expired = [k for k, (exp, _) in _store.items() if exp <= now]
    for k in expired:
        del _store[k]
    while len(_store) >= _MAX_ENTRIES:
        # dicts preserve insertion order — first key is the oldest entry
        _store.pop(next(iter(_store)))


def clear():
    """Drop all entries (used by tests)."""
    with _lock:
        _store.clear()
