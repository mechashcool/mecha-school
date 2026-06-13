"""
Mobile API — Teacher Leave Requests
===================================
All routes require:  Authorization: Bearer <access_token>   (role: teacher)

Endpoint map
────────────
GET    /teacher/leave-requests                my leave requests (newest first)
POST   /teacher/leave-requests                create a leave request (multipart)
GET    /teacher/leave-requests/<request_id>   detail of one of my requests
DELETE /teacher/leave-requests/<request_id>   delete one of my PENDING requests

Ownership & isolation
─────────────────────
• Identity is derived only from the JWT user → linked Employee row.
• Flutter-supplied teacher_id / employee_id / user_id / school_id /
  academic_year_id are never trusted.
• Every query is scoped to (employee_id, school_id). A request belonging to
  another teacher or school is reported as not found (no existence disclosure).
• EmployeeLeaveRequest is school-scoped in the ORM tenant guard, so the school
  filter is also applied automatically as defense in depth.
"""
from datetime import datetime as _dt
from datetime import timezone

from flask import g, request

from app.models import db, EmployeeLeaveRequest, Notification
from app.utils.helpers import save_uploaded_file, delete_uploaded_file, resolve_photo_url
from app.utils.scoping import current_academic_year_id

from . import mobile_api_bp
from .utils import jwt_required, role_required, ok, err
from .teacher import _get_employee
from .parent import _LEAVE_TYPES


# ─── Attachment validation config ─────────────────────────────────────────────

_ALLOWED_LEAVE_EXTS = {'pdf', 'jpg', 'jpeg', 'png'}
_MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024  # 15 MB
_LEAVE_BUCKET = 'uploads'


def _sniff_content(file_bytes: bytes, ext: str) -> bool:
    """Verify the actual file content matches the claimed extension.

    Images are parsed with Pillow (format must match the extension); PDFs are
    checked for the %PDF- magic header. Returns True when the content is valid.
    Defends against a renamed / spoofed file (e.g. script.exe -> photo.png).
    """
    if ext == 'pdf':
        return file_bytes[:5] == b'%PDF-'
    if ext in {'jpg', 'jpeg', 'png'}:
        try:
            import io
            from PIL import Image
            with Image.open(io.BytesIO(file_bytes)) as img:
                fmt = (img.format or '').lower()
            allowed = {'jpeg'} if ext in {'jpg', 'jpeg'} else {'png'}
            return fmt in allowed
        except Exception:
            return False
    return False


def _validate_attachment(file):
    """Validate an uploaded attachment FileStorage.

    Returns (ext, None) on success or (None, error_code) on failure.
    Leaves the file stream rewound to position 0 for the subsequent save.
    """
    filename = file.filename or ''
    if '.' not in filename:
        return None, 'invalid_attachment_type'
    ext = filename.rsplit('.', 1)[1].lower()
    if ext not in _ALLOWED_LEAVE_EXTS:
        return None, 'invalid_attachment_type'

    file_bytes = file.read()
    if len(file_bytes) > _MAX_ATTACHMENT_BYTES:
        return None, 'attachment_too_large'
    if not file_bytes:
        return None, 'invalid_attachment_type'
    if not _sniff_content(file_bytes, ext):
        return None, 'invalid_attachment_type'

    file.seek(0)
    return ext, None


# ─── Serialization ────────────────────────────────────────────────────────────

def _leave_dict(r: EmployeeLeaveRequest) -> dict:
    return {
        'id':               r.id,
        'leave_type':       r.leave_type,
        'start_date':       r.from_date.isoformat() if r.from_date else None,
        'end_date':         r.to_date.isoformat() if r.to_date else None,
        'reason':           r.reason,
        'details':          r.details,
        'status':           r.status,
        'admin_response':   r.admin_response,
        'rejection_reason': r.rejection_reason,
        'reviewed_at':      (r.reviewed_at.replace(tzinfo=timezone.utc).isoformat()
                             if r.reviewed_at else None),
        'created_at':       (r.created_at.replace(tzinfo=timezone.utc).isoformat()
                             if r.created_at else None),
        'attachment_url':   resolve_photo_url(r.attachment_path),
        'can_delete':       r.status == 'pending',
    }


def _owned_query(emp):
    """Base query scoped to the authenticated employee + school."""
    return EmployeeLeaveRequest.query.filter_by(
        employee_id=emp.id, school_id=emp.school_id
    )


