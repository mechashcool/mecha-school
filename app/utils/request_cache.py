"""Request-scoped memoization helper.

Stores computed values on ``flask.g`` so repeated lookups within ONE request
(hooks → context processor → route → template) hit the database only once.
Everything stored here dies with the request — nothing is shared across
requests, users, or workers, so tenant isolation is preserved by construction
as long as the cache *key* includes the relevant school/year/selector.
"""
from flask import g, has_request_context

_MISSING = object()


def request_memo(key, loader):
    """Return ``loader()`` memoized under ``key`` for the current request.

    Outside a request context the loader is called directly (CLI, seeders,
    background jobs keep their existing behavior). Exceptions from the loader
    propagate and nothing is cached for that key.
    """
    if not has_request_context():
        return loader()
    cache = getattr(g, '_request_memo', None)
    if cache is None:
        cache = {}
        g._request_memo = cache
    value = cache.get(key, _MISSING)
    if value is _MISSING:
        value = loader()
        cache[key] = value
    return value
