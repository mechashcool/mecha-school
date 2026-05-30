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
POST /chat/rooms/<id>/members/<uid>/block          – block member
POST /chat/rooms/<id>/members/<uid>/unblock        – unblock member
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

import logging
from datetime import datetime, timezone

from flask import (
    Blueprint, abort, flash, redirect, render_template,
    request, url_for,
)
from flask_login import current_user, login_required

from app.models import (
    db, ChatRoom, ChatRoomMember, ChatMessage, ChatMessageRead,
    ChatRoomSchedule, Section, Grade, Subject,
    User, Employee, Student, Role,
    parent_students, teacher_subjects,
)
from app.utils.decorators import admin_required, get_current_school, get_active_year
from app.utils.modules import is_module_enabled

_log = logging.getLogger('mecha.chat')

chat_bp = Blueprint('chat', __name__, template_folder='../../templates/chat')


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
) -> tuple[set[int], dict]:
    """
    Compute which user_ids should be auto-added to a room based on scope.
    Returns (user_ids, stats) where stats contains counts for flash messages.

    Always includes all school admins (Role.is_admin == True).
    All DB lookups use bypass_tenant_scope + bypass_year_scope so they work
    regardless of the request's active-year context.
    """
    user_ids: set[int] = set()
    stats = {'admins': 0, 'teachers': 0, 'parents': 0}

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
        all_users = (
            User.query
            .execution_options(bypass_tenant_scope=True)
            .filter(User.school_id == school_id, User.is_active == True)
            .all()
        )
        for u in all_users:
            user_ids.add(u.id)

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
        _log.info('[chat] section=%s parents_found=%d', section_id, len(parent_rows))


def _sync_members(room: ChatRoom, user_ids: set[int], creator_id: int) -> int:
    """
    Add missing members to room; do not remove existing ones.
    If the creator is already a member, upgrades their role to 'owner'.
    Returns the count of newly added members.
    """
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
    schedules = room.schedules.filter_by(is_enabled=True).all()
    if not schedules:
        return True, ''

    try:
        from app.utils.attendance_helpers import get_local_now
        local_now = get_local_now(school)
    except Exception:
        local_now = datetime.utcnow()

    # Python weekday: Mon=0..Sun=6  →  our Sunday=0 scheme
    dow = (local_now.weekday() + 1) % 7
    now_time = local_now.time()

    for sch in schedules:
        if sch.day_of_week == dow:
            if sch.open_time <= now_time <= sch.close_time:
                return True, ''
            return (False,
                    'المراسلات غير متاحة حالياً، يمكنكم الإرسال ضمن '
                    'أوقات التواصل المحددة من المدرسة.')
    return (False,
            'المراسلات غير متاحة حالياً، يمكنكم الإرسال ضمن '
            'أوقات التواصل المحددة من المدرسة.')


# ─── Room list ────────────────────────────────────────────────────────────────

@chat_bp.route('/')
@login_required
@admin_required
def index():
    _require_chat_module()
    school = get_current_school()
    if not school:
        abort(404)

    q = (ChatRoom.query
         .execution_options(bypass_tenant_scope=True)
         .filter_by(school_id=school.id)
         .order_by(ChatRoom.created_at.desc()))

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

    return render_template('chat/index.html',
                           room_stats=room_stats,
                           type_filter=type_filter,
                           status_filter=status_filter,
                           school=school)


# ─── Create room ──────────────────────────────────────────────────────────────

