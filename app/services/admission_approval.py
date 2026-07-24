"""
External-registration approval service.

Turns a public StudentRegistrationRequest into a real Student using the SAME
internal creation building blocks as the Add Student wizard (student code, photo,
documents, parent account/link) — but deliberately EXCLUDING fees and attendance
devices. No fee is ever created here.

Guarantees:
  * Atomic + concurrency-safe: the request row is locked (SELECT ... FOR UPDATE)
    so two administrators cannot approve the same request twice. A second attempt
    finds status != 'pending' and returns the existing result idempotently — no
    duplicate Student, parent, link, or notification.
  * Strict school isolation: student, section, documents, and parent all belong
    to the acting staff member's school (also enforced by the ORM write guards).
  * Credentials: a NEW parent gets a securely generated username + initial
    password (returned in memory so the route can show it once to staff). An
    EXISTING parent is only linked — its password is never changed/reset/revealed.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.exc import IntegrityError

from app.models import (db, Role, User, Student, StudentDocument, Section,
                        parent_students, StudentRegistrationRequest)
from app.utils import code_generator
from app.utils.decorators import get_active_year
from app.utils.audit import log_action
from app.utils.registration_tokens import normalize_phone


class ApprovalError(Exception):
    """Raised for a non-retryable approval problem; carries a safe Arabic message."""


def _parent_role():
    return Role.query.filter_by(name='parent').first()


def find_matching_parent(school_id: int, guardian_phone: str | None):
    """Return a same-school parent whose normalized phone matches, or None.

    Used to SUGGEST an existing account to staff. School-scoped: an account from
    another school is never returned.
    """
    target = normalize_phone(guardian_phone)
    if not target:
        return None
    role = _parent_role()
    if not role:
        return None
    candidates = (User.query.execution_options(bypass_tenant_scope=True)
                  .filter_by(school_id=school_id, role_id=role.id, is_active=True)
                  .all())
    for user in candidates:
        if normalize_phone(user.phone) == target:
            return user
    return None


def _link_parent_student(user_id: int, student_id: int, relation: str):
    """Insert the parent↔student link if it does not already exist (idempotent)."""
    exists = db.session.execute(
        db.select(parent_students.c.user_id)
        .where(parent_students.c.user_id == user_id,
               parent_students.c.student_id == student_id)
    ).first()
    if exists:
        return
    db.session.execute(parent_students.insert().values(
        user_id=user_id, student_id=student_id, relation=relation or 'guardian'))


def approve_request(request_id: int, school, actor, *,
                    section_id: int | None = None,
                    link_parent_id: int | None = None) -> dict:
    """
    Approve a pending registration request for ``school``.

    Returns a dict:
      {'ok': True, 'already': bool, 'student_id': int, 'parent_created': bool,
       'parent_username': str|None, 'parent_password': str|None}

    ``parent_password`` is only present for a newly-created account and is held in
    memory for a single one-time display to staff — never persisted or logged.

    Raises ApprovalError (safe Arabic message) for validation problems.
    """
    year = get_active_year(school.id)
    if year is None:
        raise ApprovalError('لا يوجد عام دراسي نشط لهذه المدرسة.')

    # ── Lock the request row to serialize concurrent approvals ────────────────
    req = (StudentRegistrationRequest.query
           .filter_by(id=request_id, school_id=school.id)
           .with_for_update()
           .first())
    if req is None:
        raise ApprovalError('الطلب غير موجود.')

    # ── Idempotency: already processed → return existing result, no side effects
    if req.status == 'approved':
        return {'ok': True, 'already': True, 'student_id': req.approved_student_id,
                'parent_created': False, 'parent_username': None,
                'parent_password': None}
    if req.status == 'rejected':
        raise ApprovalError('تم رفض هذا الطلب مسبقاً ولا يمكن اعتماده.')

    # ── Validate optional section (must belong to this school + active year) ──
    section = None
    if section_id:
        section = (Section.query
                   .filter_by(id=section_id, school_id=school.id,
                              academic_year_id=year.id)
                   .first())
        if section is None:
            raise ApprovalError('الشعبة المحددة غير صالحة لهذه المدرسة.')

    # ── Create the Student (reuse the internal student-code generator) ────────
    student = Student(
        student_id=code_generator.generate_student_id(school.id),
        full_name=req.full_name,
        date_of_birth=req.date_of_birth,
        gender=req.gender,
        nationality=req.nationality,
        address=req.address,
        phone=req.phone,
        photo=req.student_photo_path,
        notes=req.notes,
        section_id=section.id if section else None,
        guardian_name=req.guardian_name,
        guardian_phone=req.guardian_phone,
        guardian_email=req.guardian_email,
        guardian_relation=req.guardian_relation,
        school_id=school.id,
        academic_year_id=year.id,
        status='active',
        enrollment_date=date.today(),
    )
    db.session.add(student)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        raise ApprovalError('تعذّر إنشاء الطالب بسبب تعارض في رقم الطالب. حاول مرة أخرى.')

    # ── Convert request documents into student documents (no re-upload) ───────
    for doc in req.documents.all():
        db.session.add(StudentDocument(
            student_id=student.id,
            school_id=school.id,
            academic_year_id=year.id,
            document_type=doc.document_type,
            file_path=doc.file_path,
        ))

    # ── Parent: link existing (staff-confirmed) OR create new ─────────────────
    role = _parent_role()
    parent_created = False
    parent_username = None
    parent_password = None
    linked_parent_id = None

    if link_parent_id:
        parent = (User.query.execution_options(bypass_tenant_scope=True)
                  .filter_by(id=link_parent_id, school_id=school.id,
                             role_id=role.id if role else 0).first())
        if parent is None:
            raise ApprovalError('حساب ولي الأمر المحدد غير صالح لهذه المدرسة.')
        _link_parent_student(parent.id, student.id, req.guardian_relation)
        linked_parent_id = parent.id
        # Existing account — password is never changed, reset, or revealed.
    elif role:
        parent_username = code_generator.generate_parent_username()
        parent_password = code_generator.generate_parent_password()
        parent_email = None
        if req.guardian_email:
            if not User.query.execution_options(bypass_tenant_scope=True)\
                    .filter_by(email=req.guardian_email).first():
                parent_email = req.guardian_email
        parent = User(
            username=parent_username,
            full_name=req.guardian_name or parent_username,
            email=parent_email,
            phone=req.guardian_phone or None,
            school_id=school.id,
            role_id=role.id,
            is_active=True,
        )
        parent.set_password(parent_password)
        db.session.add(parent)
        db.session.flush()
        _link_parent_student(parent.id, student.id, req.guardian_relation)
        parent_created = True
        linked_parent_id = parent.id

    # ── Finalize the request ──────────────────────────────────────────────────
    req.status = 'approved'
    req.reviewed_at = datetime.utcnow()
    req.reviewed_by = actor.id
    req.approved_student_id = student.id
    req.linked_parent_id = linked_parent_id
    req.parent_account_created = parent_created

    # Audit — never include the temporary password.
    log_action('approve', 'registration_request', req.id,
               details=(f'اعتماد طلب تسجيل خارجي وإنشاء الطالب {student.student_id}'
                        + (' مع حساب ولي أمر جديد' if parent_created
                           else ' وربطه بحساب ولي أمر موجود' if linked_parent_id
                           else '')))

    db.session.commit()

    return {'ok': True, 'already': False, 'student_id': student.id,
            'parent_created': parent_created,
            'parent_username': parent_username,
            'parent_password': parent_password}


def reject_request(request_id: int, school, actor, reason: str | None = None) -> bool:
    """Reject a pending request (idempotent). Sets the parent-facing reason."""
    req = (StudentRegistrationRequest.query
           .filter_by(id=request_id, school_id=school.id)
           .with_for_update()
           .first())
    if req is None:
        raise ApprovalError('الطلب غير موجود.')
    if req.status == 'rejected':
        return True
    if req.status == 'approved':
        raise ApprovalError('تم اعتماد هذا الطلب مسبقاً ولا يمكن رفضه.')

    req.status = 'rejected'
    req.reviewed_at = datetime.utcnow()
    req.reviewed_by = actor.id
    req.rejection_reason = (reason or '').strip()[:1000] or None
    log_action('reject', 'registration_request', req.id,
               details='رفض طلب تسجيل خارجي')
    db.session.commit()
    return True
