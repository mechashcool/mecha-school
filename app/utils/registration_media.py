"""
Public (external) registration media — storage layout of NEW uploads and the
approval-time display copy of the registration photo.

NEW public-registration uploads are validated/prepared before any Storage write
(see app/blueprints/registration) and stored under a versioned ``v2`` prefix:

    registration/<school_id>/photos/v2/<uuid>.<jpg|jpeg|png>   original, byte-identical
    registration/<school_id>/documents/v2/<uuid>.<webp|png|pdf> optimised image / exact PDF

The ``v2`` segment is the ONLY marker that distinguishes a hardened upload from a
legacy one (``registration/<school_id>/photos/<uuid>.<ext>``); there is no
database column for it. Legacy objects are never read, rewritten, moved or
deleted by anything here.

Approval assigns the registration photo to Student.photo unchanged (it stays the
AI Face source). AFTER the approval transaction has committed, and only for a v2
photo, ``create_registration_display_photo`` makes the same display-only WebP
copy a Student create/edit upload gets (app/utils/student_display_photo.py) and
stores it in Student.photo_display. It is best effort: any failure leaves
photo_display NULL and display falls back to Student.photo, as for every
student without a copy. A legacy photo is never downloaded.
"""
from __future__ import annotations

import io
import logging
import os
import re

from flask import current_app

log = logging.getLogger(__name__)

REGISTRATION_UPLOAD_MAX_BYTES = 5 * 1024 * 1024      # photo and each document
_V2 = 'v2'
_V2_PHOTO_KEY = re.compile(
    r'^registration/(\d+)/photos/' + _V2 + r'/[0-9a-f]{32}\.(?:jpg|jpeg|png)$')


def registration_photo_subfolder(school_id: int) -> str:
    return f'registration/{int(school_id)}/photos/{_V2}'


def registration_document_subfolder(school_id: int) -> str:
    return f'registration/{int(school_id)}/documents/{_V2}'


def _v2_photo_location(value: str | None, school_id: int):
    """``('supabase', bucket, key)`` / ``('local', None, key)`` for a v2
    registration photo of ``school_id``; ``None`` for anything else — a legacy
    registration photo, another school's key, or any other value."""
    from app.utils.upload_access import object_path_of, storage_ref_of
    if not value:
        return None
    ref = storage_ref_of(value)
    if ref is not None:
        bucket, key = ref
        if bucket != current_app.config.get('SUPABASE_STORAGE_BUCKET_MEDIA', 'school-media'):
            return None
        location = ('supabase', bucket, key)
    else:
        op = object_path_of(value)                # local fallback: uploads/<key>
        if not op or not op.startswith('uploads/'):
            return None
        key = op[len('uploads/'):]
        location = ('local', None, key)
    match = _V2_PHOTO_KEY.match(key)
    if match is None or int(match.group(1)) != int(school_id):
        return None
    return location


def is_v2_registration_photo(value: str | None, school_id: int) -> bool:
    return _v2_photo_location(value, school_id) is not None


def _read_original(location) -> bytes | None:
    source, bucket, key = location
    if source == 'supabase':
        from app.utils.helpers import _supabase_fetch
        raw, _ = _supabase_fetch(key, bucket=bucket)
        return raw
    # key matched _V2_PHOTO_KEY, so it cannot leave static/uploads/.
    path = os.path.join(current_app.root_path, 'static', 'uploads', *key.split('/'))
    if not os.path.isfile(path):
        return None
    with open(path, 'rb') as fh:
        return fh.read()


def create_registration_display_photo(student_id: int, school_id: int) -> str | None:
    """Best-effort display copy for a student just approved from a v2
    registration photo. Call only AFTER the approval has committed.

    Returns the stored display value, or None (nothing to do, or failure —
    logged). Never raises, never changes Student.photo, never touches a legacy
    registration photo, and never rolls anything back but its own update.
    """
    from app.models import db, Student
    try:
        student = (Student.query
                   .filter_by(id=student_id, school_id=school_id)
                   .first())
        if student is None or student.photo_display:
            return None
        original = student.photo
        location = _v2_photo_location(original, school_id)
        if location is None:
            return None                             # legacy / no photo: untouched

        raw = _read_original(location)
        if not raw or len(raw) > REGISTRATION_UPLOAD_MAX_BYTES:
            log.warning('[registration-photo] original unavailable for student %s; '
                        'display falls back to the original', student_id)
            return None

        from app.utils.student_display_photo import make_display_photo, save_display_photo
        data = make_display_photo(raw)
        stored = save_display_photo(data)
        if not stored:
            return None

        # Short, separate transaction: set the copy only while the student still
        # holds this very original and has no copy yet.
        locked = (Student.query
                  .filter_by(id=student_id, school_id=school_id)
                  .populate_existing()
                  .with_for_update()
                  .first())
        if locked is None or locked.photo != original or locked.photo_display:
            db.session.rollback()
            from app.blueprints.students import _discard_unreferenced_upload
            _discard_unreferenced_upload(stored)
            return None
        locked.photo_display = stored
        db.session.commit()
        return stored
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        log.warning('[registration-photo] display derivative failed for student %s; '
                    'falling back to the original', student_id, exc_info=True)
        return None


def normalize_registration_student_photo(student_id: int, school_id: int) -> str | None:
    """Store a just-approved student's Student.photo in the Face ID form.

    Call only AFTER the approval has committed AND after
    create_registration_display_photo(), so the display copy is still made from
    the ORIGINAL registration photo. For a v2 registration photo this stores the
    640 px / RGB / JPEG q85 copy (app/utils/face_photo.py) as a NEW object in
    Supabase only and points Student.photo at it — only while the student still
    holds that original. The registration request keeps its original object;
    nothing is deleted. Best effort: on any failure Student.photo keeps the
    (existing, valid) original. Never raises; never touches photo_display.
    """
    from app.models import db, Student
    try:
        student = (Student.query
                   .filter_by(id=student_id, school_id=school_id)
                   .first())
        if student is None:
            return None
        original = student.photo
        location = _v2_photo_location(original, school_id)
        if location is None:
            return None                             # legacy / no photo: untouched
        raw = _read_original(location)
        if not raw or len(raw) > REGISTRATION_UPLOAD_MAX_BYTES:
            log.warning('[registration-photo] original unavailable for student %s; '
                        'Student.photo keeps the original', student_id)
            return None

        from werkzeug.datastructures import FileStorage
        from app.utils.face_photo import normalize_face_photo
        from app.utils.helpers import save_uploaded_file
        data = normalize_face_photo(raw)
        stored = save_uploaded_file(
            FileStorage(io.BytesIO(data), filename='photo.jpg', content_type='image/jpeg'),
            'students', local_fallback=False)
        if not stored:
            log.warning('[registration-photo] normalised photo not stored for student %s; '
                        'Student.photo keeps the original', student_id)
            return None

        locked = (Student.query
                  .filter_by(id=student_id, school_id=school_id)
                  .populate_existing()
                  .with_for_update()
                  .first())
        if locked is None or locked.photo != original:
            db.session.rollback()
            from app.blueprints.students import _discard_unreferenced_upload
            _discard_unreferenced_upload(stored)
            return None
        locked.photo = stored
        db.session.commit()
        return stored
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        log.warning('[registration-photo] photo normalisation failed for student %s; '
                    'Student.photo keeps the original', student_id, exc_info=True)
        return None
