"""
Al-Muhandis – Sections & Subjects Blueprint
Handles: Grade/Section/Subject CRUD, teacher assignments
"""
from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request)
from flask_login import login_required
from app.models import (
    db, Grade, Section, Subject, AcademicYear, Employee, Student,
    Exam, Schedule, teacher_subjects,
)
from app.utils.decorators import (admin_required, get_current_school, get_active_year,
                                   historical_guard, module_required, action_required)

sections_bp = Blueprint('sections', __name__,
                          template_folder='../../templates/sections')


def _section_dependency_counts(section):
    """Return dependent row counts that make deleting a section unsafe."""
    teacher_assignments = (
        db.session.query(teacher_subjects)
        .filter(teacher_subjects.c.section_id == section.id)
        .count()
    )
    return {
        'students': Student.query.execution_options(include_all_years=True)
                           .filter_by(section_id=section.id).count(),
        'exams': Exam.query.execution_options(include_all_years=True)
                     .filter_by(section_id=section.id).count(),
        'schedules': Schedule.query.execution_options(include_all_years=True)
                             .filter_by(section_id=section.id).count(),
        'teacher_assignments': teacher_assignments,
    }


def _format_dependency_message(counts, item_name):
    blocking = [name for name, count in counts.items() if count]
    if not blocking:
        return None
    labels = {
        'sections': 'sections',
        'students': 'students',
        'exams': 'exams',
        'schedules': 'schedule entries',
        'teacher_assignments': 'teacher assignments',
    }
    return (
        f'Cannot delete {item_name} because it is still linked to '
        f'{", ".join(labels[name] for name in blocking)}.'
    )


# ─────────────────────────────────────────────────────────────────────────────
#  OVERVIEW
# ─────────────────────────────────────────────────────────────────────────────

@sections_bp.route('/')
@login_required
@admin_required
def index():
    school   = get_current_school()
    years_q  = AcademicYear.query
    if school:
        years_q = years_q.filter_by(school_id=school.id)
    years    = years_q.order_by(AcademicYear.start_date.desc()).all()
    year_id  = request.args.get('year_id', type=int)
    current_q = AcademicYear.query.filter_by(is_current=True)
    if school:
        current_q = current_q.filter_by(school_id=school.id)
    current  = current_q.first()
    if not year_id and current:
        year_id = current.id

    grades = []
    if year_id:
        grades = Grade.query.execution_options(include_all_years=True)\
            .filter_by(academic_year_id=year_id).all()

    return render_template('sections/index.html',
                           years=years, grades=grades,
                           year_id=year_id)


# ─────────────────────────────────────────────────────────────────────────────
#  GRADES
# ─────────────────────────────────────────────────────────────────────────────

@sections_bp.route('/grades/create', methods=['POST'])
@login_required
@historical_guard
@admin_required
def create_grade():
    name    = request.form.get('name', '').strip()
    stage   = request.form.get('stage', '').strip() or None
    year_id = request.form.get('academic_year_id', type=int)
    if name and year_id:
        year = AcademicYear.query.get_or_404(year_id)
        g = Grade(name=name, stage=stage, school_id=year.school_id, academic_year_id=year.id)
        db.session.add(g)
        db.session.commit()
        flash('تم إضافة الصف.', 'success')
    return redirect(url_for('sections.index', year_id=year_id))


@sections_bp.route('/grades/<int:grade_id>/edit', methods=['POST'])
@login_required
@historical_guard
@admin_required
def edit_grade(grade_id):
    grade   = Grade.query.execution_options(include_all_years=True).get_or_404(grade_id)
    year_id = grade.academic_year_id
    name    = request.form.get('name', '').strip()
    stage   = request.form.get('stage', '').strip() or None

    if not name:
        flash('اسم الصف مطلوب.', 'danger')
        return redirect(url_for('sections.index', year_id=year_id))

    duplicate = (Grade.query
                 .execution_options(include_all_years=True)
                 .filter(
                     Grade.school_id        == grade.school_id,
                     Grade.academic_year_id == grade.academic_year_id,
                     Grade.name             == name,
                     Grade.id               != grade_id,
                 ).first())
    if duplicate:
        flash('يوجد صف بنفس الاسم في هذا العام الدراسي.', 'danger')
        return redirect(url_for('sections.index', year_id=year_id))

    grade.name  = name
    grade.stage = stage
    db.session.commit()
    flash('تم تحديث الصف.', 'success')
    return redirect(url_for('sections.index', year_id=year_id))


