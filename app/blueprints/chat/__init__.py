"""
Chat / Messaging module — school admin web interface
=====================================================
Routes (all require login + chat module enabled):

GET  /chat/                                 – room list
GET  /chat/rooms/create                     – create room form
POST /chat/rooms/create                     – submit new room
GET  /chat/rooms/<id>                       – room messages viewer
GET  /chat/rooms/<id>/edit                  – edit room form
POST /chat/rooms/<id>/edit                  – update room
POST /chat/rooms/<id>/close                 – close room
POST /chat/rooms/<id>/reopen               – reopen room
POST /chat/rooms/<id>/members/<uid>/block  – block member
POST /chat/rooms/<id>/members/<uid>/unblock– unblock member
POST /chat/rooms/<id>/messages/<mid>/delete– soft-delete message
GET  /chat/settings                         – school chat settings page
POST /chat/settings                         – save school chat settings
"""
from __future__ import annotations

from datetime import datetime, timezone

from flask import (
    Blueprint, abort, flash, redirect, render_template,
    request, url_for,
)
from flask_login import current_user, login_required

from app.models import (
    db, ChatRoom, ChatRoomMember, ChatMessage,
    ChatRoomSchedule, Section, Grade, Subject,
    User, Employee, Student, parent_students,
    teacher_subjects,
)
from app.utils.decorators import admin_required, get_current_school, get_active_year
from app.utils.modules import is_module_enabled

chat_bp = Blueprint('chat', __name__, template_folder='../../templates/chat')


# ─── Module guard helper ──────────────────────────────────────────────────────

def _require_chat_module():
    """Return 403 if chat module is disabled for current school."""
    school_id = getattr(current_user, 'school_id', None)
    if not current_user.is_super_admin and not is_module_enabled(school_id, 'chat'):
        abort(403)


# ─── Member auto-generation ───────────────────────────────────────────────────

