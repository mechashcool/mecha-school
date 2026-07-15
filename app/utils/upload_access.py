"""
Central upload-access policy — "private by default, public by explicit allowlist".

Stage 1 (safe, additive foundation)
───────────────────────────────────
This module is the single source of truth for deciding whether an uploaded file
is PUBLIC (branding / school identity, needed before login) or PRIVATE (student /
employee / parent / financial documents, attachments, receipts, personal photos).

It provides:
  * ``is_public_upload`` / ``is_private_upload`` — path-based classification.
  * ``resolve_upload_owner`` — find the owning record + school for a stored value
    by searching every model that persists an upload column.
  * ``can_access_upload`` — authorise a user against a stored value (school +
    role + parent-child / teacher-scope isolation).
  * ``protected_upload_url`` — the URL web templates should use to render a file;
    public/external files keep their existing URL, private local files are routed
    through the authenticated ``/files/`` endpoint.
  * ``make_signed_token`` / ``verify_signed_token`` — HMAC, time-limited tokens
    for future credential-less (mobile) access.

Scope boundary
──────────────
Stage 1 governs only files that Flask itself serves from the local filesystem
(``app/static/uploads/...``). Values stored as full public Supabase / CDN URLs are
returned unchanged — closing that path requires the Stage 2 private-bucket +
signed-URL cutover and is intentionally NOT done here, to avoid breaking the
production web app and the mobile app. See the task write-up for Stage 2.

Default-deny: when ownership cannot be proven for a PRIVATE value the answer is
"no access". Public branding assets are never over-blocked.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

from flask import current_app, url_for


# ── Public allowlist ──────────────────────────────────────────────────────────
# ONLY school identity / branding assets are public. They are required on the
# login page and the app splash before authentication and carry no personal data.
# Uploads are written under 'uploads/schools/<id>/identity/...'.
_PUBLIC_PATH_SEGMENTS = ('/identity/',)
_PUBLIC_PATH_PREFIXES: tuple[str, ...] = ()


def object_path_of(value: str | None) -> str | None:
    """Normalise any stored upload value to a static-relative ``uploads/...`` path.

    Handles every legacy shape so existing DB values keep working:
      - ``uploads/sub/file.ext``            → unchanged
      - ``/static/uploads/sub/file.ext``    → ``uploads/sub/file.ext``
      - bare ``file.ext`` (legacy)          → ``uploads/file.ext``
    Returns ``None`` for empty values or external (http/https) URLs — those are
    not served by Flask and are handled by the caller as pass-through.
    """
    if not value:
        return None
    v = value.strip()
    if v.startswith(('http://', 'https://')):
        return None
    v = v.lstrip('/')
    if v.startswith('static/'):
        v = v[len('static/'):]
    if not v:
        return None
    if '/' not in v:
        # Legacy bare filename stored without a directory prefix.
        v = f'uploads/{v}'
    return v


def is_public_upload(value: str | None) -> bool:
    """True only for allowlisted branding / identity assets (or empty values).

    External URLs return True here in the sense of "not a locally-guarded path";
    callers that must distinguish should check ``object_path_of`` first.
    """
    op = object_path_of(value)
    if op is None:
        # Empty or external URL — no local guard applies.
        return True
    if not op.startswith('uploads/'):
        return False
    low = op.lower()
    if any(seg in low for seg in _PUBLIC_PATH_SEGMENTS):
        return True
    if any(low.startswith(p) for p in _PUBLIC_PATH_PREFIXES):
        return True
    return False


def is_private_upload(value: str | None) -> bool:
    """True when the value is a locally-served file that is NOT public."""
    op = object_path_of(value)
    if op is None:
        return False
    return not is_public_upload(op)


# ── Ownership resolution ──────────────────────────────────────────────────────

def _student_ids_for_parent(user) -> set:
    from app.models import db, parent_students
    try:
        rows = db.session.execute(
            db.select(parent_students.c.student_id)
            .where(parent_students.c.user_id == user.id)
        ).all()
        return {r[0] for r in rows}
    except Exception:
        return set()


def resolve_upload_owner(value: str | None) -> dict | None:
    """Find the record that references ``value`` and return its ownership scope.

    Searches every model that stores an upload column. Uses
    ``bypass_tenant_scope`` + ``include_all_years`` so the TRUE owner is found
    regardless of the caller's current school / year scope; the school check is
    then enforced explicitly by :func:`can_access_upload`.

    Returns a dict with keys ``school_id``, ``student_id``, ``employee_id``,
    ``parent_id``, ``section_id``, ``kind`` — or ``None`` when no row references
    the value (which callers treat as "deny").
    """
    import app.models as m

    op = object_path_of(value)
    candidates = [c for c in {value, op} if c]
    if not candidates:
        return None

    def first(model_name: str, column_name: str):
        model = getattr(m, model_name, None)
        if model is None:
            return None
        col = getattr(model, column_name, None)
        if col is None:
            return None
        try:
            return (model.query
                    .execution_options(bypass_tenant_scope=True, include_all_years=True)
                    .filter(col.in_(candidates))
                    .first())
        except Exception:
            return None

    def owner(row, *, student_id=None, employee_id=None, parent_id=None,
              section_id=None, kind=''):
        return {
            'school_id':   getattr(row, 'school_id', None),
            'student_id':  student_id,
            'employee_id': employee_id,
            'parent_id':   parent_id,
            'section_id':  section_id,
            'kind':        kind,
        }

    # Order: most-specific document tables first, then photos, then attachments.
    row = first('StudentDocument', 'file_path')
    if row:
        return owner(row, student_id=getattr(row, 'student_id', None), kind='student_document')

    row = first('EmployeeDocument', 'file_path')
    if row:
        return owner(row, employee_id=getattr(row, 'employee_id', None), kind='employee_document')

    row = first('LeaveRequest', 'attachment_path')
    if row:
        return owner(row, student_id=getattr(row, 'student_id', None),
                     parent_id=getattr(row, 'parent_id', None), kind='leave_request')

    row = first('EmployeeLeaveRequest', 'attachment_path')
    if row:
        return owner(row, employee_id=getattr(row, 'employee_id', None), kind='employee_leave_request')

    row = first('Complaint', 'attachment_path')
    if row:
        return owner(row, student_id=getattr(row, 'student_id', None),
                     parent_id=getattr(row, 'parent_id', None), kind='complaint')

    row = first('Expense', 'receipt')
    if row:
        return owner(row, kind='expense_receipt')

    row = first('Homework', 'attachment_path')
    if row:
        return owner(row, section_id=getattr(row, 'section_id', None), kind='homework')

    row = first('Student', 'photo')
    if row:
        return owner(row, student_id=getattr(row, 'id', None), kind='student_photo')

    row = first('Employee', 'photo')
    if row:
        return owner(row, employee_id=getattr(row, 'id', None), kind='employee_photo')

    return None


# ── Authorisation ─────────────────────────────────────────────────────────────

def _teacher_can_access(user, owner: dict) -> bool:
    """Teacher scope: own employee-tied files, homework in-school, or a student in
    one of the teacher's assigned sections."""
    from app.models import Employee, Student
    from app.utils.decorators import get_teacher_section_ids

    emp_id = owner.get('employee_id')
    if emp_id is not None:
        emp = (Employee.query.execution_options(bypass_tenant_scope=True)
               .filter_by(id=emp_id).first())
        return bool(emp and emp.user_id == user.id)

    if owner.get('kind') == 'homework':
        # Same-school already verified by the caller; teachers may view homework.
        return True

    stu_id = owner.get('student_id')
    if stu_id is None:
        return False
    student = (Student.query.execution_options(bypass_tenant_scope=True)
               .filter_by(id=stu_id).first())
    if not student or student.section_id is None:
        return False
    return student.section_id in get_teacher_section_ids(user)