@chat_bp.route('/rooms/create', methods=['GET', 'POST'])
@login_required
@admin_required
def create_room():
    _require_chat_module()
    school = get_current_school()
    if not school:
        abort(404)
    year = get_active_year(school.id)

    grades   = (Grade.query
                .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
                .filter_by(school_id=school.id)
                .order_by(Grade.name).all())
    sections = (Section.query
                .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
                .filter_by(school_id=school.id)
                .order_by(Section.name).all())
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

        if not name:
            flash('اسم المحادثة مطلوب.', 'danger')
            return render_template('chat/create_room.html',
                                   grades=grades, sections=sections,
                                   subjects=subjects, school_users=school_users)

        _log.info('[chat] create_room scope=%s section=%s grade=%s stage=%r subject=%s',
                  scope, section_id, grade_id, stage, subject_id)

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
            )

        # Creator is always owner — add explicitly so even an empty uid_set
        # still produces at least one member row.
        uid_set.add(current_user.id)

        added = _sync_members(room, uid_set, current_user.id)
        db.session.commit()

        _log.info('[chat] room %d created — uid_set=%d added=%d stats=%s',
                  room.id, len(uid_set), added, stats)

        flash(f'تم إنشاء المحادثة "{name}" بنجاح. '
              f'الأعضاء المضافون: {added} '
              f'(مشرفون: {stats["admins"]} — معلمون: {stats["teachers"]} — '
              f'أولياء أمور: {stats["parents"]}).', 'success')

        if stats['parents'] == 0 and scope not in ('custom', 'school', 'announcement'):
            flash('تنبيه: لم يتم العثور على أولياء أمور مرتبطين بالنطاق المحدد. '
                  'تحقق من ربط أولياء الأمور بطلابهم في النظام.', 'warning')

        return redirect(url_for('chat.room_detail', room_id=room.id))

    return render_template('chat/create_room.html',
                           grades=grades, sections=sections,
                           subjects=subjects, school_users=school_users)


# ─── Room detail (messages viewer) ───────────────────────────────────────────

@chat_bp.route('/rooms/<int:room_id>')
@login_required
@admin_required
def room_detail(room_id):
    _require_chat_module()
    school = get_current_school()
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id if school else 0)
            .first_or_404())

    limit    = min(int(request.args.get('limit', 100)), 500)
    messages = (ChatMessage.query
                .filter_by(room_id=room.id)
                .order_by(ChatMessage.created_at.asc())
                .limit(limit).all())

    members   = (ChatRoomMember.query
                 .filter_by(room_id=room.id)
                 .order_by(ChatRoomMember.role)
                 .all())

    schedules = room.schedules.order_by(ChatRoomSchedule.day_of_week).all()

    return render_template('chat/room_detail.html',
                           room=room, messages=messages,
                           members=members, schedules=schedules)


# ─── Edit room ────────────────────────────────────────────────────────────────

@chat_bp.route('/rooms/<int:room_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_room(room_id):
    _require_chat_module()
    school = get_current_school()
    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=school.id if school else 0)
            .first_or_404())

    if request.method == 'POST':
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
    return render_template('chat/edit_room.html', room=room, members=members)


# ─── Close / reopen ───────────────────────────────────────────────────────────

@chat_bp.route('/rooms/<int:room_id>/close', methods=['POST'])
@login_required
@admin_required
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
@admin_required
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


# ─── Rebuild members ──────────────────────────────────────────────────────────

@chat_bp.route('/rooms/<int:room_id>/rebuild-members', methods=['POST'])
@login_required
@admin_required
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
    )
    uid_set.add(room.created_by_user_id or current_user.id)
    uid_set.add(current_user.id)

    added = _sync_members(room, uid_set,
                          room.created_by_user_id or current_user.id)
    db.session.commit()

    flash(f'تم إعادة توليد الأعضاء. أُضيف {added} عضو جديد. '
          f'(مشرفون: {stats["admins"]} — معلمون: {stats["teachers"]} — '
          f'أولياء أمور: {stats["parents"]}).', 'success')

    if stats['parents'] == 0 and room.scope not in ('school', 'announcement'):
        flash('تنبيه: لم يتم العثور على أولياء أمور مرتبطين بهذه الشعبة.', 'warning')

    return redirect(url_for('chat.room_detail', room_id=room.id))


# ─── Block / unblock member ───────────────────────────────────────────────────

@chat_bp.route('/rooms/<int:room_id>/members/<int:user_id>/block', methods=['POST'])
@login_required
@admin_required
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
@admin_required
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


