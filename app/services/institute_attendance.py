"""
Mecha-School — Institute group schedules and manual attendance.

THE SINGLE DOMAIN SERVICE for institute attendance. The web blueprint and the
mobile API both call into here; neither re-implements occurrence computation,
roster resolution, authorization, idempotency or notification dispatch. That is
also what makes a future card/device source a small addition rather than a
third copy of the rules.

Four invariants this module exists to guarantee:

1.  NO AUTOMATIC ABSENCE, EVER. Occurrences are computed from the weekly rules
    for display only. A row is written solely because a human submitted one.
    Nothing here, and nothing anywhere else in the codebase, turns "the time
    passed" into a status. There is no scheduler, no sweep and no default.

2.  NOT RECORDED != RECORDED ABSENT. A session with no submission either has no
    row at all or carries status 'not_recorded'. An unmarked student inside a
    recorded session simply has no attendance row.

3.  IDEMPOTENT UNDER RETRY AND CONCURRENCY. Uniqueness is enforced by the
    database (uq_institute_session_group_date_start and
    uq_institute_attendance_session_student), and the insert paths recover from
    an IntegrityError by re-selecting the winner instead of trusting a
    pre-insert SELECT.

4.  HISTORY IS IMMUTABLE BY STRUCTURE. A session snapshots its own date, times
    and instructor, so editing or deleting a weekly rule afterwards changes
    only future computed occurrences.
"""
from __future__ import annotations

from datetime import date as date_type, datetime, time as time_type, timedelta

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from app.models import (db, Employee, InstituteAttendanceRecord,
                        InstituteAttendanceSession, InstituteGroupEnrollment,
                        InstituteGroupSchedule, InstituteStudyGroup,
                        InstituteSuspensionGroup, InstituteSuspensionScope,
                        Student, StudentSuspension)
from app.utils.attendance_helpers import (_get_tz, get_local_date, get_local_now,
                                          utc_to_local)
from app.utils.institute_groups import institute_enabled
# Importing the module (not its table) is side-effect free: no query runs at
# import time, so startup is safe even before the outbox migration is applied.
from app.services import notification_outbox as outbox

import logging

log = logging.getLogger('mecha.institute.attendance')

OPTS = {'bypass_tenant_scope': True}

# 0 = Sunday … 6 = Saturday — identical to Schedule.day_of_week and to the DAYS
# list in the schedules blueprint. Python's date.weekday() is Monday-based, so
# every conversion goes through _py_to_app_dow / _app_to_py_dow, never by hand.
DAY_NAMES_AR = ['الأحد', 'الاثنين', 'الثلاثاء', 'الأربعاء',
                'الخميس', 'الجمعة', 'السبت']

STATUS_LABELS_AR = {
    InstituteAttendanceRecord.STATUS_PRESENT: 'حاضر',
    InstituteAttendanceRecord.STATUS_ABSENT:  'غائب',
    InstituteAttendanceRecord.STATUS_LATE:    'متأخر',
    InstituteAttendanceRecord.STATUS_EXCUSED: 'بعذر',
}


class AttendanceError(Exception):
    """A validation or authorization failure. Carries an Arabic message.

    Raised before any write, so the caller can render it and be certain the
    transaction wrote nothing.
    """


# ═════════════════════════════════════════════════════════════════════════════
#  Weekday helpers
# ═════════════════════════════════════════════════════════════════════════════

def _py_to_app_dow(d: date_type) -> int:
    """date.weekday() (Mon=0) -> app convention (Sun=0)."""
    return (d.weekday() + 1) % 7


def _app_to_py_dow(dow: int) -> int:
    """App convention (Sun=0) -> date.weekday() (Mon=0)."""
    return (dow - 1) % 7


def day_name(dow: int) -> str:
    return DAY_NAMES_AR[dow] if 0 <= dow <= 6 else '—'


# ── Presentation-time timezone conversion ────────────────────────────────────
#
# recorded_at is STORED as naive UTC (datetime.utcnow()), which is the existing
# application convention and is deliberately left unchanged — nothing is
# rewritten in the database and no offset is ever baked into a stored value.
#
# The conversion happens ONLY here, at the moment of display, reusing the
# school's configured timezone (School.timezone, default Asia/Baghdad) through
# the helpers the school attendance module already uses.
#
# Both helpers are safe on an already timezone-aware value: utc_to_local() and
# the localize() below only attach UTC when tzinfo is None, so a value that
# already carries an offset is converted exactly once, never twice.
#
# Schedule slot times (start_time / end_time) are LOCAL WALL-CLOCK values typed
# by administration — 12:00-14:00 means 12:00-14:00 in Baghdad. They are plain
# Time columns with no date and no timezone, and NOTHING here touches them.


def to_local(dt, school=None):
    """A stored UTC timestamp as naive LOCAL wall-clock, for templates.

    Returns None for None so a never-recorded value stays empty.
    """
    if dt is None:
        return None
    return utc_to_local(dt, school)


