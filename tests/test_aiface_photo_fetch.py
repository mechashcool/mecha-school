"""
AI Face device photo-fetch tests (prepare_photo_for_device).

Regression guard for the private-bucket cutover: student/employee photo rows
store full Supabase URLs shaped .../object/public/<bucket>/<key>. Once the
buckets are Private, a plain GET of that stored URL returns 400 and the device
sync silently enrolled people WITHOUT a photo. prepare_photo_for_device must
fetch Supabase objects server-side via _supabase_fetch (service key first,
public endpoint fallback) so the face photo actually reaches the device.

Covers:
  * private bucket → authenticated fetch with the service key succeeds,
  * public bucket, no service key → public-endpoint fallback still works,
  * legacy relative 'uploads/…' row missing locally (ephemeral fs) → bytes
    recovered from the uploads bucket,
  * non-Supabase external URL → direct GET path unchanged,
  * object missing everywhere → clean 'file_not_found' error (no photo sent).

No database and no live network: requests.get is stubbed.
"""
import io

import requests

from app import create_app


_SUPA = 'https://jizrarelapmzrjajrtfo.supabase.co'
_PUBLIC = _SUPA + '/storage/v1/object/public/uploads/'


def _app(service_key='service-key-123'):
    app = create_app('testing')
    app.config.update(
        SUPABASE_URL=_SUPA,
        SUPABASE_SERVICE_KEY=service_key,
        SUPABASE_BUCKET='uploads',
        SUPABASE_STORAGE_BUCKET_MEDIA='school-media',
        SERVER_NAME='localhost',
        PREFERRED_URL_SCHEME='http',
    )
    return app


def _jpeg_bytes():
    from PIL import Image
    img = Image.new('RGB', (100, 120), 'white')
    buf = io.BytesIO()
    img.save(buf, format='JPEG')
    return buf.getvalue()


class _Resp:
    def __init__(self, status=200, content=b'', ctype='image/jpeg'):
        self.status_code = status
        self.content = content
        self.headers = {'Content-Type': ctype}


def _install_stub(monkeypatch, *, auth_ok, public_ok, external_ok=False):
    """Stub requests.get; record calls; grant 200 per endpoint class."""
    jpeg = _jpeg_bytes()
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append({'url': url, 'headers': headers or {}})
        if '/storage/v1/object/public/' in url:
            return _Resp(200, jpeg) if public_ok else _Resp(400)
        if '/storage/v1/object/' in url:
            authed = bool((headers or {}).get('Authorization'))
            return _Resp(200, jpeg) if (auth_ok and authed) else _Resp(400)
        return _Resp(200, jpeg) if external_ok else _Resp(404)

    monkeypatch.setattr(requests, 'get', fake_get)
    return calls


# ── Private bucket: stored public-shaped URL must be fetched with the key ─────

def test_private_bucket_photo_fetched_with_service_key(monkeypatch):
    app = _app()
    calls = _install_stub(monkeypatch, auth_ok=True, public_ok=False)
    from app.services.aiface_sync import prepare_photo_for_device

    stored = _PUBLIC + 'students/abc123.jpg'
    with app.app_context():
        jpeg, info = prepare_photo_for_device(stored, 'student_1')

    assert jpeg, f'expected photo bytes, got error={info.get("error")}'
    assert info['source'] == 'supabase'
    assert info['resolved'] == 'uploads/students/abc123.jpg'
    # The authenticated endpoint was used and carried the service key.
    auth_calls = [c for c in calls
                  if '/object/uploads/students/abc123.jpg' in c['url']]
    assert auth_calls and auth_calls[0]['headers'].get('Authorization')
    # The raw stored public URL was never relied on for the final bytes.
    assert info.get('error') is None


# ── Public bucket, no service key: public fallback keeps working ──────────────

def test_public_bucket_without_key_uses_public_fallback(monkeypatch):
    app = _app(service_key='')
    _install_stub(monkeypatch, auth_ok=False, public_ok=True)
    from app.services.aiface_sync import prepare_photo_for_device

    stored = _PUBLIC + 'students/abc123.jpg'
    with app.app_context():
        jpeg, info = prepare_photo_for_device(stored, 'student_1')

    assert jpeg
    assert info['source'] == 'supabase'


# ── Legacy relative row, file gone from local disk (ephemeral fs) ─────────────

def test_relative_path_missing_locally_recovered_from_bucket(monkeypatch):
    app = _app()
    _install_stub(monkeypatch, auth_ok=True, public_ok=False)
    from app.services.aiface_sync import prepare_photo_for_device

    with app.app_context():
        jpeg, info = prepare_photo_for_device(
            'uploads/students/not-on-disk.jpg', 'student_2')

    assert jpeg
    assert info['source'] == 'supabase_uploads_fallback'
    assert info['resolved'] == 'uploads/students/not-on-disk.jpg'


# ── Non-Supabase external URL: direct GET path unchanged ──────────────────────

def test_external_non_supabase_url_uses_direct_get(monkeypatch):
    app = _app()
    _install_stub(monkeypatch, auth_ok=False, public_ok=False, external_ok=True)
    from app.services.aiface_sync import prepare_photo_for_device

    with app.app_context():
        jpeg, info = prepare_photo_for_device(
            'https://example.com/media/photo.jpg', 'student_3')

    assert jpeg
    assert info['source'] == 'url'


# ── Object missing everywhere: clean error, no photo ──────────────────────────

def test_missing_everywhere_reports_file_not_found(monkeypatch):
    app = _app()
    _install_stub(monkeypatch, auth_ok=False, public_ok=False)
    from app.services.aiface_sync import prepare_photo_for_device

    with app.app_context():
        jpeg, info = prepare_photo_for_device(
            'uploads/students/gone.jpg', 'student_4')

    assert jpeg is None
    assert info['error'] == 'file_not_found'
