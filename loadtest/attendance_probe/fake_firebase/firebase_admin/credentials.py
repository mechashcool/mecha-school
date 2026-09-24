"""Offline stand-in for firebase_admin.credentials.

Certificate() deliberately IGNORES whatever it is handed. It never opens, reads
or parses a file, so it is structurally incapable of loading a real
service-account key even if one were somehow pointed at it. That is the point:
the experiment worker must be unable to use a real credential by accident.
"""
from __future__ import annotations


class Base:
    pass


class Certificate(Base):
    def __init__(self, cert=None):
        # Not stored, not read, not parsed. Only its TYPE is remembered so the
        # harness can assert nothing tried to hand us a live credential object.
        self.source_kind = type(cert).__name__
        self.project_id = 'attlt-fake'

    def get_access_token(self):
        raise RuntimeError('attlt fake credentials: no token is ever minted')

    def __repr__(self):
        return '<attlt fake Certificate>'


class ApplicationDefault(Base):
    def __init__(self):
        self.project_id = 'attlt-fake'


class RefreshToken(Base):
    def __init__(self, cert=None):
        self.project_id = 'attlt-fake'
