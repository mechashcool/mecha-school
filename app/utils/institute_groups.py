"""
Mecha-School — Institute study-group helpers shared by the student forms.

Everything here is a no-op for a school-type institution: the callers only
invoke it behind `school.is_institute`, and each function additionally filters
by the school_id it is handed. Grade, Section, Student.section_id and
teacher_subjects are never read or written.

The write helpers deliberately do NOT commit. They stage rows in the caller's
session so a student save and its enrollments land in ONE transaction: if any
later step of the student save fails, neither the student nor a partial set of
memberships persists.
"""
from __future__ import annotations

from datetime import datetime

from app.models import (db, InstituteGroupEnrollment, InstituteStudyGroup,
                        Student)


def institute_enabled(school) -> bool:
    """True only for an institution explicitly classified as an institute."""
    return bool(school is not None and getattr(school, 'is_institute', False))


def active_groups_for_form(school_id: int, academic_year_id: int):
    """Active groups of THIS institute in THIS academic year, ordered by name.

    These are the only groups a student form may offer, so a posted id can
    never reach a group from another school or another year.
    """
    if not school_id or not academic_year_id:
        return []
    return (InstituteStudyGroup.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school_id, academic_year_id=academic_year_id,
                       is_active=True)
            .order_by(InstituteStudyGroup.name)
            .all())


def active_group_ids_for_student(school_id: int, student_id: int) -> set[int]:
    """Group ids in which this student currently holds an ACTIVE enrollment."""
    if not school_id or not student_id:
        return set()
    rows = (InstituteGroupEnrollment.query
            .execution_options(bypass_tenant_scope=True)
            .with_entities(InstituteGroupEnrollment.group_id)
            .filter_by(school_id=school_id, student_id=student_id,
                       status=InstituteGroupEnrollment.STATUS_ACTIVE)
            .all())
    return {r.group_id for r in rows}


def active_enrollments_for_student(school_id: int, student_id: int):
    """ACTIVE enrollment rows for this student, for the student view page."""
    if not school_id or not student_id:
        return []
    return (InstituteGroupEnrollment.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school_id, student_id=student_id,
                       status=InstituteGroupEnrollment.STATUS_ACTIVE)
            .order_by(InstituteGroupEnrollment.enrolled_at.desc())
            .all())


def parse_posted_group_ids(raw_values) -> tuple[list[int], bool]:
    """Turn posted group id strings into ints.

    Returns (ids, ok). ok is False when any value is not an integer, so the
    caller can reject the whole submission instead of silently dropping one.
    Order is preserved and duplicates are removed.
    """
    ids: list[int] = []
    for raw in raw_values or []:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            return [], False
    return list(dict.fromkeys(ids)), True


def validate_group_ids(group_ids, school_id: int, academic_year_id: int):
    """Return (valid_ids, error_or_None).

    A group id is valid only when the group is ACTIVE and belongs to BOTH this
    institute and this academic year. One bad id fails the whole call, so the
    caller writes nothing — never a partial enrollment set.
    """
    group_ids = list(group_ids or [])
    if not group_ids:
        return [], None
    allowed = {
        g.id for g in InstituteStudyGroup.query
        .execution_options(bypass_tenant_scope=True)
        .filter(InstituteStudyGroup.school_id == school_id,
                InstituteStudyGroup.academic_year_id == academic_year_id,
                InstituteStudyGroup.is_active.is_(True),
                InstituteStudyGroup.id.in_(group_ids))
        .all()
    }
    if any(gid not in allowed for gid in group_ids):
        # Never disclose whether the id exists in another institute.
        return [], ('بعض المجموعات الدراسية المحددة غير صالحة لهذه المؤسسة أو '
                    'غير مفعّلة. لم يتم حفظ أي تغيير.')
    return group_ids, None


def stage_enrollments(school_id: int, student_id: int, group_ids) -> int:
    """Stage ACTIVE enrollment rows for a NEW student. Does not commit.

    Used by the create flow, where the student has just been flushed and can
    hold no prior membership.
    """
    now = datetime.utcnow()
    count = 0
    for gid in group_ids or []:
        db.session.add(InstituteGroupEnrollment(
            school_id=school_id, group_id=gid, student_id=student_id,
            enrolled_at=now, ended_at=None,
            status=InstituteGroupEnrollment.STATUS_ACTIVE,
        ))
        count += 1
    return count


def stage_enrollment_changes(school_id: int, student_id: int,
                             selected_group_ids) -> tuple[int, int]:
    """Reconcile a student's ACTIVE memberships to `selected_group_ids`.

    * kept      — already active and still selected: left completely untouched.
    * added     — newly selected: a fresh ACTIVE row is staged.
    * removed   — no longer selected: the existing row is ENDED
                  (status='ended', ended_at=utcnow). It is never deleted.

    Does not commit, so a failure later in the student save rolls the whole
    reconciliation back with it. Returns (added, ended).
    """
    selected = set(selected_group_ids or [])
    now = datetime.utcnow()

    current = (InstituteGroupEnrollment.query
               .execution_options(bypass_tenant_scope=True)
               .filter_by(school_id=school_id, student_id=student_id,
                          status=InstituteGroupEnrollment.STATUS_ACTIVE)
               .all())
    current_ids = {e.group_id for e in current}

    ended = 0
    for enrollment in current:
        if enrollment.group_id not in selected:
            enrollment.status   = InstituteGroupEnrollment.STATUS_ENDED
            enrollment.ended_at = now
            ended += 1

    added = 0
    for gid in selected:
        if gid not in current_ids:
            db.session.add(InstituteGroupEnrollment(
                school_id=school_id, group_id=gid, student_id=student_id,
                enrolled_at=now, ended_at=None,
                status=InstituteGroupEnrollment.STATUS_ACTIVE,
            ))
            added += 1

    return added, ended


def student_is_institute_student(student: Student, school) -> bool:
    """True when this student belongs to an institute-classified institution."""
    return institute_enabled(school) and student is not None