@sections_bp.route('/grades/<int:grade_id>/delete', methods=['POST'])
@login_required
@historical_guard
@admin_required
def delete_grade(grade_id):
    grade = Grade.query.execution_options(include_all_years=True).get_or_404(grade_id)
    year_id = grade.academic_year_id
    if grade.sections.count():
        flash(
            'Cannot delete this class because it still has sections. '
            'Delete or move the sections first.',
            'warning',
        )
        return redirect(url_for('sections.index', year_id=year_id))

    db.session.delete(grade)
    db.session.commit()
    flash('تم حذف الصف.', 'success')
    return redirect(url_for('sections.index', year_id=year_id))


# ─────────────────────────────────────────────────────────────────────────────
#  SECTIONS
# ─────────────────────────────────────────────────────────────────────────────

@sections_bp.route('/grades/<int:grade_id>/sections/create', methods=['POST'])
@login_required
@historical_guard
@admin_required
def create_section(grade_id):
    grade      = Grade.query.execution_options(include_all_years=True).get_or_404(grade_id)
    name       = request.form.get('name', '').strip()
    capacity   = request.form.get('capacity', 30, type=int)
    teacher_id = request.form.get('teacher_id', type=int) or None
    if name:
        s = Section(name=name, grade_id=grade_id,
                    school_id=grade.school_id,
                    academic_year_id=grade.academic_year_id,
                    capacity=capacity, teacher_id=teacher_id)
        db.session.add(s)
        db.session.commit()
        flash('تم إضافة الشعبة.', 'success')
    return redirect(url_for('sections.index', year_id=grade.academic_year_id))


@sections_bp.route('/sections/<int:sec_id>/edit', methods=['GET', 'POST'])
@login_required
@historical_guard
@admin_required
def edit_section(sec_id):
    section   = Section.query.execution_options(include_all_years=True).get_or_404(sec_id)
    teachers  = Employee.query.filter_by(status='active').all()
    if request.method == 'POST':
        section.name       = request.form.get('name', section.name).strip()
        section.capacity   = request.form.get('capacity', section.capacity, type=int)
        section.teacher_id = request.form.get('teacher_id', type=int) or None
        db.session.commit()
        flash('تم تحديث الشعبة.', 'success')
        return redirect(url_for('sections.index',
                                year_id=section.grade.academic_year_id))
    return render_template('sections/edit_section.html',
                           section=section, teachers=teachers)


@sections_bp.route('/sections/<int:sec_id>/delete', methods=['POST'])
@login_required
@historical_guard
@admin_required
def delete_section(sec_id):
    section = Section.query.execution_options(include_all_years=True).get_or_404(sec_id)
    year_id = section.grade.academic_year_id
    counts = _section_dependency_counts(section)
    message = _format_dependency_message(counts, 'this section')
    if message:
        flash(message, 'warning')
        return redirect(url_for('sections.index', year_id=year_id))

    db.session.delete(section)
    db.session.commit()
    flash('تم حذف الشعبة.', 'success')
    return redirect(url_for('sections.index', year_id=year_id))


# ─────────────────────────────────────────────────────────────────────────────
#  SUBJECTS
# ─────────────────────────────────────────────────────────────────────────────

STAGES = ['ابتدائية', 'متوسطة', 'إعدادية']


@sections_bp.route('/subjects')
@login_required
@admin_required
@module_required('subjects')
def subjects():
    q        = request.args.get('q', '').strip()
    stage_f  = request.args.get('stage', '').strip()
    grade_f  = request.args.get('grade_id', type=int)

    query = Subject.query.order_by(Subject.name)
    if q:
        query = query.filter(
            db.or_(Subject.name.ilike(f'%{q}%'), Subject.code.ilike(f'%{q}%'))
        )
    if stage_f:
        query = (query
                 .outerjoin(Grade, Subject.grade_id == Grade.id)
                 .filter(db.or_(Subject.stage == stage_f, Grade.stage == stage_f)))
    if grade_f:
        query = query.filter(Subject.grade_id == grade_f)

    all_subjects = query.all()

    stage_order = ['ابتدائية', 'متوسطة', 'إعدادية']
    stage_map = {}
    for s in all_subjects:
        eff_stage = (s.grade.stage if s.grade and s.grade.stage else s.stage) or ''
        grade_name = s.grade.name if s.grade else ''
        stage_map.setdefault(eff_stage, {}).setdefault(grade_name, []).append(s)

    grouped_subjects = []
    for st in stage_order:
        if st in stage_map:
            grouped_subjects.append((st, sorted(stage_map[st].items())))
    for st in sorted(k for k in stage_map if k and k not in stage_order):
        grouped_subjects.append((st, sorted(stage_map[st].items())))
    if '' in stage_map:
        grouped_subjects.append(('', sorted(stage_map[''].items())))

    grades = Grade.query.order_by(Grade.name).all()
    return render_template('sections/subjects.html',
                           subjects=all_subjects, grades=grades,
                           grouped_subjects=grouped_subjects,
                           stages=STAGES, q=q, stage_f=stage_f, grade_f=grade_f)


