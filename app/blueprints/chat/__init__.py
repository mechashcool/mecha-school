"""
Chat / Messaging module — school admin web interface
=====================================================
Routes (all require login + chat module enabled):

GET  /chat/                                        – room list
GET  /chat/rooms/create                            – create room form
POST /chat/rooms/create                            – submit new room
GET  /chat/rooms/<id>                              – room messages viewer
GET  /chat/rooms/<id>/edit                         – edit room form
POST /chat/rooms/<id>/edit                         – update room
POST /chat/rooms/<id>/close                        – close room
POST /chat/rooms/<id>/reopen                       – reopen room
POST /chat/rooms/<id>/delete                       – permanently delete group room
POST /chat/rooms/<id>/members/<uid>/block          – block member
POST /chat/rooms/<id>/members/<uid>/unblock        – unblock member
POST /chat/rooms/<id>/members/<uid>/remove         – remove member from group
POST /chat/rooms/<id>/members/<uid>/make-admin     – promote to admin
POST /chat/rooms/<id>/members/<uid>/remove-admin   – demote to member
POST /chat/rooms/<id>/messages/<mid>/delete        – soft-delete message
POST /chat/rooms/<id>/rebuild-members              – rebuild auto-members
GET  /chat/rooms/<id>/schedule                     – send-schedule form
POST /chat/rooms/<id>/schedule                     – save schedule
GET  /chat/settings                                – school chat settings page
POST /chat/settings                                – save school chat settings
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import func

from flask import (
    Blueprint, Response, abort, current_app, flash, jsonify, redirect,
    render_template, request, stream_with_context, url_for,
)
from flask_login import current_user, login_required

from app.models import (
    db, ChatRoom, ChatRoomMember, ChatMessage, ChatMessageRead,
    ChatRoomSchedule, Section, Grade, Subject,
    User, Employee, Student, Role,
    parent_students, teacher_subjects,
)
from app.utils.decorators import (admin_required, permission_required,
                                   get_current_school, get_active_year)
from app.utils.modules import is_module_enabled

_log = logging.getLogger('mecha.chat')

chat_bp = Blueprint('chat', __name__, template_folder='../../templates/chat')

# SSE streams hold a gthread thread for their lifetime; limit each stream so
# threads are freed periodically and POST requests are never starved.
SSE_MAX_ITERATIONS = 55  # ~55 s; client reconnects immediately on timeout


# ─── AJAX helpers ─────────────────────────────────────────────────────────────

def _is_ajax_request() -> bool:
    """Check if request is AJAX (expects JSON response)."""
    return (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or 'application/json' in request.headers.get('Accept', '')
    )


def _format_message_json(msg: ChatMessage) -> dict:
    """Format a ChatMessage for JSON response."""
    return {
        'id': msg.id,
        'body': msg.body or '[مرفق]',
        'sender_name': msg.sender.full_name if msg.sender else 'محذوف',
        'created_at': msg.created_at.strftime('%Y-%m-%d %H:%M'),
        'is_self': msg.sender_user_id == current_user.id,
    }


# ─── SSE / polling helpers ────────────────────────────────────────────────────

def _sse_generator(room_id: int, user_id: int, after_id: int):
    """
    Yield SSE events for new messages, then heartbeats.

    Key correctness rules:
    - Materialize sender.full_name while session is still open.
    - Call db.session.remove() BEFORE yielding so the DB connection is
      returned to the pool while the generator is suspended.
    - Exit after SSE_MAX_ITERATIONS so the Gunicorn gthread is freed and
      POST requests are never starved.  The client reconnects immediately
      on receiving the 'timeout' event.
    """
    last_id = after_id
    iters   = 0
    _log.info('[chat-sse] connected room_id=%s user_id=%s after_id=%s', room_id, user_id, last_id)
    try:
        while iters < SSE_MAX_ITERATIONS:
            iters += 1
            try:
                new_msgs = (ChatMessage.query
                            .filter(
                                ChatMessage.room_id == room_id,
                                ChatMessage.id > last_id,
                                ChatMessage.is_deleted == False,
                            )
                            .order_by(ChatMessage.id.asc())
                            .limit(50)
                            .all())

                # Build all event strings while session is open (accesses sender relation).
                events: list[tuple[int, str]] = []
                for msg in new_msgs:
                    events.append((msg.id, json.dumps({
                        'id':          msg.id,
                        'body':        msg.body or '[مرفق]',
                        'sender_name': msg.sender.full_name if msg.sender else 'محذوف',
                        'created_at':  msg.created_at.strftime('%Y-%m-%d %H:%M'),
                        'is_self':     msg.sender_user_id == user_id,
                    }, ensure_ascii=False)))

                # Release DB connection BEFORE yielding — never hold it while suspended.
                db.session.remove()

                if events:
                    for (msg_id, payload) in events:
                        yield f'event: message\nid: {msg_id}\ndata: {payload}\n\n'
                        last_id = msg_id
                    _log.info('[chat-sse] sent room_id=%s count=%s', room_id, len(events))
                else:
                    yield ': ping\n\n'

            except Exception as exc:
                _log.warning('[chat-sse] query error room_id=%s: %s', room_id, exc)
                try:
                    db.session.remove()
                except Exception:
                    pass
                yield ': error-retry\n\n'

            time.sleep(1)

        # Stream lifetime limit reached — tell client to reconnect immediately.
        _log.debug('[chat-sse] timeout room_id=%s after %d iters', room_id, iters)
        yield 'event: timeout\ndata: reconnect\n\n'

    except GeneratorExit:
        _log.info('[chat-sse] disconnected room_id=%s user_id=%s', room_id, user_id)
        try:
            db.session.remove()
        except Exception:
            pass


def _make_sse_response(room_id: int, user_id: int, after_id: int) -> Response:
    return Response(
        stream_with_context(_sse_generator(room_id, user_id, after_id)),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        },
    )


def _poll_messages_json(room_id: int, user_id: int, after_id: int):
    """Return JSON with messages after after_id (polling fallback)."""
    msgs = (ChatMessage.query
            .filter(
                ChatMessage.room_id == room_id,
                ChatMessage.id > after_id,
                ChatMessage.is_deleted == False,
            )
            .order_by(ChatMessage.id.asc())
            .limit(50)
            .all())
    return jsonify({
        'messages': [{
            'id':          m.id,
            'body':        m.body or '[مرفق]',
            'sender_name': m.sender.full_name if m.sender else 'محذوف',
            'created_at':  m.created_at.strftime('%Y-%m-%d %H:%M'),
            'is_self':     m.sender_user_id == user_id,
        } for m in msgs]
    })


# ─── Module guard ─────────────────────────────────────────────────────────────

def _require_chat_module():
    school_id = getattr(current_user, 'school_id', None)
    if not current_user.is_super_admin and not is_module_enabled(school_id, 'chat'):
        abort(403)


# ─── Member auto-generation ───────────────────────────────────────────────────

def _collect_user_ids(
    school_id: int,
    scope: str,
    section_id=None,
    grade_id=None,
    subject_id=None,
    stage=None,
    academic_year_id=None,
) -> tuple[set[int], dict]:
    """
    Compute which user_ids should be auto-added to a room based on scope.
    Returns (user_ids, stats) where stats contains counts for flash messages.

    Always includes all school admins (Role.is_admin == True).
    All DB lookups use bypass_tenant_scope + bypass_year_scope so they work
    regardless of the request's active-year context.
    """
    user_ids: set[int] = set()
    stats = {'admins': 0, 'teachers': 0, 'parents': 0, 'students': 0, 'no_parent_students': 0}

    # ── Always include all school-level admin users ───────────────────────────
    try:
        admins = (
            User.query
            .execution_options(bypass_tenant_scope=True)
            .join(User.role)
            .filter(
                User.school_id == school_id,
                User.is_active == True,
                Role.is_admin == True,
            )
            .all()
        )
        for u in admins:
            user_ids.add(u.id)
        stats['admins'] = len(admins)
        _log.info('[chat] scope=%s school=%s admins_found=%d',
                  scope, school_id, len(admins))
    except Exception as exc:
        _log.error('[chat] admin query failed school=%s: %s', school_id, exc)

    # ── Scope-specific members ────────────────────────────────────────────────

    if scope in ('school', 'announcement'):
        # All active teachers via Employee records
        emps = (
            Employee.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school_id, status='active')
            .all()
        )
        for emp in emps:
            if emp.user_id:
                user_ids.add(emp.user_id)
                stats['teachers'] += 1

        # Parents only via their linked active students — not all school users
        active_student_ids = [
            s.id for s in
            Student.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school_id, status='active')
            .all()
        ]
        _log.info('[chat] scope=%s active_students=%d', scope, len(active_student_ids))
        if active_student_ids:
            parent_rows = (
                db.session.query(parent_students.c.user_id)
                .filter(parent_students.c.student_id.in_(active_student_ids))
                .all()
            )
            for (uid,) in parent_rows:
                user_ids.add(uid)
                stats['parents'] += 1
            # Warn about students with no linked parent
            linked_student_ids = set(
                row[0] for row in
                db.session.query(parent_students.c.student_id)
                .filter(parent_students.c.student_id.in_(active_student_ids))
                .distinct()
                .all()
            )
            stats['no_parent_students'] += len(active_student_ids) - len(linked_student_ids)

    elif scope == 'section' and section_id:
        _add_section_members(school_id, int(section_id), user_ids, stats)

    elif scope == 'grade' and grade_id:
        grade = (
            Grade.query
            .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
            .filter_by(id=int(grade_id), school_id=school_id)
            .first()
        )
        if grade:
            sections = (
                Section.query
                .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
                .filter_by(grade_id=grade.id, school_id=school_id)
                .all()
            )
            for sec in sections:
                _add_section_members(school_id, sec.id, user_ids, stats)
        else:
            _log.warning('[chat] grade_id=%s not found for school=%s', grade_id, school_id)

    elif scope == 'stage' and stage:
        grades = (
            Grade.query
            .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
            .filter_by(school_id=school_id, stage=stage)
            .all()
        )
        _log.info('[chat] stage=%r grades_found=%d', stage, len(grades))
        for grade in grades:
            sections = (
                Section.query
                .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
                .filter_by(grade_id=grade.id, school_id=school_id)
                .all()
            )
            for sec in sections:
                _add_section_members(school_id, sec.id, user_ids, stats)

    elif scope == 'subject' and subject_id:
        rows = (
            db.session.query(
                teacher_subjects.c.section_id,
                teacher_subjects.c.employee_id,
            )
            .filter(teacher_subjects.c.subject_id == int(subject_id))
            .all()
        )
        _log.info('[chat] subject_id=%s teacher_subject_rows=%d', subject_id, len(rows))
        for sec_id, emp_id in rows:
            emp = (
                Employee.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(id=emp_id, school_id=school_id)
                .first()
            )
            if emp and emp.user_id:
                user_ids.add(emp.user_id)
                stats['teachers'] += 1
            _add_section_members(school_id, sec_id, user_ids, stats,
                                 include_teachers=False)

    elif scope == 'parent_scope':
        # Parents-only hierarchical scope — no teachers added.
        # The selected academic filters resolve to a concrete set of CURRENT-YEAR
        # sections, and only the parents of active students in those sections are
        # added. Granularity: section_id > grade_id > stage > all.
        #
        # Year isolation is mandatory: Grade/Section are year-scoped models, but
        # these queries set bypass_tenant_scope (which disables BOTH school and
        # year ORM criteria), so we MUST re-apply academic_year_id explicitly.
        # Otherwise a stage/grade match would pull grades/sections from every
        # academic year and add parents school-wide.
        #
        # Snapshot the user_ids already present (admins) so the parent count below
        # reflects UNIQUE parents added, not raw parent_students rows.
        _before_parent_uids = set(user_ids)

        def _year_sections_query():
            q = (Section.query
                 .execution_options(bypass_tenant_scope=True)
                 .filter_by(school_id=school_id))
            if academic_year_id:
                q = q.filter_by(academic_year_id=academic_year_id)
            return q

        target_section_ids: list[int] = []
        if section_id:
            # Exact section — validate it belongs to this school and (when known)
            # this academic year before using it.
            sec = _year_sections_query().filter_by(id=int(section_id)).first()
            if sec:
                target_section_ids = [sec.id]
            else:
                _log.warning('[chat] parent_scope section_id=%s not found for '
                             'school=%s year=%s', section_id, school_id, academic_year_id)
        elif grade_id:
            target_section_ids = [
                s.id for s in _year_sections_query()
                .filter_by(grade_id=int(grade_id)).all()
            ]
            if not target_section_ids:
                _log.warning('[chat] parent_scope grade_id=%s has no sections for '
                             'school=%s year=%s', grade_id, school_id, academic_year_id)
        elif stage:
            grade_q = (Grade.query
                       .execution_options(bypass_tenant_scope=True)
                       .filter_by(school_id=school_id, stage=stage))
            if academic_year_id:
                grade_q = grade_q.filter_by(academic_year_id=academic_year_id)
            grade_ids = [g.id for g in grade_q.all()]
            _log.info('[chat] parent_scope stage=%r year=%s grades_found=%d',
                      stage, academic_year_id, len(grade_ids))
            if grade_ids:
                target_section_ids = [
                    s.id for s in _year_sections_query()
                    .filter(Section.grade_id.in_(grade_ids)).all()
                ]
        else:
            # No filter selected → all sections in the current academic year.
            target_section_ids = [s.id for s in _year_sections_query().all()]

        _log.info('[chat] parent_scope resolved sections=%d (section=%s grade=%s '
                  'stage=%r year=%s)', len(target_section_ids),
                  section_id, grade_id, stage, academic_year_id)
        for sid in target_section_ids:
            _add_parent_members_only(school_id, sid, user_ids, stats)

        # Accurate unique-parent count: everything added during this branch is a
        # parent (no teachers), so the new user_ids minus the admin snapshot are
        # exactly the distinct parents added.
        stats['parents'] = len(user_ids - _before_parent_uids)

    elif scope == 'teachers':
        # All active teachers for the school only — no parents, no students.
        # Two-step: collect employee user_ids, then validate each linked User also
        # belongs to school_id.  An employee→user school mismatch (e.g. a user
        # reassigned to another school without updating the employee record) must
        # never cause a cross-school member to be added.
        emps = (
            Employee.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school_id, status='active')
            .all()
        )
        emp_user_ids = [emp.user_id for emp in emps if emp.user_id]
        if emp_user_ids:
            valid_users = (
                User.query
                .execution_options(bypass_tenant_scope=True)
                .filter(
                    User.id.in_(emp_user_ids),
                    User.school_id == school_id,
                    User.is_active == True,
                )
                .all()
            )
            for u in valid_users:
                user_ids.add(u.id)
                stats['teachers'] += 1
            skipped = len(emp_user_ids) - stats['teachers']
            if skipped > 0:
                _log.warning('[chat] teachers scope: skipped %d employee-linked users '
                             'with mismatched school_id (expected school=%s)',
                             skipped, school_id)
        _log.info('[chat] scope=teachers school=%s teachers_found=%d',
                  school_id, stats['teachers'])

    _log.info('[chat] _collect_user_ids scope=%s total=%d stats=%s',
              scope, len(user_ids), stats)
    return user_ids, stats


def _add_section_members(
    school_id: int,
    section_id: int,
    user_ids: set[int],
    stats: dict,
    include_teachers: bool = True,
) -> None:
    """
    Add teachers and parents for a single section into user_ids (mutates in place).
    Uses bypass options so year-scope doesn't block the queries.
    """
    # Homeroom teacher
    if include_teachers:
        section = (
            Section.query
            .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
            .filter_by(id=section_id, school_id=school_id)
            .first()
        )
        if not section:
            _log.warning('[chat] section_id=%s not found for school=%s',
                         section_id, school_id)
            return

        if section.teacher_id:
            emp = (
                Employee.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(id=section.teacher_id, school_id=school_id)
                .first()
            )
            if emp and emp.user_id:
                user_ids.add(emp.user_id)
                stats['teachers'] += 1

        # Subject teachers for this section
        subj_rows = (
            db.session.query(teacher_subjects.c.employee_id)
            .filter(teacher_subjects.c.section_id == section_id)
            .all()
        )
        for (eid,) in subj_rows:
            emp = (
                Employee.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(id=eid, school_id=school_id)
                .first()
            )
            if emp and emp.user_id:
                user_ids.add(emp.user_id)
                stats['teachers'] += 1

    # Parents of active students in this section
    student_ids = [
        s.id for s in
        Student.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(section_id=section_id, school_id=school_id, status='active')
        .all()
    ]
    _log.info('[chat] section=%s active_students=%d', section_id, len(student_ids))

    if student_ids:
        parent_rows = (
            db.session.query(parent_students.c.user_id)
            .filter(parent_students.c.student_id.in_(student_ids))
            .all()
        )
        for (uid,) in parent_rows:
            user_ids.add(uid)
            stats['parents'] += 1

        linked_student_ids = set(
            row[0] for row in
            db.session.query(parent_students.c.student_id)
            .filter(parent_students.c.student_id.in_(student_ids))
            .distinct()
            .all()
        )
        no_parent = len(student_ids) - len(linked_student_ids)
        stats['no_parent_students'] = stats.get('no_parent_students', 0) + no_parent
        _log.info('[chat] section=%s parents_found=%d no_parent_students=%d',
                  section_id, len(parent_rows), no_parent)


def _add_parent_members_only(
    school_id: int,
    section_id: int,
    user_ids: set[int],
    stats: dict,
) -> None:
    """
    Add only parent users (no teachers) for active students in one section.
    Used exclusively by the parent_scope hierarchical filter.
    """
    section = (
        Section.query
        .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
        .filter_by(id=section_id, school_id=school_id)
        .first()
    )
    if not section:
        _log.warning('[chat] parent_scope section_id=%s not found for school=%s',
                     section_id, school_id)
        return

    student_ids = [
        s.id for s in
        Student.query
        .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
        .filter_by(section_id=section_id, school_id=school_id, status='active')
        .all()
    ]
    stats['students'] = stats.get('students', 0) + len(student_ids)
    _log.info('[chat] parent_scope section=%s active_students=%d', section_id, len(student_ids))

    if student_ids:
        parent_rows = (
            db.session.query(parent_students.c.user_id)
            .filter(parent_students.c.student_id.in_(student_ids))
            .all()
        )
        for (uid,) in parent_rows:
            user_ids.add(uid)
            stats['parents'] += 1

        linked_ids = set(
            r[0] for r in
            db.session.query(parent_students.c.student_id)
            .filter(parent_students.c.student_id.in_(student_ids))
            .distinct().all()
        )
        stats['no_parent_students'] = stats.get('no_parent_students', 0) + len(student_ids) - len(linked_ids)
        _log.info('[chat] parent_scope section=%s parents_found=%d no_parent_students=%d',
                  section_id, len(parent_rows), len(student_ids) - len(linked_ids))


def _sync_members(room: ChatRoom, user_ids: set[int], creator_id: int) -> int:
    """
    Add missing members to room; do not remove existing ones.
    If the creator is already a member, upgrades their role to 'owner'.
    Returns the count of newly added members.

    Fail-closed cross-school guard
    ──────────────────────────────
    This is the single chokepoint through which every auto-scope, multi-section,
    and custom membership flows.  Regardless of how `user_ids` was collected —
    employee→user links, parent_students rows, or client-submitted custom
    member_ids — each candidate is re-validated against the room's own school
    before a ChatRoomMember row is created.  A mismatched Employee.school_id /
    User.school_id, a stale cross-school parent link, or a forged member_id can
    therefore never inject a member from another school.

    The creator is added explicitly below and is intentionally NOT filtered
    here: a super-admin creator legitimately has User.school_id = NULL while the
    room belongs to the selected school.
    """
    if user_ids:
        valid_ids = {
            row[0] for row in
            User.query
            .execution_options(bypass_tenant_scope=True)
            .with_entities(User.id)
            .filter(User.id.in_(user_ids), User.school_id == room.school_id)
            .all()
        }
        dropped = set(user_ids) - valid_ids
        if dropped:
            _log.warning(
                '[chat] _sync_members blocked %d cross-school user_id(s) '
                'room_id=%s school_id=%s dropped=%s',
                len(dropped), room.id, room.school_id, sorted(dropped),
            )
        user_ids = valid_ids

    existing = {m.user_id: m for m in room.members.all()}
    added = 0

    # Ensure creator is owner
    if creator_id in existing:
        if existing[creator_id].role != 'owner':
            existing[creator_id].role = 'owner'
    else:
        db.session.add(ChatRoomMember(
            room_id=room.id, user_id=creator_id, role='owner',
        ))
        added += 1

    for uid in user_ids:
        if uid == creator_id:
            continue
        if uid not in existing:
            db.session.add(ChatRoomMember(
                room_id=room.id, user_id=uid, role='member',
            ))
            added += 1

    return added


# ─── Schedule helpers ─────────────────────────────────────────────────────────

def _can_send_now(room: ChatRoom, school) -> tuple[bool, str]:
    """
    Per-day independent schedule check.

    Each day is evaluated in isolation:
    - No row for today, or today's row is disabled  → allowed (no restriction for today).
    - Today's row is enabled and current time is within open/close → allowed.
    - Today's row is enabled but current time is outside the range  → blocked.

    Other days' enabled/disabled state never affects today's result.
    """
    try:
        from app.utils.attendance_helpers import get_local_now
        local_now = get_local_now(school)
    except Exception:
        # Fall back to Iraq local time rather than raw UTC.
        # UTC gives the wrong weekday between 21:00–24:00 UTC (midnight–03:00 Iraq).
        import pytz as _pytz
        _log.warning(
            '[chat] _can_send_now: get_local_now failed for room_id=%s — '
            'falling back to Asia/Baghdad', room.id,
        )
        local_now = datetime.now(_pytz.timezone('Asia/Baghdad')).replace(tzinfo=None)

    # Python weekday: Mon=0..Sun=6  →  our Sunday=0 scheme
    dow = (local_now.weekday() + 1) % 7
    now_time = local_now.time()

    # Look up ONLY today's row — other days are irrelevant
    today_sch = room.schedules.filter_by(day_of_week=dow).first()

    if today_sch is None or not today_sch.is_enabled:
        # No schedule configured for today, or today is disabled → no restriction
        return True, ''

    if today_sch.open_time <= now_time <= today_sch.close_time:
        return True, ''

    return (False,
            'المراسلات غير متاحة حالياً، يمكنكم الإرسال ضمن '
            'أوقات التواصل المحددة من المدرسة.')


# ─── Admin permission helpers ─────────────────────────────────────────────────

def _is_room_admin(room: ChatRoom) -> bool:
    """
    True if current_user is the school-level admin/manager for this room's school.
    For super_admin: requires get_current_school() to resolve to the room's school
    so we never grant cross-school access.
    """
    if not current_user.is_authenticated:
        return False
    if current_user.is_super_admin:
        s = get_current_school()
        return s is not None and s.id == room.school_id
    # School manager, or any role granted manage_chat — in BOTH cases the
    # room must belong to the user's own school (unchanged school binding).
    return (
        (current_user.is_school_admin
         or current_user.has_permission('manage_chat'))
        and current_user.school_id == room.school_id
    )


def _ensure_admin_member(room: ChatRoom) -> ChatRoomMember:
    """
    Idempotently guarantee current_user has a ChatRoomMember row for this room.
    • Creates with role='admin' if absent.
    • Upgrades role to 'admin' if currently 'member'.
    • Leaves 'owner'/'admin' rows untouched.
    Flushes but does NOT commit — caller must commit.
    """
    mem = ChatRoomMember.query.filter_by(
        room_id=room.id, user_id=current_user.id
    ).first()
    if mem is None:
        mem = ChatRoomMember(room_id=room.id, user_id=current_user.id, role='admin')
        db.session.add(mem)
        db.session.flush()
        _log.info(
            '[chat] manager override: auto-added admin member '
            'user_id=%s room_id=%s school_id=%s',
            current_user.id, room.id, room.school_id,
        )
    elif mem.role == 'member':
        mem.role = 'admin'
        db.session.flush()
        _log.info(
            '[chat] manager override: upgraded member→admin '
            'user_id=%s room_id=%s',
            current_user.id, room.id,
        )
    return mem


# ─── Read-receipt helper ──────────────────────────────────────────────────────

def _mark_all_room_messages_read(room_id: int, user_id: int, label: str = '') -> None:
    """Insert ChatMessageRead rows for every non-deleted, non-self message in
    the room that the user has not yet read.

    Scans the entire room — not a limited window — so rooms with many messages
    (where the load-100 window covers only old messages) are fully cleared.
    Safe to call even if all messages are already read (no-op in that case).
    Silently rolls back and returns on any DB error so a read-marking failure
    never breaks the page load.
    """
    try:
        _all_ids = [
            r[0] for r in
            db.session.query(ChatMessage.id)
            .filter(
                ChatMessage.room_id == room_id,
                ChatMessage.is_deleted == False,  # noqa: E712
                ChatMessage.sender_user_id != user_id,
            )
            .all()
        ]
        if not _all_ids:
            return
        _already = {
            r.message_id for r in
            ChatMessageRead.query
            .filter(
                ChatMessageRead.user_id == user_id,
                ChatMessageRead.message_id.in_(_all_ids),
            )
            .all()
        }
        _new = [
            ChatMessageRead(message_id=mid, user_id=user_id)
            for mid in _all_ids
            if mid not in _already
        ]
        if _new:
            db.session.add_all(_new)
            db.session.commit()
            _log.debug(
                '[chat] %s: marked %d messages read room_id=%s user_id=%s',
                label or 'mark_read', len(_new), room_id, user_id,
            )
    except Exception:
        db.session.rollback()


# ─── Room list ────────────────────────────────────────────────────────────────

@chat_bp.route('/')
@login_required
@permission_required('manage_chat')
def index():
    _require_chat_module()
    school = get_current_school()
    if not school:
        abort(404)

    q = (ChatRoom.query
         .execution_options(bypass_tenant_scope=True)
         .filter_by(school_id=school.id)
         .order_by(ChatRoom.updated_at.desc()))

    type_filter   = request.args.get('type', '')
    status_filter = request.args.get('status', '')
    if type_filter:
        q = q.filter_by(type=type_filter)
    if status_filter == 'open':
        q = q.filter_by(is_closed=False, is_active=True)
    elif status_filter == 'closed':
        q = q.filter_by(is_closed=True)

    rooms = q.all()

    room_stats = []
    for room in rooms:
        last_msg = (ChatMessage.query
                    .filter_by(room_id=room.id, is_deleted=False)
                    .order_by(ChatMessage.created_at.desc())
                    .first())
        room_stats.append({
            'room':          room,
            'member_count':  room.members.count(),
            'message_count': room.messages.filter_by(is_deleted=False).count(),
            'last_message':  last_msg,
        })

    # Batch per-room unread counts — one query for all rooms, no N+1.
    # room_ids are already school-scoped via filter_by(school_id=school.id) above.
    if rooms:
        _read_subq = (db.session.query(ChatMessageRead.message_id)
                      .filter(ChatMessageRead.user_id == current_user.id))
        _unread_rows = (
            db.session.query(
                ChatMessage.room_id,
                func.count(ChatMessage.id).label('unread'),
            )
            .filter(
                ChatMessage.room_id.in_([r.id for r in rooms]),
                ChatMessage.is_deleted == False,  # noqa: E712
                ChatMessage.sender_user_id != current_user.id,
                ChatMessage.id.notin_(_read_subq),
            )
            .group_by(ChatMessage.room_id)
            .all()
        )
        _unread_by_room = {row.room_id: row.unread for row in _unread_rows}
    else:
        _unread_by_room = {}

    for stat in room_stats:
        stat['unread_count'] = _unread_by_room.get(stat['room'].id, 0)

    # Separate private (1-to-1) from group/announcement rooms.
    private_stats = [s for s in room_stats if s['room'].type == 'private']
    group_stats   = [s for s in room_stats if s['room'].type != 'private']

    # For each private room, find the other participant so the template can
    # show "محادثة مع <name>" instead of the raw stored room name.
    private_other_user = {}  # room_id -> User
    for stat in private_stats:
        room = stat['room']
        other_mem = (
            ChatRoomMember.query
            .filter(
                ChatRoomMember.room_id == room.id,
                ChatRoomMember.user_id != current_user.id,
            )
            .first()
        )
        if other_mem and other_mem.user:
            private_other_user[room.id] = other_mem.user

    # ── Load grades/stages for filter dropdowns ───────────────────────────────
    all_grades = (Grade.query
                  .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
                  .filter_by(school_id=school.id)
                  .order_by(Grade.name).all())
    all_stages = sorted(set(g.stage for g in all_grades if g.stage))

    return render_template('chat/index.html',
                           private_stats=private_stats,
                           group_stats=group_stats,
                           private_other_user=private_other_user,
                           type_filter=type_filter,
                           status_filter=status_filter,
                           all_grades=all_grades,
                           all_stages=all_stages,
                           school=school)


# ─── Create room ──────────────────────────────────────────────────────────────

@chat_bp.route('/rooms/create', methods=['GET', 'POST'])
@login_required
@permission_required('manage_chat')
def create_room():
    _require_chat_module()
    school = get_current_school()
    if not school:
        abort(404)
    year = get_active_year(school.id)

    # Restrict to the active year so duplicate grade names from older years
    # don't appear in the cascade picker (older grades have no live sections).
    if year:
        grades   = (Grade.query
                    .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
                    .filter_by(school_id=school.id, academic_year_id=year.id)
                    .order_by(Grade.name).all())
        sections = (Section.query
                    .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
                    .filter_by(school_id=school.id, academic_year_id=year.id)
                    .order_by(Section.name).all())
    else:
        grades   = []
        sections = []
    subjects = (Subject.query
                .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
                .filter_by(school_id=school.id)
                .order_by(Subject.name).all())

    # All active school users for the custom-scope member picker
    school_users = (
        User.query
        .execution_options(bypass_tenant_scope=True)
        .filter(User.school_id == school.id, User.is_active == True)
        .join(User.role)
        .order_by(Role.name, User.full_name)
        .all()
    )

    # Build grades→sections tree for cascade picker in the template
    grades_tree = []
    for g in grades:
        g_sections = [s for s in sections if s.grade_id == g.id]
        grades_tree.append({'grade': g, 'sections': g_sections})
    stages = sorted(set(g.stage for g in grades if g.stage))

    if request.method == 'POST':
        name       = request.form.get('name', '').strip()
        rtype      = request.form.get('type', 'group')
        scope      = request.form.get('scope', 'custom')
        is_ann     = bool(request.form.get('is_announcement_only'))
        allow_rep  = not bool(request.form.get('no_replies'))
        section_id = request.form.get('section_id') or None
        grade_id   = request.form.get('grade_id')   or None
        subject_id = request.form.get('subject_id') or None
        stage      = request.form.get('stage', '').strip() or None

        # For parent_scope, read the three hierarchical filter fields and override
        # the standard scope-fields so room storage and _collect_user_ids work correctly.
        if scope == 'parent_scope':
            stage      = request.form.get('parent_stage', '').strip() or None
            grade_id   = request.form.get('parent_grade_id') or None
            section_id = request.form.get('parent_section_id') or None

        if not name:
            flash('اسم المحادثة مطلوب.', 'danger')
            return render_template('chat/create_room.html',
                                   grades=grades, sections=sections,
                                   subjects=subjects, school_users=school_users,
                                   grades_tree=grades_tree, stages=stages)

        _log.info('[chat] create_room scope=%s section=%s grade=%s stage=%r subject=%s',
                  scope, section_id, grade_id, stage, subject_id)

        # ── Multi-section scope: collect members from multiple sections ────────
        if scope == 'multi_section':
            raw_ids = request.form.getlist('section_ids')
            uid_set: set[int] = set()
            stats = {'admins': 0, 'teachers': 0, 'parents': 0, 'no_parent_students': 0}

            # Always include school admins (same as _collect_user_ids does for every scope)
            try:
                admins = (
                    User.query
                    .execution_options(bypass_tenant_scope=True)
                    .join(User.role)
                    .filter(User.school_id == school.id,
                            User.is_active == True,
                            Role.is_admin == True)
                    .all()
                )
                for u in admins:
                    uid_set.add(u.id)
                stats['admins'] = len(admins)
            except Exception as exc:
                _log.error('[chat] multi_section admin query failed: %s', exc)

            valid_section_ids = []
            for sid_raw in raw_ids:
                try:
                    sid = int(sid_raw)
                except (ValueError, TypeError):
                    continue
                # Strict school ownership check before collecting members
                sec = (Section.query
                       .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
                       .filter_by(id=sid, school_id=school.id)
                       .first())
                if sec:
                    valid_section_ids.append(sid)
                    _add_section_members(school.id, sid, uid_set, stats)
                else:
                    _log.warning('[chat] create_room multi_section: section %s not in school %s',
                                 sid, school.id)

            _log.info('[chat] multi_section: %d sections → %d unique users stats=%s',
                      len(valid_section_ids), len(uid_set), stats)

            # Store as custom scope (multi-section is a special form of custom)
            room = ChatRoom(
                school_id            = school.id,
                academic_year_id     = year.id if year else None,
                name                 = name,
                type                 = rtype,
                scope                = 'custom',
                created_by_user_id   = current_user.id,
                is_announcement_only = is_ann,
                allow_replies        = allow_rep,
            )
            db.session.add(room)
            db.session.flush()
            uid_set.add(current_user.id)
            added = _sync_members(room, uid_set, current_user.id)
            db.session.commit()

            flash(f'تم إنشاء المحادثة "{name}" بنجاح من {len(valid_section_ids)} شعبة. '
                  f'الأعضاء: {added} (مشرفون: {stats["admins"]} — معلمون: {stats["teachers"]} — '
                  f'أولياء أمور: {stats["parents"]}).', 'success')
            if stats.get('no_parent_students', 0) > 0:
                flash(f'تنبيه: {stats["no_parent_students"]} طالب ليس لهم أولياء أمور مرتبطون.', 'warning')
            return redirect(url_for('chat.room_detail', room_id=room.id))

        # ── All other scopes (unchanged) ──────────────────────────────────────
        room = ChatRoom(
            school_id            = school.id,
            academic_year_id     = year.id if year else None,
            name                 = name,
            type                 = rtype,
            scope                = scope,
            stage                = stage,
            grade_id             = int(grade_id)   if grade_id   else None,
            section_id           = int(section_id) if section_id else None,
            subject_id           = int(subject_id) if subject_id else None,
            created_by_user_id   = current_user.id,
            is_announcement_only = is_ann,
            allow_replies        = allow_rep,
        )
        db.session.add(room)
        db.session.flush()  # get room.id before adding members

        if scope == 'custom':
            uid_set = set()
            for cid in request.form.getlist('member_ids'):
                try:
                    uid_set.add(int(cid))
                except (ValueError, TypeError):
                    pass
            stats = {'admins': 0, 'teachers': 0, 'parents': len(uid_set)}
        else:
            uid_set, stats = _collect_user_ids(
                school.id, scope,
                section_id = int(section_id) if section_id else None,
                grade_id   = int(grade_id)   if grade_id   else None,
                subject_id = int(subject_id) if subject_id else None,
                stage      = stage,
                academic_year_id = year.id if year else None,
            )

        # Creator is always owner — add explicitly so even an empty uid_set
        # still produces at least one member row.
        uid_set.add(current_user.id)

        added = _sync_members(room, uid_set, current_user.id)
        db.session.commit()

        _log.info('[chat] room %d created — uid_set=%d added=%d stats=%s',
                  room.id, len(uid_set), added, stats)

        if scope == 'parent_scope':
            flash(f'تم إنشاء المحادثة "{name}" بنجاح. '
                  f'الطلاب المطابقون: {stats.get("students", 0)} — '
                  f'أولياء الأمور المضافون: {stats["parents"]} — '
                  f'إجمالي الأعضاء: {len(uid_set)}.', 'success')
        else:
            flash(f'تم إنشاء المحادثة "{name}" بنجاح. '
                  f'الأعضاء المضافون: {added} '
                  f'(مشرفون: {stats["admins"]} — معلمون: {stats["teachers"]} — '
                  f'أولياء أمور: {stats["parents"]}).', 'success')

        if stats['parents'] == 0 and scope not in ('custom', 'teachers'):
            flash('تنبيه: لم يتم العثور على أولياء أمور مرتبطين بالنطاق المحدد. '
                  'تحقق من ربط أولياء الأمور بطلابهم في النظام.', 'warning')
        elif stats.get('no_parent_students', 0) > 0:
            flash(f'تنبيه: {stats["no_parent_students"]} طالب ليس لهم أولياء أمور مرتبطون في النظام.', 'warning')

        return redirect(url_for('chat.room_detail', room_id=room.id))

    return render_template('chat/create_room.html',
                           grades=grades, sections=sections,
                           subjects=subjects, school_users=school_users,
                           grades_tree=grades_tree, stages=stages)


# ─── Room detail (messages viewer) ───────────────────────────────────────────

@chat_bp.route('/rooms/<int:room_id>', methods=['GET', 'POST'])
@login_required
@permission_required('manage_chat')
def room_detail(room_id):
    _require_chat_module()
    school = get_current_school()
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id if school else 0)
            .first_or_404())

    # Auto-ensure admin is a ChatRoomMember (idempotent — no-op if already present).
    membership = _ensure_admin_member(room)
    db.session.commit()

    # Admin send permission: only hard-blocked by is_closed.
    # Announcement-only, allow_replies=False, and schedules do NOT apply to admin.
    can_send = not room.is_closed
    send_blocked_reason = (
        'هذه المحادثة مغلقة. يمكنك إعادة فتحها أولاً لإرسال الرسائل.'
        if room.is_closed else ''
    )

    if request.method == 'POST':
        import threading
        is_ajax = _is_ajax_request()
        _log.info('[chat-send] POST received room_id=%s user_id=%s ajax=%s',
                  room.id, current_user.id, is_ajax)

        if not can_send:
            _log.info(
                '[chat] send denied user_id=%s role=%s room_id=%s reason=room_closed',
                current_user.id, getattr(current_user.role, 'name', None), room.id,
            )
            if is_ajax:
                return jsonify({'ok': False, 'error': send_blocked_reason or 'لا يمكنك الإرسال.'}), 400
            flash(send_blocked_reason, 'warning')
            return redirect(url_for('chat.room_detail', room_id=room.id))

        body = request.form.get('body', '').strip()
        if not body:
            error_msg = 'الرسالة لا يمكن أن تكون فارغة.'
            if is_ajax:
                return jsonify({'ok': False, 'error': error_msg}), 400
            flash(error_msg, 'warning')
            return redirect(url_for('chat.room_detail', room_id=room.id))
        if len(body) > 2000:
            body = body[:2000]

        try:
            msg = ChatMessage(
                room_id=room.id,
                sender_user_id=current_user.id,
                body=body,
                message_type='text',
            )
            db.session.add(msg)
            db.session.flush()
            db.session.add(ChatMessageRead(message_id=msg.id, user_id=current_user.id))
            room.updated_at = datetime.utcnow()
            db.session.commit()
            _log.info('[chat-send] committed message_id=%s room_id=%s', msg.id, room.id)
        except Exception as exc:
            _log.error('[chat-send] error room_id=%s user_id=%s: %s', room.id, current_user.id, exc,
                       exc_info=True)
            db.session.rollback()
            if is_ajax:
                return jsonify({'ok': False, 'error': 'حدث خطأ أثناء حفظ الرسالة.'}), 500
            flash('حدث خطأ أثناء حفظ الرسالة.', 'danger')
            return redirect(url_for('chat.room_detail', room_id=room.id))

        # Capture all data needed by the daemon thread before the request context ends.
        # current_user and db.session are NOT available inside daemon threads.
        _push_data = {
            'room_id':              room.id,
            'room_name':            room.name,
            'room_type':            room.type,
            'is_announcement_only': room.is_announcement_only,
            'msg_id':               msg.id,
            'msg_body':             msg.body,
            'sender_user_id':       msg.sender_user_id,
            'sender_name':          current_user.full_name or 'مستخدم',
        }
        _app = current_app._get_current_object()
        threading.Thread(
            target=_push_chat_message, args=(_app, _push_data), daemon=True
        ).start()

        if is_ajax:
            _log.info('[chat-send] returning ajax json message_id=%s room_id=%s', msg.id, room.id)
            return jsonify({'ok': True, 'message': _format_message_json(msg)})
        return redirect(url_for('chat.room_detail', room_id=room.id))

    limit = min(int(request.args.get('limit', 100)), 500)

    # Load the *latest* `limit` messages but display them in chronological order.
    # The inner subquery picks the newest IDs (desc + limit); the outer query
    # re-orders them ascending so the chat reads oldest-to-newest on screen.
    # room.id is already school-verified above, so the room_id filter is safe.
    _latest_ids_subq = (
        db.session.query(ChatMessage.id)
        .filter(ChatMessage.room_id == room.id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
        .subquery()
    )
    messages = (ChatMessage.query
                .filter(ChatMessage.id.in_(_latest_ids_subq))
                .order_by(ChatMessage.created_at.asc())
                .all())
    total_messages = (ChatMessage.query
                      .filter_by(room_id=room.id)
                      .count())
    members  = (ChatRoomMember.query
                .filter_by(room_id=room.id)
                .order_by(ChatRoomMember.role)
                .all())

    # Pre-build display names bypassing ORM school scope so that members whose
    # User.school_id differs from this room's school (data inconsistency, or a
    # user later reassigned) still display a readable name instead of '—'.
    # Priority: employee full_name → user full_name → username → '#<id>'.
    _member_uids = [m.user_id for m in members]
    member_display_names: dict[int, str] = {}
    if _member_uids:
        _emp_rows = (Employee.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter(Employee.user_id.in_(_member_uids))
                     .all())
        _emp_name_map = {
            e.user_id: e.full_name
            for e in _emp_rows if e.user_id and e.full_name
        }
        _user_rows = (User.query
                      .execution_options(bypass_tenant_scope=True)
                      .filter(User.id.in_(_member_uids))
                      .all())
        _user_map = {u.id: u for u in _user_rows}
        for uid in _member_uids:
            if uid in _emp_name_map:
                member_display_names[uid] = _emp_name_map[uid]
            elif uid in _user_map:
                u = _user_map[uid]
                member_display_names[uid] = u.full_name or u.username or f'#{uid}'
            else:
                member_display_names[uid] = f'#{uid}'

    schedules = room.schedules.order_by(ChatRoomSchedule.day_of_week).all()

    # Mark ALL non-deleted, non-self messages in this room as read.
    # Must scan the whole room, not just the loaded `messages` slice: the default
    # limit=100 returns the OLDEST messages first, so any newer unread messages
    # (beyond position 100 in message history, or arriving via live polling)
    # would never be included in the slice and would stay unread in the DB.
    _mark_all_room_messages_read(room.id, current_user.id, label='room_detail')

    return render_template('chat/room_detail.html',
                           room=room, messages=messages,
                           total_messages=total_messages,
                           members=members, schedules=schedules,
                           member_display_names=member_display_names,
                           can_send=can_send,
                           send_blocked_reason=send_blocked_reason,
                           membership=membership,
                           sse_url=url_for('chat.room_events', room_id=room.id),
                           poll_url=url_for('chat.room_poll', room_id=room.id),
                           older_url=url_for('chat.room_older_messages', room_id=room.id))


# ─── Edit room ────────────────────────────────────────────────────────────────

@chat_bp.route('/rooms/<int:room_id>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('manage_chat')
def edit_room(room_id):
    _require_chat_module()
    school = get_current_school()
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id if school else 0)
            .first_or_404())

    if request.method == 'POST':
        action = request.form.get('action', 'update')

        # ── Add members from selected sections (academic filter) ──────────────
        if action == 'add_by_sections':
            raw_ids = request.form.getlist('add_section_ids')
            uid_set: set[int] = set()
            stats = {'admins': 0, 'teachers': 0, 'parents': 0, 'no_parent_students': 0}
            valid_count = 0
            for sid_raw in raw_ids:
                try:
                    sid = int(sid_raw)
                except (ValueError, TypeError):
                    continue
                # Strict school ownership check
                sec = (Section.query
                       .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
                       .filter_by(id=sid, school_id=school.id)
                       .first())
                if sec:
                    valid_count += 1
                    _add_section_members(school.id, sid, uid_set, stats)
                else:
                    _log.warning('[chat] edit_room add_by_sections: section %s not in school %s',
                                 sid, school.id)
            if uid_set:
                added = _sync_members(room,
                                      uid_set,
                                      room.created_by_user_id or current_user.id)
                room.updated_at = datetime.utcnow()
                db.session.commit()
                flash(f'تمت إضافة {added} عضو جديد من {valid_count} شعبة. '
                      f'(معلمون: {stats["teachers"]} — أولياء أمور: {stats["parents"]}).', 'success')
            else:
                flash('لم يتم اختيار أي شعبة أو لم يتم العثور على أعضاء.', 'warning')
            return redirect(url_for('chat.edit_room', room_id=room.id))

        # ── Standard update ───────────────────────────────────────────────────
        room.name                = (request.form.get('name', room.name).strip()
                                    or room.name)
        room.is_announcement_only = bool(request.form.get('is_announcement_only'))
        room.allow_replies        = not bool(request.form.get('no_replies'))
        room.updated_at           = datetime.utcnow()
        db.session.commit()
        flash('تم تحديث المحادثة بنجاح.', 'success')
        return redirect(url_for('chat.room_detail', room_id=room.id))

    members = (ChatRoomMember.query
               .filter_by(room_id=room.id)
               .order_by(ChatRoomMember.role).all())

    # Grades/sections/stages for academic filter panel — restrict to the active
    # year so that older years' grades (which have no live sections) are excluded.
    year = get_active_year(school.id)
    if year:
        edit_grades   = (Grade.query
                         .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
                         .filter_by(school_id=school.id, academic_year_id=year.id)
                         .order_by(Grade.name).all())
        edit_sections = (Section.query
                         .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
                         .filter_by(school_id=school.id, academic_year_id=year.id)
                         .order_by(Section.name).all())
    else:
        edit_grades   = []
        edit_sections = []
    edit_stages   = sorted(set(g.stage for g in edit_grades if g.stage))

    # Build grades→sections tree
    edit_grades_tree = []
    for g in edit_grades:
        g_secs = [s for s in edit_sections if s.grade_id == g.id]
        edit_grades_tree.append({'grade': g, 'sections': g_secs})

    return render_template('chat/edit_room.html',
                           room=room, members=members,
                           edit_grades_tree=edit_grades_tree,
                           edit_stages=edit_stages)


# ─── Close / reopen ───────────────────────────────────────────────────────────

@chat_bp.route('/rooms/<int:room_id>/close', methods=['POST'])
@login_required
@permission_required('manage_chat')
def close_room(room_id):
    _require_chat_module()
    school = get_current_school()
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id if school else 0)
            .first_or_404())
    room.is_closed  = True
    room.updated_at = datetime.utcnow()
    db.session.commit()
    flash('تم إغلاق المحادثة.', 'warning')
    return redirect(url_for('chat.room_detail', room_id=room.id))


@chat_bp.route('/rooms/<int:room_id>/reopen', methods=['POST'])
@login_required
@permission_required('manage_chat')
def reopen_room(room_id):
    _require_chat_module()
    school = get_current_school()
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id if school else 0)
            .first_or_404())
    room.is_closed  = False
    room.updated_at = datetime.utcnow()
    db.session.commit()
    flash('تم فتح المحادثة.', 'success')
    return redirect(url_for('chat.room_detail', room_id=room.id))


@chat_bp.route('/rooms/<int:room_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_chat')
def delete_room(room_id):
    """
    Permanently delete a group or announcement chat room together with all its
    messages, read receipts, members, and schedules.

    Security:
    - Admin-only (@admin_required).
    - Room must belong to the admin's school (school_id check).
    - Private/direct rooms are rejected (403) — this action is for groups only.
    - All deletions run inside a single transaction; any failure rolls back.
    """
    _require_chat_module()
    school = get_current_school()
    if not school:
        abort(404)

    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id)
            .first_or_404())

    if room.type == 'private':
        abort(403)

    room_name = room.name
    try:
        # Explicit deletion in FK-dependency order so no constraint is violated
        # and no SQLAlchemy lazy-dynamic cascade ambiguity arises.

        # 1. Read receipts for every message in this room
        msg_ids_sq = db.session.query(ChatMessage.id).filter(
            ChatMessage.room_id == room.id
        )
        ChatMessageRead.query.filter(
            ChatMessageRead.message_id.in_(msg_ids_sq)
        ).delete(synchronize_session=False)

        # 2. Messages
        ChatMessage.query.filter_by(room_id=room.id).delete(synchronize_session=False)

        # 3. Members
        ChatRoomMember.query.filter_by(room_id=room.id).delete(synchronize_session=False)

        # 4. Schedules
        ChatRoomSchedule.query.filter_by(room_id=room.id).delete(synchronize_session=False)

        # 5. The room itself
        db.session.delete(room)
        db.session.commit()

        _log.info('[chat] delete_room room_id=%s name=%r school=%s by user=%s',
                  room_id, room_name, school.id, current_user.id)
        flash('تم حذف المجموعة وجميع الرسائل المرتبطة بها بنجاح.', 'success')

    except Exception:
        db.session.rollback()
        _log.exception('[chat] delete_room failed room_id=%s school=%s', room_id, school.id)
        flash('حدث خطأ أثناء حذف المجموعة. يرجى المحاولة مرة أخرى.', 'danger')

    return redirect(url_for('chat.index'))


# ─── Manual add-member ───────────────────────────────────────────────────────

@chat_bp.route('/rooms/<int:room_id>/members/add', methods=['GET', 'POST'])
@login_required
@permission_required('manage_chat')
def add_member(room_id):
    """
    GET  — show a same-school user picker (excludes existing members).
    POST — add the chosen user to the room as a member.

    Security:
    - Admin only (@admin_required).
    - Room must belong to the admin's school.
    - Private rooms are rejected (403) — manual addition is for groups only.
    - Target user must be active and belong to the same school.
    - Duplicate membership is silently guarded (flash + redirect).
    """
    _require_chat_module()
    school = get_current_school()
    if not school:
        abort(404)
    room = (
        ChatRoom.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(id=room_id, school_id=school.id)
        .first_or_404()
    )

    if room.type == 'private':
        abort(403)

    if request.method == 'POST':
        user_id = request.form.get('user_id', type=int)
        if not user_id:
            flash('يرجى اختيار مستخدم.', 'warning')
            return redirect(url_for('chat.add_member', room_id=room_id))

        # Strict same-school guard.
        target = (
            User.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=user_id, school_id=school.id, is_active=True)
            .first()
        )
        if not target:
            flash('المستخدم غير موجود أو لا ينتمي إلى هذه المدرسة.', 'danger')
            return redirect(url_for('chat.add_member', room_id=room_id))

        # Duplicate guard.
        if ChatRoomMember.query.filter_by(room_id=room_id, user_id=user_id).first():
            flash('هذا المستخدم موجود مسبقاً في المجموعة.', 'warning')
            return redirect(url_for('chat.room_detail', room_id=room_id))

        db.session.add(ChatRoomMember(room_id=room_id, user_id=user_id, role='member'))
        room.updated_at = datetime.utcnow()
        db.session.commit()
        _log.info('[chat] add_member room_id=%s added user_id=%s by admin=%s',
                  room_id, user_id, current_user.id)
        flash(f'تمت إضافة {target.full_name} إلى المجموعة.', 'success')
        return redirect(url_for('chat.room_detail', room_id=room_id))

    # ── GET — build user picker ───────────────────────────────────────────────
    q_str       = request.args.get('q', '').strip()
    role_filter = request.args.get('role', '')

    # Exclude users who are already members.
    existing_ids = {
        m.user_id for m in ChatRoomMember.query.filter_by(room_id=room_id).all()
    }

    user_query = (
        User.query
        .execution_options(bypass_tenant_scope=True)
        .join(User.role)
        .filter(
            User.school_id == school.id,
            User.is_active == True,
            User.id.notin_(existing_ids) if existing_ids else True,
        )
    )
    if role_filter == 'parent':
        user_query = user_query.filter(Role.name == 'parent')
    elif role_filter == 'teacher':
        user_query = user_query.filter(Role.name == 'teacher')
    elif role_filter == 'admin':
        user_query = user_query.filter(Role.is_admin == True)

    if q_str:
        user_query = user_query.filter(User.full_name.ilike(f'%{q_str}%'))

    users = user_query.order_by(Role.name, User.full_name).all()

    # For parents: map user_id → [child name, …]
    parent_ids = [u.id for u in users if u.role and u.role.name == 'parent']
    children_map: dict[int, list[str]] = {}
    if parent_ids:
        rows = (
            db.session.query(parent_students.c.user_id, Student.full_name)
            .join(Student, Student.id == parent_students.c.student_id)
            .filter(parent_students.c.user_id.in_(parent_ids))
            .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
            .all()
        )
        for uid, name in rows:
            children_map.setdefault(uid, []).append(name)

    # For employees: map user_id → job_title
    user_ids = [u.id for u in users]
    job_titles: dict[int, str] = {}
    if user_ids:
        emp_rows = (
            Employee.query
            .execution_options(bypass_tenant_scope=True)
            .filter(
                Employee.school_id == school.id,
                Employee.user_id.in_(user_ids),
                Employee.job_title != None,
            )
            .with_entities(Employee.user_id, Employee.job_title)
            .all()
        )
        for uid, title in emp_rows:
            if uid and title:
                job_titles[uid] = title

    return render_template(
        'chat/add_member.html',
        room=room,
        users=users,
        children_map=children_map,
        job_titles=job_titles,
        existing_count=len(existing_ids),
        q=q_str,
        role_filter=role_filter,
        school=school,
    )


# ─── Rebuild members ──────────────────────────────────────────────────────────

@chat_bp.route('/rooms/<int:room_id>/rebuild-members', methods=['POST'])
@login_required
@permission_required('manage_chat')
def rebuild_members(room_id):
    """Re-derive auto-members from room scope without removing existing members."""
    _require_chat_module()
    school = get_current_school()
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id if school else 0)
            .first_or_404())

    if room.scope == 'custom':
        flash('لا يمكن إعادة توليد أعضاء غرفة مخصصة تلقائياً.', 'info')
        return redirect(url_for('chat.room_detail', room_id=room.id))

    uid_set, stats = _collect_user_ids(
        school.id, room.scope,
        section_id = room.section_id,
        grade_id   = room.grade_id,
        subject_id = room.subject_id,
        stage      = room.stage,
        academic_year_id = room.academic_year_id,
    )
    uid_set.add(room.created_by_user_id or current_user.id)
    uid_set.add(current_user.id)

    added = _sync_members(room, uid_set,
                          room.created_by_user_id or current_user.id)
    db.session.commit()

    if room.scope == 'parent_scope':
        flash(f'تم إعادة توليد الأعضاء. أُضيف {added} عضو جديد. '
              f'(الطلاب المطابقون: {stats.get("students", 0)} — '
              f'أولياء الأمور: {stats["parents"]}).', 'success')
    else:
        flash(f'تم إعادة توليد الأعضاء. أُضيف {added} عضو جديد. '
              f'(مشرفون: {stats["admins"]} — معلمون: {stats["teachers"]} — '
              f'أولياء أمور: {stats["parents"]}).', 'success')

    if stats['parents'] == 0 and room.scope not in ('custom', 'teachers'):
        flash('تنبيه: لم يتم العثور على أولياء أمور مرتبطين بالنطاق المحدد. '
              'تحقق من ربط أولياء الأمور بطلابهم في النظام.', 'warning')
    elif stats.get('no_parent_students', 0) > 0:
        flash(f'تنبيه: {stats["no_parent_students"]} طالب ليس لهم أولياء أمور مرتبطون في النظام.', 'warning')

    return redirect(url_for('chat.room_detail', room_id=room.id))


# ─── Sections preview AJAX (for create/edit academic filter) ─────────────────

@chat_bp.route('/ajax/sections-preview')
@login_required
@permission_required('manage_chat')
def sections_preview():
    """
    AJAX: given section_ids[], return deduplicated member list for preview.
    Each section is validated to belong to the current school before use.
    """
    _require_chat_module()
    school = get_current_school()
    if not school:
        return jsonify({'error': 'school not found'}), 403

    raw_ids = request.args.getlist('section_ids')
    uid_set: set[int] = set()
    stats = {'admins': 0, 'teachers': 0, 'parents': 0, 'no_parent_students': 0}
    sections_info = []

    for sid_raw in raw_ids:
        try:
            sid = int(sid_raw)
        except (ValueError, TypeError):
            continue
        # Strict school ownership — never accept a section from another school
        sec = (Section.query
               .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
               .filter_by(id=sid, school_id=school.id)
               .first())
        if not sec:
            continue
        grade_name = sec.grade.name if sec.grade else ''
        sections_info.append({'id': sec.id, 'name': sec.name, 'grade': grade_name})
        _add_section_members(school.id, sid, uid_set, stats)

    # Build a preview member list (cap at 30 for the UI panel)
    preview_members = []
    if uid_set:
        users = (
            User.query
            .execution_options(bypass_tenant_scope=True)
            .filter(User.id.in_(uid_set), User.school_id == school.id)
            .join(User.role)
            .order_by(Role.name, User.full_name)
            .limit(30)
            .all()
        )
        for u in users:
            preview_members.append({
                'name': u.full_name,
                'role': u.role.label if u.role else '',
            })

    return jsonify({
        'sections':        sections_info,
        'total_members':   len(uid_set),
        'preview_members': preview_members,
        'has_more':        len(uid_set) > 30,
        'stats':           stats,
    })


# ─── Block / unblock member ───────────────────────────────────────────────────

@chat_bp.route('/rooms/<int:room_id>/members/<int:user_id>/block', methods=['POST'])
@login_required
@permission_required('manage_chat')
def block_member(room_id, user_id):
    _require_chat_module()
    school = get_current_school()
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id if school else 0)
            .first_or_404())
    member = ChatRoomMember.query.filter_by(room_id=room.id, user_id=user_id).first_or_404()
    member.is_blocked         = True
    member.blocked_at         = datetime.utcnow()
    member.blocked_by_user_id = current_user.id
    db.session.commit()
    flash('تم حظر العضو من هذه المحادثة.', 'warning')
    return redirect(url_for('chat.room_detail', room_id=room.id))


@chat_bp.route('/rooms/<int:room_id>/members/<int:user_id>/unblock', methods=['POST'])
@login_required
@permission_required('manage_chat')
def unblock_member(room_id, user_id):
    _require_chat_module()
    school = get_current_school()
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id if school else 0)
            .first_or_404())
    member = ChatRoomMember.query.filter_by(room_id=room.id, user_id=user_id).first_or_404()
    member.is_blocked         = False
    member.blocked_at         = None
    member.blocked_by_user_id = None
    db.session.commit()
    flash('تم إلغاء حظر العضو.', 'success')
    return redirect(url_for('chat.room_detail', room_id=room.id))


# ─── Remove member from group ─────────────────────────────────────────────────

@chat_bp.route('/rooms/<int:room_id>/members/<int:user_id>/remove', methods=['POST'])
@login_required
@permission_required('manage_chat')
def remove_member(room_id, user_id):
    """
    Remove (kick) a member from a group/announcement room.

    Deletes ONLY the ChatRoomMember row for (room, user):
    - The user account and school records are untouched.
    - Historical messages and their read receipts remain intact.
    - Memberships in other rooms are unaffected.
    - Any room-level moderator role disappears with the membership row.
    - The user may be re-added later via add_member / rebuild_members.

    Security:
    - Admin only (@admin_required) — enforced server-side regardless of
      whether the button is visible in any UI.
    - Room lookup is scoped by the authenticated school context; the
      client-supplied room_id/user_id are never trusted on their own.
    - Private rooms are rejected (403) — removal is for groups only.
    - The acting user cannot remove themselves.
    - The room owner cannot be removed.
    """
    _require_chat_module()
    school = get_current_school()
    if not school:
        abort(404)
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id)
            .first_or_404())

    if room.type == 'private':
        abort(403)

    member = ChatRoomMember.query.filter_by(
        room_id=room.id, user_id=user_id).first_or_404()

    # Allow-listed redirect target — the action exists on both the room page
    # and the edit page; anything other than 'edit' falls back to room_detail.
    _dest = (url_for('chat.edit_room', room_id=room.id)
             if request.form.get('next') == 'edit'
             else url_for('chat.room_detail', room_id=room.id))

    if member.user_id == current_user.id:
        flash('لا يمكنك طرد نفسك من المجموعة.', 'warning')
        return redirect(_dest)

    if member.role == 'owner':
        flash('لا يمكن طرد منشئ المجموعة.', 'warning')
        return redirect(_dest)

    target = (User.query
              .execution_options(bypass_tenant_scope=True)
              .filter_by(id=member.user_id)
              .first())
    target_name = ((target.full_name or target.username)
                   if target else f'#{user_id}')
    member_role = member.role

    db.session.delete(member)
    db.session.commit()
    _log.info('[chat] remove_member room_id=%s removed user_id=%s role=%s '
              'by user=%s school=%s',
              room.id, user_id, member_role, current_user.id, school.id)
    flash(f'تم طرد {target_name} من المجموعة "{room.name}".', 'success')
    return redirect(_dest)


# ─── Assign / remove room admin ───────────────────────────────────────────────

@chat_bp.route('/rooms/<int:room_id>/members/<int:user_id>/make-admin', methods=['POST'])
@login_required
@permission_required('manage_chat')
def make_room_admin(room_id, user_id):
    _require_chat_module()
    school = get_current_school()
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id if school else 0)
            .first_or_404())
    member = ChatRoomMember.query.filter_by(room_id=room.id, user_id=user_id).first_or_404()
    if member.role == 'member':
        member.role = 'admin'
        db.session.commit()
        flash('تم تعيين العضو مشرفاً للمحادثة.', 'success')
    return redirect(url_for('chat.edit_room', room_id=room.id))


@chat_bp.route('/rooms/<int:room_id>/members/<int:user_id>/remove-admin', methods=['POST'])
@login_required
@permission_required('manage_chat')
def remove_room_admin(room_id, user_id):
    _require_chat_module()
    school = get_current_school()
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id if school else 0)
            .first_or_404())
    member = ChatRoomMember.query.filter_by(room_id=room.id, user_id=user_id).first_or_404()
    if member.role == 'admin':
        member.role = 'member'
        db.session.commit()
        flash('تم إلغاء صلاحية الإشراف.', 'info')
    return redirect(url_for('chat.edit_room', room_id=room.id))


# ─── Soft-delete message ─────────────────────────────────────────────────────

@chat_bp.route('/rooms/<int:room_id>/messages/<int:msg_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_chat')
def delete_message(room_id, msg_id):
    _require_chat_module()
    school = get_current_school()
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id if school else 0)
            .first_or_404())
    msg = ChatMessage.query.filter_by(id=msg_id, room_id=room.id).first_or_404()
    msg.is_deleted         = True
    msg.deleted_by_user_id = current_user.id
    msg.deleted_at         = datetime.utcnow()
    db.session.commit()
    flash('تم حذف الرسالة.', 'info')
    return redirect(url_for('chat.room_detail', room_id=room.id))


# ─── Room schedule management ─────────────────────────────────────────────────

_DAY_NAMES = {
    0: 'الأحد', 1: 'الاثنين', 2: 'الثلاثاء',
    3: 'الأربعاء', 4: 'الخميس', 5: 'الجمعة', 6: 'السبت',
}


@chat_bp.route('/rooms/<int:room_id>/schedule', methods=['GET', 'POST'])
@login_required
@permission_required('manage_chat')
def room_schedule(room_id):
    _require_chat_module()
    school = get_current_school()
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id if school else 0)
            .first_or_404())

    if request.method == 'POST':
        ChatRoomSchedule.query.filter_by(room_id=room.id).delete()
        for dow in range(7):
            open_s  = request.form.get(f'open_{dow}', '').strip()
            close_s = request.form.get(f'close_{dow}', '').strip()
            enabled = bool(request.form.get(f'enabled_{dow}'))
            if open_s and close_s:
                from datetime import time as dtime
                try:
                    oh, om = map(int, open_s.split(':'))
                    ch, cm = map(int, close_s.split(':'))
                    db.session.add(ChatRoomSchedule(
                        room_id=room.id, day_of_week=dow,
                        open_time=dtime(oh, om), close_time=dtime(ch, cm),
                        is_enabled=enabled,
                    ))
                except (ValueError, AttributeError):
                    pass
        db.session.commit()
        flash('تم حفظ جدول أوقات الإرسال.', 'success')
        return redirect(url_for('chat.room_detail', room_id=room.id))

    schedules = {s.day_of_week: s for s in room.schedules.all()}
    return render_template('chat/room_schedule.html',
                           room=room, schedules=schedules,
                           day_names=_DAY_NAMES, range7=range(7))


# ─── School chat settings ─────────────────────────────────────────────────────

@chat_bp.route('/settings', methods=['GET', 'POST'])
@login_required
@permission_required('manage_chat')
def settings():
    _require_chat_module()
    from app.utils.school_config import get_school_config, save_module_config
    school = get_current_school()
    if not school:
        abort(404)

    school_id = school.id
    cfg = get_school_config(school_id)

    if request.method == 'POST':
        bool_fields = [
            'allow_private_chats', 'allow_group_chats',
            'allow_school_announcement_group',
            'allow_parent_teacher_private_chat',
            'allow_parent_admin_private_chat',
            'allow_file_attachments', 'allow_images', 'allow_pdf',
            'allow_message_delete', 'allow_read_receipts',
            'allow_admin_monitoring', 'allow_chat_schedule',
            'allow_member_blocking', 'allow_group_admins',
        ]
        hidden_fields = [f for f in bool_fields if not request.form.get(f)]
        max_attach = int(request.form.get('max_attachment_size_mb') or 10)
        max_len    = int(request.form.get('message_max_length') or 2000)

        config = {
            'hidden_sections':  [],
            'hidden_fields':    hidden_fields,
            'required_fields':  [],
            'disabled_actions': [],
            'extra': {
                'max_attachment_size_mb': max_attach,
                'message_max_length':     max_len,
            },
        }
        save_module_config(school_id, 'chat', config)
        db.session.commit()
        flash('تم حفظ إعدادات المراسلات.', 'success')
        return redirect(url_for('chat.settings'))

    raw_cfg = cfg.as_dict('chat')
    return render_template('chat/settings.html', school=school, cfg=raw_cfg)


# ─── FCM push helper (shared with user routes) ───────────────────────────────

def _push_chat_message(app, push_data: dict) -> None:
    """
    Push FCM to non-blocked, non-sender members with active device tokens.

    Runs in a daemon thread — the Flask request context (current_user, scoped
    db.session) is NOT available in the new thread.  All values that would
    require the request context are captured as plain primitives before the
    thread is started and passed in `push_data`.  The Flask application context
    is passed explicitly as `app` and re-pushed here so that SQLAlchemy queries
    work correctly.

    push_data keys (all captured in the request context before thread start):
        room_id, room_name, room_type, is_announcement_only,
        msg_id, msg_body, sender_user_id, sender_name
    """
    import time
    from app.models import MobileDeviceToken, ChatRoomMember as _CRM
    from app.services.fcm_service import is_enabled, send_push_to_user

    start_ms = time.time() * 1000
    room_id  = push_data.get('room_id', '?')

    with app.app_context():
        try:
            if not is_enabled():
                return

            room_id              = push_data['room_id']
            room_name            = push_data['room_name']
            room_type            = push_data['room_type']
            is_announcement_only = push_data['is_announcement_only']
            msg_id               = push_data['msg_id']
            msg_body             = push_data['msg_body']
            sender_user_id       = push_data['sender_user_id']
            sender_name          = push_data['sender_name']

            # ── Query members and active tokens in batch ─────────────────────
            members = (_CRM.query
                       .filter_by(room_id=room_id, is_blocked=False, is_muted=False)
                       .all())
            if not members:
                _log.info('[chat-fcm] room_id=%s no non-blocked/non-muted members — skip', room_id)
                return

            member_ids = {m.user_id for m in members if m.user_id != sender_user_id}
            if not member_ids:
                _log.info('[chat-fcm] room_id=%s sender is only member — skip', room_id)
                return

            token_query_start = time.time() * 1000
            try:
                tokens = (MobileDeviceToken.query
                          .filter(MobileDeviceToken.user_id.in_(member_ids),
                                  MobileDeviceToken.is_active == True)
                          .all())
                users_with_tokens = {t.user_id for t in tokens}
            except Exception as exc:
                _log.error('[chat-fcm] failed to query device tokens room_id=%s: %s',
                           room_id, exc)
                return

            token_query_elapsed = time.time() * 1000 - token_query_start
            users_without_tokens = member_ids - users_with_tokens

            _log.info(
                '[chat-fcm] room_id=%s recipients=%d with_tokens=%d without_tokens=%d query_ms=%.1f',
                room_id, len(member_ids), len(users_with_tokens),
                len(users_without_tokens), token_query_elapsed,
            )

            if not users_with_tokens:
                return

            # ── Build FCM payload ─────────────────────────────────────────────
            ntype = 'school_announcement' if is_announcement_only else 'chat_message'
            title = (f'رسالة جديدة في {room_name}'
                     if room_type in ('group', 'announcement')
                     else f'رسالة جديدة من {sender_name}')
            body_text = (msg_body or '[مرفق]')[:150]
            data = {
                'type':        'message',    # Flutter routes on data['type']
                'route':       '/chat',      # Flutter navigates to this screen
                'ntype':       ntype,
                'room_id':     str(room_id),
                'message_id':  str(msg_id),
                'room_type':   room_type,    # helps Flutter choose private vs group view
                'sender_name': sender_name,  # display name for Flutter notification tap
            }

            fcm_start = time.time() * 1000
            fcm_sent = fcm_failed = 0
            for user_id in users_with_tokens:
                try:
                    sent, failed = send_push_to_user(user_id, title, body_text, data)
                    fcm_sent   += sent
                    fcm_failed += failed
                except Exception as exc:
                    _log.warning('[chat-fcm] push failed user_id=%s room_id=%s: %s',
                                 user_id, room_id, exc)
                    fcm_failed += 1

            fcm_elapsed   = time.time() * 1000 - fcm_start
            total_elapsed = time.time() * 1000 - start_ms

            _log.info(
                '[chat-fcm] room_id=%s fcm_sent=%d fcm_failed=%d fcm_ms=%.1f total_ms=%.1f',
                room_id, fcm_sent, fcm_failed, fcm_elapsed, total_elapsed,
            )

        except Exception as exc:
            _log.error('[chat-fcm] _push_chat_message error room_id=%s: %s', room_id, exc)


# ─── User-facing routes (parent / teacher) ───────────────────────────────────

@chat_bp.route('/my-rooms')
@login_required
def user_index():
    """Room list for parent/teacher — shows only rooms where user is a member."""
    _require_chat_module()
    school_id = getattr(current_user, 'school_id', None)
    if not school_id:
        abort(403)

    memberships = ChatRoomMember.query.filter_by(user_id=current_user.id).all()
    if not memberships:
        return render_template('chat/user_index.html', rooms=[])

    mem_map  = {m.room_id: m for m in memberships}
    room_ids = list(mem_map.keys())

    db_rooms = (ChatRoom.query
                .execution_options(bypass_tenant_scope=True)
                .filter(ChatRoom.id.in_(room_ids),
                        ChatRoom.school_id == school_id,
                        ChatRoom.is_active == True)
                .order_by(ChatRoom.updated_at.desc())
                .all())

    read_ids_sub = (db.session.query(ChatMessageRead.message_id)
                    .filter_by(user_id=current_user.id)
                    .subquery())

    rooms = []
    for room in db_rooms:
        mem      = mem_map.get(room.id)
        last_msg = (ChatMessage.query
                    .filter_by(room_id=room.id, is_deleted=False)
                    .order_by(ChatMessage.created_at.desc())
                    .first())
        unread = (ChatMessage.query
                  .filter_by(room_id=room.id, is_deleted=False)
                  .filter(ChatMessage.sender_user_id != current_user.id,
                          ChatMessage.id.notin_(read_ids_sub))
                  .count())
        can_send_quick = (
            mem and not mem.is_blocked and not room.is_closed and
            room.allow_replies and
            (not room.is_announcement_only or mem.role in ('owner', 'admin'))
        )
        rooms.append({
            'room':         room,
            'membership':   mem,
            'last_message': last_msg,
            'unread_count': unread,
            'can_send':     can_send_quick,
        })

    return render_template('chat/user_index.html', rooms=rooms)


@chat_bp.route('/my-rooms/<int:room_id>', methods=['GET', 'POST'])
@login_required
def user_room(room_id):
    """Room view for parent/teacher: read messages + send if permitted."""
    _require_chat_module()
    school_id = getattr(current_user, 'school_id', None)
    if not school_id:
        abort(403)

    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school_id, is_active=True)
            .first_or_404())

    membership = ChatRoomMember.query.filter_by(
        room_id=room.id, user_id=current_user.id
    ).first()
    if not membership:
        if _is_room_admin(room):
            # Auto-add admin as ChatRoomMember so they can send and appear in FCM.
            membership = _ensure_admin_member(room)
            db.session.commit()
        else:
            _log.info(
                '[chat] send denied user_id=%s role=%s room_id=%s reason=not_member',
                current_user.id, getattr(current_user.role, 'name', None), room.id,
            )
            abort(403)

    school = get_current_school()

    # ── Determine send permission ─────────────────────────────────────────────
    is_admin = _is_room_admin(room)
    can_send = True
    send_blocked_reason = ''

    if room.is_closed:
        # Hard block — even admin cannot send into a closed room.
        can_send = False
        send_blocked_reason = 'هذه المحادثة مغلقة حالياً.'
    elif membership.is_blocked:
        # Admins should never be blocked; if they are, log a warning.
        can_send = False
        send_blocked_reason = ('تم تقييدك من إرسال الرسائل في هذه المحادثة. '
                               'يرجى مراجعة إدارة المدرسة.')
        _log.warning(
            '[chat] send denied user_id=%s role=%s room_id=%s reason=blocked',
            current_user.id, getattr(current_user.role, 'name', None), room.id,
        )
    elif room.is_announcement_only and membership.role not in ('owner', 'admin') and not is_admin:
        can_send = False
        send_blocked_reason = 'هذه المحادثة للإعلانات فقط. لا يمكنك الإرسال.'
        _log.info(
            '[chat] send denied user_id=%s role=%s room_id=%s reason=announcement_only',
            current_user.id, getattr(current_user.role, 'name', None), room.id,
        )
    elif not room.allow_replies and not is_admin:
        can_send = False
        send_blocked_reason = 'لا تملك صلاحية الإرسال في هذه المحادثة.'
        _log.info(
            '[chat] send denied user_id=%s role=%s room_id=%s reason=no_replies',
            current_user.id, getattr(current_user.role, 'name', None), room.id,
        )
    elif not is_admin:
        ok_send, reason = _can_send_now(room, school)
        if not ok_send:
            can_send = False
            send_blocked_reason = reason
            _log.info(
                '[chat] send denied user_id=%s role=%s room_id=%s reason=schedule',
                current_user.id, getattr(current_user.role, 'name', None), room.id,
            )

    # ── POST: send message ────────────────────────────────────────────────────
    if request.method == 'POST':
        import threading
        is_ajax = _is_ajax_request()
        _log.info('[chat-send] POST received room_id=%s user_id=%s ajax=%s',
                  room.id, current_user.id, is_ajax)

        if not can_send:
            error_msg = send_blocked_reason or 'لا يمكنك الإرسال حالياً.'
            if is_ajax:
                return jsonify({'ok': False, 'error': error_msg}), 400
            flash(error_msg, 'warning')
            return redirect(url_for('chat.user_room', room_id=room.id))

        if is_admin:
            _log.info(
                '[chat] manager override send user_id=%s room_id=%s school_id=%s',
                current_user.id, room.id, room.school_id,
            )

        body = request.form.get('body', '').strip()
        if not body:
            error_msg = 'الرسالة لا يمكن أن تكون فارغة.'
            if is_ajax:
                return jsonify({'ok': False, 'error': error_msg}), 400
            flash(error_msg, 'warning')
            return redirect(url_for('chat.user_room', room_id=room.id))

        if len(body) > 2000:
            body = body[:2000]

        try:
            msg = ChatMessage(
                room_id        = room.id,
                sender_user_id = current_user.id,
                body           = body,
                message_type   = 'text',
            )
            db.session.add(msg)
            db.session.flush()
            db.session.add(ChatMessageRead(message_id=msg.id, user_id=current_user.id))
            room.updated_at = datetime.utcnow()
            db.session.commit()
            _log.info('[chat-send] committed message_id=%s room_id=%s', msg.id, room.id)
        except Exception as exc:
            _log.error('[chat-send] error room_id=%s user_id=%s: %s', room.id, current_user.id, exc,
                       exc_info=True)
            db.session.rollback()
            if is_ajax:
                return jsonify({'ok': False, 'error': 'حدث خطأ أثناء حفظ الرسالة.'}), 500
            flash('حدث خطأ أثناء حفظ الرسالة.', 'danger')
            return redirect(url_for('chat.user_room', room_id=room.id))

        # Capture all data needed by the daemon thread before the request context ends.
        # current_user and db.session are NOT available inside daemon threads.
        _push_data = {
            'room_id':              room.id,
            'room_name':            room.name,
            'room_type':            room.type,
            'is_announcement_only': room.is_announcement_only,
            'msg_id':               msg.id,
            'msg_body':             msg.body,
            'sender_user_id':       msg.sender_user_id,
            'sender_name':          current_user.full_name or 'مستخدم',
        }
        _app = current_app._get_current_object()
        threading.Thread(
            target=_push_chat_message, args=(_app, _push_data), daemon=True
        ).start()

        if is_ajax:
            _log.info('[chat-send] returning ajax json message_id=%s room_id=%s', msg.id, room.id)
            return jsonify({'ok': True, 'message': _format_message_json(msg)})
        return redirect(url_for('chat.user_room', room_id=room.id))

    # ── GET: load messages + mark unread as read ──────────────────────────────
    messages = (ChatMessage.query
                .filter_by(room_id=room.id)
                .order_by(ChatMessage.created_at.asc())
                .limit(100).all())

    # Mark ALL non-deleted, non-self messages in this room as read.
    # Same fix as room_detail: the limit-100 window only covers the oldest
    # messages; unread messages beyond that window must also be cleared.
    _mark_all_room_messages_read(room.id, current_user.id, label='user_room')

    schedules = room.schedules.order_by(ChatRoomSchedule.day_of_week).all()

    return render_template('chat/user_room.html',
                           room=room,
                           messages=messages,
                           membership=membership,
                           can_send=can_send,
                           send_blocked_reason=send_blocked_reason,
                           schedules=schedules,
                           sse_url=url_for('chat.user_room_events', room_id=room.id),
                           poll_url=url_for('chat.user_room_poll', room_id=room.id))


# ─── SSE & polling endpoints ─────────────────────────────────────────────────

@chat_bp.route('/rooms/<int:room_id>/events')
@login_required
@permission_required('manage_chat')
def room_events(room_id: int):
    """SSE stream of new messages for school-admin room view."""
    _require_chat_module()
    school = get_current_school()
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id if school else 0)
            .first_or_404())
    _ensure_admin_member(room)
    db.session.commit()
    after_id = max(0, int(request.headers.get('Last-Event-ID',
                           request.args.get('after_id', 0))))
    return _make_sse_response(room_id, current_user.id, after_id)


@chat_bp.route('/rooms/<int:room_id>/poll')
@login_required
@permission_required('manage_chat')
def room_poll(room_id: int):
    """JSON polling fallback for school-admin room view.

    Also marks the returned messages as read so messages received while the
    room page is open are cleared from the unread counter immediately.
    """
    _require_chat_module()
    school = get_current_school()
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id if school else 0)
            .first_or_404())
    after_id = max(0, int(request.args.get('after_id', 0)))
    _mark_all_room_messages_read(room.id, current_user.id, label='room_poll')
    return _poll_messages_json(room_id, current_user.id, after_id)


@chat_bp.route('/rooms/<int:room_id>/messages/older')
@login_required
@permission_required('manage_chat')
def room_older_messages(room_id: int):
    """Return a JSON batch of messages older than ``before_id`` for the admin room view.

    Used by the "تحميل الرسائل السابقة" button.  School isolation: room is
    fetched with ``school_id`` so a different school's room_id returns 404.
    ``before_id`` must be a positive integer that already belongs to this room;
    the query is further scoped by ``room_id`` so a forged before_id from
    another room cannot leak messages.
    """
    _require_chat_module()
    school = get_current_school()
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id if school else 0)
            .first_or_404())

    try:
        before_id = int(request.args.get('before_id', 0))
        limit     = min(max(1, int(request.args.get('limit', 100))), 200)
    except (ValueError, TypeError):
        abort(400)
    if before_id <= 0:
        abort(400)

    # Fetch the `limit` messages immediately before before_id.
    # Order desc so the LIMIT trims to the newest of the older batch,
    # then reverse to chronological (asc) order for display.
    older = (ChatMessage.query
             .filter(
                 ChatMessage.room_id == room.id,
                 ChatMessage.id < before_id,
             )
             .order_by(ChatMessage.created_at.desc())
             .limit(limit)
             .all())
    older = list(reversed(older))

    # Determine whether even older messages exist beyond this batch.
    has_more = bool(
        older and
        (ChatMessage.query
         .filter(
             ChatMessage.room_id == room.id,
             ChatMessage.id < older[0].id,
         )
         .first())
    )

    # Mark all room messages read: the user is actively viewing the room.
    _mark_all_room_messages_read(room.id, current_user.id, label='room_older')

    return jsonify({
        'messages': [
            {
                'id':          m.id,
                'body':        m.body or '[مرفق]',
                'sender_name': m.sender.full_name if m.sender else 'محذوف',
                'created_at':  m.created_at.strftime('%Y-%m-%d %H:%M'),
                'is_self':     m.sender_user_id == current_user.id,
                'is_deleted':  bool(m.is_deleted),
            }
            for m in older
        ],
        'has_more': has_more,
    })


@chat_bp.route('/my-rooms/<int:room_id>/events')
@login_required
def user_room_events(room_id: int):
    """SSE stream of new messages for parent/teacher room view."""
    _require_chat_module()
    school_id = getattr(current_user, 'school_id', None)
    if not school_id:
        abort(403)
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school_id, is_active=True)
            .first_or_404())
    membership = ChatRoomMember.query.filter_by(
        room_id=room.id, user_id=current_user.id
    ).first()
    if not membership:
        abort(403)
    after_id = max(0, int(request.headers.get('Last-Event-ID',
                           request.args.get('after_id', 0))))
    return _make_sse_response(room_id, current_user.id, after_id)


@chat_bp.route('/my-rooms/<int:room_id>/poll')
@login_required
def user_room_poll(room_id: int):
    """JSON polling fallback for parent/teacher room view.

    Also marks messages as read so messages delivered via polling are cleared
    from the user's unread counter, matching the room-open behavior.
    """
    _require_chat_module()
    school_id = getattr(current_user, 'school_id', None)
    if not school_id:
        abort(403)
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school_id, is_active=True)
            .first_or_404())
    membership = ChatRoomMember.query.filter_by(
        room_id=room.id, user_id=current_user.id
    ).first()
    if not membership:
        abort(403)
    after_id = max(0, int(request.args.get('after_id', 0)))
    _mark_all_room_messages_read(room.id, current_user.id, label='user_room_poll')
    return _poll_messages_json(room_id, current_user.id, after_id)


# ─── Admin direct messaging ───────────────────────────────────────────────────

@chat_bp.route('/direct')
@login_required
@permission_required('manage_chat')
def direct_new():
    """
    User-picker page: choose a same-school user to start a private chat with.

    GET /chat/direct?q=<search>&role=<parent|teacher|admin>
    Security: only lists active users from the admin's own school.
    """
    _require_chat_module()
    school = get_current_school()
    if not school:
        abort(404)

    q_str       = request.args.get('q', '').strip()
    role_filter = request.args.get('role', '')  # 'parent' | 'teacher' | 'admin' | ''

    user_query = (
        User.query
        .execution_options(bypass_tenant_scope=True)
        .join(User.role)
        .filter(
            User.school_id == school.id,
            User.is_active == True,
            User.id != current_user.id,
        )
    )
    if role_filter == 'parent':
        user_query = user_query.filter(Role.name == 'parent')
    elif role_filter == 'teacher':
        user_query = user_query.filter(Role.name == 'teacher')
    elif role_filter == 'admin':
        user_query = user_query.filter(Role.is_admin == True)

    if q_str:
        user_query = user_query.filter(User.full_name.ilike(f'%{q_str}%'))

    users = user_query.order_by(Role.name, User.full_name).all()

    # For parents: map user_id → [child_full_name, …]
    parent_ids = [u.id for u in users if u.role and u.role.name == 'parent']
    children_map: dict[int, list[str]] = {}
    if parent_ids:
        rows = (
            db.session.query(parent_students.c.user_id, Student.full_name)
            .join(Student, Student.id == parent_students.c.student_id)
            .filter(parent_students.c.user_id.in_(parent_ids))
            .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
            .all()
        )
        for uid, name in rows:
            children_map.setdefault(uid, []).append(name)

    # For employees: map user_id → job_title
    user_ids = [u.id for u in users]
    job_titles: dict[int, str] = {}
    if user_ids:
        emp_rows = (
            Employee.query
            .execution_options(bypass_tenant_scope=True)
            .filter(
                Employee.school_id == school.id,
                Employee.user_id.in_(user_ids),
                Employee.job_title != None,
            )
            .with_entities(Employee.user_id, Employee.job_title)
            .all()
        )
        for uid, title in emp_rows:
            if uid and title:
                job_titles[uid] = title

    return render_template(
        'chat/direct_new.html',
        users=users,
        children_map=children_map,
        job_titles=job_titles,
        q=q_str,
        role_filter=role_filter,
        school=school,
    )


@chat_bp.route('/direct/<int:target_user_id>')
@login_required
@permission_required('manage_chat')
def direct_chat(target_user_id):
    """
    Find or create a private 1-to-1 room between the admin and a school user.

    Security: target_user must belong to the same school as the admin.
    If a private room between the two already exists, redirect to it.
    Otherwise create a new one and redirect.
    """
    _require_chat_module()
    school = get_current_school()
    if not school:
        abort(404)

    # Strict same-school check — never allow cross-school access.
    target = (
        User.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(id=target_user_id, school_id=school.id, is_active=True)
        .first_or_404()
    )

    if target.id == current_user.id:
        flash('لا يمكنك مراسلة نفسك.', 'warning')
        return redirect(url_for('chat.index'))

    # Look for an existing private room shared by both users.
    admin_room_ids  = {m.room_id for m in
                       ChatRoomMember.query.filter_by(user_id=current_user.id).all()}
    target_room_ids = {m.room_id for m in
                       ChatRoomMember.query.filter_by(user_id=target.id).all()}
    shared = admin_room_ids & target_room_ids

    existing = None
    if shared:
        existing = (
            ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter(
                ChatRoom.id.in_(shared),
                ChatRoom.school_id == school.id,
                ChatRoom.type == 'private',
                ChatRoom.is_active == True,
            )
            .order_by(ChatRoom.updated_at.desc())
            .first()
        )

    if existing:
        _log.info(
            '[chat] direct_chat: reusing room_id=%s admin=%s target=%s',
            existing.id, current_user.id, target.id,
        )
        return redirect(url_for('chat.room_detail', room_id=existing.id))

    # Create a new private room.
    year = get_active_year(school.id)
    room_name = f'{current_user.full_name} ↔ {target.full_name}'
    room = ChatRoom(
        school_id            = school.id,
        academic_year_id     = year.id if year else None,
        name                 = room_name,
        type                 = 'private',
        scope                = 'custom',
        created_by_user_id   = current_user.id,
        is_announcement_only = False,
        allow_replies        = True,
    )
    db.session.add(room)
    db.session.flush()

    db.session.add(ChatRoomMember(room_id=room.id, user_id=current_user.id, role='owner'))
    db.session.add(ChatRoomMember(room_id=room.id, user_id=target.id, role='member'))
    db.session.commit()

    _log.info(
        '[chat] direct_chat: created room_id=%s admin=%s target=%s school=%s',
        room.id, current_user.id, target.id, school.id,
    )
    flash(f'تم إنشاء محادثة مباشرة مع {target.full_name}.', 'success')
    return redirect(url_for('chat.room_detail', room_id=room.id))