def to_local_iso(dt, school=None):
    """A stored UTC timestamp as an ISO-8601 string carrying the local offset.

    e.g. '2026-09-23T11:44:00+03:00' — unambiguous for a mobile client, which
    therefore must NOT apply any further conversion of its own.
    """
    if dt is None:
        return None
    import pytz
    tz = _get_tz(school)
    aware = pytz.utc.localize(dt) if dt.tzinfo is None else dt
    return aware.astimezone(tz).isoformat()


def local_formatter(school):
    """A `dt -> naive local datetime` callable bound to one school.

    Handed to the attendance templates so each call site stays a plain
    `local_dt(x).strftime(...)` instead of repeating the school argument.
    """
    def _to_local(dt):
        return to_local(dt, school)
    return _to_local


def local_today(school=None) -> date_type:
    """Today in the SCHOOL's timezone, reusing the existing attendance helper.

    Never date.today() and never utcnow().date(): a session opened at 23:30
    Baghdad time must belong to that local day, not to the UTC one.
    """
    return get_local_date(school)


def local_now(school=None) -> datetime:
    """Now, as naive LOCAL wall-clock in the SCHOOL's timezone."""
    return get_local_now(school)


# ═════════════════════════════════════════════════════════════════════════════
#  Weekly schedule rules
# ═════════════════════════════════════════════════════════════════════════════

def group_slots(school_id: int, group_id: int, *, active_only: bool = True):
    """The weekly rules of one group, ordered for display."""
    if not school_id or not group_id:
        return []
    q = (InstituteGroupSchedule.query
         .execution_options(**OPTS)
         .filter_by(school_id=school_id, group_id=group_id))
    if active_only:
        q = q.filter(InstituteGroupSchedule.is_active.is_(True))
    return q.order_by(InstituteGroupSchedule.day_of_week,
                      InstituteGroupSchedule.start_time).all()


def slots_by_group(school_id: int, academic_year_id: int, group_ids,
                   *, active_only: bool = False) -> dict:
    """{group_id: [weekly rules ordered for display]} for MANY groups at once.

    One query regardless of how many groups are listed — the bulk counterpart
    of group_slots() for the all-groups schedules page. bypass_tenant_scope
    disables BOTH the school and the year criteria, so both are re-applied
    explicitly here rather than trusted to the ORM.
    """
    group_ids = list(group_ids or [])
    if not school_id or not academic_year_id or not group_ids:
        return {}
    q = (InstituteGroupSchedule.query
         .execution_options(**OPTS)
         .filter(InstituteGroupSchedule.school_id == school_id,
                 InstituteGroupSchedule.academic_year_id == academic_year_id,
                 InstituteGroupSchedule.group_id.in_(group_ids)))
    if active_only:
        q = q.filter(InstituteGroupSchedule.is_active.is_(True))
    out = {}
    for slot in q.order_by(InstituteGroupSchedule.day_of_week,
                           InstituteGroupSchedule.start_time).all():
        out.setdefault(slot.group_id, []).append(slot)
    return out


def validate_slot(day_of_week, start_time, end_time):
    """(day, start, end) or raise AttendanceError. Pure validation, no I/O."""
    try:
        dow = int(day_of_week)
    except (TypeError, ValueError):
        raise AttendanceError('اليوم غير صالح.')
    if not (InstituteGroupSchedule.DAY_MIN <= dow <= InstituteGroupSchedule.DAY_MAX):
        raise AttendanceError('اليوم غير صالح.')

    def _parse(raw, label):
        if isinstance(raw, time_type):
            return raw
        if not raw:
            raise AttendanceError(f'{label} مطلوب.')
        for fmt in ('%H:%M', '%H:%M:%S'):
            try:
                return datetime.strptime(str(raw).strip(), fmt).time()
            except ValueError:
                continue
        raise AttendanceError(f'صيغة {label} غير صحيحة.')

    start = _parse(start_time, 'وقت البداية')
    end = _parse(end_time, 'وقت النهاية')
    if start >= end:
        raise AttendanceError('وقت البداية يجب أن يكون قبل وقت النهاية.')
    return dow, start, end


def add_slot(school, group, day_of_week, start_time, end_time):
    """Create one weekly rule. Raises AttendanceError; never partially writes.

    Duplicate protection is the DB constraint, not a pre-check, so two
    simultaneous adds cannot both succeed.
    """
    dow, start, end = validate_slot(day_of_week, start_time, end_time)
    slot = InstituteGroupSchedule(
        school_id=school.id, academic_year_id=group.academic_year_id,
        group_id=group.id, day_of_week=dow,
        start_time=start, end_time=end, is_active=True)
    db.session.add(slot)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise AttendanceError(
            f'يوجد موعد بالفعل لهذه المجموعة يوم {day_name(dow)} '
            f'في الساعة {start.strftime("%H:%M")}.')
    return slot


