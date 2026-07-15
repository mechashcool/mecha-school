"""P2 tests — backend read-cache layer (ttl_cache, context_cache, badge
invalidation keys, ETag responses).

Covers:
  * ttl_cache — key isolation, get_or_set single-load, predicate invalidation,
    zero-TTL no-op, clear().
  * context_cache — per-school caching of the active-year id and branding
    payload with stubbed loaders (no DB), per-school key isolation, explicit
    invalidation, master-flag fall-through, defensive copying of the cached
    branding dict.
  * invalidate_user_badges — removes ONE user's mobile badge entries only,
    across all their school/role/year variants; other users' entries survive.
  * ok_etag — strong ETag on the exact payload, 304 on If-None-Match match,
    fresh 200 + different ETag when the payload changes, private/no-cache
    headers, and byte-identical 200 body for clients that send no validator.

No database and no live network.
"""
import json

from app import create_app
from app.utils import badge_cache, ttl_cache


def _app(**overrides):
    app = create_app('testing')
    app.config.update(SERVER_NAME='localhost', PREFERRED_URL_SCHEME='http')
    app.config.update(overrides)
    ttl_cache.clear()
    badge_cache.clear()
    return app


# ── ttl_cache ─────────────────────────────────────────────────────────────────

def test_ttl_cache_key_isolation_and_single_load():
    ttl_cache.clear()
    calls = []

    def loader_a():
        calls.append('a')
        return 'value-a'

    assert ttl_cache.get_or_set(('x', 1), loader_a, 60) == 'value-a'
    assert ttl_cache.get_or_set(('x', 1), loader_a, 60) == 'value-a'
    assert calls == ['a']                          # loaded exactly once
    assert ttl_cache.get(('x', 2)) is None         # different key → miss
    assert ttl_cache.get(('y', 1)) is None


def test_ttl_cache_zero_ttl_never_stores_and_invalidate_is_selective():
    ttl_cache.clear()
    ttl_cache.put(('k', 1), 'v', 0)
    assert ttl_cache.get(('k', 1)) is None

    ttl_cache.put(('k', 1), 'v1', 60)
    ttl_cache.put(('k', 2), 'v2', 60)
    removed = ttl_cache.invalidate(lambda k: k == ('k', 1))
    assert removed == 1
    assert ttl_cache.get(('k', 1)) is None
    assert ttl_cache.get(('k', 2)) == 'v2'


# ── context_cache ─────────────────────────────────────────────────────────────

def test_active_year_cached_per_school_and_invalidated(monkeypatch):
    app = _app()
    from app.utils import context_cache

    calls = []
    monkeypatch.setattr(context_cache, '_load_active_year_id',
                        lambda sid: calls.append(sid) or 100 + sid)

    with app.app_context():
        assert context_cache.get_active_year_id(1) == 101
        assert context_cache.get_active_year_id(1) == 101   # cached
        assert context_cache.get_active_year_id(2) == 102   # separate school key
        assert calls == [1, 2]

        # Rollover hook: only school 1's entry is dropped.
        context_cache.invalidate_school_context(1)
        assert context_cache.get_active_year_id(2) == 102   # still cached
        assert context_cache.get_active_year_id(1) == 101   # re-loaded
        assert calls == [1, 2, 1]


def test_context_cache_flag_off_falls_through_every_time(monkeypatch):
    app = _app(BACKEND_CACHE_ENABLED=False)
    from app.utils import context_cache

    calls = []
    monkeypatch.setattr(context_cache, '_load_active_year_id',
                        lambda sid: calls.append(sid) or 55)

    with app.app_context():
        assert context_cache.get_active_year_id(7) == 55
        assert context_cache.get_active_year_id(7) == 55
        assert calls == [7, 7]                     # no caching — pre-P2 behaviour