def _notify_admins(emp, school_id: int) -> None:
    """In-app notification to school administrators of the same school."""
    db.session.add(Notification(
        school_id=school_id,
        title='طلب إجازة جديد',  # طلب إجازة جديد
        body=f'تم تقديم طلب إجازة من {emp.full_name}.',
        ntype='teacher_leave_request',
        target_role='school_admin',
        created_by=g.mobile_user.id,
    ))


# ─── Routes ───────────────────────────────────────────────────────────────────

@mobile_api_bp.route('/teacher/leave-requests', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_leave_requests():
    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)
    rows = (_owned_query(emp)
            .order_by(EmployeeLeaveRequest.created_at.desc())
            .all())
    return ok(leave_requests=[_leave_dict(r) for r in rows])


@mobile_api_bp.route('/teacher/leave-requests', methods=['POST'])
@jwt_required()
@role_required('teacher')
def teacher_create_leave_request():
    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)

    form       = request.form
    leave_type = (form.get('leave_type') or '').strip()
    start_str  = (form.get('start_date') or '').strip()
    end_str    = (form.get('end_date') or '').strip()
    reason     = (form.get('reason') or '').strip()
    details    = (form.get('details') or '').strip() or None

    if not leave_type:
        return err('required_field_missing: leave_type')
    if leave_type not in _LEAVE_TYPES:
        return err('invalid_leave_type')
    if not start_str:
        return err('required_field_missing: start_date')
    if not end_str:
        return err('required_field_missing: end_date')
    if not reason:
        return err('required_field_missing: reason')

    try:
        from_date = _dt.strptime(start_str, '%Y-%m-%d').date()
    except ValueError:
        return err('invalid_date_format: start_date')
    try:
        to_date = _dt.strptime(end_str, '%Y-%m-%d').date()
    except ValueError:
        return err('invalid_date_format: end_date')
    if to_date < from_date:
        return err('end_date_before_start_date')

    # Optional attachment — validated for extension, size, and real content.
    attachment_path = None
    file = request.files.get('attachment')
    if file and file.filename:
        ext, verr = _validate_attachment(file)
        if verr:
            return err(verr)
        subfolder = f'schools/{emp.school_id}/teacher-leave-requests/{emp.id}'
        attachment_path = save_uploaded_file(
            file, subfolder,
            bucket=_LEAVE_BUCKET,
            allowed_exts=_ALLOWED_LEAVE_EXTS,
            max_size=_MAX_ATTACHMENT_BYTES,
        )
        if not attachment_path:
            return err('attachment_upload_failed')

    leave = EmployeeLeaveRequest(
        employee_id=emp.id,
        school_id=emp.school_id,
        academic_year_id=current_academic_year_id(),
        leave_type=leave_type,
        from_date=from_date,
        to_date=to_date,
        reason=reason,
        details=details,
        attachment_path=attachment_path,
        status='pending',
    )
    db.session.add(leave)
    _notify_admins(emp, emp.school_id)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        # Roll back the uploaded object so we don't orphan it in storage.
        if attachment_path:
            delete_uploaded_file(attachment_path, bucket=_LEAVE_BUCKET)
        return err('server_error', 500)

    return ok(leave_request=_leave_dict(leave)), 201


@mobile_api_bp.route('/teacher/leave-requests/<int:request_id>', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_leave_request_detail(request_id):
    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)
    leave = _owned_query(emp).filter_by(id=request_id).first()
    if not leave:
        return err('leave_request_not_found', 404)
    return ok(leave_request=_leave_dict(leave))


@mobile_api_bp.route('/teacher/leave-requests/<int:request_id>', methods=['DELETE'])
@jwt_required()
@role_required('teacher')
def teacher_delete_leave_request(request_id):
    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)
    leave = _owned_query(emp).filter_by(id=request_id).first()
    if not leave:
        return err('leave_request_not_found', 404)
    if leave.status != 'pending':
        return err('cannot_delete_non_pending_request')

    attachment_path = leave.attachment_path
    db.session.delete(leave)
    db.session.commit()
    # Storage cleanup after the DB row is gone. A storage failure is logged
    # internally and never surfaced to the client (no paths / credentials).
    if attachment_path:
        delete_uploaded_file(attachment_path, bucket=_LEAVE_BUCKET)
    return ok(message='Leave request deleted successfully')
