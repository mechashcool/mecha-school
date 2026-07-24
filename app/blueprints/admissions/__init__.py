"""
Admissions — staff review of external (public) student-registration requests.

School staff with the existing student-management permission review submissions
and approve (creating the real Student + parent account/link via the shared
approval service) or reject them. Everything is strictly school-scoped: the ORM
auto-filters by school and every route also 404s a cross-school id.
"""
from __future__ import annotations

from flask import (Blueprint, render_template, redirect, url_for, flash,
                   request, abort)
from flask_login import login_required, current_user

from app.models import db, StudentRegistrationRequest, Section, Grade
from app.utils.decorators import (permission_required, any_permission_required,
                                   get_current_school, get_active_year,
                                   historical_guard)
from app.services.admission_approval import (approve_request, reject_request,
                                             find_matching_parent, ApprovalError)

admissions_bp = Blueprint(
    'admissions', __name__,
    template_folder='../../templates/admissions',
)


def _school_or_404():
    school = get_current_school()
    if not school:
        abort(404)
    return school


def _request_or_404(school, request_id):
    req = (StudentRegistrationRequest.query
           .filter_by(id=request_id, school_id=school.id)
           .first())
    if req is None:
        abort(404)
    return req


@admissions_bp.route('/')
@login_required
@permission_required('view_students')
def index():
    school = _school_or_404()
    status = request.args.get('status', 'pending')
    if status not in ('pending', 'approved', 'rejected', 'all'):
        status = 'pending'

    query = StudentRegistrationRequest.query.filter_by(school_id=school.id)
    if status != 'all':
        query = query.filter_by(status=status)
    requests = query.order_by(StudentRegistrationRequest.created_at.desc()).all()

    counts = {
        'pending':  StudentRegistrationRequest.query.filter_by(
                        school_id=school.id, status='pending').count(),
        'approved': StudentRegistrationRequest.query.filter_by(
                        school_id=school.id, status='approved').count(),
        'rejected': StudentRegistrationRequest.query.filter_by(
                        school_id=school.id, status='rejected').count(),
    }
    return render_template('admissions/index.html',
                           requests=requests, status=status, counts=counts)


@admissions_bp.route('/<int:request_id>')
@login_required
@any_permission_required('view_students', 'add_student')
def detail(request_id):
    school = _school_or_404()
    req = _request_or_404(school, request_id)
    year = get_active_year(school.id)

    grade = Grade.query.filter_by(id=req.desired_grade_id,
                                  school_id=school.id).first()

    # Sections of the active year for staff to assign at approval (school-scoped).
    sections = []
    if year:
        grade_ids = [g.id for g in Grade.query.execution_options(include_all_years=True)
                     .filter_by(school_id=school.id, academic_year_id=year.id).all()]
        if grade_ids:
            sections = (Section.query
                        .filter(Section.grade_id.in_(grade_ids))
                        .order_by(Section.name).all())

    # Same-school existing-parent suggestion by normalized phone (staff confirm).
    match = None
    if req.status == 'pending':
        match = find_matching_parent(school.id, req.guardian_phone)

    return render_template('admissions/detail.html',
                           req=req, grade=grade, sections=sections,
                           parent_match=match)


@admissions_bp.route('/<int:request_id>/approve', methods=['POST'])
@login_required
@any_permission_required('add_student')
@historical_guard
def approve(request_id):
    school = _school_or_404()
    _request_or_404(school, request_id)  # 404 cross-school before any work

    section_id = request.form.get('section_id', type=int)
    parent_choice = request.form.get('parent_choice', 'new')
    link_parent_id = (request.form.get('link_parent_id', type=int)
                      if parent_choice == 'link' else None)

    try:
        result = approve_request(request_id, school, current_user,
                                 section_id=section_id,
                                 link_parent_id=link_parent_id)
    except ApprovalError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admissions.detail', request_id=request_id))

    if result.get('already'):
        flash('تم اعتماد هذا الطلب مسبقاً.', 'info')
    elif result.get('parent_created'):
        # One-time display of the new parent's credentials to staff (never stored
        # as plaintext, never shown to the public). Staff hand these to the guardian.
        flash(
            'تم اعتماد الطلب وإنشاء حساب ولي الأمر. '
            f"اسم المستخدم: {result['parent_username']} — "
            f"كلمة المرور: {result['parent_password']}. "
            'يرجى حفظ هذه البيانات وتسليمها لولي الأمر (لن تظهر مرة أخرى).',
            'success')
    else:
        flash('تم اعتماد الطلب وربط الطالب بحساب ولي الأمر الحالي.', 'success')

    return redirect(url_for('admissions.detail', request_id=request_id))


@admissions_bp.route('/<int:request_id>/reject', methods=['POST'])
@login_required
@any_permission_required('add_student')
@historical_guard
def reject(request_id):
    school = _school_or_404()
    _request_or_404(school, request_id)
    reason = request.form.get('rejection_reason', '')
    try:
        reject_request(request_id, school, current_user, reason=reason)
    except ApprovalError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admissions.detail', request_id=request_id))
    flash('تم رفض الطلب.', 'warning')
    return redirect(url_for('admissions.detail', request_id=request_id))