def update_slot(slot, day_of_week, start_time, end_time, is_active=True):
    """Edit a weekly rule. FUTURE occurrences only.

    Stored sessions keep their own snapshot and are never touched here — the
    caller does not have to remember that, because this function simply does
    not reference InstituteAttendanceSession.
    """
    dow, start, end = validate_slot(day_of_week, start_time, end_time)
    slot.day_of_week = dow
    slot.start_time = start
    slot.end_time = end
    slot.is_active = bool(is_active)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise AttendanceError(
            f'يوجد موعد آخر لهذه المجموعة يوم {day_name(dow)} '
            f'في الساعة {start.strftime("%H:%M")}.')
    return slot


def delete_slot(slot):
    """Remove a weekly rule.

    Recorded sessions survive: institute_attendance_sessions.schedule_id is
    ON DELETE SET NULL, so the history keeps its date/time snapshot and only
    loses the link to a rule that no longer exists.
    """
    db.session.delete(slot)
    db.session.commit()


# ═════════════════════════════════════════════════════════════════════════════
#  Computed occurrences  (never pre-generated, never persisted by themselves)
# ═════════════════════════════════════════════════════════════════════════════

class Occurrence:
    """One computed meeting of a group on one date.

    A plain value object. `session` is the materialized row when one already
    exists, otherwise None — which is exactly what "not recorded" looks like
    before anybody opens it.
    """

    __slots__ = ('group', 'slot', 'date', 'start_time', 'end_time', 'session')

    def __init__(self, group, slot, on_date, session=None):
        self.group = group
        self.slot = slot
        self.date = on_date
        self.start_time = slot.start_time if slot else (
            session.start_time if session else None)
        self.end_time = slot.end_time if slot else (
            session.end_time if session else None)
        self.session = session

    @property
    def is_recorded(self) -> bool:
        return bool(self.session and self.session.is_recorded)

    @property
    def status(self) -> str:
        return (InstituteAttendanceSession.STATUS_RECORDED if self.is_recorded
                else InstituteAttendanceSession.STATUS_NOT_RECORDED)

    @property
    def day_of_week(self) -> int:
        return _py_to_app_dow(self.date)


def _group_covers(group, on_date: date_type) -> bool:
    """True when the group's own start/end dates include this date.

    Reuses the group's existing bounds rather than duplicating them onto every
    schedule rule.
    """
    if group.start_date and on_date < group.start_date:
        return False
    if group.end_date and on_date > group.end_date:
        return False
    return True


def occurrences_for_range(school, groups, start: date_type, end: date_type,
                          *, include_sessions: bool = True):
    """Every computed occurrence of `groups` between start and end inclusive.

    Pure computation from the weekly rules plus, optionally, a single bulk
    lookup of the sessions that happen to be materialized already. Two queries
    total for the whole range regardless of how many groups or days it spans —
    never one per day and never one per group.

    Nothing is created here. Listing a date in the past does not conjure a
    session and cannot produce an absence.
    """
    groups = list(groups or [])
    if not groups or school is None or start > end:
        return []
    if (end - start).days > 400:
        raise AttendanceError('المدى الزمني المطلوب كبير جداً.')

    group_ids = [g.id for g in groups]
    group_by_id = {g.id: g for g in groups}

    slots = (InstituteGroupSchedule.query
             .execution_options(**OPTS)
             .filter(InstituteGroupSchedule.school_id == school.id,
                     InstituteGroupSchedule.group_id.in_(group_ids),
                     InstituteGroupSchedule.is_active.is_(True))
             .all())
    slots_by_group = {}
    for slot in slots:
        slots_by_group.setdefault(slot.group_id, []).append(slot)

    # One bulk fetch keyed by (group, date, start_time) — the same tuple the
    # unique constraint uses, so a materialized session always matches exactly
    # one computed occurrence.
    session_map = {}
    if include_sessions:
        for sess in (InstituteAttendanceSession.query
                     .execution_options(**OPTS)
                     .filter(InstituteAttendanceSession.school_id == school.id,
                             InstituteAttendanceSession.group_id.in_(group_ids),
                             InstituteAttendanceSession.session_date >= start,
                             InstituteAttendanceSession.session_date <= end)
                     .all()):
            session_map[(sess.group_id, sess.session_date, sess.start_time)] = sess

    out = []
    seen = set()
    day = start
    while day <= end:
        dow = _py_to_app_dow(day)
        for gid, group_slots_ in slots_by_group.items():
            group = group_by_id[gid]
            if not _group_covers(group, day):
                continue
            for slot in group_slots_:
                if slot.day_of_week != dow:
                    continue
                key = (gid, day, slot.start_time)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Occurrence(group, slot, day, session_map.get(key)))
        day += timedelta(days=1)

    # Sessions that no longer match any active rule (the rule was edited or
    # deleted after the fact) must still be listed: the history is real even
    # though the rule is gone.
    if include_sessions:
        for key, sess in session_map.items():
            if key in seen:
                continue
            group = group_by_id.get(sess.group_id)
            if group is None:
                continue
            occ = Occurrence(group, None, sess.session_date, sess)
            out.append(occ)

    out.sort(key=lambda o: (o.date, o.start_time or time_type(0, 0),
                            o.group.name or ''))
    return out