def can_access_upload(user, value: str | None) -> bool:
    """Authorise ``user`` to read the file identified by the stored ``value``.

    Enforces, in order: authenticated → same school (super-admin excepted) →
    role rules (investor never; parent only own children; teacher only assigned
    scope) → building scope for student-tied files → same-school staff allowed.
    Default-deny when ownership is unknown.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False

    owner = resolve_upload_owner(value)
    if owner is None:
        return False  # unknown owner → private default-deny

    school_id = owner.get('school_id')

    # Super admin keeps its intentional cross-school capability.
    if getattr(user, 'is_super_admin', False):
        return True

    # School isolation — the core tenant boundary.
    if school_id is not None and getattr(user, 'school_id', None) != school_id:
        return False

    role = user.role.name if getattr(user, 'role', None) else ''

    # Read-only investor accounts never access documents.
    if role == 'investor_viewer':
        return False

    # Parent — only files tied to one of their explicitly linked children.
    if role == 'parent':
        stu_id = owner.get('student_id')
        if stu_id is None:
            return False
        return stu_id in _student_ids_for_parent(user)

    # Teacher — only their assigned scope / own files.
    if role == 'teacher':
        return _teacher_can_access(user, owner)

    # Other same-school staff / admins: allowed, with building-scope narrowing
    # for student-tied files (mirrors existing page-level access control).
    stu_id = owner.get('student_id')
    if stu_id is not None:
        from app.models import Student
        from app.utils.decorators import get_current_school
        from app.utils.buildings import user_can_access_student
        student = (Student.query.execution_options(bypass_tenant_scope=True)
                   .filter_by(id=stu_id).first())
        return user_can_access_student(user, get_current_school(), student)

    return True


# ── URL building for templates ────────────────────────────────────────────────

def protected_upload_url(value: str | None) -> str | None:
    """Return the URL a web template should use to link an uploaded file.

    - Empty                 → ``None``
    - External Supabase URL → signed/proxied when private uploads are enabled
      (Stage 2); otherwise unchanged.
    - Other external URL    → unchanged
    - Public local asset    → existing ``/static`` resolution (unchanged behaviour)
    - Private local file    → the authenticated ``/files/`` route
    """
    if not value:
        return None

    # ── Feature OFF → byte-for-byte pre-security behaviour ────────────────────
    # No /files/ route, no ownership resolver, no Supabase code path, no service
    # key required. Local files link straight to /static (as the original
    # template expressions did); external URLs are returned unchanged.
    if not current_app.config.get('PRIVATE_UPLOADS_ENABLED'):
        if value.startswith(('http://', 'https://')):
            return value
        return url_for('static', filename=(object_path_of(value) or value))

    # ── Feature ON → Stage 1/2 privacy behaviour ─────────────────────────────
    # Route every mappable upload (full Supabase URL OR relative uploads/… path)
    # through the signed proxy / public-branding resolver so nothing links to the
    # unsigned /files/ route while the feature is on.
    signed = supabase_media_url(value)
    if signed is not None:
        return signed
    if value.startswith(('http://', 'https://')):
        return value                            # external non-Supabase URL
    from app.utils.helpers import resolve_photo_url
    op = object_path_of(value)
    if op is None or is_public_upload(op):
        return resolve_photo_url(value)         # public branding/identity
    return url_for('media.serve_protected', stored=op)  # last-resort local


# ── Stage 2: Supabase object references & privacy-aware signing ────────────────

def storage_ref_of(value: str | None) -> tuple[str, str] | None:
    """Parse a Supabase Storage URL into ``(bucket, object_path)``.

    Recognises public (``/object/public/<bucket>/<path>``) and signed
    (``/object/sign/<bucket>/<path>?token=…``) URL shapes, and the plain
    authenticated object form. Returns ``None`` for empty values or any
    non-Supabase / local value (callers then use their legacy path).
    """
    if not value or not value.startswith(('http://', 'https://')):
        return None
    for marker in ('/storage/v1/object/public/',
                   '/storage/v1/object/sign/',
                   '/storage/v1/object/'):
        idx = value.find(marker)
        if idx != -1:
            remainder = value[idx + len(marker):].split('?', 1)[0]
            bucket, _, path = remainder.partition('/')
            if bucket and path:
                return bucket, path
    return None


def is_public_ref(bucket: str, object_path: str) -> bool:
    """True only when a Supabase object actually resides in the dedicated
    public-branding bucket (the fixed, global, pre-login assets).

    School identity/logo objects are private school data and live in the
    school-media bucket regardless of path — they must resolve to a signed
    URL, never to a public-branding URL (that bucket never contains them,
    so guessing that mapping 404s). See ``supabase_media_url`` for how
    school-media objects — identity included — are signed.
    """
    pub = current_app.config.get('SUPABASE_PUBLIC_BRANDING_BUCKET', 'public-branding')
    return bucket == pub


def _public_branding_url(object_path: str) -> str | None:
    """Public URL for an object that already resides in the public-branding
    bucket (the fixed global login assets). Only called when ``is_public_ref``
    confirmed the object's own bucket is public-branding — never guessed for
    objects that live elsewhere (e.g. school-media identity)."""
    base = (current_app.config.get('SUPABASE_URL', '') or '').rstrip('/')
    pub = current_app.config.get('SUPABASE_PUBLIC_BRANDING_BUCKET', 'public-branding')
    if not base:
        return None
    return f"{base}/storage/v1/object/public/{pub}/{object_path}"


def make_remote_token(bucket: str, object_path: str, ttl: int = 900) -> tuple[str, str]:
    """HMAC token authorising proxy access to a private Supabase object.

    P1 — stable windows: the expiry is quantised to fixed windows (window =
    ``ttl``) so every mint for the same object inside one window yields the
    SAME ``exp``/``sig`` and therefore a byte-identical URL. This is what lets
    client-side HTTP/image caches hit instead of re-downloading the same photo
    on every API response. Remaining validity is always within [ttl, 2*ttl);
    ``verify_remote_token`` is unchanged.

    Security: quantisation changes WHEN the token expires, never WHAT it
    authorises — the signature still binds exactly one (bucket, object_path)
    and callers still mint it only AFTER route-level ownership checks. Set
    SIGNED_URL_STABLE_WINDOWS=false to restore per-request expiries.
    """
    now = int(time.time())
    ttl = int(ttl)
    if ttl >= 60 and current_app.config.get('SIGNED_URL_STABLE_WINDOWS', True):
        window = ttl
        exp_val = ((now // window) + 1) * window + ttl
    else:
        exp_val = now + ttl
    exp = str(exp_val)
    return exp, _sign(f'{bucket}|{object_path}|{exp}')


def verify_remote_token(bucket: str, object_path: str,
                        exp: str | None, sig: str | None) -> bool:
    """Constant-time verify a proxy token (path + expiry bound to SECRET_KEY)."""
    if not exp or not sig:
        return False
    try:
        if int(exp) < int(time.time()):
            return False
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(_sign(f'{bucket}|{object_path}|{exp}'), sig)


def signed_proxy_url(bucket: str, object_path: str, ttl: int = 900) -> str:
    """Absolute Flask-HMAC proxy URL for a small private Supabase object.

    Absolute so both web ``<img>`` and the Flutter app (which fetches without a
    session / JWT header) can open it; authorisation is carried by the signature.
    """
    exp, sig = make_remote_token(bucket, object_path, ttl)
    scheme = current_app.config.get('PREFERRED_URL_SCHEME', 'https')
    try:
        return url_for('media.serve_remote', bucket=bucket, object_path=object_path,
                       exp=exp, sig=sig, _external=True, _scheme=scheme)
    except Exception:
        return url_for('media.serve_remote', bucket=bucket, object_path=object_path,
                       exp=exp, sig=sig)


def _local_ref_of(value: str | None) -> tuple[str, str] | None:
    """Map a static-relative ``uploads/<key>`` value to ``(uploads_bucket, key)``.

    Legacy rows and Supabase-upload fallbacks store the local static-relative
    path (``uploads/students/documents/<file>``) rather than a full Supabase URL.
    The object key *inside* the ``uploads`` bucket is that path with the leading
    ``uploads/`` static-folder prefix removed. Returns ``None`` for anything that
    is not an ``uploads/`` value (public identity, external URLs, etc.).

    Only the ``uploads`` bucket is inferred: the local fallback always writes
    under ``uploads/`` regardless of the target bucket, so a relative value
    cannot be attributed to ``school-media``; board media is stored as full
    Supabase URLs and handled by ``storage_ref_of`` instead.
    """
    op = object_path_of(value)
    if op is None or not op.startswith('uploads/'):
        return None
    key = op[len('uploads/'):]
    if not key:
        return None
    return current_app.config.get('SUPABASE_BUCKET', 'uploads'), key


def supabase_media_url(value: str | None, *, want_video: bool = False) -> str | None:
    """Privacy-aware URL for an uploaded object, or ``None`` to signal "not a
    mappable upload / feature disabled — use the caller's legacy path".

    - Feature off (PRIVATE_UPLOADS_ENABLED False) → ``None`` (legacy behaviour).
    - Full Supabase URL  → parsed to (bucket, key).
    - Relative uploads/…  → mapped to the uploads bucket (key without prefix),
      so legacy/fallback values also stream through the signed proxy.
    - Public branding (fixed global login assets, public-branding bucket)
      → public URL (public bucket).
    - School-media (board media AND school identity/logo alike) → Supabase-
      native signed URL. Identity is private school data, not global branding.
    - Other private files (uploads bucket, …)      → Flask-HMAC proxy URL.
    - Non-mappable (external non-Supabase, public local) → ``None``.
    """
    if not current_app.config.get('PRIVATE_UPLOADS_ENABLED'):
        return None
    ref = storage_ref_of(value)
    if ref is None:
        ref = _local_ref_of(value)
    if ref is None:
        return None
    bucket, path = ref

    if is_public_ref(bucket, path):
        return _public_branding_url(path)

    media = current_app.config.get('SUPABASE_STORAGE_BUCKET_MEDIA', 'school-media')
    if want_video or bucket == media:
        # Board media → Supabase-native signed URL (streams from the CDN).
        # P0: the sign call is a network round-trip, so successful results are
        # cached per (bucket, object_path, ttl) — the URL is object-scoped and
        # authorization for the owning record was already enforced by the
        # serializer that called us (see signed_url_cache module docstring).
        from app.utils import signed_url_cache
        from app.utils.helpers import _supabase_sign
        ttl = current_app.config.get('SIGNED_VIDEO_TTL_SECONDS', 21600)
        signed = signed_url_cache.get(bucket, path, ttl)
        if signed is None:
            signed = _supabase_sign(path, bucket=bucket, ttl=ttl)
            if signed is not None:
                signed_url_cache.put(bucket, path, ttl, signed)
        if signed is not None:
            return signed
        # Fail closed-but-working: NEVER return the raw stored value (a private
        # bucket URL that 400s, or a bare relative path). Degrade to the
        # authenticated Flask HMAC proxy instead — access stays authorized and
        # the URL always resolves (no Range/seek, but secure and functional).
        file_ttl = current_app.config.get('SIGNED_FILE_TTL_SECONDS', 900)
        return signed_proxy_url(bucket, path, ttl=file_ttl)

    # Small private file → Flask-HMAC proxy.
    ttl = current_app.config.get('SIGNED_FILE_TTL_SECONDS', 900)
    return signed_proxy_url(bucket, path, ttl=ttl)


# ── Signed tokens (credential-less access; used by Stage 2 / optional) ─────────

def _sign(message: str) -> str:
    key = (current_app.config.get('SECRET_KEY') or '').encode()
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def make_signed_token(object_path: str, ttl: int = 3600) -> tuple[str, str]:
    """Return ``(exp, sig)`` authorising read access to ``object_path`` until
    ``exp`` (unix seconds). The signature binds the path + expiry to SECRET_KEY."""
    exp = str(int(time.time()) + int(ttl))
    return exp, _sign(f'{object_path}:{exp}')


def verify_signed_token(object_path: str, exp: str | None, sig: str | None) -> bool:
    """Constant-time verify a signed token for ``object_path``. False when missing,
    malformed, expired, or the signature does not match."""
    if not exp or not sig:
        return False
    try:
        if int(exp) < int(time.time()):
            return False
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(_sign(f'{object_path}:{exp}'), sig)