def _collect_user_ids(school_id: int, scope: str, section_id=None,
                      grade_id=None, subject_id=None, stage=None) -> set[int]:
    """
    Compute the set of user_ids that should be auto-added to a room
    based on its scope. Always includes all school admins.
    """
    user_ids: set[int] = set()

    # All admin/staff users of the school
    admins = (User.query
              .execution_options(bypass_tenant_scope=True)
              .filter_by(school_id=school_id, is_active=True)
              .join(User.role)
              .filter(db.text("roles.name IN ('admin', 'school_manager', 'staff')"))
              .all())
    for u in admins:
        user_ids.add(u.id)

    if scope == 'school' or scope == 'announcement':
        # All active parents + all active teachers/staff
        all_users = (User.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter_by(school_id=school_id, is_active=True)
                     .all())
        for u in all_users:
            user_ids.add(u.id)

    elif scope == 'section' and section_id:
        section = db.session.get(Section, section_id)
        if section:
            # Teachers of this section (homeroom + subject)
            if section.teacher_id:
                emp = db.session.get(Employee, section.teacher_id)
                if emp and emp.user_id:
                    user_ids.add(emp.user_id)
            subj_emps = (db.session.query(teacher_subjects.c.employee_id)
                         .filter(teacher_subjects.c.section_id == section_id)
                         .all())
            for (eid,) in subj_emps:
                emp = db.session.get(Employee, eid)
                if emp and emp.user_id:
                    user_ids.add(emp.user_id)
            # Parents of students in this section
            student_ids = [s.id for s in section.students.filter_by(status='active').all()]
            if student_ids:
                parent_rows = (db.session.query(parent_students.c.user_id)
                               .filter(parent_students.c.student_id.in_(student_ids))
                               .all())
                for (uid,) in parent_rows:
                    user_ids.add(uid)

    elif scope == 'grade' and grade_id:
        grade = db.session.get(Grade, grade_id)
        if grade:
            for section in grade.sections.all():
                # Homeroom teacher
                if section.teacher_id:
                    emp = db.session.get(Employee, section.teacher_id)
                    if emp and emp.user_id:
                        user_ids.add(emp.user_id)
                # Subject teachers
                subj_emps = (db.session.query(teacher_subjects.c.employee_id)
                             .filter(teacher_subjects.c.section_id == section.id)
                             .all())
                for (eid,) in subj_emps:
                    emp = db.session.get(Employee, eid)
                    if emp and emp.user_id:
                        user_ids.add(emp.user_id)
                # Parents
                student_ids = [s.id for s in section.students.filter_by(status='active').all()]
                if student_ids:
                    parent_rows = (db.session.query(parent_students.c.user_id)
                                   .filter(parent_students.c.student_id.in_(student_ids))
                                   .all())
                    for (uid,) in parent_rows:
                        user_ids.add(uid)

    elif scope == 'stage' and stage:
        grades = (Grade.query
                  .execution_options(bypass_tenant_scope=True)
                  .filter_by(school_id=school_id, stage=stage)
                  .all())
        for grade in grades:
            for section in grade.sections.all():
                if section.teacher_id:
                    emp = db.session.get(Employee, section.teacher_id)
                    if emp and emp.user_id:
                        user_ids.add(emp.user_id)
                subj_emps = (db.session.query(teacher_subjects.c.employee_id)
                             .filter(teacher_subjects.c.section_id == section.id)
                             .all())
                for (eid,) in subj_emps:
                    emp = db.session.get(Employee, eid)
                    if emp and emp.user_id:
                        user_ids.add(emp.user_id)
                student_ids = [s.id for s in section.students.filter_by(status='active').all()]
                if student_ids:
                    parent_rows = (db.session.query(parent_students.c.user_id)
                                   .filter(parent_students.c.student_id.in_(student_ids))
                                   .all())
                    for (uid,) in parent_rows:
                        user_ids.add(uid)

    elif scope == 'subject' and subject_id:
        section_rows = (db.session.query(teacher_subjects.c.section_id,
                                         teacher_subjects.c.employee_id)
                        .filter(teacher_subjects.c.subject_id == subject_id)
                        .all())
        for sec_id, emp_id in section_rows:
            emp = db.session.get(Employee, emp_id)
            if emp and emp.user_id:
                user_ids.add(emp.user_id)
            section = db.session.get(Section, sec_id)
            if section:
                student_ids = [s.id for s in section.students.filter_by(status='active').all()]
                if student_ids:
                    parent_rows = (db.session.query(parent_students.c.user_id)
                                   .filter(parent_students.c.student_id.in_(student_ids))
                                   .all())
                    for (uid,) in parent_rows:
                        user_ids.add(uid)

    # Filter: only active users belonging to this school
    if user_ids:
        valid = {
            u.id for u in
            User.query
            .execution_options(bypass_tenant_scope=True)
            .filter(User.id.in_(user_ids), User.school_id == school_id,
                    User.is_active == True)
            .all()
        }
        return valid
    return user_ids


def _sync_members(room: ChatRoom, user_ids: set[int], creator_id: int) -> None:
    """Add missing members to room; do not remove existing ones."""
    existing = {m.user_id for m in room.members.all()}
    for uid in user_ids:
        if uid not in existing:
            role = 'owner' if uid == creator_id else 'member'
            db.session.add(ChatRoomMember(
                room_id=room.id, user_id=uid, role=role,
            ))


# ─── Schedule helpers ─────────────────────────────────────────────────────────

