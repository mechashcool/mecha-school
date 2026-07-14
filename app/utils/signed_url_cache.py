"""Short-TTL in-process cache for OBJECT-SCOPED Supabase signed URLs.

Why this exists (P0): minting a Supabase-native signed URL is a synchronous
network call (`_supabase_sign`, up to 15 s). School-board lists sign every
video on every request, and the /media-proxy redirect signs per object hit.
This cache makes the sign call once per object per TTL window.

Isolation contract — read before changing anything here:
  * A cached entry grants access to exactly ONE storage object. The key is
    ``(bucket, object_path, ttl)`` and nothing else. It deliberately contains
    no user / school / role / academic-year / child dimension because the
    signed URL itself is object-scoped, not user-scoped: whoever is authorized
    to see the object receives the same URL.
  * Per-user / per-school / per-role / per-child / per-audience authorization
    MUST therefore be enforced by the caller BEFORE a URL is requested from
    this cache. The two callers honour that:
      - ``supabase_media_url()`` is only reached from serializers that already
        verified ownership of the source record (parent-child link, teacher
        assignment, audience filter, school_id equality), and
      - ``/media-proxy`` verifies its HMAC token — minted only after those
        same checks — before consulting the cache.
    Never call into this cache from a code path that has not completed
    authorization for the owning record.
  * ``object_path`` is the full in-bucket key (e.g. ``schools/3/board/…``),
    so entries for different schools/objects can never collide.
  * Entries expire with a safety margin well before the underlying signature
    does, so a returned URL always has remaining validity for the client.
  * Sign FAILURES are never cached — the caller falls through to its secure
    fallback and retries signing on the next request.
  * Multi-worker note: the cache is per-process (like badge_cache). Workers
    independently mint equivalent URLs; no cross-process state is shared.
"""
import threading
import time

_MAX_ENTRIES = 2048
_lock = threading.Lock()
_store: dict = {}   # (bucket, object_path, ttl) -> (expires_at_monotonic, signed_url)

# Minimum remaining-signature margin (seconds). An entry is dropped this long
# before the signature itself expires so clients never receive a nearly-dead URL.
_MIN_MARGIN = 60


def _cache_ttl_for(ttl: int) -> int:
    """Cache lifetime for a signature of ``ttl`` seconds: ttl minus a margin of
    max(60 s, ttl/4). Returns 0 when the signature is too short to cache safely."""
    margin = max(_MIN_MARGIN, ttl // 4)
    return max(0, ttl - margin)


def get(bucket: str, object_path: str, ttl: int) -> str | None:
    """Return a still-valid cached signed URL for the object, or None."""
    key = (bucket, object_path, int(ttl))
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit is not None:
            if hit[0] > now:
                return hit[1]
            del _store[key]
    return None


def put(bucket: str, object_path: str, ttl: int, signed_url: str) -> None:
    """Cache a freshly minted signed URL. No-op for empty URLs or TTLs too
    short to cache with a safe margin."""
    if not signed_url:
        return
    ttl = int(ttl)
    cache_ttl = _cache_ttl_for(ttl)
    if cache_ttl <= 0:
        return
    now = time.monotonic()
    key = (bucket, object_path, ttl)
    with _lock:
        if len(_store) >= _MAX_ENTRIES:
            _evict(now)
        _store[key] = (now + cache_ttl, signed_url)


def _evict(now) -> None:
    """Purge expired entries; if still full, drop oldest-inserted entries."""
    expired = [k for k, (exp, _) in _store.items() if exp <= now]
    for k in expired:
        del _store[k]
    while len(_store) >= _MAX_ENTRIES:
        _store.pop(next(iter(_store)))


def invalidate(bucket: str, object_path: str) -> int:
    """Drop every cached entry for one object (all TTL variants). Call after an
    object is deleted or replaced so a stale URL is never served. Returns the
    number of entries removed."""
    with _lock:
        doomed = [k for k in _store if k[0] == bucket and k[1] == object_path]
        for k in doomed:
            del _store[k]
    return len(doomed)


def clear() -> None:
    """Drop all entries (used by tests)."""
    with _lock:
        _store.clear()
