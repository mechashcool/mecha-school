"""
Security-hardening regression tests (audit follow-up).

Covers, without any database or live network:

  * M-1  — grade names are rendered through Jinja's |tojson, which escapes
           </script> so a crafted grade name cannot break out of a <script>
           block (stored-XSS regression).
  * M-2  — ProxyFix makes request.remote_addr reflect the real client IP from
           X-Forwarded-For when behind exactly one trusted proxy.
  * M-4  — authenticated responses receive Cache-Control: no-store; anonymous
           responses do not (public content stays cacheable).
  * Header — a conservative Permissions-Policy is always present.

Probe routes are registered on a freshly-built app per test and never touch the
database, so these run even without a local Postgres.
"""
from flask import render_template_string, request

from app import create_app


def _app():
    return create_app('testing')


# ── M-1: stored XSS via grade name ─────────────────────────────────────────────

def test_grades_tojson_escapes_script_breakout():
    """A grade name containing </script><img onerror> must be neutralised by
    |tojson (the filter the fixed template uses), not break out of <script>."""
    app = _app()
    payload = '</script><img src=x onerror=alert(1)>'
    with app.test_request_context('/'):
        out = render_template_string(
            'var GRADES = {{ grades_data | tojson }};',
            grades_data=[{'id': 1, 'name': payload, 'stage': ''}],
        )
    # The literal closing tag must NOT appear — it is unicode-escaped instead.
    assert '</script>' not in out
    assert '\\u003c' in out            # '<' was escaped to <
    # The data itself is preserved (as an inert JSON string), just made safe.
    assert 'onerror' in out
    assert '"id": 1' in out or '"id":1' in out


# ── M-2: ProxyFix / real client IP ─────────────────────────────────────────────

def test_proxyfix_uses_forwarded_for():
    app = _app()

    @app.route('/__probe_ip')
    def _probe_ip():
        return request.remote_addr or ''

    resp = app.test_client().get(
        '/__probe_ip', headers={'X-Forwarded-For': '203.0.113.7'})
    assert resp.get_data(as_text=True) == '203.0.113.7'


def test_proxyfix_spoofed_chain_is_not_trusted_beyond_one_hop():
    """With x_for=1, only the rightmost (proxy-appended) hop is trusted. A
    client-spoofed left-most XFF entry must NOT become remote_addr."""
    app = _app()

    @app.route('/__probe_ip_chain')
    def _probe_ip_chain():
        return request.remote_addr or ''

    # Simulate Nginx having appended the real peer to a client-supplied value.
    resp = app.test_client().get(
        '/__probe_ip_chain',
        headers={'X-Forwarded-For': '1.2.3.4, 203.0.113.7'})
    assert resp.get_data(as_text=True) == '203.0.113.7'


def test_proxyfix_without_header_falls_back_to_peer():
    app = _app()

    @app.route('/__probe_ip_none')
    def _probe_ip_none():
        return request.remote_addr or ''

    resp = app.test_client().get('/__probe_ip_none')
    # No XFF → the direct peer (test client default), never blank/None.
    assert resp.get_data(as_text=True) == '127.0.0.1'


# ── M-4: Cache-Control no-store for authenticated responses ────────────────────

class _DummyUser:
    is_authenticated = True
    is_active = True
    is_anonymous = False

    def get_id(self):
        return '1'


def test_no_store_on_authenticated_response():
    app = _app()

    @app.route('/__probe_auth')
    def _probe_auth():
        from flask_login import login_user
        login_user(_DummyUser())
        return 'ok'

    resp = app.test_client().get('/__probe_auth')
    cc = resp.headers.get('Cache-Control', '')
    assert 'no-store' in cc and 'private' in cc
    assert resp.headers.get('Pragma') == 'no-cache'
    assert resp.headers.get('Expires') == '0'


def test_no_store_absent_on_anonymous_response():
    app = _app()

    @app.route('/__probe_anon')
    def _probe_anon():
        return 'ok'

    resp = app.test_client().get('/__probe_anon')
    assert 'no-store' not in resp.headers.get('Cache-Control', '')


# ── Permissions-Policy header ──────────────────────────────────────────────────

def test_permissions_policy_header_present():
    app = _app()

    @app.route('/__probe_pp')
    def _probe_pp():
        return 'ok'

    resp = app.test_client().get('/__probe_pp')
    pp = resp.headers.get('Permissions-Policy', '')
    for feature in ('camera=()', 'microphone=()', 'geolocation=()', 'payment=()'):
        assert feature in pp
