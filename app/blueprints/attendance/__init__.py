"""Mecha-School – Attendance Blueprint  (Phase 6: school + year scoped)"""
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from datetime import datetime as dt, date as date_type
from app.models import db, StudentAttendance, Student, Section, SchoolSettings, Grade, StudentSuspension
from app.utils.decorators import (permission_required, get_teacher_section_ids,
                                   admin_required, get_current_school, get_active_year, get_view_year,
                                   historical_guard)
from app.utils.attendance_helpers import (determine_check_in_status,
                                           get_local_now, get_local_date)
from app.services.notifications import NotificationService

attendance_bp = Blueprint('attendance', __name__,
                           template_folder='../../templates/attendance')


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


def _run_auto_absent(school, year, settings):
    """
    Mark all active students who have no attendance record for today as absent.
    Sets source='automatic'. Blocked if current time is before att_absence_threshold.
    Sends parent notifications for newly marked students.
    Returns {'too_early': bool, 'count': int, 'students': list}.
    """
    today    = get_local_date(settings)
    now_time = get_local_now(settings).time()
    cutoff   = getattr(settings, 'att_absence_threshold', None)

    if cutoff and now_time < cutoff:
        return {'too_early': True, 'count': 0, 'students': []}

    all_students_q = Student.query.filter_by(status='active')
    if school:
        all_students_q = all_students_q.filter_by(school_id=school.id)
    if year:
        all_students_q = all_students_q.filter_by(academic_year_id=year.id)
    all_students = all_students_q.all()
    student_ids  = [s.id for s in all_students]

    if not student_ids:
        return {'too_early': False, 'count': 0, 'students': []}

    already_ids = {
        row.student_id for row in
        StudentAttendance.query
            .filter_by(date=today)
            .filter(StudentAttendance.student_id.in_(student_ids))
            .with_entities(StudentAttendance.student_id)
            .all()
    }
    unmarked = [s for s in all_students if s.id not in already_ids]

    for student in unmarked:
        db.session.add(StudentAttendance(
            student_id       = student.id,
            school_id        = school.id if school else None,
            academic_year_id = year.id if year else None,
            date             = today,
            status           = 'absent',
            source           = 'automatic',
            recorded_by      = current_user.id,
        ))
    if unmarked:
        db.session.commit()

    for student in unmarked:
        NotificationService.send_to_parents_of_student(
            student.id,
            'غياب الطالب عن المدرسة',
            f'طالبك {student.full_name} غاب عن المدرسة اليوم.',
            ntype='attendance',
            data={'action': 'absent', 'source': 'automatic', 'date': today.isoformat()}
        )

    return {'too_early': False, 'count': len(unmarked), 'students': unmarked}


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
            _run_auto_absent(school, _auto_year, settings)

    # Attendance records for the selected date
    filtered_section_ids = [s.id for s in filtered_sections]
    if filtered_section_ids:
        att_q = (StudentAttendance.query
                 .join(Student, StudentAttendance.student_id == Student.id)
                 .filter(StudentAttendance.date == sel_date)
                 .filter(Student.section_id.in_(filtered_section_ids)))
        if q:
            att_q = att_q.filter(
                db.or_(
                    Student.full_name.ilike(f'%{q}%'),
                    Student.student_id.ilike(f'%{q}%'),
                )
            )
        day_records = att_q.order_by(Student.full_name).all()
    else:
        day_records = []

    return render_template('attendance/index.html',
                           sections=filtered_sections,
                           all_sections=sections_qs,
                           all_grades=all_grades,
                           today=today, settings=settings,
                           sel_date=sel_date, day_records=day_records,
                           sel_stage=sel_stage, sel_grade_id=sel_grade_id,
                           sel_section_id=sel_section_id, q=q)


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
                    actual_status = determine_check_in_status(now_time, settings)
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
                if status_choice == 'present':
                    actual_status = determine_check_in_status(now_time, settings)
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
                ))
                if actual_status in ('present', 'late'):
                    newly_checked_in.append((student, actual_status))

        db.session.commit()

        if already_marked_count:
            flash(f'{already_marked_count} طالب لديهم سجل حضور مسبق اليوم ولم يتم تعديله.', 'info')

        # Check-out push notifications
        departure_str = now_time.strftime('%H:%M')
        for student in newly_checked_out:
            NotificationService.send_to_parents_of_student(
                student.id,
                'انصراف الطالب من المدرسة',
                f'طالبك {student.full_name} انصرف من المدرسة الساعة {departure_str}.',
                ntype='attendance',
                data={'action': 'check_out', 'at': now_time.isoformat(),
                      'source': 'manual', 'date': att_date.isoformat()}
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
            NotificationService.send_to_parents_of_student(
                student.id, title, body, ntype='attendance',
                data={'action': 'check_in', 'status': status,
                      'at': now_time.isoformat(), 'source': 'manual',
                      'date': att_date.isoformat()}
            )

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

    result = _run_auto_absent(school, year, settings)

    if result['too_early']:
        cutoff = getattr(settings, 'att_absence_threshold', None)
        cutoff_str = cutoff.strftime('%H:%M') if cutoff else ''
        flash(f'لا يمكن تسجيل الغياب قبل وقت الغياب المحدد ({cutoff_str}).', 'warning')
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


@attendance_bp.route('/report')
@login_required
@permission_required('take_attendance')
def report():
    settings   = SchoolSettings.get()
    local_today = get_local_date(settings)
    section_id  = request.args.get('section_id', type=int)
    start       = request.args.get('start', local_today.replace(day=1).isoformat())
    end         = request.args.get('end',   local_today.isoformat())

    try:
        start_date = dt.strptime(start, '%Y-%m-%d').date()
        end_date   = dt.strptime(end,   '%Y-%m-%d').date()
    except ValueError:
        start_date = local_today.replace(day=1)
        end_date   = local_today

    if _is_teacher():
        teacher_ids = get_teacher_section_ids(current_user)
        sections    = Section.query.filter(Section.id.in_(teacher_ids)).all() if teacher_ids else []
        if section_id and section_id not in teacher_ids:
            section_id = None
    else:
        sections = Section.query.all()

    records = []
    if section_id:
        students = (Student.query
                    .execution_options(include_all_years=True)
                    .filter_by(section_id=section_id, status='active')
                    .all())
        for s in students:
            atts = (StudentAttendance.query
                    .filter_by(student_id=s.id)
                    .filter(StudentAttendance.date.between(start_date, end_date))
                    .order_by(StudentAttendance.date.asc())
                    .all())
            records.append({
                'student': s,
                'present': sum(1 for a in atts if a.status == 'present'),
                'absent':  sum(1 for a in atts if a.status == 'absent'),
                'late':    sum(1 for a in atts if a.status == 'late'),
                'details': atts,
            })

    return render_template('attendance/report.html',
                           records=records, sections=sections,
                           section_id=section_id, start=start, end=end)


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
