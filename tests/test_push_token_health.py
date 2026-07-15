"""Push-delivery token-health tests (push-fix).

Guards the two backend halves of the push-notification fix:

  1. ``_is_stale_token`` — device rows are deactivated ONLY on errors that
     unambiguously identify the registration token as dead/invalid. A bare
     INVALID_ARGUMENT (payload/API problem) must NEVER deactivate a valid
     token: that previously silenced healthy devices permanently, because the
     app only re-registered tokens at login.
  2. ``send_push_batch`` — per-item failures are counted truthfully and one
     bad item never aborts delivery to the remaining users.

No database and no live network.
"""
from app.services.fcm_service import _is_stale_token, send_push_batch


# ── 1. stale-token classification ─────────────────────────────────────────────

def test_unambiguous_token_errors_are_stale():
    for err in (
        'Requested entity was not found',
        'UNREGISTERED',
        'registration-token-not-registered',
        'invalid-registration-token',
        'The registration token is not a valid FCM registration token',
        'INVALID_ARGUMENT: The registration token is not a valid FCM '
        'registration token',
    ):
        assert _is_stale_token(err), err


def test_payload_and_generic_errors_never_deactivate_tokens():
    for err in (
        'INVALID_ARGUMENT',
        'INVALID_ARGUMENT: Invalid JSON payload received.',
        'INVALID_ARGUMENT: The size of the message payload exceeded the '
        'maximum allowed size',
        'INTERNAL',
        'UNAVAILABLE: The service is currently unavailable',
        'Deadline Exceeded',
        '',
    ):
        assert not _is_stale_token(err), err


def test_stale_check_is_case_insensitive():
    assert _is_stale_token('unregistered')
    assert _is_stale_token('REQUESTED ENTITY WAS NOT FOUND')


# ── 2. batch accounting ───────────────────────────────────────────────────────

def test_send_push_batch_counts_and_isolates_failures(monkeypatch):
    import app.services.fcm_service as fcm

    sent_to = []

    def fake_send(user_id, title, body, data):
        sent_to.append(user_id)
        if user_id == 2:
            raise RuntimeError('boom')          # one bad item…
        return (1, 0) if user_id != 3 else (0, 2)

    monkeypatch.setattr(fcm, 'send_push_to_user', fake_send)

    items = [
        (1, 't', 'b', {'k': 'v'}),
        (2, 't', 'b', {'k': 'v'}),              # raises
        (3, 't', 'b', {'k': 'v'}),              # 2 device failures
        (4, 't', 'b', {'k': 'v'}),
    ]
    sent, failed = send_push_batch(items)

    assert sent_to == [1, 2, 3, 4]              # failure never aborts the rest
    assert sent == 2                            # users 1 and 4
    assert failed == 3                          # 1 exception + 2 device fails