# ── One day's attendance queue (display only) ───────────────────────────────
# Presentation states for ONE day's occurrences. Never stored, never a session
# or record status, and never a restriction: every lesson keeps its own
# attendance_take link exactly as before, whatever its state.
QUEUE_CURRENT    = 'current'     # today, not recorded, start <= now < end
QUEUE_DUE        = 'due'         # today, not recorded, end already passed
QUEUE_UPCOMING   = 'upcoming'    # today, not recorded, not started yet
QUEUE_UNRECORDED = 'unrecorded'  # another date, not recorded
QUEUE_RECORDED   = 'recorded'    # attendance submitted (any date)

QUEUE_PENDING = (QUEUE_CURRENT, QUEUE_DUE, QUEUE_UNRECORDED)


def daily_queue(occurrences, *, is_today: bool, now_time=None) -> list:
    """Group ONE day's occurrences by study group, most urgent group first.

    Pure: reads only the occurrences handed in — no query, no write, no session
    materialized. Each occurrence stays its own attendance unit; grouping is
    visual only.

    Returns [{'group', 'lessons': [(occurrence, state)], 'rank'}], lessons by
    start time. rank 0 = has a current/due (or, on another date, unrecorded)
    lesson, 1 = only upcoming ones left, 2 = everything recorded.
    """
    by_group = {}
    for occ in occurrences or []:
        by_group.setdefault(occ.group.id, (occ.group, []))[1].append(occ)

    entries = []
    for group, occs in by_group.values():
        occs.sort(key=lambda o: o.start_time or time_type(0, 0))
        lessons = []
        for occ in occs:
            if occ.is_recorded:
                state = QUEUE_RECORDED
            elif not is_today or now_time is None:
                state = QUEUE_UNRECORDED
            elif now_time < occ.start_time:
                state = QUEUE_UPCOMING
            elif occ.end_time and now_time < occ.end_time:
                state = QUEUE_CURRENT
            else:
                state = QUEUE_DUE
            lessons.append((occ, state))
        states = {s for _o, s in lessons}
        rank = (0 if states.intersection(QUEUE_PENDING)
                else 1 if QUEUE_UPCOMING in states else 2)
        entries.append({'group': group, 'lessons': lessons, 'rank': rank})

    entries.sort(key=lambda e: (e['rank'],
                                e['lessons'][0][0].start_time or time_type(0, 0),
                                e['group'].name or ''))
    return entries


def find_occurrence(school, group, on_date: date_type, start_time):
    """The single computed occurrence matching a (group, date, start) tuple.

    This is the ONLY way a client-supplied date/time becomes a session: the
    tuple must correspond to a real active weekly rule (or to an already
    materialized session), so an arbitrary date cannot be invented through the
    API.
    """
    for occ in occurrences_for_range(school, [group], on_date, on_date):
        if occ.start_time == start_time:
            return occ
    return None


# ═════════════════════════════════════════════════════════════════════════════
#  Materialization
# ═════════════════════════════════════════════════════════════════════════════

def _find_session(school, group, occurrence):
    """The already materialized session for one occurrence, or None.

    A named seam used BOTH as the pre-check and as the post-IntegrityError
    recovery lookup in get_or_create_session(), so the concurrency path can be
    exercised deterministically in tests.
    """
    return (InstituteAttendanceSession.query
            .execution_options(**OPTS)
            .filter_by(school_id=school.id, group_id=group.id,
                       session_date=occurrence.date,
                       start_time=occurrence.start_time)
            .first())


def get_or_create_session(school, group, occurrence):
    """The session row for one occurrence, creating it on first open.

    Concurrency-safe: on the unique-constraint violation that a simultaneous
    request causes, this rolls back and returns the row the winner inserted,
    so both callers end up with the SAME session and no duplicate exists.

    Creating a session does NOT record attendance. The new row is
    status='not_recorded' with no source and no recorded_at, which is precisely
    the "nobody submitted anything" state.
    """
    existing = _find_session(school, group, occurrence)
    if existing is not None:
        return existing

    sess = InstituteAttendanceSession(
        school_id=school.id,
        academic_year_id=group.academic_year_id,
        group_id=group.id,
        schedule_id=occurrence.slot.id if occurrence.slot else None,
        session_date=occurrence.date,
        start_time=occurrence.start_time,
        end_time=occurrence.end_time,
        # Snapshot: reassigning the group's instructor later must not rewrite
        # who was responsible for a past session.
        instructor_id=group.instructor_id,
        status=InstituteAttendanceSession.STATUS_NOT_RECORDED,
    )
    db.session.add(sess)
    try:
        db.session.commit()
        return sess
    except IntegrityError:
        # Lost the race on uq_institute_session_group_date_start. The winner
        # already inserted an identical row; return THAT one so both callers
        # converge and no duplicate exists.
        db.session.rollback()
        winner = _find_session(school, group, occurrence)
        if winner is None:
            raise
        return winner


# ═════════════════════════════════════════════════════════════════════════════
#  Roster
# ═════════════════════════════════════════════════════════════════════════════

