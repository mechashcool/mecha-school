"""P0 stability tests — media direct delivery, signed-URL cache, async dispatch.

Covers the four backend-facing P0 changes:

  1. signed_url_cache — object-scoped keying (bucket + object_path + ttl),
     TTL safety margin, per-object invalidation. Verifies entries can never be
     confused between different objects/buckets (isolation by key).
  2. supabase_media_url — successful Supabase-native signs are cached (one
     network sign per object per TTL window); sign FAILURE falls back to the
     authenticated Flask HMAC proxy URL, NEVER to the raw stored value
     (private URL / bare relative path) — fail closed.
  3. /media-proxy redirect — after HMAC verification the route 302s to a
     Supabase-native signed URL instead of buffering bytes through the worker;
     the HMAC token is still mandatory (tampered → 403); signing unavailable →
     the previous authenticated streaming path; flag off → streaming restored.
  4. async_dispatch — inline in testing mode, background execution inside a
     fresh app context otherwise, and task exceptions never propagate.

No database and no live network: requests.get / requests.post are stubbed.
"""
import threading
from urllib.parse import urlparse

import requests

from app import create_app
from app.utils import signed_url_cache


_SUPA = 'https://jizrarelapmzrjajrtfo.supabase.co'
_PUBLIC = _SUPA + '/storage/v1/object/public/uploads/'


def _app(**overrides):
    app = create_app('testing')
    app.config.update(
        PRIVATE_UPLOADS_ENABLED=True,
        SUPABASE_URL=_SUPA,
        SUPABASE_SERVICE_KEY='',           # default: signing unavailable
        SUPABASE_BUCKET='uploads',
        SUPABASE_STORAGE_BUCKET_MEDIA='school-media',
        SERVER_NAME='localhost',
        PREFERRED_URL_SCHEME='http',
    )
    app.config.update(overrides)
    signed_url_cache.clear()               # never leak entries between tests
    return app


class _Resp:
    def __init__(self, status=200, content=b'DATA', ctype='image/png',
                 json_body=None):
        self.status_code = status
        self.content = content
        self.headers = {'Content-Type': ctype}
        self._json = json_body or {}

    def json(self):
        return self._json


def _stub_fetch(monkeypatch):
    """requests.get stub: public endpoint streams, everything else 400s."""
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if '/object/public/' in url:
            return _Resp(200)
        return _Resp(400)

    monkeypatch.setattr(requests, 'get', fake_get)
    return calls


def _stub_sign(monkeypatch, ok=True):
    """requests.post stub for the Supabase sign endpoint; records object URLs."""
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(url)
        if not ok:
            return _Resp(status=500)
        # '/storage/v1/object/sign/<bucket>/<path>' → echo a signed path back.
        marker = '/storage/v1/object/sign/'
        rel = url[url.find(marker) + len('/storage/v1'):]
        return _Resp(json_body={'signedURL': f'{rel}?token=sig-{len(calls)}'})

    monkeypatch.setattr(requests, 'post', fake_post)
    return calls


def _proxy_path(app, stored_url):
    from app.utils.upload_access import supabase_media_url
    with app.test_request_context('/'):
        gen = supabase_media_url(stored_url)
    pu = urlparse(gen)
    return gen, pu.path + ('?' + pu.query if pu.query else '')


# ── 1. signed_url_cache: keying, margin, isolation ────────────────────────────

def test_cache_key_isolation_between_objects_and_buckets():
    signed_url_cache.clear()
    signed_url_cache.put('uploads', 'schools/1/a.png', 3600, 'url-school1')
    signed_url_cache.put('uploads', 'schools/2/a.png', 3600, 'url-school2')
    signed_url_cache.put('school-media', 'schools/1/a.png', 3600, 'url-media')

    assert signed_url_cache.get('uploads', 'schools/1/a.png', 3600) == 'url-school1'
    assert signed_url_cache.get('uploads', 'schools/2/a.png', 3600) == 'url-school2'
    assert signed_url_cache.get('school-media', 'schools/1/a.png', 3600) == 'url-media'
    # A different TTL variant is a different entry — never served interchangeably.
    assert signed_url_cache.get('uploads', 'schools/1/a.png', 900) is None