@sections_bp.route('/subjects/create', methods=['POST'])
@login_required
@historical_guard
@admin_required
@module_required('subjects')
@action_required('subjects', 'create')
def create_subject():
    from app.utils.school_config import get_school_config
    school = get_current_school()
    year   = get_active_year(school.id) if school else None
    if not school or not year:
        flash('حدد مدرسة بعام دراسي نشط أولاً.', 'danger')
        return redirect(url_for('sections.subjects'))

    cfg = get_school_config(school.id)

    # ── name ─────────────────────────────────────────────────────────────────
    name = ''
    if cfg.field_visible('subjects', 'name'):
        name = request.form.get('name', '').strip()
        if not name:  # name is always logically required even if config says optional
            flash('اسم المادة مطلوب.', 'danger')
            return redirect(url_for('sections.subjects'))
    if not name:
        flash('اسم المادة مطلوب.', 'danger')
        return redirect(url_for('sections.subjects'))

    # ── code ──────────────────────────────────────────────────────────────────
    code = None
    if cfg.field_visible('subjects', 'code'):
        code = request.form.get('code', '').strip().upper() or None
        if cfg.field_required('subjects', 'code') and not code:
            flash('رمز المادة مطلوب.', 'danger')
            return redirect(url_for('sections.subjects'))
    if code and Subject.query.filter_by(code=code).first():
        flash('رمز المادة مستخدم بالفعل.', 'danger')
        return redirect(url_for('sections.subjects'))

    # ── stage ─────────────────────────────────────────────────────────────────
    stage = None
    if cfg.field_visible('subjects', 'stage'):
        stage = request.form.get('stage', '').strip() or None
        if cfg.field_required('subjects', 'stage') and not stage:
            flash('المرحلة مطلوبة.', 'danger')
            return redirect(url_for('sections.subjects'))

    # ── grade ─────────────────────────────────────────────────────────────────
    grade_id = None
    if cfg.field_visible('subjects', 'grade'):
        grade_id = request.form.get('grade_id', type=int) or None
        if cfg.field_required('subjects', 'grade') and not grade_id:
            flash('الصف مطلوب.', 'danger')
            return redirect(url_for('sections.subjects'))

    # ── marks ─────────────────────────────────────────────────────────────────
    total_marks = None
    if cfg.field_visible('subjects', 'total_marks'):
        total_marks = request.form.get('total_marks', type=float) or None
        if cfg.field_required('subjects', 'total_marks') and total_marks is None:
            flash('الدرجة الكلية مطلوبة.', 'danger')
            return redirect(url_for('sections.subjects'))

    pass_marks = None
    if cfg.field_visible('subjects', 'pass_marks'):
        pass_marks = request.form.get('pass_marks', type=float) or None
        if cfg.field_required('subjects', 'pass_marks') and pass_marks is None:
            flash('درجة النجاح مطلوبة.', 'danger')
            return redirect(url_for('sections.subjects'))

    # ── description ───────────────────────────────────────────────────────────
    desc = ''
    if cfg.field_visible('subjects', 'description'):
        desc = request.form.get('description', '').strip()
        if cfg.field_required('subjects', 'description') and not desc:
            flash('الوصف مطلوب.', 'danger')
            return redirect(url_for('sections.subjects'))

    s = Subject(name=name, code=code, description=desc,
                stage=stage, grade_id=grade_id,
                total_marks=total_marks, pass_marks=pass_marks,
                school_id=school.id, academic_year_id=year.id)
    db.session.add(s)
    db.session.commit()
    flash('تم إضافة المادة.', 'success')
    return redirect(url_for('sections.subjects'))


