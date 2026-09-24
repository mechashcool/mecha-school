"""Offline stand-in for firebase_admin.messaging.

Exposes exactly the surface app/services/fcm_service.py uses, so the production
send path runs unmodified: Message, Notification, AndroidConfig,
AndroidNotification, APNSConfig, APNSPayload, Aps and send().

send() returns a message id shaped like the real one, which fcm_service treats
as an unambiguous success. No socket is opened; this module imports nothing
that can perform I/O beyond the local ledger file.

The error classes carry the same NAMES the production classifier keys on
(UnregisteredError, SenderIdMismatchError, QuotaExceededError), so the opt-in
failure modes exercise the real classification code rather than a stub of it.
"""
from __future__ import annotations

import uuid

from . import ledger, mode


class FirebaseError(Exception):
    def __init__(self, message='', code=None):
        super().__init__(message)
        self.code = code


class UnregisteredError(FirebaseError):
    """Permanent: this registration token is dead."""


class SenderIdMismatchError(FirebaseError):
    """Permanent: token belongs to a different sender."""


class QuotaExceededError(FirebaseError):
    """Transient: back off and retry."""


class UnavailableError(FirebaseError):
    """Transient."""


class _Payload:
    """Inert value object. Stores what it was given and does nothing with it."""
    __slots__ = ('kwargs',)

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __repr__(self):                     # never renders a token
        return f'<{type(self).__name__}>'


class Notification(_Payload):
    pass


class AndroidNotification(_Payload):
    pass


class AndroidConfig(_Payload):
    pass


class Aps(_Payload):
    pass


class APNSPayload(_Payload):
    pass


class APNSConfig(_Payload):
    pass


class Message:
    __slots__ = ('token', 'notification', 'android', 'apns', 'data')

    def __init__(self, token=None, notification=None, android=None,
                 apns=None, data=None):
        self.token = token
        self.notification = notification
        self.android = android
        self.apns = apns
        self.data = data or {}

    def __repr__(self):                     # never renders the token
        return '<Message>'


def send(message, dry_run=False, app=None):
    """Pretend to deliver. Never touches the network.

    Default mode returns a message id, which fcm_service records as a success.
    The opt-in failure modes raise the real exception TYPES so the production
    permanent/transient classifier is the thing under test.
    """
    token = getattr(message, 'token', None)
    m = mode()
    if m == 'unregistered':
        ledger().record(token, ok=False)
        raise UnregisteredError('Requested entity was not found.',
                                code='NOT_FOUND')
    if m == 'senderid':
        ledger().record(token, ok=False)
        raise SenderIdMismatchError('SenderId mismatch',
                                    code='SENDER_ID_MISMATCH')
    if m == 'transient':
        ledger().record(token, ok=False)
        raise UnavailableError('The service is currently unavailable.',
                               code='UNAVAILABLE')
    fp = ledger().record(token, ok=True)
    return f'projects/attlt-fake/messages/{fp}-{uuid.uuid4().hex[:12]}'


def send_each(messages, dry_run=False, app=None):
    raise NotImplementedError('attlt fake: only send() is used by fcm_service')


def send_multicast(message, dry_run=False, app=None):
    raise NotImplementedError('attlt fake: only send() is used by fcm_service')