def test_cache_refuses_ttls_too_short_for_a_safe_margin():
    signed_url_cache.clear()
    # ttl 60 → margin max(60, 15) = 60 → cache_ttl 0 → not cached.
    signed_url_cache.put('uploads', 'x.png', 60, 'short-lived')
    assert signed_url_cache.get('uploads', 'x.png', 60) is None


def test_cache_invalidate_targets_one_object_only():
    signed_url_cache.clear()
    signed_url_cache.put('uploads', 'a.png', 3600, 'url-a')
    signed_url_cache.put('uploads', 'a.png', 900, 'url-a-short')
    signed_url_cache.put('uploads', 'b.png', 3600, 'url-b')
    removed = signed_url_cache.invalidate('uploads', 'a.png')
    assert removed == 2
    assert signed_url_cache.get('uploads', 'a.png', 3600) is None
    assert signed_url_cache.get('uploads', 'b.png', 3600) == 'url-b'


# ── 2. supabase_media_url: sign caching + fail-closed fallback ────────────────

def test_video_sign_result_is_cached_one_network_call(monkeypatch):
    app = _app(SUPABASE_SERVICE_KEY='test-key')
    calls = _stub_sign(monkeypatch)
    from app.utils.upload_access import supabase_media_url
    stored = _SUPA + '/storage/v1/object/public/school-media/schools/3/board/media/v.mp4'
    with app.test_request_context('/'):
        first = supabase_media_url(stored, want_video=True)
        second = supabase_media_url(stored, want_video=True)
    assert first is not None and '/storage/v1/object/sign/school-media/' in first
    assert second == first                       # served from cache
    assert len(calls) == 1                       # exactly one sign round-trip


def test_video_sign_failure_falls_back_to_hmac_proxy_never_raw(monkeypatch):
    app = _app(SUPABASE_SERVICE_KEY='test-key')
    _stub_sign(monkeypatch, ok=False)
    from app.utils.upload_access import supabase_media_url
    stored = _SUPA + '/storage/v1/object/public/school-media/schools/3/board/media/v.mp4'
    with app.test_request_context('/'):
        out = supabase_media_url(stored, want_video=True)
    assert out is not None
    assert out != stored                          # never the raw stored URL
    assert '/media-proxy/school-media/schools/3/board/media/v.mp4' in out
    assert 'sig=' in out and 'exp=' in out        # authenticated HMAC fallback


def test_relative_video_sign_failure_never_returns_bare_path(monkeypatch):
    """Regression: a relative uploads/… video whose sign fails used to be
    returned as a bare relative path (not even a URL)."""
    app = _app(SUPABASE_SERVICE_KEY='test-key')
    _stub_sign(monkeypatch, ok=False)
    from app.utils.upload_access import supabase_media_url
    with app.test_request_context('/'):
        out = supabase_media_url('uploads/schools/3/board/media/v.mp4',
                                 want_video=True)
    assert out is not None and out.startswith('http')
    assert '/media-proxy/uploads/schools/3/board/media/v.mp4' in out
    assert 'sig=' in out and 'exp=' in out


# ── 3. /media-proxy: direct-delivery redirect ─────────────────────────────────

def test_media_proxy_redirects_to_native_signed_url(monkeypatch):
    app = _app(SUPABASE_SERVICE_KEY='test-key')
    sign_calls = _stub_sign(monkeypatch)
    key = 'students/documents/x.png'
    _, path = _proxy_path(app, _PUBLIC + key)

    resp = app.test_client().get(path)
    assert resp.status_code == 302
    loc = resp.headers['Location']
    assert f'/storage/v1/object/sign/uploads/{key}' in loc
    assert 'token=' in loc
    # The redirect target rotates with the signature — never shared/cached.
    assert resp.headers.get('Cache-Control') == 'private, no-store'
    assert len(sign_calls) == 1

    # A second hit reuses the cached signed URL — no extra sign round-trip.
    resp2 = app.test_client().get(path)
    assert resp2.status_code == 302
    assert resp2.headers['Location'] == loc
    assert len(sign_calls) == 1