@sections_bp.route('/subjects/<int:sub_id>/edit', methods=['GET', 'POST'])
@login_required
@historical_guard
@admin_required
@module_required('subjects')
@action_required('subjects', 'edit')
def edit_subject(sub_id):
    from app.utils.school_config import get_school_config
    subject = Subject.query.get_or_404(sub_id)
    grades  = Grade.query.order_by(Grade.name).all()
    school  = get_current_school()
    cfg     = get_school_config(school.id if school else None)

    if request.method == 'POST':
        # ── name ─────────────────────────────────────────────────────────────
        if cfg.field_visible('subjects', 'name'):
            name_val = request.form.get('name', '').strip()
            if not name_val and cfg.field_required('subjects', 'name'):
                flash('اسم المادة مطلوب.', 'danger')
                return render_template('sections/edit_subject.html',
                                       subject=subject, grades=grades,
                                       stages=STAGES, cfg=cfg)
            subject.name = name_val or subject.name
        else:
            # hidden field sends a hidden input preserving existing value
            subject.name = request.form.get('name', subject.name).strip() or subject.name

        # ── stage ─────────────────────────────────────────────────────────────
        if cfg.field_visible('subjects', 'stage'):
            stage_val = request.form.get('stage', '').strip() or None
            if cfg.field_required('subjects', 'stage') and not stage_val:
                flash('المرحلة مطلوبة.', 'danger')
                return render_template('sections/edit_subject.html',
                                       subject=subject, grades=grades,
                                       stages=STAGES, cfg=cfg)
            subject.stage = stage_val
        # hidden → preserve existing (no form input sent, so no change needed)

        # ── grade ─────────────────────────────────────────────────────────────
        if cfg.field_visible('subjects', 'grade'):
            grade_val = request.form.get('grade_id', type=int) or None
            if cfg.field_required('subjects', 'grade') and not grade_val:
                flash('الصف مطلوب.', 'danger')
                return render_template('sections/edit_subject.html',
                                       subject=subject, grades=grades,
                                       stages=STAGES, cfg=cfg)
            subject.grade_id = grade_val
        # hidden → preserve existing

        # ── marks ─────────────────────────────────────────────────────────────
        if cfg.field_visible('subjects', 'total_marks'):
            tm = request.form.get('total_marks', type=float) or None
            if cfg.field_required('subjects', 'total_marks') and tm is None:
                flash('الدرجة الكلية مطلوبة.', 'danger')
                return render_template('sections/edit_subject.html',
                                       subject=subject, grades=grades,
                                       stages=STAGES, cfg=cfg)
            subject.total_marks = tm
        # hidden → preserve existing

        if cfg.field_visible('subjects', 'pass_marks'):
            pm = request.form.get('pass_marks', type=float) or None
            if cfg.field_required('subjects', 'pass_marks') and pm is None:
                flash('درجة النجاح مطلوبة.', 'danger')
                return render_template('sections/edit_subject.html',
                                       subject=subject, grades=grades,
                                       stages=STAGES, cfg=cfg)
            subject.pass_marks = pm
        # hidden → preserve existing

        # ── description ───────────────────────────────────────────────────────
        if cfg.field_visible('subjects', 'description'):
            desc_val = request.form.get('description', '').strip()
            if cfg.field_required('subjects', 'description') and not desc_val:
                flash('الوصف مطلوب.', 'danger')
                return render_template('sections/edit_subject.html',
                                       subject=subject, grades=grades,
                                       stages=STAGES, cfg=cfg)
            subject.description = desc_val
        # hidden → preserve existing

        db.session.commit()
        flash('تم تحديث المادة.', 'success')
        return redirect(url_for('sections.subjects'))

    return render_template('sections/edit_subject.html',
                           subject=subject, grades=grades, stages=STAGES, cfg=cfg)


@sections_bp.route('/subjects/<int:sub_id>/delete', methods=['POST'])
@login_required
@historical_guard
@admin_required
@module_required('subjects')
@action_required('subjects', 'delete')
def delete_subject(sub_id):
    subject = Subject.query.get_or_404(sub_id)
    db.session.delete(subject)
    db.session.commit()
    flash('تم حذف المادة.', 'success')
    return redirect(url_for('sections.subjects'))
