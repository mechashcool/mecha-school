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

from app.models import (db, Employee, InstituteGroupEnrollment,
                        InstituteStudyGroup, Student)


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


# ═════════════════════════════════════════════════════════════════════════════
#  INSTRUCTOR SCOPE — the institute counterpart of get_teacher_section_ids()
# ═════════════════════════════════════════════════════════════════════════════
#
# A school teacher is scoped by section:
#     teacher -> Section (homeroom or teacher_subjects) -> Student.section_id
# An institute instructor is scoped by study group instead, because one
# institute student may belong to several groups and carries no section:
#     instructor -> InstituteStudyGroup.instructor_id
#                -> ACTIVE InstituteGroupEnrollment -> Student
#
# These are the ONLY authorization queries for that branch; routes call them
# rather than re-deriving slightly different conditions. Every one of them
# fails closed: no Employee row, no school, no year or no group yields an empty
# scope, never a wider one.


def resolve_instructor(school, user):
    """The Employee row backing this logged-in user, or None.

    Reuses the existing user -> Employee link (Employee.user_id) that
    get_teacher_section_ids() and every other teacher surface already rely on;
    no second identity is introduced. Returns None when the account has no
    linked employee record, which callers MUST treat as an empty scope.
    """
    if school is None or user is None or not getattr(user, 'id', None):
        return None
    return (Employee.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(user_id=user.id, school_id=school.id)
            .first())


def instructor_groups(school, user, year, *, active_only=True):
    """Study groups this instructor is assigned to, for THIS institute/year.

    Ordered by name. Returns [] for a non-institute, an unlinked account or a
    missing year: an instructor never sees a group they are not assigned to.
    """
    if not institute_enabled(school) or year is None:
        return []
    employee = resolve_instructor(school, user)
    if employee is None:
        return []
    q = (InstituteStudyGroup.query
         .execution_options(bypass_tenant_scope=True)
         .filter_by(school_id=school.id, academic_year_id=year.id,
                    instructor_id=employee.id))
    if active_only:
        q = q.filter(InstituteStudyGroup.is_active.is_(True))
    return q.order_by(InstituteStudyGroup.name).all()


def instructor_group_ids(school, user, year, *, active_only=True) -> set:
    """Ids only — the institute analogue of get_teacher_section_ids()."""
    return {g.id for g in instructor_groups(school, user, year,
                                            active_only=active_only)}


def active_enrollment_count_map(school, group_ids) -> dict:
    """{group_id: active student count} in ONE query, never N+1."""
    group_ids = list(group_ids or [])
    if not group_ids or school is None:
        return {}
    rows = (db.session.query(InstituteGroupEnrollment.group_id,
                             db.func.count(InstituteGroupEnrollment.id))
            .filter(InstituteGroupEnrollment.school_id == school.id,
                    InstituteGroupEnrollment.group_id.in_(group_ids),
                    InstituteGroupEnrollment.status
                    == InstituteGroupEnrollment.STATUS_ACTIVE)
            .group_by(InstituteGroupEnrollment.group_id)
            .all())
    return {gid: cnt for gid, cnt in rows}


def instructor_student_ids(school, user, year) -> set:
    """DISTINCT ids of students actively enrolled in this instructor's groups.

    A student in two of the instructor's groups appears ONCE: the query is
    DISTINCT over student_id. Returns an empty set — never a wider scope — for
    a non-institute, an unlinked account, or an instructor with no groups.
    """
    group_ids = instructor_group_ids(school, user, year)
    if not group_ids:
        return set()
    rows = (db.session.query(InstituteGroupEnrollment.student_id)
            .filter(InstituteGroupEnrollment.school_id == school.id,
                    InstituteGroupEnrollment.group_id.in_(group_ids),
                    InstituteGroupEnrollment.status
                    == InstituteGroupEnrollment.STATUS_ACTIVE)
            .distinct()
            .all())
    return {r[0] for r in rows}


def instructor_can_access_student(school, user, year, student_id) -> bool:
    """True only when the student holds an ACTIVE enrollment in an ACTIVE group
    assigned to this instructor, inside this institute and academic year.

    One EXISTS query. Used by every direct-object guard, so a student-detail
    URL can never be reached by id alone.
    """
    if not student_id:
        return False
    group_ids = instructor_group_ids(school, user, year)
    if not group_ids:
        return False
    return bool(db.session.query(
        db.session.query(InstituteGroupEnrollment.id)
        .filter(InstituteGroupEnrollment.school_id == school.id,
                InstituteGroupEnrollment.group_id.in_(group_ids),
                InstituteGroupEnrollment.student_id == student_id,
                InstituteGroupEnrollment.status
                == InstituteGroupEnrollment.STATUS_ACTIVE)
        .exists()).scalar())


def active_roster(school, group_id):
    """(enrollment, student) pairs for the ACTIVE members of one group.

    Ended rows are excluded from the roster but never touched — this is a
    read-only projection. Joined so student rows load in a single query.
    """
    if school is None or not group_id:
        return []
    return (db.session.query(InstituteGroupEnrollment, Student)
            .join(Student, Student.id == InstituteGroupEnrollment.student_id)
            .filter(InstituteGroupEnrollment.school_id == school.id,
                    InstituteGroupEnrollment.group_id == group_id,
                    InstituteGroupEnrollment.status
                    == InstituteGroupEnrollment.STATUS_ACTIVE,
                    Student.school_id == school.id)
            .order_by(Student.full_name)
            .all())


def active_student_ids_in_group(school_id: int, group_id) -> list[int]:
    """DISTINCT ids of students holding an ACTIVE enrollment in ONE group.

    The audience of anything targeted at a study group — currently homework and
    the notifications homework raises. Ended memberships are excluded, so a
    student who left the group stops receiving its assignments while the
    history row itself stays untouched.

    Returns [] for a missing school or group, never a wider audience.
    """
    if not school_id or not group_id:
        return []
    rows = (db.session.query(InstituteGroupEnrollment.student_id)
            .filter(InstituteGroupEnrollment.school_id == school_id,
                    InstituteGroupEnrollment.group_id == group_id,
                    InstituteGroupEnrollment.status
                    == InstituteGroupEnrollment.STATUS_ACTIVE)
            .distinct()
            .all())
    return [r[0] for r in rows]
