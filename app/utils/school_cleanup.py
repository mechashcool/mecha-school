"""Safe school cleanup helpers for generated demo/test tenants."""
from __future__ import annotations

import re
from collections import OrderedDict

from sqlalchemy import or_

from app.models import (
    db, AcademicYear, Announcement, AnnouncementTarget, AttendanceDevice,
    AuditLog, Complaint, Device, Employee, EmployeeAttendance, EmployeeDocument,
    EmployeeEvaluation, Exam, ExamResult, Expense, ExpenseCategory,
    FeeInstallment, FeeRecord, FeeReminderLog, FeeType, Grade,
    InventoryCategory, InventoryCount, InventoryItem, InventoryMovement,
    LeaveRequest, Notification, NotificationRead, PushNotification, Revenue,
    RevenueCategory, SalaryRecord, Schedule, School, Section, Student,
    StudentAttendance, StudentDocument, StudentRegistrationRecord,
    StudentSuspension, StudentTransport, Subject, TransportRoute, User,
    parent_students, teacher_subjects, user_permissions,
)


_TEST_SCHOOL_RE = re.compile(
    r'^(?:'
    r'Access School|Manager School|Other School|New Flow School|'
    r'School A|School B|NoYear School|Year School|Other Year School|'
    r'Isolation School|Evaluation School|Finance School|Notify School|'
    r'Notify Other|Grade Filter School|Delete Guard School|'
    r'Demo School|Test School|'
    r'Chat Admin School A|Chat Admin School B|'
    r'Parent Request A|Parent Request B|'
    r'RegRec School A|RegRec School B'
    r') [0-9a-f]{6,32}$',
    re.IGNORECASE,
)


LINKED_SCHOOL_MODELS = (
    (User, 'المستخدمون'),
    (AcademicYear, 'الأعوام الدراسية'),
    (Grade, 'المراحل'),
    (Section, 'الشُعب'),
    (Subject, 'المواد'),
    (Student, 'الطلاب'),
    (StudentRegistrationRecord, 'سجلات القيد'),
    (StudentDocument, 'مستندات الطلاب'),
    (StudentSuspension, 'إيقافات الطلاب'),
    (Complaint, 'الشكاوى'),
    (LeaveRequest, 'طلبات الغياب'),
    (Employee, 'الموظفون/التدريسيون'),
    (EmployeeDocument, 'مستندات الموظفين'),
    (FeeType, 'أنواع الرسوم'),
    (FeeRecord, 'سجلات الرسوم'),
    (FeeInstallment, 'أقساط الرسوم'),
    (FeeReminderLog, 'سجلات تذكير الرسوم'),
    (RevenueCategory, 'تصنيفات الإيرادات'),
    (Revenue, 'الإيرادات'),
    (ExpenseCategory, 'تصنيفات المصروفات'),
    (Expense, 'المصروفات'),
    (SalaryRecord, 'الرواتب'),
    (StudentAttendance, 'حضور الطلاب'),
    (EmployeeAttendance, 'حضور الموظفين'),
    (Device, 'الأجهزة'),
    (Exam, 'الاختبارات'),
    (ExamResult, 'نتائج الاختبارات'),
    (EmployeeEvaluation, 'تقييمات الموظفين'),
    (Notification, 'الإشعارات'),
    (Announcement, 'سجل الإعلانات'),
    (PushNotification, 'سجل الإشعارات الفورية'),
    (Schedule, 'الجداول'),
    (AuditLog, 'سجل التدقيق'),
    (StudentTransport, 'نقل الطلاب'),
    (TransportRoute, 'مسارات النقل'),
    (InventoryCategory, 'تصنيفات المخزون'),
    (InventoryItem, 'مواد المخزون'),
    (InventoryMovement, 'حركات المخزون'),
    (InventoryCount, 'جرد المخزون'),
    (AttendanceDevice, 'أجهزة الحضور'),
)