def _can_send_now(room: ChatRoom, school) -> tuple[bool, str]:
    """
    Check if current local time is within an allowed schedule window.
    Returns (can_send, reason_if_not).
    """
    schedules = room.schedules.filter_by(is_enabled=True).all()
    if not schedules:
        return True, ''

    try:
        from app.utils.attendance_helpers import get_local_now
        local_now = get_local_now(school)
    except Exception:
        local_now = datetime.utcnow()

    today_dow = local_now.weekday()
    # Python weekday: Mon=0..Sun=6; convert to our Sunday=0 scheme
    dow = (today_dow + 1) % 7
    now_time = local_now.time()

    for sch in schedules:
        if sch.day_of_week == dow:
            if sch.open_time <= now_time <= sch.close_time:
                return True, ''
            return (False,
                    'المراسلات غير متاحة حالياً، يمكنكم الإرسال ضمن أوقات التواصل '
                    'المحددة من المدرسة.')
    return (False,
            'المراسلات غير متاحة حالياً، يمكنكم الإرسال ضمن أوقات التواصل '
            'المحددة من المدرسة.')


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

    type_filter = request.args.get('type', '')
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
    year   = get_active_year(school.id) if school else None
    if not school:
        abort(404)

    grades   = (Grade.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=school.id)
                .order_by(Grade.name).all()) if year else []
    sections = (Section.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=school.id)
                .order_by(Section.name).all()) if year else []
    subjects = (Subject.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=school.id)
                .order_by(Subject.name).all()) if year else []

    if request.method == 'POST':
        name     = request.form.get('name', '').strip()
        rtype    = request.form.get('type', 'group')
        scope    = request.form.get('scope', 'custom')
        is_ann   = bool(request.form.get('is_announcement_only'))
        allow_rep = not bool(request.form.get('no_replies'))
        section_id = request.form.get('section_id') or None
        grade_id   = request.form.get('grade_id')   or None
        subject_id = request.form.get('subject_id') or None
        stage      = request.form.get('stage', '').strip() or None

        if not name:
            flash('اسم المحادثة مطلوب.', 'danger')
            return render_template('chat/create_room.html',
                                   grades=grades, sections=sections, subjects=subjects)

        room = ChatRoom(
            school_id=school.id,
            academic_year_id=year.id if year else None,
            name=name,
            type=rtype,
            scope=scope,
            stage=stage,
            grade_id=int(grade_id) if grade_id else None,
            section_id=int(section_id) if section_id else None,
            subject_id=int(subject_id) if subject_id else None,
            created_by_user_id=current_user.id,
            is_announcement_only=is_ann,
            allow_replies=allow_rep,
        )
        db.session.add(room)
        db.session.flush()  # get room.id

        # Auto-add members based on scope
        if scope != 'custom':
            uid_set = _collect_user_ids(
                school.id, scope,
                section_id=int(section_id) if section_id else None,
                grade_id=int(grade_id) if grade_id else None,
                subject_id=int(subject_id) if subject_id else None,
                stage=stage,
            )
        else:
            uid_set = set()
            custom_ids = request.form.getlist('member_ids')
            for cid in custom_ids:
                try:
                    uid_set.add(int(cid))
                except (ValueError, TypeError):
                    pass
            uid_set.add(current_user.id)

        _sync_members(room, uid_set, current_user.id)
        db.session.commit()
        flash(f'تم إنشاء المحادثة "{name}" بنجاح وتمت إضافة {len(uid_set)} عضو.', 'success')
        return redirect(url_for('chat.index'))

    return render_template('chat/create_room.html',
                           grades=grades, sections=sections, subjects=subjects)


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

    limit = min(int(request.args.get('limit', 100)), 500)
    messages = (ChatMessage.query
                .filter_by(room_id=room.id)
                .order_by(ChatMessage.created_at.asc())
                .limit(limit).all())

    members = (ChatRoomMember.query
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
        room.name = request.form.get('name', room.name).strip() or room.name
        room.is_announcement_only = bool(request.form.get('is_announcement_only'))
        room.allow_replies = not bool(request.form.get('no_replies'))
        room.updated_at = datetime.utcnow()
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
    room.is_closed = True
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
    room.is_closed = False
    room.updated_at = datetime.utcnow()
    db.session.commit()
    flash('تم فتح المحادثة.', 'success')
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
    member.is_blocked        = True
    member.blocked_at        = datetime.utcnow()
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
    msg.is_deleted        = True
    msg.deleted_by_user_id = current_user.id
    msg.deleted_at        = datetime.utcnow()
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
        # Delete all existing schedules and rebuild
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
        int_fields = {
            'max_attachment_size_mb': 10,
            'message_max_length':     2000,
        }
        # Store as field-level config (we encode "disabled" as hidden_fields)
        # Instead use a simple JSON config stored via save_module_config
        hidden_fields = []
        for f in bool_fields:
            if not request.form.get(f):
                hidden_fields.append(f)

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
    return render_template('chat/settings.html',
                           school=school,
                           cfg=raw_cfg)