def session_roster(school, session):
    """[(student, record_or_None)] for one session, in one pair of queries.

    Two distinct populations are merged, deliberately:

      * CURRENTLY eligible students — active enrollment in this exact group and
        an active student record. These may receive a new status.
      * Students who ALREADY have a record for this session, even if they have
        since left the group or been deactivated. A student removed later must
        not vanish from a session that was already recorded.

    A student in both sets appears once: the merge is keyed by student id. No
    N+1 — two queries regardless of roster size.
    """
    if school is None or session is None:
        return []

    eligible = (db.session.query(Student)
                .join(InstituteGroupEnrollment,
                      InstituteGroupEnrollment.student_id == Student.id)
                .filter(InstituteGroupEnrollment.school_id == school.id,
                        InstituteGroupEnrollment.group_id == session.group_id,
                        InstituteGroupEnrollment.status
                        == InstituteGroupEnrollment.STATUS_ACTIVE,
                        Student.school_id == school.id,
                        Student.status == 'active')
                .all())

    records = (InstituteAttendanceRecord.query
               .execution_options(**OPTS)
               .filter_by(school_id=school.id, session_id=session.id)
               .all())
    record_by_student = {r.student_id: r for r in records}

    merged = {s.id: s for s in eligible}
    missing = [sid for sid in record_by_student if sid not in merged]
    if missing:
        for stu in (Student.query.execution_options(**OPTS)
                    .filter(Student.id.in_(missing),
                            Student.school_id == school.id).all()):
            merged[stu.id] = stu

    rows = [(stu, record_by_student.get(stu.id)) for stu in merged.values()]
    rows.sort(key=lambda pair: pair[0].full_name or '')
    return rows


def eligible_student_ids(school, group_id) -> set:
    """Students who may receive a NEW status in this group right now."""
    if school is None or not group_id:
        return set()
    rows = (db.session.query(Student.id)
            .join(InstituteGroupEnrollment,
                  InstituteGroupEnrollment.student_id == Student.id)
            .filter(InstituteGroupEnrollment.school_id == school.id,
                    InstituteGroupEnrollment.group_id == group_id,
                    InstituteGroupEnrollment.status
                    == InstituteGroupEnrollment.STATUS_ACTIVE,
                    Student.school_id == school.id,
                    Student.status == 'active')
            .distinct().all())
    return {r[0] for r in rows}


def suspended_student_ids(school, student_ids, on_date, group_id) -> set:
    """Students suspended from `group_id` on `on_date`, in ONE query.

    Date rule mirrors school manual attendance exactly (start_date <= date <=
    end_date). A suspension active on that date blocks this group when:
      * it has NO InstituteSuspensionScope (legacy row -> all groups), or
      * its scope applies_to_all_groups (evaluated now, so it also covers a
        group joined after the suspension was created), or
      * its scope explicitly selects this group.
    A selected-groups suspension that does not name this group blocks nothing.
    """
    student_ids = list(student_ids or [])
    if school is None or not student_ids or on_date is None:
        return set()
    scope, sel = InstituteSuspensionScope, InstituteSuspensionGroup
    rows = (db.session.query(StudentSuspension.student_id)
            .select_from(StudentSuspension)
            .outerjoin(scope, and_(scope.suspension_id == StudentSuspension.id,
                                   scope.school_id == school.id))
            .outerjoin(sel, and_(sel.scope_id == scope.id,
                                 sel.school_id == school.id,
                                 sel.group_id == group_id))
            .filter(StudentSuspension.school_id == school.id,
                    StudentSuspension.student_id.in_(student_ids),
                    StudentSuspension.start_date <= on_date,
                    StudentSuspension.end_date >= on_date,
                    or_(scope.id.is_(None),
                        scope.applies_to_all_groups.is_(True),
                        sel.id.isnot(None)))
            .execution_options(**OPTS)
            .distinct()
            .all())
    return {r[0] for r in rows}


# ═════════════════════════════════════════════════════════════════════════════
#  Submission — the ONE write path for web and mobile
# ═════════════════════════════════════════════════════════════════════════════

