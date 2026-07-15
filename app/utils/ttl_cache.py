"""Generic short-TTL in-process cache for small, tenant-keyed backend values.

P2 companion to ``badge_cache`` (which is reserved for badge/count payloads):
this module holds other small read-heavy values — the active academic-year id
per school, the serialized school-branding dict, and similar slow-changing
context data.

Isolation contract:
  * Keys are tuples that MUST embed every dimension the cached value depends
    on. For per-school facts that is ``('name', school_id)``; anything that
    varies per user/role/year/child MUST carry those ids too. A value must
    never be readable under a key that omits one of its dimensions.
  * Values must be immutable or defensively copied by the caller before being
    returned to request code — cached objects are shared across threads.
  * Failures are never cached; a loader exception propagates and nothing is
    stored, so the next request retries the database (fail open to the DB,
    never to another tenant's data).
  * Multi-worker note: per-process, like badge_cache. Workers may briefly
    disagree within one TTL; invalidation is also per-process, which is why
    TTLs stay short and rollover-critical values get explicit invalidation
    hooks AND a short TTL as the cross-worker bound.

Memory bound: at most ``_MAX_ENTRIES`` keys; expired entries are purged first,
then oldest-inserted entries are evicted.
"""
import threading
import time

_MAX_ENTRIES = 4096
_lock = threading.Lock()
_store: dict = {}   # key -> (expires_at_monotonic, value)


def get(key):
    """Return the cached value for ``key`` or None (miss/expired)."""
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit is not None:
            if hit[0] > now:
                return hit[1]
            del _store[key]
    return None


def put(key, value, ttl: float) -> None:
    """Store ``value`` under ``key`` for ``ttl`` seconds. No-op for ttl <= 0."""
    if ttl <= 0:
        return
    now = time.monotonic()
    with _lock:
        if len(_store) >= _MAX_ENTRIES:
            _evict(now)
        _store[key] = (now + ttl, value)


def get_or_set(key, loader, ttl: float):
    """Return the cached value or compute, store, and return it.

    The loader runs OUTSIDE the lock; loader exceptions propagate and nothing
    is cached for that key.
    """
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]

    value = loader()

    if ttl > 0:
        with _lock:
            if len(_store) >= _MAX_ENTRIES:
                _evict(now)
            _store[key] = (now + ttl, value)
    return value


def _evict(now) -> None:
    expired = [k for k, (exp, _) in _store.items() if exp <= now]
    for k in expired:
        del _store[k]
    while len(_store) >= _MAX_ENTRIES:
        _store.pop(next(iter(_store)))


def invalidate(predicate) -> int:
    """Drop every entry whose key satisfies ``predicate(key)``.

    ``predicate`` runs under the lock — keep it cheap and side-effect free.
    Returns the number of entries removed.
    """
    with _lock:
        doomed = [k for k in _store if predicate(k)]
        for k in doomed:
            del _store[k]
    return len(doomed)


def clear() -> None:
    """Drop all entries (used by tests)."""
    with _lock:
        _store.clear()
