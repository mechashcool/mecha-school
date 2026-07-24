"""
Secure token helpers for the external (public) student-registration feature.

Two independent token families, both 256-bit and cryptographically random:

  * School registration token — one per school. Stored as ``sha256`` hash for
    lookup/verification AND Fernet-encrypted for authorized Super-Admin recovery
    (so the active link stays copyable). Regeneration overwrites both, which
    invalidates the previous link immediately.

  * Tracking token — one per request. Stored ONLY as ``sha256`` hash; the raw
    token lives solely in the guardian's URL and is never persisted or logged.

Security notes
--------------
* Verification uses ``hmac.compare_digest`` (constant-time).
* Raw tokens are never logged by this module and must never be logged by callers.
* The Fernet key comes from ``REGISTRATION_TOKEN_KEY`` when set, otherwise it is
  derived deterministically from ``SECRET_KEY`` so development works with no
  extra configuration. Rotating ``SECRET_KEY`` does not break public links (the
  sha256 lookup is unaffected); it only makes stored links non-recoverable for
  copy until regenerated.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import unicodedata

from flask import current_app


# ── Raw token generation & hashing ─────────────────────────────────────────────

def generate_token() -> str:
    """Return a fresh URL-safe 256-bit token (~43 chars)."""
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    """Return the hex sha256 digest used for storage/lookup. Empty → ''."""
    if not raw:
        return ''
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def verify_token(raw: str, stored_hash: str | None) -> bool:
    """Constant-time check that ``raw`` hashes to ``stored_hash``."""
    if not raw or not stored_hash:
        return False
    return hmac.compare_digest(hash_token(raw), stored_hash)


# ── Fernet encryption (recoverable storage for the school link) ─────────────────

def _fernet():
    """Build a Fernet from REGISTRATION_TOKEN_KEY or a SECRET_KEY-derived key."""
    from cryptography.fernet import Fernet

    key = current_app.config.get('REGISTRATION_TOKEN_KEY')
    if key:
        if isinstance(key, str):
            key = key.encode('utf-8')
        return Fernet(key)

    secret = (current_app.config.get('SECRET_KEY') or '').encode('utf-8')
    derived = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(derived)


def encrypt_token(raw: str) -> str:
    """Return the Fernet ciphertext (str) for a raw token."""
    return _fernet().encrypt(raw.encode('utf-8')).decode('ascii')


def decrypt_token(ciphertext: str | None) -> str | None:
    """Return the raw token, or None if missing/undecryptable (e.g. key rotated)."""
    if not ciphertext:
        return None
    try:
        from cryptography.fernet import InvalidToken
        try:
            return _fernet().decrypt(ciphertext.encode('ascii')).decode('utf-8')
        except InvalidToken:
            return None
    except Exception:
        return None


# ── Normalization (Unicode-safe; used for storage + phone matching) ─────────────

# Arabic-Indic (٠-٩) and Extended Arabic-Indic / Persian (۰-۹) → ASCII digits.
_DIGIT_MAP = {ord(a): str(i) for i, a in enumerate('٠١٢٣٤٥٦٧٨٩')}
_DIGIT_MAP.update({ord(a): str(i) for i, a in enumerate('۰۱۲۳۴۵۶۷۸۹')})


def _to_ascii_digits(value: str) -> str:
    return value.translate(_DIGIT_MAP)


def normalize_text(value: str | None) -> str:
    """NFC-normalize, collapse internal whitespace, and strip. Safe for names."""
    if not value:
        return ''
    text = unicodedata.normalize('NFC', str(value))
    return ' '.join(text.split()).strip()


def normalize_name(value: str | None) -> str:
    """Normalized display form of a person/guardian name."""
    return normalize_text(value)


def normalize_phone(value: str | None) -> str:
    """
    Canonical phone form for equality matching: Arabic/Persian digits converted
    to ASCII, all non-digit characters removed (keeps only 0-9). A single
    leading '+' becomes nothing (we compare national digit strings). Empty stays
    empty. Used ONLY for same-school existing-parent matching, never displayed.
    """
    if not value:
        return ''
    ascii_val = _to_ascii_digits(unicodedata.normalize('NFC', str(value)))
    return ''.join(ch for ch in ascii_val if ch.isdigit())