SCHOOL_DELETE_ORDER = (
    # ── Leaf tables with no outgoing FKs to school-owned rows ──────────────────
    (PushNotification, 'سجل الإشعارات الفورية'),
    (AuditLog, 'سجل التدقيق'),
    # FeeReminderLog must precede FeeInstallment (installment_id FK) and
    # Student (student_id FK) and User (parent_user_id FK).
    (FeeReminderLog, 'سجلات تذكير الرسوم'),
    # ── Student child tables (must precede Student) ────────────────────────────
    (StudentDocument, 'مستندات الطلاب'),
    (StudentSuspension, 'إيقافات الطلاب'),
    (FeeInstallment, 'أقساط الرسوم'),
    (FeeRecord, 'سجلات الرسوم'),
    (ExamResult, 'نتائج الاختبارات'),
    (Schedule, 'الجداول'),
    (Exam, 'الاختبارات'),
    (StudentRegistrationRecord, 'سجلات القيد'),
    (StudentAttendance, 'حضور الطلاب'),
    # StudentTransport.student_id → students.id  (no ondelete)
    (StudentTransport, 'نقل الطلاب'),
    # ── Employee / finance child tables ────────────────────────────────────────
    (EmployeeAttendance, 'حضور الموظفين'),
    (EmployeeDocument, 'مستندات الموظفين'),
    (EmployeeEvaluation, 'تقييمات الموظفين'),
    (SalaryRecord, 'الرواتب'),
    (Revenue, 'الإيرادات'),
    (Expense, 'المصروفات'),
    (Notification, 'الإشعارات'),
    (Announcement, 'سجل الإعلانات'),
    (Device, 'الأجهزة'),
    (FeeType, 'أنواع الرسوم'),
    (RevenueCategory, 'تصنيفات الإيرادات'),
    (ExpenseCategory, 'تصنيفات المصروفات'),
    (Complaint, 'الشكاوى'),
    (LeaveRequest, 'طلبات الغياب'),
    # ── Core school-year structure ─────────────────────────────────────────────
    (Student, 'الطلاب'),
    (Section, 'الشُعب'),
    (Subject, 'المواد'),
    (Grade, 'المراحل'),
    (Employee, 'الموظفون/التدريسيون'),
    (User, 'المستخدمون'),
    # ── Inventory (must precede AcademicYear) ──────────────────────────────────
    # InventoryMovement/Count.item_id → inventory_items.id  (no ondelete)
    (InventoryMovement, 'حركات المخزون'),
    (InventoryCount, 'جرد المخزون'),
    # InventoryItem.category_id → inventory_categories.id  (no ondelete)
    (InventoryItem, 'مواد المخزون'),
    # InventoryCategory.academic_year_id → academic_years.id  (no ondelete)
    (InventoryCategory, 'تصنيفات المخزون'),
    # ── Transport (must precede school deletion) ───────────────────────────────
    # StudentTransport already deleted above; no FK blocks TransportRoute now.
    (TransportRoute, 'مسارات النقل'),
    # ── Attendance devices (academic_year_id FK, nullable but RESTRICT) ────────
    # DeviceEventLog + DeviceStudentMapping auto-cascade via DB ondelete='CASCADE'.
    (AttendanceDevice, 'أجهزة الحضور'),
    # ── Must be last among year-scoped tables ──────────────────────────────────
    (AcademicYear, 'الأعوام الدراسية'),
)


def is_demo_school(school: School) -> bool:
    """Return True only for generated demo/test school names."""
    name = (getattr(school, 'school_name', '') or '').strip()
    return bool(_TEST_SCHOOL_RE.match(name))


def linked_school_counts(school_id: int) -> OrderedDict[str, int]:
    """Count rows that directly link to a school for a friendly block message."""
    counts: OrderedDict[str, int] = OrderedDict()
    for model, label in LINKED_SCHOOL_MODELS:
        count = (
            model.query.execution_options(bypass_tenant_scope=True)
            .filter(model.school_id == school_id)
            .count()
        )
        if count:
            counts[label] = count
    return counts


def format_linked_counts(counts: OrderedDict[str, int]) -> str:
    return '، '.join(f'{label}: {count}' for label, count in counts.items())


def _ids_for(model, school_id: int) -> list[int]:
    return [
        row[0]
        for row in (
            model.query.execution_options(bypass_tenant_scope=True)
            .with_entities(model.id)
            .filter(model.school_id == school_id)
            .all()
        )
    ]