def test_school_branding_cached_isolated_and_copied(monkeypatch):
    app = _app()
    from app.utils import context_cache

    def fake_load(sid):
        return {'id': sid, 'name': f'School {sid}', 'logo': None}

    monkeypatch.setattr(context_cache, '_load_school_branding', fake_load)

    with app.app_context():
        b1 = context_cache.get_school_branding(1)
        b2 = context_cache.get_school_branding(2)
        assert b1['name'] == 'School 1' and b2['name'] == 'School 2'

        # Defensive copy: mutating the returned dict must not poison the cache.
        b1['name'] = 'HACKED'
        assert context_cache.get_school_branding(1)['name'] == 'School 1'

        # Invalidation drops only the targeted school.
        context_cache.invalidate_school_context(1)
        assert ttl_cache.get(('school_branding', 1)) is None
        assert ttl_cache.get(('school_branding', 2)) is not None


# ── mobile badge cache invalidation ───────────────────────────────────────────

def test_invalidate_user_badges_targets_one_user_only():
    app = _app()
    with app.app_context():
        from app.blueprints.mobile_api.badges import invalidate_user_badges

        badge_cache.clear()
        badge_cache.get_or_set(('mobile_badges', 1, 10, 'parent', 5),
                               lambda: {'notifications': 3}, 60)
        badge_cache.get_or_set(('mobile_badges', 1, 10, 'parent', 6),
                               lambda: {'notifications': 1}, 60)   # other year
        badge_cache.get_or_set(('mobile_badges', 1, 11, 'parent', 5),
                               lambda: {'notifications': 9}, 60)   # other user
        badge_cache.get_or_set(('mobile_badges', 2, 12, 'teacher', 8),
                               lambda: {'notifications': 4}, 60)   # other school

        invalidate_user_badges(10)

        sentinel = object()
        # Both of user 10's variants are gone …
        assert badge_cache.get_or_set(('mobile_badges', 1, 10, 'parent', 5),
                                      lambda: sentinel, 0) is sentinel
        assert badge_cache.get_or_set(('mobile_badges', 1, 10, 'parent', 6),
                                      lambda: sentinel, 0) is sentinel
        # … while other users' entries survive untouched.
        assert badge_cache.get_or_set(('mobile_badges', 1, 11, 'parent', 5),
                                      lambda: sentinel, 0) == {'notifications': 9}
        assert badge_cache.get_or_set(('mobile_badges', 2, 12, 'teacher', 8),
                                      lambda: sentinel, 0) == {'notifications': 4}


# ── ok_etag ───────────────────────────────────────────────────────────────────

def test_ok_etag_full_response_and_304_on_match():
    app = _app()
    from app.blueprints.mobile_api.utils import ok_etag

    with app.test_request_context('/'):
        resp = ok_etag(schedule=[{'id': 1, 'day': 'sunday'}])
        assert resp.status_code == 200
        etag = resp.headers.get('ETag')
        assert etag
        assert resp.headers.get('Cache-Control') == 'private, no-cache'
        body = json.loads(resp.get_data())
        assert body == {'ok': True, 'schedule': [{'id': 1, 'day': 'sunday'}]}

    # Same payload + matching validator → bodyless 304.
    with app.test_request_context('/', headers={'If-None-Match': etag}):
        resp2 = ok_etag(schedule=[{'id': 1, 'day': 'sunday'}])
        assert resp2.status_code == 304
        assert resp2.get_data() == b''

    # Changed payload → fresh 200 with a DIFFERENT ETag (stale validator loses).
    with app.test_request_context('/', headers={'If-None-Match': etag}):
        resp3 = ok_etag(schedule=[{'id': 2, 'day': 'monday'}])
        assert resp3.status_code == 200
        assert resp3.headers.get('ETag') != etag


def test_ok_etag_without_validator_matches_ok_payload():
    """Clients that never send If-None-Match (the current app) must receive a
    body byte-identical to the plain ok() helper."""
    app = _app()
    from app.blueprints.mobile_api.utils import ok, ok_etag

    with app.test_request_context('/'):
        plain = ok(schedule=[], section=None)
        tagged = ok_etag(schedule=[], section=None)
        assert tagged.status_code == 200
        assert json.loads(tagged.get_data()) == json.loads(plain.get_data())