def test_media_proxy_redirect_still_requires_valid_hmac(monkeypatch):
    """Authorization order: the HMAC token is verified BEFORE any signing or
    cache lookup — a tampered token must 403 without one sign call."""
    app = _app(SUPABASE_SERVICE_KEY='test-key')
    sign_calls = _stub_sign(monkeypatch)
    _, path = _proxy_path(app, _PUBLIC + 'students/documents/x.png')

    tampered = path[:-2] + ('00' if path[-2:] != '00' else '11')
    assert app.test_client().get(tampered).status_code == 403
    assert app.test_client().get(
        '/media-proxy/uploads/students/documents/x.png').status_code == 403
    assert len(sign_calls) == 0                   # never signed for a bad token


def test_media_proxy_falls_back_to_streaming_when_sign_unavailable(monkeypatch):
    """No service key → signing impossible → previous authenticated streaming
    path (public-bucket fallback) still serves the bytes. Fail closed, never a
    raw-URL redirect."""
    app = _app()                                   # SUPABASE_SERVICE_KEY=''
    _stub_fetch(monkeypatch)
    _, path = _proxy_path(app, _PUBLIC + 'students/documents/x.png')
    resp = app.test_client().get(path)
    assert resp.status_code == 200
    assert resp.data == b'DATA'


def test_media_proxy_flag_off_restores_streaming(monkeypatch):
    """MEDIA_PROXY_REDIRECT_ENABLED=False is the instant rollback: bytes stream
    through the proxy exactly as before, even when signing would succeed."""
    app = _app(SUPABASE_SERVICE_KEY='test-key',
               MEDIA_PROXY_REDIRECT_ENABLED=False)
    sign_calls = _stub_sign(monkeypatch)
    fetch_calls = _stub_fetch(monkeypatch)
    _, path = _proxy_path(app, _PUBLIC + 'students/documents/x.png')
    resp = app.test_client().get(path)
    assert resp.status_code == 200
    assert resp.data == b'DATA'
    assert len(sign_calls) == 0
    assert len(fetch_calls) >= 1


# ── 4. async_dispatch ─────────────────────────────────────────────────────────

def test_dispatch_runs_inline_in_testing_mode():
    app = _app()
    from app.services import async_dispatch
    seen = []
    with app.app_context():
        queued = async_dispatch.submit(seen.append, 'ran')
    assert queued is False                        # inline (testing mode)
    assert seen == ['ran']                        # executed exactly once


def test_dispatch_swallows_task_exceptions():
    app = _app()
    from app.services import async_dispatch

    def boom():
        raise RuntimeError('task failure must never reach the request')

    with app.app_context():
        async_dispatch.submit(boom)               # must not raise


def test_dispatch_background_runs_in_app_context():
    """With testing mode off, the task runs on the pool inside a fresh app
    context (current_app resolves; result delivered across threads)."""
    app = _app()
    app.testing = False
    app.config['TESTING'] = False
    from app.services import async_dispatch

    done = threading.Event()
    result = {}

    def task(tag):
        from flask import current_app
        result['app_name'] = current_app.name
        result['tag'] = tag
        result['thread'] = threading.current_thread().name
        done.set()

    with app.app_context():
        queued = async_dispatch.submit(task, 'bg')
    assert queued is True
    assert done.wait(timeout=10), 'background task did not run'
    assert result['tag'] == 'bg'
    assert result['app_name'] == 'app'
    assert result['thread'].startswith('mecha-dispatch')