def submit_attendance(school, session, statuses, *, source, actor_user_id,
                      notify=True):
    """Record or correct attendance for one session. Atomic and idempotent.

    `statuses` is {student_id: status}. Every entry is validated BEFORE a
    single row is written, so an invalid or unauthorised entry leaves the whole
    session untouched — there is no partially recorded class.

    Returns a dict summarising what changed. Only genuinely NEW absences
    notify; a re-submission of the same values notifies nobody, which is what
    makes a retry safe.

    Omitting a student is not "absent". It leaves them unmarked, exactly as
    before the call.
    """
    if school is None or session is None:
        raise AttendanceError('الجلسة غير صالحة.')
    if session.school_id != school.id:
        raise AttendanceError('الجلسة غير صالحة.')

    if source not in InstituteAttendanceSession.SOURCES:
        raise AttendanceError('مصدر التسجيل غير صالح.')

    # ── Validate everything first ───────────────────────────────────────────
    cleaned = {}
    for raw_sid, raw_status in (statuses or {}).items():
        try:
            sid = int(raw_sid)
        except (TypeError, ValueError):
            raise AttendanceError('معرّف طالب غير صالح. لم يتم حفظ أي سجل.')
        status = (raw_status or '').strip()
        if not status:
            continue           # unmarked — deliberately left alone
        if status not in InstituteAttendanceRecord.STATUSES:
            raise AttendanceError('حالة حضور غير صالحة. لم يتم حفظ أي سجل.')
        cleaned[sid] = status

    if not cleaned:
        raise AttendanceError('لم يتم تحديد حالة أي طالب.')

    existing = {r.student_id: r for r in
                InstituteAttendanceRecord.query.execution_options(**OPTS)
                .filter_by(school_id=school.id, session_id=session.id).all()}

    # A student may be graded when they are currently eligible OR when they
    # already hold a record for this session (a correction of history).
    allowed = eligible_student_ids(school, session.group_id) | set(existing)
    forged = [sid for sid in cleaned if sid not in allowed]
    if forged:
        raise AttendanceError(
            'أحد الطلاب المرسلين غير مسجّل حالياً في هذه المجموعة الدراسية. '
            'لم يتم حفظ أي سجل.')

    # "إيقاف الطالب" — the SAME rule school manual attendance applies: a
    # StudentSuspension whose date range covers the lesson date. A suspended
    # student is skipped entirely (no new row, no change to an existing row,
    # no notification) while everyone else in the submission is processed.
    suspended = suspended_student_ids(school, cleaned, session.session_date,
                                      session.group_id)
    if suspended:
        cleaned = {sid: st for sid, st in cleaned.items()
                   if sid not in suspended}
        if not cleaned:
            raise AttendanceError(
                'الطلاب المحددون موقوفون في تاريخ هذه الجلسة. '
                'لم يتم حفظ أي سجل.')

    now = datetime.utcnow()
    created, updated, unchanged = [], [], []
    newly_absent = []

    for sid, status in cleaned.items():
        rec = existing.get(sid)
        if rec is None:
            db.session.add(InstituteAttendanceRecord(
                school_id=school.id, session_id=session.id, student_id=sid,
                status=status, source=source, recorded_by=actor_user_id,
                recorded_at=now))
            created.append(sid)
            if status == InstituteAttendanceRecord.STATUS_ABSENT:
                newly_absent.append(sid)
        elif rec.status != status:
            was_absent = rec.status == InstituteAttendanceRecord.STATUS_ABSENT
            rec.status = status
            rec.source = source
            rec.recorded_by = actor_user_id
            rec.recorded_at = now
            updated.append(sid)
            if (status == InstituteAttendanceRecord.STATUS_ABSENT
                    and not was_absent):
                newly_absent.append(sid)
        else:
            # Byte-identical re-submission: touch nothing, notify nobody.
            unchanged.append(sid)

    session.status = InstituteAttendanceSession.STATUS_RECORDED
    session.source = source
    session.recorded_by = actor_user_id
    session.recorded_at = now

    # ── Durable path (INSTITUTE_ATTENDANCE_OUTBOX_ENABLED) ──────────────────
    # The parent in-app rows AND the push-delivery jobs are staged into THIS
    # transaction, so attendance and the notifications it promised commit
    # together or not at all. Nothing is sent to Firebase from this request.
    #
    # With the flag off, not a single statement below changes: the legacy
    # inline path runs exactly as it does in production today.
    outbox_path = bool(notify and newly_absent and outbox.enabled())
    if outbox_path:
        try:
            staged = outbox.stage_absence_deliveries(
                school, session, newly_absent, now=now)
        except Exception:
            # Attendance must never commit without the notification work it
            # promised. Roll the whole thing back and say so.
            db.session.rollback()
            log.exception('[institute-attendance] outbox staging failed '
                          'session_id=%s — attendance NOT saved',
                          getattr(session, 'id', None))
            raise AttendanceError(
                'تعذّر تجهيز إشعارات الغياب، ولم يتم حفظ الحضور. '
                'يرجى المحاولة مرة أخرى.')

    try:
        db.session.commit()
    except IntegrityError:
        # Lost a race on uq_institute_attendance_session_student, or on the
        # outbox dedup key when an identical submission committed first.
        # Nothing of ours committed; the winner already stored an equivalent row.
        db.session.rollback()
        raise AttendanceError(
            'تم حفظ حضور هذه الجلسة من جهة أخرى في الوقت نفسه. '
            'يرجى إعادة فتح الصفحة لعرض السجل المحدّث.')

    if notify and newly_absent:
        if outbox_path:
            # Committed and durable. A separate worker delivers it; this
            # request returns now. There is deliberately NO inline fallback —
            # falling back to Firebase here would reintroduce exactly the
            # blocking call this path exists to remove.
            log.info('[institute-attendance] session=%s staged %d push job(s) '
                     'for %d newly-absent student(s)',
                     session.id, staged, len(newly_absent))
        else:
            _notify_absent(school, session, newly_absent)

    return {'created': len(created), 'updated': len(updated),
            'unchanged': len(unchanged), 'notified': len(newly_absent),
            'skipped_suspended': len(suspended)}


