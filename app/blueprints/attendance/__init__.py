"""Mecha-School – Attendance Blueprint  (Phase 6: school + year scoped)"""
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from datetime import datetime as dt, date as date_type
from app.models import db, StudentAttendance, Student, Section, SchoolSettings, Grade, StudentSuspension, Notification, parent_students
from app.utils.decorators import (permission_required, get_teacher_section_ids,
                                   admin_required, get_current_school, get_active_year, get_view_year,
                                   historical_guard)
from app.utils.attendance_helpers import (determine_check_in_status,
                                           get_local_now, get_local_date,
                                           is_holiday_date, get_student_shift)
from app.services.notifications import NotificationService

import logging
_log = logging.getLogger(__name__)

attendance_bp = Blueprint('attendance', __name__,
                           template_folder='../../templates/attendance')


def _notify_absent_parents(student, school_id, today_str, source='manual', shift_name=None):
    """
    Create in-app Notification row + send FCM push to all parents of an absent student.
    Always persists the Notification row first; FCM failure is logged but does not raise.
    Optional shift_name is included in the FCM data payload when available.
    """
    if not school_id:
        return

    title = 'تنبيه غياب'
    body  = f'تم تسجيل الطالب {student.full_name} غائباً بتاريخ {today_str}.'
    data  = {
        'ntype':        'attendance',
        'action':       'absent',
        'status':       'absent',
        'student_id':   str(student.id),
        'student_name': student.full_name,
        'date':         today_str,
        'screen':       'attendance',
    }
    if shift_name:
        data['shift_name'] = shift_name

    parent_ids = [
        row[0] for row in
        db.session.query(parent_students.c.user_id)
        .filter(parent_students.c.student_id == student.id)
        .all()
    ]

    _log.info(
        '[attendance-notify] absence source=%s student_id=%s name=%s date=%s parent_targets=%d',
        source, student.id, student.full_name, today_str, len(parent_ids),
    )

    if not parent_ids:
        return

    for parent_id in parent_ids:
        try:
            db.session.add(Notification(
                school_id      = school_id,
                title          = title,
                body           = body,
                ntype          = 'attendance',
                target_user_id = parent_id,
                created_by     = None,
            ))
            _log.info('[attendance-notify] absent notification student_id=%s parent_user_id=%s',
                      student.id, parent_id)
        except Exception:
            _log.exception('[attendance-notify] failed to queue Notification '
                           'student_id=%s parent_user_id=%s', student.id, parent_id)

    try:
        db.session.commit()
    except Exception:
        _log.exception('[attendance-notify] commit failed student_id=%s', student.id)
        db.session.rollback()
        return

    try:
        from app.services.fcm_service import is_enabled, send_push_to_user
        if not is_enabled():
            _log.info('[attendance-notify] FCM disabled — absent push skipped student_id=%s', student.id)
            return
        for parent_id in parent_ids:
            _log.info('[attendance-notify] dispatching absent FCM push parent_user_id=%s student_id=%s',
                      parent_id, student.id)
            send_push_to_user(parent_id, title, body, data)
    except Exception:
        _log.exception('[attendance-notify] FCM push failed student_id=%s', student.id)


def _is_teacher():
    return (current_user.is_authenticated and
            current_user.role and
            current_user.role.name == 'teacher')


def _get_settings():
    """Return a settings-like object (School or SchoolSettings fallback)."""
    from app.models import School
    s = get_current_school()
    if s and isinstance(s, School):
        return s
    return SchoolSettings.get()


