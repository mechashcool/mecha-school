"""
Mecha-School — Buildings Blueprint  (optional per-school building/branch mgmt)

Only usable when the current school has School.enable_buildings=True.  Lets a
school admin (or super admin with an active school) manage the buildings/branches
inside their school.  Building-level user access is assigned from the user edit
page, not here.

Routes:
  GET  /buildings/                      — list buildings
  GET/POST /buildings/new               — create a building
  GET/POST /buildings/<id>/edit         — edit a building
  POST /buildings/<id>/delete           — delete (blocked if students assigned)
  POST /buildings/<id>/toggle-active    — activate / deactivate
"""
from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, abort)
from flask_login import login_required, current_user

from app.models import db, SchoolBuilding, Student
from app.utils.decorators import (admin_required, permission_required,
                                   get_current_school)
from app.utils.buildings import school_buildings_enabled
from app.utils.audit import log_action

buildings_bp = Blueprint('buildings', __name__,
                         template_folder='../../templates/buildings')


def _require_buildings_school():
    """Return the current school if it has the buildings feature enabled.

    Otherwise flash + return None so the caller can redirect.  Super admin must
    have an active school selected.
    """
    school = get_current_school()
    if not school:
        flash('يرجى اختيار مدرسة أولاً.', 'warning')
        return None
    if not school_buildings_enabled(school):
        flash('نظام البنايات غير مفعّل لهذه المدرسة.', 'warning')
        return None
    return school


def _get_building_or_404(building_id, school):
    building = (SchoolBuilding.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(id=building_id, school_id=school.id)
                .first())
    if building is None:
        abort(404)
    return building


@buildings_bp.route('/')
@login_required
@permission_required('manage_buildings')
def index():
    school = _require_buildings_school()
    if not school:
        return redirect(url_for('admin.dashboard'))

    buildings = (SchoolBuilding.query
                 .execution_options(bypass_tenant_scope=True)
                 .filter_by(school_id=school.id)
                 .order_by(SchoolBuilding.name)
                 .all())

    # Student counts per building (active students only) for the list view.
    counts = dict(
        db.session.query(Student.building_id, db.func.count(Student.id))
        .execution_options(bypass_tenant_scope=True, include_all_years=True)
        .filter(Student.school_id == school.id,
                Student.building_id.isnot(None))
        .group_by(Student.building_id)
        .all()
    )

    return render_template('buildings/index.html',
                           buildings=buildings, student_counts=counts)


@buildings_bp.route('/new', methods=['GET', 'POST'])
@login_required
@permission_required('manage_buildings')
def new():
    school = _require_buildings_school()
    if not school:
        return redirect(url_for('admin.dashboard'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip() or None
        is_active = request.form.get('is_active', '1') == '1'

        if not name:
            flash('اسم البناية مطلوب.', 'danger')
            return render_template('buildings/form.html', building=None,
                                   form_name=name, form_description=description)

        # Prevent duplicate names within the same school.
        existing = (SchoolBuilding.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(school_id=school.id, name=name)
                    .first())
        if existing:
            flash('يوجد بناية بنفس الاسم في هذه المدرسة.', 'danger')
            return render_template('buildings/form.html', building=None,
                                   form_name=name, form_description=description)

        building = SchoolBuilding(
            school_id=school.id,
            name=name,
            description=description,
            is_active=is_active,
        )
        db.session.add(building)
        db.session.commit()
        log_action('create', 'school_building', building.id,
                   details=f'created building "{name}"')
        flash(f'تم إنشاء البناية "{name}" بنجاح.', 'success')
        return redirect(url_for('buildings.index'))

    return render_template('buildings/form.html', building=None)


@buildings_bp.route('/<int:building_id>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('manage_buildings')
def edit(building_id):
    school = _require_buildings_school()
    if not school:
        return redirect(url_for('admin.dashboard'))
    building = _get_building_or_404(building_id, school)

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip() or None
        is_active = request.form.get('is_active', '1') == '1'

        if not name:
            flash('اسم البناية مطلوب.', 'danger')
            return render_template('buildings/form.html', building=building)

        duplicate = (SchoolBuilding.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter(SchoolBuilding.school_id == school.id,
                             SchoolBuilding.name == name,
                             SchoolBuilding.id != building.id)
                     .first())
        if duplicate:
            flash('يوجد بناية أخرى بنفس الاسم في هذه المدرسة.', 'danger')
            return render_template('buildings/form.html', building=building)

        building.name = name
        building.description = description
        building.is_active = is_active
        db.session.commit()
        log_action('edit', 'school_building', building.id,
                   details=f'updated building "{name}"')
        flash('تم تحديث بيانات البناية بنجاح.', 'success')
        return redirect(url_for('buildings.index'))

    return render_template('buildings/form.html', building=building)


@buildings_bp.route('/<int:building_id>/toggle-active', methods=['POST'])
@login_required
@permission_required('manage_buildings')
def toggle_active(building_id):
    school = _require_buildings_school()
    if not school:
        return redirect(url_for('admin.dashboard'))
    building = _get_building_or_404(building_id, school)

    building.is_active = not building.is_active
    db.session.commit()
    state = 'تفعيل' if building.is_active else 'تعطيل'
    log_action('edit', 'school_building', building.id,
               details=f'{state} building "{building.name}"')
    flash(f'تم {state} البناية "{building.name}".', 'success')
    return redirect(url_for('buildings.index'))


@buildings_bp.route('/<int:building_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_buildings')
def delete(building_id):
    school = _require_buildings_school()
    if not school:
        return redirect(url_for('admin.dashboard'))
    building = _get_building_or_404(building_id, school)

    # Block delete if any student (any year) is still assigned to this building,
    # to avoid orphaning data. Reassign / deactivate instead.
    assigned = (Student.query
                .execution_options(bypass_tenant_scope=True, include_all_years=True)
                .filter_by(school_id=school.id, building_id=building.id)
                .count())
    if assigned:
        flash(
            f'لا يمكن حذف البناية "{building.name}" لوجود {assigned} طالب مرتبط بها. '
            'انقل الطلاب إلى بناية أخرى أو قم بتعطيل البناية بدلاً من حذفها.',
            'danger',
        )
        return redirect(url_for('buildings.index'))

    name = building.name
    # UserBuildingAccess rows referencing this building are removed by DB cascade.
    db.session.delete(building)
    db.session.commit()
    log_action('delete', 'school_building', building_id,
               details=f'deleted building "{name}"')
    flash(f'تم حذف البناية "{name}".', 'success')
    return redirect(url_for('buildings.index'))