def _notify_absent(school, session, student_ids):
    """Parent absence notification, reusing the existing Core School path.

    Deliberately identical in shape to the school-section absence notification
    (same ntype 'attendance', same 'action'/'status' keys, same
    send_to_parents_of_student targeting) so the mobile router, badge counts
    and tap routing keep working unchanged. Institute context is added as
    EXTRA keys only; nothing existing is renamed or removed.

    Only genuinely NEW absences reach here, so a retry or an unchanged
    re-submission produces no duplicate notification. Never raises: a delivery
    failure must not undo a committed attendance record.
    """
    import logging
    log = logging.getLogger('mecha.institute.attendance')
    try:
        from app.models import Notification, parent_students
        from app.services.notifications import NotificationService
        group = db.session.get(InstituteStudyGroup, session.group_id,
                               execution_options=OPTS)
        group_name = group.name if group else ''
        date_str = session.session_date.strftime('%Y-%m-%d')

        students = (Student.query.execution_options(**OPTS)
                    .filter(Student.id.in_(student_ids),
                            Student.school_id == school.id).all())

        # The in-app parent feed row. NotificationService.send_to_users only
        # persists a PushNotification (the delivery log), so the school absence
        # flow creates this row separately — mirrored here so the parent feed,
        # badge counts and tap routing behave identically for an institute.
        for student in students:
            body = (f'تم تسجيل الطالب {student.full_name} غائباً في '
                    f'{group_name} بتاريخ {date_str}.')
            for row in db.session.query(parent_students.c.user_id).filter(
                    parent_students.c.student_id == student.id).all():
                db.session.add(Notification(
                    school_id=school.id, title='تنبيه غياب', body=body,
                    ntype='attendance', target_user_id=row[0],
                    created_by=None))
        db.session.commit()

        for student in students:
            NotificationService.send_to_parents_of_student(
                student.id,
                'تنبيه غياب',
                f'تم تسجيل الطالب {student.full_name} غائباً في '
                f'{group_name} بتاريخ {date_str}.',
                ntype='attendance',
                data={
                    # Existing school-absence contract, unchanged.
                    'type':         'attendance',
                    'ntype':        'attendance',
                    'action':       'absent',
                    'status':       'absent',
                    'student_id':   str(student.id),
                    'student_name': student.full_name,
                    'date':         date_str,
                    'screen':       'attendance',
                    # Additive institute context.
                    'institute_group_id':   str(session.group_id),
                    'institute_group_name': group_name,
                    'session_id':           str(session.id),
                },
            )
    except Exception:
        log.exception('[institute-attendance] absence notification failed '
                      'session_id=%s', getattr(session, 'id', None))


def attendance_summary(school, session_ids):
    """{session_id: {status: count}} for a page of sessions, in ONE query."""
    session_ids = list(session_ids or [])
    if not session_ids or school is None:
        return {}
    rows = (db.session.query(InstituteAttendanceRecord.session_id,
                             InstituteAttendanceRecord.status,
                             db.func.count(InstituteAttendanceRecord.id))
            .filter(InstituteAttendanceRecord.school_id == school.id,
                    InstituteAttendanceRecord.session_id.in_(session_ids))
            .group_by(InstituteAttendanceRecord.session_id,
                      InstituteAttendanceRecord.status)
            .all())
    out = {}
    for sid, status, count in rows:
        out.setdefault(sid, {})[status] = count
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Read-only attendance report
# ═════════════════════════════════════════════════════════════════════════════

# Display-only bucket for a student-lesson with NO attendance row: either the
# lesson was never recorded, or it was recorded without a status for this
# student. Never stored, never an InstituteAttendanceRecord status, and never
# counted as an absence (invariant 2).
REPORT_UNRECORDED = 'unrecorded'
REPORT_UNRECORDED_LABEL_AR = 'غير مسجلة'
REPORT_MAX_NAME_QUERY = 100


def _enrollment_covers(enrollment, occurrence, school) -> bool:
    """True when the membership overlapped the lesson's local time window.

    enrolled_at / ended_at are stored as naive UTC; the lesson's date and times
    are local wall-clock values. Both sides are compared in LOCAL time, so a
    student who joined after a lesson ended, or whose membership ended before a
    lesson started, is not counted for that lesson.
    """
    lesson_start = datetime.combine(occurrence.date, occurrence.start_time)
    lesson_end = datetime.combine(occurrence.date, occurrence.end_time)
    joined = to_local(enrollment.enrolled_at, school)
    if joined is not None and joined >= lesson_end:
        return False
    if enrollment.ended_at is not None:
        left = to_local(enrollment.ended_at, school)
        if left <= lesson_start:
            return False
    return True