def _run_auto_absent(school, year, settings, recorded_by_id=None, target_date=None):
    """
    Mark all active students who have no attendance record for `target_date` as absent.
    Sets source='automatic'. Blocked if current time is before att_absence_threshold,
    or if the target date is a weekly holiday / named school holiday.
    Sends parent notifications for newly marked students.
    Returns {'too_early': bool, 'holiday': bool, 'count': int, 'students': list}.

    recorded_by_id — pass current_user.id from web requests; None from background jobs.
    target_date    — specific date to process; defaults to today. When provided the
                     too_early clock check is skipped (caller guarantees cutoff passed).
    Fully idempotent: calling multiple times per day is safe (won't create duplicates).
    """
    school_id   = school.id if school else None
    school_name = getattr(school, 'name', f'school_{school_id}')
    today       = target_date if target_date is not None else get_local_date(settings)
    now_local   = get_local_now(settings)
    now_time    = now_local.time()
    cutoff      = getattr(settings, 'att_absence_threshold', None)

    _log.info(
        '[attendance] _run_auto_absent school_id=%s "%s" date=%s '
        'local_now=%s cutoff=%s year_id=%s',
        school_id, school_name, today,
        now_local.strftime('%H:%M:%S'), cutoff,
        year.id if year else None,
    )

    # Skip too_early check for explicit target_date (catch-up path — cutoff already passed)
    if target_date is None and cutoff and now_time < cutoff:
        _log.info('[attendance] school_id=%s — absence time not yet reached '
                  '(now=%s < cutoff=%s) — skipped', school_id, now_time, cutoff)
        return {'too_early': True, 'holiday': False, 'count': 0, 'students': []}

    if school and is_holiday_date(today, school_id, school):
        _log.info('[attendance] school_id=%s date=%s — holiday detected — absent skipped',
                  school_id, today)
        return {'too_early': False, 'holiday': True, 'count': 0, 'students': []}

    _log.info('[attendance] school_id=%s date=%s — normal school day, querying students',
              school_id, today)

    # Use bypass_tenant_scope so queries work correctly from background threads
    # (no Flask request context → current_school_id() returns None → no auto-filter).
    # Students are NOT year-scoped: academic_year_id on the Student row is
    # the enrollment year and is never updated across years. Filtering by the
    # current year would exclude all students enrolled in previous years.
    # The year is correctly stamped on each StudentAttendance record below.
    all_students_q = (Student.query
                      .execution_options(bypass_tenant_scope=True)
                      .filter_by(status='active'))
    if school:
        all_students_q = all_students_q.filter_by(school_id=school_id)
    all_students = all_students_q.all()
    student_ids  = [s.id for s in all_students]

    _log.info('[attendance] school_id=%s active_students=%d year_id=%s',
              school_id, len(student_ids), year.id if year else None)

    if not student_ids:
        _log.info('[attendance] school_id=%s — no active students found, skip', school_id)
        return {'too_early': False, 'holiday': False, 'count': 0, 'students': []}

    already_ids = {
        row.student_id for row in
        StudentAttendance.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(date=today)
            .filter(StudentAttendance.student_id.in_(student_ids))
            .with_entities(StudentAttendance.student_id)
            .all()
    }
    unmarked = [s for s in all_students if s.id not in already_ids]

    _log.info(
        '[attendance] school_id=%s date=%s existing_attendance=%d missing_attendance=%d',
        school_id, today, len(already_ids), len(unmarked),
    )

    for student in unmarked:
        db.session.add(StudentAttendance(
            student_id       = student.id,
            school_id        = school_id,
            academic_year_id = year.id if year else None,
            date             = today,
            status           = 'absent',
            source           = 'automatic',
            recorded_by      = recorded_by_id,
        ))
    if unmarked:
        db.session.commit()
        _log.info('[attendance] school_id=%s — committed %d absent records',
                  school_id, len(unmarked))

    notified = 0
    for student in unmarked:
        try:
            _notify_absent_parents(student, school_id, today.isoformat(), source='automatic')
            notified += 1
        except Exception:
            _log.exception('[attendance-notify] notification failed student_id=%s', student.id)

    _log.info(
        '[attendance] school_id=%s date=%s — absent_created=%d notifications_sent=%d',
        school_id, today, len(unmarked), notified,
    )
    return {'too_early': False, 'holiday': False, 'count': len(unmarked), 'students': unmarked}


@attendance_bp.route('/')
@login_required
@permission_required('take_attendance')
def index():
    school = get_current_school()
    if _is_teacher():
        section_ids = get_teacher_section_ids(current_user)
        sections_qs = Section.query.filter(Section.id.in_(section_ids)).all() if section_ids else []
        teacher_grade_ids = list({s.grade_id for s in sections_qs if s.grade_id})
        all_grades = (Grade.query.filter(Grade.id.in_(teacher_grade_ids)).order_by(Grade.name).all()
                      if teacher_grade_ids else [])
    else:
        year = get_view_year(school.id) if school else None
        if school and year:
            all_grades  = Grade.query.filter_by(academic_year_id=year.id).order_by(Grade.name).all()
            grade_ids   = [g.id for g in all_grades]
            sections_qs = Section.query.filter(Section.grade_id.in_(grade_ids)).all() if grade_ids else []
        else:
            all_grades  = Grade.query.order_by(Grade.name).all()
            sections_qs = Section.query.all()

    settings = _get_settings()
    today    = get_local_date(settings)

    # Date filter — defaults to today
    date_str = request.args.get('date', today.isoformat())
    try:
        sel_date = dt.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        sel_date = today

    # Additional filters
    sel_stage      = request.args.get('stage', '')
    sel_grade_id   = request.args.get('grade_id', type=int)
    sel_section_id = request.args.get('section_id', type=int)
    q              = request.args.get('q', '').strip()

    # Filter section cards by stage / grade / section (q does not affect cards)
    grade_map         = {g.id: g for g in all_grades}
    filtered_sections = sections_qs
    if sel_stage:
        filtered_sections = [s for s in filtered_sections
                             if grade_map.get(s.grade_id) and grade_map[s.grade_id].stage == sel_stage]
    if sel_grade_id:
        filtered_sections = [s for s in filtered_sections if s.grade_id == sel_grade_id]
    if sel_section_id:
        filtered_sections = [s for s in filtered_sections if s.id == sel_section_id]

    # Auto-mark absent if viewing today and cutoff has passed (idempotent, silent)
    if sel_date == today and school and not _is_teacher():
        _auto_year = get_active_year(school.id)
        if _auto_year:
            _run_auto_absent(school, _auto_year, settings, recorded_by_id=current_user.id)

    # Attendance records for the selected date
    filtered_section_ids = [s.id for s in filtered_sections]
    if filtered_section_ids:
        att_q = (StudentAttendance.query
                 .join(Student, StudentAttendance.student_id == Student.id)
                 .filter(StudentAttendance.date == sel_date)
                 .filter(Student.section_id.in_(filtered_section_ids)))
        day_records = att_q.order_by(Student.full_name).all()
    else:
        day_records = []

    # Student name search — finds ALL matching active students (with or without
    # an attendance record for the selected date) so the search never misses a
    # student just because they haven't checked in yet.
    students_found = []
    if q:
        sq = (Student.query
              .filter_by(status='active')
              .filter(db.or_(
                  Student.full_name.ilike(f'%{q}%'),
                  Student.student_id.ilike(f'%{q}%'),
              )))
        if school:
            sq = sq.filter_by(school_id=school.id)
        if filtered_section_ids:
            sq = sq.filter(Student.section_id.in_(filtered_section_ids))
        matching_students = sq.order_by(Student.full_name).all()
        if matching_students:
            m_ids = [s.id for s in matching_students]
            att_map = {
                r.student_id: r
                for r in StudentAttendance.query
                   .filter_by(date=sel_date)
                   .filter(StudentAttendance.student_id.in_(m_ids))
                   .all()
            }
            for stu in matching_students:
                students_found.append({'student': stu, 'record': att_map.get(stu.id)})

    is_holiday_sel = is_holiday_date(sel_date, school.id, school) if school else False
    auto_absent_count_sel = 0
    if is_holiday_sel and school:
        auto_absent_count_sel = (
            StudentAttendance.query
            .filter_by(date=sel_date, status='absent', source='automatic')
            .count()
        )

    return render_template('attendance/index.html',
                           sections=filtered_sections,
                           all_sections=sections_qs,
                           all_grades=all_grades,
                           today=today, settings=settings,
                           sel_date=sel_date, day_records=day_records,
                           students_found=students_found,
                           sel_stage=sel_stage, sel_grade_id=sel_grade_id,
                           sel_section_id=sel_section_id, q=q,
                           is_holiday_sel=is_holiday_sel,
                           auto_absent_count_sel=auto_absent_count_sel)