def _remember(deleted: OrderedDict[str, int], label: str, count: int | None) -> None:
    count = count or 0
    if count <= 0:
        return
    deleted[label] = deleted.get(label, 0) + count


def _delete_assoc(deleted: OrderedDict[str, int], table, label: str, clauses) -> None:
    clauses = [clause for clause in clauses if clause is not None]
    if not clauses:
        return
    result = db.session.execute(table.delete().where(or_(*clauses)))
    _remember(deleted, label, result.rowcount)


def _delete_school_rows(deleted: OrderedDict[str, int], model, label: str,
                        school_id: int) -> None:
    count = (
        model.query.execution_options(bypass_tenant_scope=True)
        .filter(model.school_id == school_id)
        .delete(synchronize_session=False)
    )
    _remember(deleted, label, count)


def cleanup_school_cascade(school_id: int) -> OrderedDict[str, int]:
    """Delete a school and all rows owned by that school.

    The caller controls authorization and commits/rollbacks. This function is
    intentionally scoped by one school_id and does not touch global tables such
    as roles, permissions, or exam types.
    """
    school = db.session.get(
        School,
        school_id,
        execution_options={'bypass_tenant_scope': True},
    )
    if school is None:
        return OrderedDict()

    deleted: OrderedDict[str, int] = OrderedDict()
    previous_skip = db.session.info.get('skip_tenant_validation')
    db.session.info['skip_tenant_validation'] = True

    try:
        user_ids = _ids_for(User, school_id)
        student_ids = _ids_for(Student, school_id)
        employee_ids = _ids_for(Employee, school_id)
        section_ids = _ids_for(Section, school_id)
        subject_ids = _ids_for(Subject, school_id)
        notification_ids = _ids_for(Notification, school_id)
        announcement_ids = _ids_for(Announcement, school_id)

        _delete_assoc(
            deleted,
            PushNotification.__table__,
            'سجل الإشعارات الفورية',
            [
                PushNotification.school_id == school_id,
                PushNotification.user_id.in_(user_ids) if user_ids else None,
            ],
        )
        _delete_assoc(
            deleted,
            AuditLog.__table__,
            'سجل التدقيق',
            [
                AuditLog.school_id == school_id,
                AuditLog.user_id.in_(user_ids) if user_ids else None,
            ],
        )
        _delete_assoc(
            deleted,
            NotificationRead.__table__,
            'قراءات الإشعارات',
            [
                NotificationRead.notification_id.in_(notification_ids)
                if notification_ids else None,
                NotificationRead.user_id.in_(user_ids) if user_ids else None,
            ],
        )
        _delete_assoc(
            deleted,
            AnnouncementTarget.__table__,
            'مستلمو الإعلانات',
            [
                AnnouncementTarget.announcement_id.in_(announcement_ids)
                if announcement_ids else None,
                AnnouncementTarget.user_id.in_(user_ids) if user_ids else None,
            ],
        )
        _delete_assoc(
            deleted,
            parent_students,
            'ربط أولياء الأمور بالطلاب',
            [
                parent_students.c.user_id.in_(user_ids) if user_ids else None,
                parent_students.c.student_id.in_(student_ids)
                if student_ids else None,
            ],
        )
        _delete_assoc(
            deleted,
            teacher_subjects,
            'ربط التدريسيين بالمواد والشُعب',
            [
                teacher_subjects.c.employee_id.in_(employee_ids)
                if employee_ids else None,
                teacher_subjects.c.subject_id.in_(subject_ids)
                if subject_ids else None,
                teacher_subjects.c.section_id.in_(section_ids)
                if section_ids else None,
            ],
        )
        _delete_assoc(
            deleted,
            user_permissions,
            'صلاحيات المستخدمين الإضافية',
            [user_permissions.c.user_id.in_(user_ids) if user_ids else None],
        )

        for model, label in SCHOOL_DELETE_ORDER:
            _delete_school_rows(deleted, model, label, school_id)

        db.session.delete(school)
        db.session.flush()
        _remember(deleted, 'المدرسة', 1)
    finally:
        if previous_skip is None:
            db.session.info.pop('skip_tenant_validation', None)
        else:
            db.session.info['skip_tenant_validation'] = previous_skip

    return deleted