# ─── Assign / remove room admin ───────────────────────────────────────────────

@chat_bp.route('/rooms/<int:room_id>/members/<int:user_id>/make-admin', methods=['POST'])
@login_required
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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

def _push_chat_message(room: ChatRoom, msg: ChatMessage) -> None:
    """Push FCM notification to all non-blocked, non-sender members."""
    try:
        from app.services.fcm_service import is_enabled, send_push_to_user
        if not is_enabled():
            return
        members = (ChatRoomMember.query
                   .filter_by(room_id=room.id, is_blocked=False)
                   .all())
        ntype      = 'school_announcement' if room.is_announcement_only else 'chat_message'
        sender_name = current_user.full_name or 'مستخدم'
        title = (f'رسالة جديدة في {room.name}'
                 if room.type in ('group', 'announcement')
                 else f'رسالة جديدة من {sender_name}')
        body_text = (msg.body or '[مرفق]')[:150]
        data = {
            'type':       ntype,
            'room_id':    str(room.id),
            'message_id': str(msg.id),
            'screen':     'chat',
        }
        for mem in members:
            if mem.user_id == msg.sender_user_id:
                continue
            try:
                send_push_to_user(mem.user_id, title, body_text, data)
            except Exception as exc:
                _log.warning('[chat] FCM push failed user_id=%s: %s', mem.user_id, exc)
    except Exception as exc:
        _log.error('[chat] _push_chat_message error: %s', exc)


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
        abort(403)

    school = get_current_school()

    # ── Determine send permission ─────────────────────────────────────────────
    can_send = True
    send_blocked_reason = ''

    if room.is_closed:
        can_send = False
        send_blocked_reason = 'هذه المحادثة مغلقة حالياً.'
    elif membership.is_blocked:
        can_send = False
        send_blocked_reason = ('تم تقييدك من إرسال الرسائل في هذه المحادثة. '
                               'يرجى مراجعة إدارة المدرسة.')
    elif room.is_announcement_only and membership.role not in ('owner', 'admin'):
        can_send = False
        send_blocked_reason = 'هذه المحادثة للإعلانات فقط. لا يمكنك الإرسال.'
    elif not room.allow_replies:
        can_send = False
        send_blocked_reason = 'لا يمكنك إرسال رسالة في هذه المحادثة حالياً.'
    else:
        ok_send, reason = _can_send_now(room, school)
        if not ok_send:
            can_send = False
            send_blocked_reason = reason

    # ── POST: send message ────────────────────────────────────────────────────
    if request.method == 'POST':
        if not can_send:
            flash(send_blocked_reason or 'لا يمكنك الإرسال حالياً.', 'warning')
            return redirect(url_for('chat.user_room', room_id=room.id))

        body = request.form.get('body', '').strip()
        if not body:
            flash('الرسالة لا يمكن أن تكون فارغة.', 'warning')
            return redirect(url_for('chat.user_room', room_id=room.id))

        if len(body) > 2000:
            body = body[:2000]

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

        _push_chat_message(room, msg)

        return redirect(url_for('chat.user_room', room_id=room.id))

    # ── GET: load messages + mark unread as read ──────────────────────────────
    messages = (ChatMessage.query
                .filter_by(room_id=room.id)
                .order_by(ChatMessage.created_at.asc())
                .limit(100).all())

    already_read = {
        r.message_id for r in
        ChatMessageRead.query.filter_by(user_id=current_user.id).all()
    }
    new_reads = [
        ChatMessageRead(message_id=m.id, user_id=current_user.id)
        for m in messages
        if not m.is_deleted
        and m.id not in already_read
        and m.sender_user_id != current_user.id
    ]
    if new_reads:
        db.session.add_all(new_reads)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    schedules = room.schedules.order_by(ChatRoomSchedule.day_of_week).all()

    return render_template('chat/user_room.html',
                           room=room,
                           messages=messages,
                           membership=membership,
                           can_send=can_send,
                           send_blocked_reason=send_blocked_reason,
                           schedules=schedules)