def attendance_report(school, groups, start: date_type, end: date_type, *,
                      name_query: str | None = None, today=None) -> dict:
    """Per-student, per-lesson attendance rows for `groups`. READ-ONLY.

    `groups` must already be the caller's AUTHORIZED scope (manager: the
    institute's groups; instructor: their own). Anything outside `school` is
    dropped again here, so a scope bug cannot widen the result.

    Which lessons: the same computed occurrences the sessions page lists
    (occurrences_for_range), limited to lessons up to `today` — a future lesson
    is not "unrecorded", it simply has not happened — plus any future lesson
    that was nevertheless recorded.

    Which students per lesson:
      * every student whose enrollment in that group overlapped the lesson
        (joined before it ended, not ended before it started), and
      * any student holding a record for that lesson, even if the membership
        has since ended — mirroring session_roster(): recorded history never
        disappears.

    Status: the stored record status, or REPORT_UNRECORDED when no record
    exists. Nothing is inferred from the time having passed.

    Queries: two for the occurrences, one for enrollments, one for records.
    Nothing is written.
    """
    totals = {s: 0 for s in InstituteAttendanceRecord.STATUSES}
    totals[REPORT_UNRECORDED] = 0
    result = {'rows': [], 'totals': totals, 'lessons': 0,
              'recorded_lessons': 0, 'unrecorded_lessons': 0,
              'students': 0, 'student_names': [], 'rate': None}

    groups = [g for g in (groups or []) if school is not None
              and g.school_id == school.id]
    if not groups or start > end:
        return result
    today = today or local_today(school)
    name_query = (name_query or '').strip()[:REPORT_MAX_NAME_QUERY]

    occurrences = [o for o in occurrences_for_range(school, groups, start, end)
                   if o.date <= today or o.is_recorded]
    if not occurrences:
        return result
    group_ids = {o.group.id for o in occurrences}
    session_ids = [o.session.id for o in occurrences if o.session]

    enr_q = (db.session.query(InstituteGroupEnrollment, Student)
             .join(Student, Student.id == InstituteGroupEnrollment.student_id)
             .filter(InstituteGroupEnrollment.school_id == school.id,
                     InstituteGroupEnrollment.group_id.in_(group_ids),
                     Student.school_id == school.id))
    if name_query:
        enr_q = enr_q.filter(
            Student.full_name.icontains(name_query, autoescape=True))
    enrollments_by_group = {}
    for enr, stu in enr_q.all():
        enrollments_by_group.setdefault(enr.group_id, []).append((enr, stu))

    records_by_session = {}
    if session_ids:
        rec_q = (db.session.query(InstituteAttendanceRecord, Student)
                 .join(Student,
                       Student.id == InstituteAttendanceRecord.student_id)
                 .filter(InstituteAttendanceRecord.school_id == school.id,
                         InstituteAttendanceRecord.session_id.in_(session_ids),
                         Student.school_id == school.id))
        if name_query:
            rec_q = rec_q.filter(
                Student.full_name.icontains(name_query, autoescape=True))
        for rec, stu in rec_q.all():
            records_by_session.setdefault(rec.session_id, {})[stu.id] = (rec, stu)

    rows = []
    lessons = recorded_lessons = 0
    for occ in occurrences:
        recs = records_by_session.get(occ.session.id, {}) if occ.session else {}
        lesson_rows = {}
        for enr, stu in enrollments_by_group.get(occ.group.id, ()):
            if stu.id in lesson_rows or not _enrollment_covers(enr, occ, school):
                continue
            lesson_rows[stu.id] = (stu, recs.get(stu.id, (None, None))[0])
        for sid, (rec, stu) in recs.items():
            lesson_rows.setdefault(sid, (stu, rec))
        if not lesson_rows:
            continue

        lessons += 1
        if occ.is_recorded:
            recorded_lessons += 1
        for stu, rec in sorted(lesson_rows.values(),
                               key=lambda pair: pair[0].full_name or ''):
            status = rec.status if rec is not None else REPORT_UNRECORDED
            totals[status] = totals.get(status, 0) + 1
            rows.append({
                'student': stu, 'group': occ.group, 'date': occ.date,
                'day_of_week': occ.day_of_week,
                'start_time': occ.start_time, 'end_time': occ.end_time,
                'lesson_recorded': occ.is_recorded,
                'status': status,
                'recorded_at': rec.recorded_at if rec is not None else None,
                'notes': rec.notes if rec is not None else None,
            })

    # Same convention as the school report: late counts as attended, and the
    # excused bucket (the analogue of "on leave") is outside the denominator.
    # Unrecorded rows are never part of it.
    present = totals[InstituteAttendanceRecord.STATUS_PRESENT]
    late = totals[InstituteAttendanceRecord.STATUS_LATE]
    absent = totals[InstituteAttendanceRecord.STATUS_ABSENT]
    counted = present + late + absent
    names = sorted({r['student'].id: r['student'].full_name or ''
                    for r in rows}.values())
    result.update({
        'rows': rows, 'lessons': lessons,
        'recorded_lessons': recorded_lessons,
        'unrecorded_lessons': lessons - recorded_lessons,
        'students': len(names), 'student_names': names,
        'rate': round((present + late) / counted * 100, 1) if counted else None,
    })
    return result
