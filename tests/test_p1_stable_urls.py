"""P1 tests — stable HMAC signed-URL windows (make_remote_token quantisation).

Verifies:
  * two mints for the same object inside one window produce a byte-identical
    /media-proxy URL (this is what makes client HTTP/image caches effective),
  * the quantised expiry stays within [ttl, 2*ttl) and still verifies,
  * a token minted for one object NEVER verifies for another (object binding
    is unchanged by quantisation — isolation preserved),
  * SIGNED_URL_STABLE_WINDOWS=false restores the per-request expiry (rollback).

No database and no live network.
"""
import time

from app import create_app


def _app(**overrides):
    app = create_app('testing')
    app.config.update(
        PRIVATE_UPLOADS_ENABLED=True,
        SUPABASE_URL='https://example.supabase.co',
        SUPABASE_SERVICE_KEY='',
        SUPABASE_BUCKET='uploads',
        SERVER_NAME='localhost',
        PREFERRED_URL_SCHEME='http',
    )
    app.config.update(overrides)
    return app


def test_urls_are_identical_within_a_window_and_still_verify():
    app = _app()
    from app.utils.upload_access import (
        make_remote_token, signed_proxy_url, verify_remote_token,
    )
    with app.test_request_context('/'):
        url1 = signed_proxy_url('uploads', 'students/a.png', ttl=900)
        url2 = signed_proxy_url('uploads', 'students/a.png', ttl=900)
        if url1 != url2:
            # The two mints straddled a window boundary (rare) — remint inside
            # the fresh window; they must now be identical.
            url1 = signed_proxy_url('uploads', 'students/a.png', ttl=900)
            url2 = signed_proxy_url('uploads', 'students/a.png', ttl=900)
        assert url1 == url2                     # byte-identical → cacheable

        exp, sig = make_remote_token('uploads', 'students/a.png', 900)
        now = int(time.time())
        assert int(exp) % 900 == 0              # window-aligned expiry
        assert 900 <= int(exp) - now < 1800     # validity within [ttl, 2*ttl)
        assert verify_remote_token('uploads', 'students/a.png', exp, sig)


def test_token_binding_to_one_object_is_unchanged():
    """Quantisation shares the WINDOW across objects, never the SIGNATURE:
    object A's token must not grant access to object B."""
    app = _app()
    from app.utils.upload_access import make_remote_token, verify_remote_token
    with app.test_request_context('/'):
        exp_a, sig_a = make_remote_token('uploads', 'schools/1/a.png', 900)
        exp_b, sig_b = make_remote_token('uploads', 'schools/2/b.png', 900)
        assert sig_a != sig_b                                    # per-object signature
        assert not verify_remote_token('uploads', 'schools/1/a.png', exp_b, sig_b)
        assert not verify_remote_token('uploads', 'schools/2/b.png', exp_a, sig_a)
        # Cross-bucket binding too.
        assert not verify_remote_token('school-media', 'schools/1/a.png', exp_a, sig_a)


def test_flag_off_restores_per_request_expiry():
    app = _app(SIGNED_URL_STABLE_WINDOWS=False)
    from app.utils.upload_access import make_remote_token, verify_remote_token
    with app.test_request_context('/'):
        now = int(time.time())
        exp, sig = make_remote_token('uploads', 'x.png', 900)
        assert 0 <= int(exp) - now - 900 <= 2   # exp == now + ttl (clock slack)
        assert verify_remote_token('uploads', 'x.png', exp, sig)
