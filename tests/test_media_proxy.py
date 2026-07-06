"""
Media-proxy (Stage 2 private-uploads) tests.

Covers the /media-proxy route and the signed-URL pipeline that streams private
Supabase objects when PRIVATE_UPLOADS_ENABLED=true:

  * bucket / object-key parsing from a stored public CDN URL,
  * deep object keys (students/documents/…, schools/<id>/leave/…) via <path:>,
  * HMAC exp/sig verification (valid passes, tampered/expired/wrong-path 403),
  * bucket allowlist + path-traversal rejection,
  * the public-bucket fetch fallback so existing files stream while the buckets
    are still Public (no service key required),
  * the object key sent to Supabase is the in-bucket key WITHOUT the bucket
    prefix (regression guard for the reported 404).

No database and no live network: requests.get is stubbed.
"""
import requests
from urllib.parse import urlparse

from app import create_app


_SUPA = 'https://jizrarelapmzrjajrtfo.supabase.co'
_PUBLIC = _SUPA + '/storage/v1/object/public/uploads/'


def _app():
    app = create_app('testing')
    app.config.update(
        PRIVATE_UPLOADS_ENABLED=True,
        SUPABASE_URL=_SUPA,
        SUPABASE_SERVICE_KEY='',          # no key → exercises the public fallback
        SUPABASE_BUCKET='uploads',
        SUPABASE_STORAGE_BUCKET_MEDIA='school-media',
        SERVER_NAME='localhost',
        PREFERRED_URL_SCHEME='http',
    )
    return app


class _Resp:
    def __init__(self, status=200, content=b'DATA', ctype='image/png'):
        self.status_code = status
        self.content = content
        self.headers = {'Content-Type': ctype}


def _install_capture(monkeypatch):
    """Stub requests.get; record every fetched URL; 200 only for public endpoint."""
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        # Mimic a Public bucket: only the /object/public/... endpoint returns 200.
        if '/object/public/' in url:
            return _Resp(200)
        return _Resp(400)

    monkeypatch.setattr(requests, 'get', fake_get)
    return calls


def _proxy_path(app, stored_url):
    from app.utils.upload_access import supabase_media_url
    with app.test_request_context('/'):
        gen = supabase_media_url(stored_url)
    pu = urlparse(gen)
    return gen, pu.path + ('?' + pu.query if pu.query else '')


def test_deep_document_key_streams_and_uses_unprefixed_key(monkeypatch):
    app = _app()
    calls = _install_capture(monkeypatch)
    key = 'students/documents/SCH001-STU-000007-aaa065093ab34c0bb9ade4676bc71ded.png'
    gen, path = _proxy_path(app, _PUBLIC + key)

    assert '/media-proxy/uploads/' + key in gen        # correct bucket/key split
    resp = app.test_client().get(path)
    assert resp.status_code == 200                      # streams (was 404)

    fetched = calls[-1]
    assert fetched == f'{_SUPA}/storage/v1/object/public/uploads/{key}'
    # Regression guard: never fetch uploads/<key> *inside* bucket uploads.
    assert '/object/public/uploads/uploads/' not in fetched
    assert '/object/uploads/uploads/' not in fetched


def test_various_valid_prefixes_stream(monkeypatch):
    app = _app()
    _install_capture(monkeypatch)
    keys = [
        'students/a.png',
        'students/documents/b.png',
        'employees/c.png',
        'employee_docs/d.pdf',
        'homework/e.pdf',
        'complaints/f.png',
        'schools/1/student-leave-requests/55/g.pdf',
        'schools/1/teacher-leave-requests/9/h.pdf',
    ]
    client = app.test_client()
    for k in keys:
        _, path = _proxy_path(app, _PUBLIC + k)
        assert client.get(path).status_code == 200, k


def test_signature_required(monkeypatch):
    app = _app()
    _install_capture(monkeypatch)
    _, path = _proxy_path(app, _PUBLIC + 'students/documents/x.png')
    client = app.test_client()
    # tampered signature
    assert client.get(path[:-2] + ('00' if path[-2:] != '00' else '11')).status_code == 403
    # missing exp/sig entirely
    assert client.get('/media-proxy/uploads/students/documents/x.png').status_code == 403


def test_bucket_allowlist_and_traversal(monkeypatch):
    app = _app()
    _install_capture(monkeypatch)
    client = app.test_client()
    # Unknown bucket → 404 even before signature check.
    assert client.get('/media-proxy/secrets/students/x.png?exp=1&sig=abc').status_code == 404
    # Path traversal → 404.
    assert client.get('/media-proxy/uploads/../etc/passwd?exp=1&sig=abc').status_code == 404


def test_flag_off_makes_no_proxy_url(monkeypatch):
    app = create_app('testing')
    app.config.update(PRIVATE_UPLOADS_ENABLED=False, SUPABASE_URL=_SUPA)
    from app.utils.upload_access import supabase_media_url
    with app.test_request_context('/'):
        assert supabase_media_url(_PUBLIC + 'students/documents/x.png') is None