@attendance_bp.route('/take/<int:section_id>', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('take_attendance')
def take(section_id):
    section  = Section.query.get_or_404(section_id)
    school   = get_current_school()
    year     = get_active_year(school.id) if school else None
    settings = _get_settings()
    if not school or not year:
        flash('Select a school with an active academic year before taking attendance.', 'danger')
        return redirect(url_for('attendance.index'))

    if _is_teacher() and section_id not in get_teacher_section_ids(current_user):
        flash('لا يمكنك أخذ حضور شعبة غير مخصصة لك.', 'danger')
        return redirect(url_for('attendance.index'))

    local_now = get_local_now(settings)
    default_date = local_now.date().isoformat()
    att_date_str = request.args.get('date', default_date)
    try:
        att_date = dt.strptime(att_date_str, '%Y-%m-%d').date()
    except ValueError:
        att_date = local_now.date()

    departure_time = getattr(settings, 'att_departure_time', None)
    now_time_snapshot = local_now.time().replace(microsecond=0)
    is_departure_time = (
        att_date == local_now.date()
        and departure_time is not None
        and now_time_snapshot >= departure_time
    )

    # Student is school-scoped only (not year-scoped) — students persist across
    # academic years. include_all_years ensures year-scoped joins (if any) don't
    # filter out students whose academic_year_id pre-dates the current year.
    students = (Student.query
                .execution_options(include_all_years=True)
                .filter_by(section_id=section_id, status='active')
                .all())
    existing = {a.student_id: a for a in
                StudentAttendance.query.filter_by(date=att_date)
                .filter(StudentAttendance.student_id.in_([s.id for s in students])).all()}

    # Identify suspended students for this date
    suspended_ids = {
        row.student_id for row in
        StudentSuspension.query
            .filter(StudentSuspension.student_id.in_([s.id for s in students]),
                    StudentSuspension.start_date <= att_date,
                    StudentSuspension.end_date   >= att_date)
            .with_entities(StudentSuspension.student_id)
            .all()
    }

    if request.method == 'POST':
        # Use local wall-clock time for recording
        now_local  = get_local_now(settings)
        now_time   = now_local.time().replace(microsecond=0)
        departure  = getattr(settings, 'att_departure_time', None)
        post_is_departure = (
            att_date == now_local.date()
            and departure is not None
            and now_time >= departure
        )
        newly_checked_in   = []   # list of (student, status)
        newly_checked_out  = []   # list of student
        newly_absent       = []   # list of student (new absent records only)
        already_marked_count = 0

        for student in students:
            # Skip attendance for suspended students
            if student.id in suspended_ids:
                continue
            status_choice = request.form.get(f'status_{student.id}', 'absent')

            if student.id in existing:
                rec = existing[student.id]
                if rec.check_in is not None:
                    if post_is_departure and rec.check_out is None:
                        # Only check out students whose checkbox was ticked
                        if request.form.get(f'checkout_{student.id}'):
                            rec.check_out = now_time
                            note = request.form.get(f'departure_note_{student.id}', '').strip()
                            if note:
                                rec.notes = note
                            newly_checked_out.append(student)
                        # else: left unchecked — skip silently
                    else:
                        already_marked_count += 1
                    continue
                # Record exists but no check_in yet — allow update
                if status_choice == 'present':
                    _shift = get_student_shift(student, school)
                    actual_status = determine_check_in_status(now_time, settings, shift=_shift)
                    rec.check_in  = now_time
                elif status_choice == 'late':
                    actual_status = 'late'
                    rec.check_in  = now_time
                else:
                    actual_status = 'absent'
                prev       = rec.status
                rec.status = actual_status
                if actual_status in ('present', 'late') and prev not in ('present', 'late'):
                    newly_checked_in.append((student, actual_status))
            else:
                _shift = get_student_shift(student, school)
                if status_choice == 'present':
                    actual_status = determine_check_in_status(now_time, settings, shift=_shift)
                    check_in_val  = now_time
                elif status_choice == 'late':
                    actual_status = 'late'
                    check_in_val  = now_time
                else:
                    actual_status = 'absent'
                    check_in_val  = None
                db.session.add(StudentAttendance(
                    student_id       = student.id,
                    school_id        = school.id if school else None,
                    academic_year_id = year.id if year else None,
                    date             = att_date,
                    status           = actual_status,
                    check_in         = check_in_val,
                    recorded_by      = current_user.id,
                    shift_id         = _shift.id if _shift else None,
                ))
                if actual_status in ('present', 'late'):
                    newly_checked_in.append((student, actual_status))
                elif actual_status == 'absent':
                    newly_absent.append(student)

        db.session.commit()

        if already_marked_count:
            flash(f'{already_marked_count} طالب لديهم سجل حضور مسبق اليوم ولم يتم تعديله.', 'info')

        # Check-out push notifications
        departure_str = now_time.strftime('%H:%M')
        for student in newly_checked_out:
            _log.info('[attendance-notify] source=manual action=check_out student_id=%s name=%s date=%s',
                      student.id, student.full_name, att_date)
            NotificationService.send_to_parents_of_student(
                student.id,
                'انصراف الطالب من المدرسة',
                f'طالبك {student.full_name} انصرف من المدرسة الساعة {departure_str}.',
                ntype='attendance',
                data={'action': 'check_out', 'at': now_time.isoformat(),
                      'source': 'manual', 'date': att_date.isoformat(), 'screen': 'attendance'}
            )

        # Status-specific push notifications (use local time string)
        arrival_str = now_time.strftime('%H:%M')
        for student, status in newly_checked_in:
            if status == 'present':
                title = 'حضور الطالب في الوقت المحدد'
                body  = f'طالبك {student.full_name} وصل في الوقت المحدد الساعة {arrival_str}.'
            else:
                title = 'تأخر الطالب عن موعد الحضور'
                body  = f'طالبك {student.full_name} وصل متأخراً الساعة {arrival_str}.'
            _log.info('[attendance-notify] source=manual action=check_in status=%s student_id=%s name=%s date=%s',
                      status, student.id, student.full_name, att_date)
            NotificationService.send_to_parents_of_student(
                student.id, title, body, ntype='attendance',
                data={'action': 'check_in', 'status': status,
                      'at': now_time.isoformat(), 'source': 'manual',
                      'date': att_date.isoformat(), 'screen': 'attendance'}
            )

        # Absent push notifications (new absent records only)
        for student in newly_absent:
            try:
                _notify_absent_parents(student, school.id if school else None,
                                       att_date.isoformat(), source='manual')
            except Exception:
                _log.exception('[attendance-notify] absent notification failed student_id=%s',
                               student.id)

        if newly_checked_out:
            flash(f'تم تسجيل انصراف {len(newly_checked_out)} طالب الساعة {departure_str}.', 'success')
        else:
            flash(f'تم حفظ الحضور ليوم {att_date.strftime("%Y-%m-%d")}.', 'success')
        return redirect(url_for('attendance.index'))

    return render_template('attendance/take.html',
                           section=section, students=students,
                           existing=existing, att_date=att_date,
                           suspended_ids=suspended_ids,
                           settings=settings,
                           is_departure_time=is_departure_time)


@attendance_bp.route('/mark-absent-today', methods=['POST'])
@login_required
@historical_guard
@admin_required
def mark_absent_today():
    """
    Manually trigger automatic absence marking for today.
    Blocked if current time has not yet passed the configured absence cutoff.
    """
    school   = get_current_school()
    settings = _get_settings()
    year     = get_active_year(school.id) if school else None

    result = _run_auto_absent(school, year, settings, recorded_by_id=current_user.id)

    if result['too_early']:
        cutoff = getattr(settings, 'att_absence_threshold', None)
        cutoff_str = cutoff.strftime('%H:%M') if cutoff else ''
        flash(f'لا يمكن تسجيل الغياب قبل وقت الغياب المحدد ({cutoff_str}).', 'warning')
        return redirect(url_for('attendance.index'))

    if result.get('holiday'):
        flash('هذا اليوم عطلة، لا يتم تسجيل الغياب التلقائي.', 'info')
        return redirect(url_for('attendance.index'))

    count = result['count']
    if count == 0:
        flash('جميع الطلاب لديهم سجلات حضور بالفعل. لا يوجد طلاب لتسجيل غيابهم.', 'info')
    else:
        flash(f'تم تسجيل غياب {count} طالب تلقائياً لم يُسجَّل حضورهم اليوم.', 'success')
    return redirect(url_for('attendance.index'))


@attendance_bp.route('/student/<int:student_id>/profile')
@login_required
@permission_required('take_attendance')
def student_profile(student_id):
    """Full attendance history + summary for one student."""
    student  = Student.query.get_or_404(student_id)
    settings = SchoolSettings.get()

    if _is_teacher() and student.section_id not in get_teacher_section_ids(current_user):
        flash('لا يمكنك عرض ملف طالب خارج شعبتك.', 'danger')
        return redirect(url_for('attendance.index'))

    local_today = get_local_date(settings)
    start_str   = request.args.get('start', local_today.replace(month=1, day=1).isoformat())
    end_str     = request.args.get('end',   local_today.isoformat())

    try:
        start_date = dt.strptime(start_str, '%Y-%m-%d').date()
        end_date   = dt.strptime(end_str,   '%Y-%m-%d').date()
    except ValueError:
        start_date = local_today.replace(month=1, day=1)
        end_date   = local_today

    records = (StudentAttendance.query
               .filter_by(student_id=student.id)
               .filter(StudentAttendance.date.between(start_date, end_date))
               .order_by(StudentAttendance.date.asc())
               .all())

    total   = len(records)
    present = sum(1 for r in records if r.status == 'present')
    absent  = sum(1 for r in records if r.status == 'absent')
    late    = sum(1 for r in records if r.status == 'late')
    att_pct = round(present / total * 100, 1) if total else 0

    stats = {
        'total': total, 'present': present,
        'absent': absent, 'late': late, 'att_pct': att_pct,
    }
    now = get_local_now(settings)
    return render_template('attendance/student_profile.html',
                           student=student, records=records, stats=stats,
                           start=start_date.isoformat(), end=end_date.isoformat(),
                           now=now)


def _build_report_pools(school, year):
    """Return (all_grades, all_sections, grade_map, all_shifts) for the current user/school."""
    from app.models import AttendanceShift
    if _is_teacher():
        teacher_ids  = get_teacher_section_ids(current_user)
        all_sections = (Section.query
                        .execution_options(include_all_years=True)
                        .filter(Section.id.in_(teacher_ids)).all()
                        if teacher_ids else [])
    else:
        if school and year:
            _gids        = [g.id for g in
                            Grade.query
                            .execution_options(include_all_years=True)
                            .filter_by(academic_year_id=year.id).all()]
            all_sections = (Section.query
                            .execution_options(include_all_years=True)
                            .filter(Section.grade_id.in_(_gids)).all()
                            if _gids else [])
        else:
            all_sections = Section.query.all()

    _gid_set   = {s.grade_id for s in all_sections if s.grade_id}
    all_grades = (Grade.query
                  .execution_options(include_all_years=True)
                  .filter(Grade.id.in_(_gid_set))
                  .order_by(Grade.name).all()
                  if _gid_set else [])
    grade_map  = {g.id: g for g in all_grades}

    all_shifts = []
    if school and getattr(school, 'enable_attendance_shifts', False):
        all_shifts = (AttendanceShift.query
                      .filter_by(school_id=school.id)
                      .order_by(AttendanceShift.start_time)
                      .all())
    return all_grades, all_sections, grade_map, all_shifts


def _get_student_query(school, all_sections, stage=None, grade_id=None,
                       section_id=None, shift_id=None, q=None):
    """Build and return a Student query with appropriate filters."""
    from app.models import AttendanceShift
    grade_map = {s.grade_id: s for s in all_sections}

    pool_sections = list(all_sections)
    if stage:
        pool_sections = [s for s in pool_sections
                         if s.grade and s.grade.stage == stage]
    if grade_id:
        pool_sections = [s for s in pool_sections if s.grade_id == grade_id]
    if section_id:
        pool_sections = [s for s in pool_sections if s.id == section_id]
    if shift_id:
        # filter by students whose effective shift = shift_id
        pool_sections = [s for s in pool_sections
                         if s.shift_id == shift_id
                         or (s.shift_id is None
                             and s.grade and s.grade.shift_id == shift_id)]

    pool_ids = [s.id for s in pool_sections]

    sq = (Student.query
          .execution_options(include_all_years=True)
          .filter_by(status='active'))
    if school:
        sq = sq.filter_by(school_id=school.id)

    if pool_ids:
        sq = sq.filter(Student.section_id.in_(pool_ids))
    else:
        sq = sq.filter(Student.id == -1)

    if q:
        sq = sq.filter(db.or_(
            Student.full_name.ilike(f'%{q}%'),
            Student.student_id.ilike(f'%{q}%'),
        ))
    return sq


def _student_stats(student_id, start_date, end_date, status_filter=None):
    """Return (present, absent, late, checkout, details) for one student/range."""
    q = (StudentAttendance.query
         .filter_by(student_id=student_id)
         .filter(StudentAttendance.date.between(start_date, end_date)))
    if status_filter:
        q = q.filter(StudentAttendance.status == status_filter)
    atts = q.order_by(StudentAttendance.date.asc()).all()
    present  = sum(1 for a in atts if a.status == 'present')
    absent   = sum(1 for a in atts if a.status == 'absent')
    late     = sum(1 for a in atts if a.status == 'late')
    checkout = sum(1 for a in atts if a.check_out is not None)
    return present, absent, late, checkout, atts


@attendance_bp.route('/report')
@login_required
@permission_required('take_attendance')
def report():
    school      = get_current_school()
    settings    = _get_settings()
    local_today = get_local_date(settings)

    # ── Params ─────────────────────────────────────────────────────────────
    report_type = request.args.get('report_type', 'detail')
    section_id  = request.args.get('section_id',  type=int)
    grade_id    = request.args.get('grade_id',    type=int)
    shift_id    = request.args.get('shift_id',    type=int)
    stage       = request.args.get('stage', '').strip()
    status_f    = request.args.get('status', '').strip()   # present/absent/late
    q           = request.args.get('q', '').strip()
    start       = request.args.get('start', local_today.replace(day=1).isoformat())
    end         = request.args.get('end',   local_today.isoformat())
    submitted   = bool(request.args)

    try:
        start_date = dt.strptime(start, '%Y-%m-%d').date()
        end_date   = dt.strptime(end,   '%Y-%m-%d').date()
    except ValueError:
        start_date = local_today.replace(day=1)
        end_date   = local_today

    year = get_view_year(school.id) if school else None
    all_grades, all_sections, grade_map, all_shifts = _build_report_pools(school, year)

    # Teacher: restrict section_id to allowed sections
    if _is_teacher():
        allowed_ids = {s.id for s in all_sections}
        if section_id and section_id not in allowed_ids:
            section_id = None

    records         = []   # for 'detail' mode (per-student rows)
    grade_summary   = []   # for 'grade' mode
    section_summary = []   # for 'section' mode
    shift_summary   = []   # for 'shift' mode

    if submitted:
        sq = _get_student_query(school, all_sections, stage, grade_id,
                                section_id, shift_id, q)
        students = sq.order_by(Student.full_name).all()

        if report_type == 'grade':
            # Group students by grade, aggregate counts
            from collections import defaultdict
            grade_buckets = defaultdict(list)
            for s in students:
                gid = s.section.grade_id if s.section else None
                grade_buckets[gid].append(s)
            for gid, sts in grade_buckets.items():
                g = grade_map.get(gid)
                total_p = total_a = total_l = 0
                for s in sts:
                    p, a, l, _co, _det = _student_stats(s.id, start_date, end_date, status_f or None)
                    total_p += p; total_a += a; total_l += l
                total = total_p + total_a + total_l
                pct = round((total_p + total_l) / total * 100, 1) if total else 0
                grade_summary.append({
                    'grade': g, 'grade_id': gid,
                    'total_students': len(sts),
                    'present': total_p, 'absent': total_a, 'late': total_l,
                    'total': total, 'pct': pct,
                })

        elif report_type == 'section':
            from collections import defaultdict
            sec_buckets = defaultdict(list)
            for s in students:
                sec_buckets[s.section_id].append(s)
            for sid, sts in sec_buckets.items():
                sec = next((s for s in all_sections if s.id == sid), None)
                total_p = total_a = total_l = 0
                for s in sts:
                    p, a, l, _co, _det = _student_stats(s.id, start_date, end_date, status_f or None)
                    total_p += p; total_a += a; total_l += l
                total = total_p + total_a + total_l
                pct = round((total_p + total_l) / total * 100, 1) if total else 0
                section_summary.append({
                    'section': sec, 'section_id': sid,
                    'total_students': len(sts),
                    'present': total_p, 'absent': total_a, 'late': total_l,
                    'total': total, 'pct': pct,
                })

        elif report_type == 'shift':
            from collections import defaultdict
            from app.models import AttendanceShift
            shift_buckets = defaultdict(list)
            for s in students:
                eff_shift_id = None
                if s.section:
                    eff_shift_id = s.section.shift_id
                    if eff_shift_id is None and s.section.grade:
                        eff_shift_id = s.section.grade.shift_id
                shift_buckets[eff_shift_id].append(s)
            for eff_sid, sts in shift_buckets.items():
                sh = (AttendanceShift.query.get(eff_sid)
                      if eff_sid else None)
                total_p = total_a = total_l = 0
                for s in sts:
                    p, a, l, _co, _det = _student_stats(s.id, start_date, end_date, status_f or None)
                    total_p += p; total_a += a; total_l += l
                total = total_p + total_a + total_l
                pct = round((total_p + total_l) / total * 100, 1) if total else 0
                shift_summary.append({
                    'shift': sh, 'shift_id': eff_sid,
                    'total_students': len(sts),
                    'present': total_p, 'absent': total_a, 'late': total_l,
                    'total': total, 'pct': pct,
                })

        else:
            # detail / student modes — per-student rows
            for s in students:
                p, a, l, co, atts = _student_stats(s.id, start_date, end_date, status_f or None)
                records.append({
                    'student': s,
                    'present': p, 'absent': a, 'late': l, 'checkout': co,
                    'details': atts,
                })

    shifts_enabled = bool(school and getattr(school, 'enable_attendance_shifts', False))
    return render_template('attendance/report.html',
                           records=records,
                           grade_summary=grade_summary,
                           section_summary=section_summary,
                           shift_summary=shift_summary,
                           all_sections=all_sections,
                           all_grades=all_grades,
                           all_shifts=all_shifts,
                           shifts_enabled=shifts_enabled,
                           report_type=report_type,
                           section_id=section_id, grade_id=grade_id,
                           shift_id=shift_id,
                           stage=stage, status_f=status_f, q=q,
                           start=start, end=end,
                           submitted=submitted)


@attendance_bp.route('/report/export-pdf')
@login_required
@permission_required('take_attendance')
def report_export_pdf():
    """Export the current report view as a PDF file."""
    from flask import make_response
    from app.utils.pdf_gen import generate_attendance_report_pdf

    school      = get_current_school()
    settings    = _get_settings()
    local_today = get_local_date(settings)

    report_type = request.args.get('report_type', 'detail')
    section_id  = request.args.get('section_id',  type=int)
    grade_id    = request.args.get('grade_id',    type=int)
    shift_id    = request.args.get('shift_id',    type=int)
    stage       = request.args.get('stage', '').strip()
    status_f    = request.args.get('status', '').strip()
    q           = request.args.get('q', '').strip()
    start       = request.args.get('start', local_today.replace(day=1).isoformat())
    end         = request.args.get('end',   local_today.isoformat())

    try:
        start_date = dt.strptime(start, '%Y-%m-%d').date()
        end_date   = dt.strptime(end,   '%Y-%m-%d').date()
    except ValueError:
        start_date = local_today.replace(day=1)
        end_date   = local_today

    year = get_view_year(school.id) if school else None
    _all_grades, all_sections, grade_map, all_shifts = _build_report_pools(school, year)

    sq = _get_student_query(school, all_sections, stage, grade_id,
                            section_id, shift_id, q)
    students = sq.order_by(Student.full_name).all()

    rows = []
    for s in students:
        p, a, l, co, atts = _student_stats(s.id, start_date, end_date, status_f or None)
        rows.append({'student': s, 'present': p, 'absent': a, 'late': l,
                     'checkout': co, 'details': atts})

    pdf_bytes = generate_attendance_report_pdf(
        rows=rows,
        report_type=report_type,
        date_from=start, date_to=end,
        school=school,
        grade_map=grade_map,
    )
    if not pdf_bytes:
        flash('تعذّر إنشاء ملف PDF — تأكد من تثبيت مكتبة ReportLab وتوفر الخط العربي.', 'danger')
        return redirect(url_for('attendance.report', **request.args))

    resp = make_response(pdf_bytes)
    resp.headers['Content-Type']        = 'application/pdf'
    resp.headers['Content-Disposition'] = f'attachment; filename="attendance_report_{start}_{end}.pdf"'
    return resp


@attendance_bp.route('/report/export-excel')
@login_required
@permission_required('take_attendance')
def report_export_excel():
    """Export the current report view as an Excel (.xlsx) file."""
    from flask import make_response
    from app.utils.attendance_export import generate_attendance_excel

    school      = get_current_school()
    settings    = _get_settings()
    local_today = get_local_date(settings)

    report_type = request.args.get('report_type', 'detail')
    section_id  = request.args.get('section_id',  type=int)
    grade_id    = request.args.get('grade_id',    type=int)
    shift_id    = request.args.get('shift_id',    type=int)
    stage       = request.args.get('stage', '').strip()
    status_f    = request.args.get('status', '').strip()
    q           = request.args.get('q', '').strip()
    start       = request.args.get('start', local_today.replace(day=1).isoformat())
    end         = request.args.get('end',   local_today.isoformat())

    try:
        start_date = dt.strptime(start, '%Y-%m-%d').date()
        end_date   = dt.strptime(end,   '%Y-%m-%d').date()
    except ValueError:
        start_date = local_today.replace(day=1)
        end_date   = local_today

    year = get_view_year(school.id) if school else None
    _all_grades, all_sections, grade_map, all_shifts = _build_report_pools(school, year)

    sq = _get_student_query(school, all_sections, stage, grade_id,
                            section_id, shift_id, q)
    students = sq.order_by(Student.full_name).all()

    rows = []
    for s in students:
        p, a, l, co, atts = _student_stats(s.id, start_date, end_date, status_f or None)
        rows.append({'student': s, 'present': p, 'absent': a, 'late': l,
                     'checkout': co, 'details': atts})

    xlsx_bytes = generate_attendance_excel(
        rows=rows,
        report_type=report_type,
        date_from=start, date_to=end,
        school=school,
    )
    resp = make_response(xlsx_bytes)
    resp.headers['Content-Type']        = (
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    resp.headers['Content-Disposition'] = (
        f'attachment; filename="attendance_report_{start}_{end}.xlsx"'
    )
    return resp


# ─────────────────────────────────────────────────────────────────────────────
#  STUDENT SUSPENSIONS
# ─────────────────────────────────────────────────────────────────────────────

@attendance_bp.route('/suspensions')
@login_required
@admin_required
def suspensions():
    school = get_current_school()

    suspensions_q = StudentSuspension.query.join(Student)
    if school:
        suspensions_q = suspensions_q.filter(StudentSuspension.school_id == school.id)
    all_suspensions = (suspensions_q
                       .order_by(StudentSuspension.start_date.desc())
                       .all())

    # Students available for new suspension — all active students in the school,
    # regardless of enrollment year, since students persist across academic years.
    students_q = Student.query.filter_by(status='active')
    if school:
        students_q = students_q.filter_by(school_id=school.id)
    students = students_q.order_by(Student.full_name).all()

    return render_template('attendance/suspensions.html',
                           all_suspensions=all_suspensions,
                           students=students)


@attendance_bp.route('/suspensions/create', methods=['POST'])
@login_required
@historical_guard
@admin_required
def create_suspension():
    school = get_current_school()
    year   = get_active_year(school.id) if school else None
    if not school or not year:
        flash('يجب تحديد مدرسة وعام دراسي نشط.', 'danger')
        return redirect(url_for('attendance.suspensions'))

    student_id = request.form.get('student_id', type=int)
    start_str  = request.form.get('start_date', '')
    end_str    = request.form.get('end_date', '')
    reason     = request.form.get('reason', '').strip()

    if not student_id or not start_str or not end_str:
        flash('يرجى تعبئة جميع الحقول المطلوبة.', 'danger')
        return redirect(url_for('attendance.suspensions'))

    try:
        start_date = dt.strptime(start_str, '%Y-%m-%d').date()
        end_date   = dt.strptime(end_str,   '%Y-%m-%d').date()
    except ValueError:
        flash('صيغة التاريخ غير صحيحة.', 'danger')
        return redirect(url_for('attendance.suspensions'))

    if end_date < start_date:
        flash('تاريخ الانتهاء يجب أن يكون بعد تاريخ البدء.', 'danger')
        return redirect(url_for('attendance.suspensions'))

    susp = StudentSuspension(
        student_id       = student_id,
        school_id        = school.id,
        academic_year_id = year.id,
        start_date       = start_date,
        end_date         = end_date,
        reason           = reason or None,
        created_by       = current_user.id,
    )
    db.session.add(susp)
    db.session.commit()
    flash('تم تسجيل إيقاف الطالب بنجاح.', 'success')
    return redirect(url_for('attendance.suspensions'))


@attendance_bp.route('/suspensions/<int:susp_id>/delete', methods=['POST'])
@login_required
@historical_guard
@admin_required
def delete_suspension(susp_id):
    susp = StudentSuspension.query.get_or_404(susp_id)
    db.session.delete(susp)
    db.session.commit()
    flash('تم إلغاء إيقاف الطالب.', 'success')
    return redirect(url_for('attendance.suspensions'))


@attendance_bp.route('/cleanup-holiday-absences', methods=['POST'])
@login_required
@admin_required
def cleanup_holiday_absences():
    """
    Delete auto-generated absent records for a date that is (or was) a holiday.
    Only removes records with source='automatic' and status='absent'.
    Manual records, RFID punches, and all other sources are untouched.
    """
    school   = get_current_school()
    date_str = request.form.get('date', '').strip()
    try:
        cleanup_date = dt.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        flash('تاريخ غير صحيح.', 'danger')
        return redirect(url_for('attendance.index'))

    if not is_holiday_date(cleanup_date, school.id if school else None):
        flash('هذا اليوم ليس عطلة — لا يمكن تنظيف سجلات الغياب التلقائي.', 'danger')
        return redirect(url_for('attendance.index', date=date_str))

    # Collect IDs first; delete by PK to avoid join-delete dialect issues.
    ids_q = (
        StudentAttendance.query
        .filter_by(date=cleanup_date, status='absent', source='automatic')
        .join(Student, StudentAttendance.student_id == Student.id)
    )
    if school:
        ids_q = ids_q.filter(Student.school_id == school.id)
    ids_to_delete = [row[0] for row in ids_q.with_entities(StudentAttendance.id).all()]

    deleted = len(ids_to_delete)
    if ids_to_delete:
        StudentAttendance.query.filter(
            StudentAttendance.id.in_(ids_to_delete)
        ).delete(synchronize_session='fetch')
        db.session.commit()

    _log.info('[attendance] holiday cleanup: deleted %d auto-absent records date=%s school_id=%s',
              deleted, cleanup_date, school.id if school else None)
    flash(
        f'تم حذف {deleted} سجل غياب تلقائي ليوم {date_str}. '
        '(السجلات اليدوية وسجلات البصمة محفوظة ولم تُمسّ)',
        'success' if deleted else 'info',
    )
    return redirect(url_for('attendance.index', date=date_str))
