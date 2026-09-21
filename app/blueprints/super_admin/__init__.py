"""
Mecha-School — Super Admin Portal Blueprint
============================================
Routes exclusively for super_admin (system owner).
URL prefix: /admin/super

Routes:
  GET  /admin/super/                              — Dashboard: governorate cards
  GET  /admin/super/governorates/<name>           — All schools in a governorate
  GET  /admin/super/schools/<id>                  — School detail & management
  POST /admin/super/schools/<id>/toggle           — Activate / deactivate school
  POST /admin/super/schools/<id>/suspend-users    — Suspend all users of school
  POST /admin/super/schools/<id>/reactivate-users — Reactivate all users of school
  POST /admin/super/schools/<id>/billing/add      — Add a billing record
  POST /admin/super/billing/<billing_id>/pay      — Record a payment on a billing record
  POST /admin/super/billing/<billing_id>/delete   — Delete a billing record
  GET  /admin/super/billing                       — Global billing overview
  GET  /admin/super/recycle-bin                   — سلة المحذوفات: soft-deleted
                                                    student documents of the
                                                    selected school
  POST /admin/super/recycle-bin/documents/<id>/restore — Restore one of them
"""
from datetime import date as _date, datetime as _dt
from decimal import Decimal, InvalidOperation

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, session)
from flask_login import login_required, current_user
from sqlalchemy import func

from app.models import (db, School, SchoolBilling, AcademicYear,
                         Student, Employee, User, FeeRecord,
                         FeeInstallment, Revenue, Expense,
                         Grade, Section, Subject, FeeType,
                         Role, MobileDeviceToken, INVESTOR_ROLE)
from app.utils.decorators import (super_admin_required,
                                  historical_guard)
from app.utils.audit import log_action
from app.utils import code_generator

super_admin_bp = Blueprint(
    'super_admin', __name__,
    template_folder='../../templates/super_admin'
)


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _school_stats(school):
    """Return a dict of per-school statistics used across multiple views."""
    student_count = Student.query.execution_options(
        bypass_tenant_scope=True, include_all_years=True
    ).filter_by(school_id=school.id, status='active').count()

    user_count = User.query.execution_options(bypass_tenant_scope=True)\
        .filter(User.school_id == school.id, User.is_active.is_(True)).count()

    current_year = AcademicYear.query.execution_options(bypass_tenant_scope=True)\
        .filter_by(school_id=school.id, is_current=True).first()

    # School-level billing totals
    billing_totals = db.session.query(
        func.coalesce(func.sum(SchoolBilling.amount_due),  Decimal('0')),
        func.coalesce(func.sum(SchoolBilling.amount_paid), Decimal('0')),
    ).filter_by(school_id=school.id).one()
    total_due  = billing_totals[0]
    total_paid = billing_totals[1]
    remaining  = total_due - total_paid

    # Expected amount from price_per_student × active students
    price = school.price_per_student or Decimal('0')
    expected = price * student_count

    return {
        'school':        school,
        'students':      student_count,
        'users':         user_count,
        'current_year':  current_year,
        'total_due':     total_due,
        'total_paid':    total_paid,
        'remaining':     remaining,
        'expected':      expected,
        'collection_pct': (
            int(total_paid / total_due * 100)
            if total_due and total_due > 0 else 0
        ),
    }


def _all_active_schools():
    return School.query.execution_options(bypass_tenant_scope=True)\
        .order_by(School.governorate.nullslast(), School.school_name).all()


# ─────────────────────────────────────────────────────────────────────────────
#  SUPER ADMIN DASHBOARD  — governorate cards
# ─────────────────────────────────────────────────────────────────────────────

@super_admin_bp.route('/')
@login_required
@super_admin_required
def dashboard():
    schools = _all_active_schools()

    # Build per-governorate aggregates
    gov_map = {}
    for school in schools:
        gov = school.governorate or 'غير محدد'
        if gov not in gov_map:
            gov_map[gov] = {
                'name':            gov,
                'total_schools':   0,
                'active_schools':  0,
                'inactive_schools':0,
                'students':        0,
                'users':           0,
                'total_paid':      Decimal('0'),
                'total_remaining': Decimal('0'),
                'total_expected':  Decimal('0'),
            }
        stats = _school_stats(school)
        g = gov_map[gov]
        g['total_schools']    += 1
        if school.is_active:
            g['active_schools'] += 1
        else:
            g['inactive_schools'] += 1
        g['students']        += stats['students']
        g['users']           += stats['users']
        g['total_paid']      += stats['total_paid']
        g['total_remaining'] += stats['remaining']
        g['total_expected']  += stats['expected']

    # Collection % per governorate
    for g in gov_map.values():
        due = g['total_paid'] + g['total_remaining']
        g['collection_pct'] = int(g['total_paid'] / due * 100) if due > 0 else 0

    governorates = sorted(gov_map.values(), key=lambda x: x['name'])

    # System-wide totals
    total_schools   = len(schools)
    total_students  = sum(v['students'] for v in gov_map.values())
    total_users     = sum(v['users']    for v in gov_map.values())
    total_paid      = sum(v['total_paid']      for v in gov_map.values())
    total_remaining = sum(v['total_remaining'] for v in gov_map.values())

    return render_template(
        'super_admin/dashboard.html',
        governorates    = governorates,
        total_schools   = total_schools,
        total_students  = total_students,
        total_users     = total_users,
        total_paid      = total_paid,
        total_remaining = total_remaining,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  GOVERNORATE DETAIL  — list schools in one governorate
# ─────────────────────────────────────────────────────────────────────────────

@super_admin_bp.route('/governorates/<path:gov_name>')
@login_required
@super_admin_required
def governorate_detail(gov_name):
    # Handle the 'غير محدد' (unclassified) sentinel
    if gov_name == 'غير محدد':
        schools = School.query.execution_options(bypass_tenant_scope=True)\
            .filter(School.governorate.is_(None)).order_by(School.school_name).all()
    else:
        schools = School.query.execution_options(bypass_tenant_scope=True)\
            .filter_by(governorate=gov_name).order_by(School.school_name).all()

    school_rows = [_school_stats(s) for s in schools]

    gov_totals = {
        'students':   sum(r['students']   for r in school_rows),
        'users':      sum(r['users']      for r in school_rows),
        'total_paid': sum(r['total_paid'] for r in school_rows),
        'remaining':  sum(r['remaining']  for r in school_rows),
    }

    return render_template(
        'super_admin/governorate.html',
        gov_name    = gov_name,
        school_rows = school_rows,
        gov_totals  = gov_totals,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  SCHOOL DETAIL  — statistics + billing for one school
# ─────────────────────────────────────────────────────────────────────────────

@super_admin_bp.route('/schools/<int:school_id>')
@login_required
@super_admin_required
def school_detail(school_id):
    school = School.query.get_or_404(school_id)
    stats  = _school_stats(school)

    billing_records = SchoolBilling.query\
        .filter_by(school_id=school_id)\
        .order_by(SchoolBilling.created_at.desc()).all()

    users = User.query.execution_options(bypass_tenant_scope=True)\
        .filter(User.school_id == school_id,
                User.username.notlike('~deleted~%'))\
        .order_by(User.full_name).all()

    all_years = AcademicYear.query.execution_options(bypass_tenant_scope=True)\
        .filter_by(school_id=school_id)\
        .order_by(AcademicYear.start_date.desc()).all()

    investor = _get_school_investor(school_id)

    # Custom (non built-in) roles the Super Admin can toggle for this school.
    custom_roles = [r for r in Role.query.order_by(Role.label).all()
                    if not r.is_builtin]
    assigned_role_ids = {r.id for r in school.custom_roles}

    return render_template(
        'super_admin/school_detail.html',
        school            = school,
        stats             = stats,
        billing_records   = billing_records,
        users             = users,
        all_years         = all_years,
        investor          = investor,
        billing_types     = SchoolBilling.BILLING_TYPES,
        today             = _date.today(),
        registration_link = _registration_link(school),
        custom_roles      = custom_roles,
        assigned_role_ids = assigned_role_ids,
    )


@super_admin_bp.route('/schools/<int:school_id>/custom-roles', methods=['POST'])
@login_required
@super_admin_required
def update_custom_roles(school_id):
    """Set exactly which custom roles are available to this school.

    Only custom (non built-in) roles are accepted; any built-in role id posted
    is ignored, so this endpoint can never change built-in role behaviour.
    Toggling a role for one school never touches its assignment to any other
    school and never alters the role's permissions or configuration.
    """
    school = School.query.get_or_404(school_id)

    posted_ids = set(request.form.getlist('role_ids', type=int))
    # Restrict to genuine custom roles — a built-in role id posted by a tampered
    # form is silently dropped (built-in roles are never stored in role_schools).
    selected = [r for r in Role.query.filter(Role.id.in_(posted_ids)).all()
                if posted_ids and not r.is_builtin] if posted_ids else []

    school.custom_roles = selected
    db.session.commit()

    log_action('edit', 'school', school.id,
               details=(f'تحديث الأدوار المخصصة لمدرسة "{school.school_name}" '
                        f'({len(selected)} دور)'))
    flash('تم تحديث الأدوار المخصصة لهذه المدرسة.', 'success')
    return redirect(url_for('super_admin.school_detail', school_id=school_id)
                    + '#custom-roles')


# ─────────────────────────────────────────────────────────────────────────────
#  EXTERNAL (PUBLIC) STUDENT-REGISTRATION LINK
#  Only the super admin may enable/disable/regenerate/view the link. The raw
#  token is stored ONLY as a sha256 hash (lookup) + Fernet ciphertext (recovery)
#  and is never written to logs or audit details.
# ─────────────────────────────────────────────────────────────────────────────

def _registration_link(school):
    """Decrypt and return the school's active public registration URL, or None.

    Returns None when the feature has never been set up or the stored token can
    no longer be decrypted (e.g. SECRET_KEY-derived key rotated) — in which case
    the super admin must regenerate to obtain a fresh copyable link.
    """
    if not school.registration_token_hash or not school.registration_token_encrypted:
        return None
    from app.utils.registration_tokens import decrypt_token
    raw = decrypt_token(school.registration_token_encrypted)
    if not raw:
        return None
    return url_for('registration.form', token=raw, _external=True)


def _issue_registration_token(school):
    """Generate a fresh token and persist its hash + encrypted copy on the school.
    Overwrites any previous token, invalidating the previous link immediately."""
    from app.utils.registration_tokens import (generate_token, hash_token,
                                                encrypt_token)
    raw = generate_token()
    school.registration_token_hash      = hash_token(raw)
    school.registration_token_encrypted = encrypt_token(raw)
    school.registration_token_created_at = _dt.utcnow()


@super_admin_bp.route('/schools/<int:school_id>/registration/enable', methods=['POST'])
@login_required
@super_admin_required
def enable_registration(school_id):
    school = School.query.get_or_404(school_id)
    school.external_registration_enabled = True
    # Generate a link on first enable; keep the existing one when re-enabling.
    if not school.registration_token_hash:
        _issue_registration_token(school)
    db.session.commit()
    log_action('edit', 'school', school.id,
               details=f'تفعيل رابط التسجيل الخارجي لمدرسة "{school.school_name}"')
    flash('تم تفعيل رابط التسجيل الخارجي.', 'success')
    return redirect(url_for('super_admin.school_detail', school_id=school_id))


@super_admin_bp.route('/schools/<int:school_id>/registration/disable', methods=['POST'])
@login_required
@super_admin_required
def disable_registration(school_id):
    school = School.query.get_or_404(school_id)
    school.external_registration_enabled = False
    db.session.commit()
    log_action('edit', 'school', school.id,
               details=f'تعطيل رابط التسجيل الخارجي لمدرسة "{school.school_name}"')
    flash('تم تعطيل رابط التسجيل الخارجي.', 'warning')
    return redirect(url_for('super_admin.school_detail', school_id=school_id))


@super_admin_bp.route('/schools/<int:school_id>/registration/regenerate', methods=['POST'])
@login_required
@super_admin_required
def regenerate_registration(school_id):
    school = School.query.get_or_404(school_id)
    _issue_registration_token(school)
    # Regenerating implies the feature is in use — keep it enabled.
    school.external_registration_enabled = True
    db.session.commit()
    log_action('edit', 'school', school.id,
               details=f'إعادة توليد رابط التسجيل الخارجي لمدرسة "{school.school_name}" '
                       '(تم إبطال الرابط السابق)')
    flash('تم إنشاء رابط تسجيل جديد وإبطال الرابط السابق فوراً.', 'success')
    return redirect(url_for('super_admin.school_detail', school_id=school_id))


# ─────────────────────────────────────────────────────────────────────────────
#  INVESTOR ACCOUNT  — per-school, read-only, managed only by super admin
# ─────────────────────────────────────────────────────────────────────────────

def _get_school_investor(school_id):
    """Return the (single) active investor account for a school, or None.

    Uses bypass_tenant_scope because the super admin operates globally; the
    query is still explicitly constrained to this school_id and the investor
    role, and excludes soft-deleted accounts.
    """
    return (User.query
            .execution_options(bypass_tenant_scope=True)
            .join(Role, User.role_id == Role.id)
            .filter(User.school_id == school_id,
                    Role.name == INVESTOR_ROLE,
                    User.username.notlike('~deleted~%'))
            .order_by(User.id)
            .first())


def _investor_role():
    """Return the investor_viewer Role, creating it if the seed has not run yet."""
    role = Role.query.filter_by(name=INVESTOR_ROLE).first()
    if role is None:
        role = Role(
            name=INVESTOR_ROLE,
            label='مستثمر - عرض فقط',
            description='حساب مستثمر للقراءة فقط — يرى لوحة التحكم والإيرادات والمصروفات لمدرسته فقط',
            is_admin=False,
        )
        db.session.add(role)
        db.session.flush()
    return role


@super_admin_bp.route('/schools/<int:school_id>/investor/create', methods=['POST'])
@login_required
@super_admin_required
def create_investor(school_id):
    school = School.query.get_or_404(school_id)

    # One active investor per school.
    if _get_school_investor(school_id):
        flash('يوجد حساب مستثمر لهذه المدرسة بالفعل.', 'warning')
        return redirect(url_for('super_admin.school_detail', school_id=school_id))

    full_name = request.form.get('full_name', '').strip()
    username  = request.form.get('username', '').strip()
    email     = request.form.get('email', '').strip()
    phone     = request.form.get('phone', '').strip()
    password  = request.form.get('password', '')

    errors = []
    if not full_name:
        errors.append('الاسم الكامل مطلوب.')
    if not password or len(password) < 6:
        errors.append('كلمة المرور يجب أن تكون 6 أحرف على الأقل.')
    if email and '@' not in email:
        errors.append('البريد الإلكتروني غير صالح.')

    # Auto-generate a school-scoped username when left blank.
    if not username and not errors:
        username = code_generator.generate_username(school_id, INVESTOR_ROLE)

    if username and User.query.execution_options(bypass_tenant_scope=True)\
            .filter_by(username=username).first():
        errors.append('اسم المستخدم مستخدم بالفعل.')
    if email and User.query.execution_options(bypass_tenant_scope=True)\
            .filter_by(email=email).first():
        errors.append('البريد الإلكتروني مستخدم بالفعل.')

    if errors:
        for e in errors:
            flash(e, 'danger')
        return redirect(url_for('super_admin.school_detail', school_id=school_id))

    # school_id is bound to the URL path (the school being managed) — never taken
    # from any client field — so the account is authoritatively tied to this school.
    user = User(
        username  = username,
        email     = email or None,
        full_name = full_name,
        phone     = phone or None,
        role      = _investor_role(),
        school_id = school_id,
        is_active = True,
    )
    user.set_password(password)
    db.session.add(user)

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        flash('تعذر إنشاء حساب المستثمر بسبب تعارض في القيم. حاول مرة أخرى.', 'danger')
        return redirect(url_for('super_admin.school_detail', school_id=school_id))

    log_action('create', 'user', user.id,
               details=f'created investor account for school={school_id}')
    flash(f'تم إنشاء حساب المستثمر. اسم المستخدم: {username}', 'success')
    return redirect(url_for('super_admin.school_detail', school_id=school_id))


@super_admin_bp.route('/schools/<int:school_id>/investor/update', methods=['POST'])
@login_required
@super_admin_required
def update_investor(school_id):
    School.query.get_or_404(school_id)
    investor = _get_school_investor(school_id)
    if not investor:
        flash('لا يوجد حساب مستثمر لهذه المدرسة.', 'danger')
        return redirect(url_for('super_admin.school_detail', school_id=school_id))

    full_name = request.form.get('full_name', '').strip()
    username  = request.form.get('username', '').strip()
    email     = request.form.get('email', '').strip()
    phone     = request.form.get('phone', '').strip()

    if not full_name:
        flash('الاسم الكامل مطلوب.', 'danger')
        return redirect(url_for('super_admin.school_detail', school_id=school_id))

    if username and username != investor.username:
        if username.startswith('~deleted~'):
            flash('اسم المستخدم غير صالح.', 'danger')
            return redirect(url_for('super_admin.school_detail', school_id=school_id))
        clash = (User.query.execution_options(bypass_tenant_scope=True)
                 .filter(User.username == username, User.id != investor.id).first())
        if clash:
            flash('اسم المستخدم مستخدم بالفعل.', 'danger')
            return redirect(url_for('super_admin.school_detail', school_id=school_id))
        investor.username = username

    if email and '@' not in email:
        flash('البريد الإلكتروني غير صالح.', 'danger')
        return redirect(url_for('super_admin.school_detail', school_id=school_id))
    if email:
        clash = (User.query.execution_options(bypass_tenant_scope=True)
                 .filter(User.email == email, User.id != investor.id).first())
        if clash:
            flash('البريد الإلكتروني مستخدم بالفعل.', 'danger')
            return redirect(url_for('super_admin.school_detail', school_id=school_id))

    investor.full_name = full_name
    investor.email     = email or None
    investor.phone     = phone or None
    db.session.commit()
    log_action('edit', 'user', investor.id,
               details=f'updated investor account for school={school_id}')
    flash('تم تحديث بيانات حساب المستثمر.', 'success')
    return redirect(url_for('super_admin.school_detail', school_id=school_id))


@super_admin_bp.route('/schools/<int:school_id>/investor/reset-password', methods=['POST'])
@login_required
@super_admin_required
def reset_investor_password(school_id):
    School.query.get_or_404(school_id)
    investor = _get_school_investor(school_id)
    if not investor:
        flash('لا يوجد حساب مستثمر لهذه المدرسة.', 'danger')
        return redirect(url_for('super_admin.school_detail', school_id=school_id))

    password = request.form.get('password', '')
    if not password or len(password) < 6:
        flash('كلمة المرور يجب أن تكون 6 أحرف على الأقل.', 'danger')
        return redirect(url_for('super_admin.school_detail', school_id=school_id))

    investor.set_password(password)
    db.session.commit()
    log_action('edit', 'user', investor.id,
               details=f'reset investor password for school={school_id}')
    flash('تم تغيير كلمة مرور المستثمر.', 'success')
    return redirect(url_for('super_admin.school_detail', school_id=school_id))


@super_admin_bp.route('/schools/<int:school_id>/investor/toggle', methods=['POST'])
@login_required
@super_admin_required
def toggle_investor(school_id):
    School.query.get_or_404(school_id)
    investor = _get_school_investor(school_id)
    if not investor:
        flash('لا يوجد حساب مستثمر لهذه المدرسة.', 'danger')
        return redirect(url_for('super_admin.school_detail', school_id=school_id))

    investor.is_active = not investor.is_active
    db.session.commit()
    state = 'تفعيل' if investor.is_active else 'تعطيل'
    log_action('edit', 'user', investor.id,
               details=f'{state} investor account for school={school_id}')
    flash(f'تم {state} حساب المستثمر.', 'success')
    return redirect(url_for('super_admin.school_detail', school_id=school_id))


@super_admin_bp.route('/schools/<int:school_id>/investor/delete', methods=['POST'])
@login_required
@super_admin_required
def delete_investor(school_id):
    School.query.get_or_404(school_id)
    investor = _get_school_investor(school_id)
    if not investor:
        flash('لا يوجد حساب مستثمر لهذه المدرسة.', 'danger')
        return redirect(url_for('super_admin.school_detail', school_id=school_id))

    # Soft-delete: mirror admin.delete_user so login is fully disabled and the
    # username/email slots are freed, while the row is kept for audit integrity.
    investor.is_active     = False
    investor.password_hash = '!'
    investor.device_token  = None
    investor.email         = None
    investor.username      = f'~deleted~{investor.id}'
    investor.extra_permissions.clear()
    MobileDeviceToken.query.filter_by(user_id=investor.id)\
        .delete(synchronize_session=False)
    db.session.commit()
    log_action('delete', 'user', investor.id,
               details=f'deleted investor account for school={school_id}')
    flash('تم حذف حساب دخول المستثمر.', 'success')
    return redirect(url_for('super_admin.school_detail', school_id=school_id))


# ─────────────────────────────────────────────────────────────────────────────
#  TOGGLE SCHOOL ACTIVE / INACTIVE
# ─────────────────────────────────────────────────────────────────────────────

@super_admin_bp.route('/schools/<int:school_id>/toggle', methods=['POST'])
@login_required
@super_admin_required
def toggle_school(school_id):
    school = School.query.get_or_404(school_id)
    school.is_active = not school.is_active
    action_label = 'تفعيل' if school.is_active else 'تعطيل'

    db.session.commit()
    log_action('edit', 'school', school.id,
               details=f'{action_label} المدرسة "{school.school_name}"')
    flash(f'تم {action_label} المدرسة "{school.school_name}".', 'success')
    return redirect(url_for('super_admin.school_detail', school_id=school_id))


# ─────────────────────────────────────────────────────────────────────────────
#  SUSPEND ALL USERS OF A SCHOOL
# ─────────────────────────────────────────────────────────────────────────────

@super_admin_bp.route('/schools/<int:school_id>/suspend-users', methods=['POST'])
@login_required
@super_admin_required
def suspend_school_users(school_id):
    school = School.query.get_or_404(school_id)
    count = User.query.execution_options(bypass_tenant_scope=True)\
        .filter(User.school_id == school_id, User.is_active.is_(True))\
        .update({'is_active': False}, synchronize_session=False)
    db.session.commit()
    log_action('edit', 'school', school_id,
               details=f'تعليق {count} مستخدم في مدرسة "{school.school_name}"')
    flash(f'تم تعليق {count} مستخدم في المدرسة "{school.school_name}".', 'warning')
    return redirect(url_for('super_admin.school_detail', school_id=school_id))


# ─────────────────────────────────────────────────────────────────────────────
#  REACTIVATE ALL USERS OF A SCHOOL
# ─────────────────────────────────────────────────────────────────────────────

@super_admin_bp.route('/schools/<int:school_id>/reactivate-users', methods=['POST'])
@login_required
@super_admin_required
def reactivate_school_users(school_id):
    school = School.query.get_or_404(school_id)
    count = User.query.execution_options(bypass_tenant_scope=True)\
        .filter(User.school_id == school_id, User.is_active.is_(False))\
        .update({'is_active': True}, synchronize_session=False)
    db.session.commit()
    log_action('edit', 'school', school_id,
               details=f'إعادة تفعيل {count} مستخدم في مدرسة "{school.school_name}"')
    flash(f'تم إعادة تفعيل {count} مستخدم في المدرسة "{school.school_name}".', 'success')
    return redirect(url_for('super_admin.school_detail', school_id=school_id))


# ─────────────────────────────────────────────────────────────────────────────
#  UPDATE SCHOOL CAPACITY / PRICE PER STUDENT
# ─────────────────────────────────────────────────────────────────────────────

@super_admin_bp.route('/schools/<int:school_id>/update-pricing', methods=['POST'])
@login_required
@super_admin_required
def update_school_pricing(school_id):
    school = School.query.get_or_404(school_id)

    try:
        cap = int(request.form.get('capacity', '0') or '0')
    except ValueError:
        cap = 0

    try:
        pps = Decimal(request.form.get('price_per_student', '0') or '0')
    except InvalidOperation:
        pps = Decimal('0')

    school.capacity          = cap
    school.price_per_student = pps
    db.session.commit()
    log_action('edit', 'school', school_id,
               details=f'تحديث السعة={cap} وسعر الطالب={pps}')
    flash('تم تحديث بيانات السعة والسعر.', 'success')
    return redirect(url_for('super_admin.school_detail', school_id=school_id))


# ─────────────────────────────────────────────────────────────────────────────
#  ACADEMIC YEAR  — create / activate from super admin panel
# ─────────────────────────────────────────────────────────────────────────────

@super_admin_bp.route('/schools/<int:school_id>/years/create', methods=['POST'])
@login_required
@super_admin_required
def create_year(school_id):
    School.query.get_or_404(school_id)
    name       = request.form.get('year_name', '').strip()
    start_raw  = request.form.get('year_start')
    end_raw    = request.form.get('year_end')
    is_current = bool(request.form.get('is_current'))

    if not name or not start_raw or not end_raw:
        flash('جميع حقول العام الدراسي مطلوبة.', 'danger')
        return redirect(url_for('super_admin.school_detail', school_id=school_id))

    try:
        start_date = _dt.strptime(start_raw, '%Y-%m-%d').date()
        end_date   = _dt.strptime(end_raw,   '%Y-%m-%d').date()
    except ValueError:
        flash('تنسيق التاريخ غير صالح. استخدم صيغة YYYY-MM-DD.', 'danger')
        return redirect(url_for('super_admin.school_detail', school_id=school_id))

    if start_date >= end_date:
        flash('تاريخ البداية يجب أن يكون قبل تاريخ النهاية.', 'danger')
        return redirect(url_for('super_admin.school_detail', school_id=school_id))

    existing = AcademicYear.query.execution_options(bypass_tenant_scope=True)\
        .filter_by(school_id=school_id, name=name).first()
    if existing:
        flash(f'يوجد عام دراسي بالاسم "{name}" مسبقاً.', 'danger')
        return redirect(url_for('super_admin.school_detail', school_id=school_id))

    if is_current:
        AcademicYear.query.execution_options(bypass_tenant_scope=True)\
            .filter_by(school_id=school_id).update({'is_current': False})

    try:
        ay = AcademicYear(
            school_id  = school_id,
            name       = name,
            start_date = start_date,
            end_date   = end_date,
            is_current = is_current,
        )
        db.session.add(ay)
        db.session.commit()
        log_action('create', 'academic_year', ay.id,
                   details=f'school={school_id}, year="{name}"')
        flash(f'تم إنشاء العام الدراسي "{name}".', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'خطأ: {e}', 'danger')

    return redirect(url_for('super_admin.school_detail', school_id=school_id))


@super_admin_bp.route('/schools/<int:school_id>/years/<int:year_id>/activate', methods=['POST'])
@login_required
@super_admin_required
def activate_year(school_id, year_id):
    year = AcademicYear.query.execution_options(bypass_tenant_scope=True)\
        .filter_by(id=year_id, school_id=school_id).first_or_404()
    AcademicYear.query.execution_options(bypass_tenant_scope=True)\
        .filter_by(school_id=school_id).update({'is_current': False})
    year.is_current = True
    db.session.commit()
    # P2: active year changed — drop this school's cached context immediately.
    from app.utils.context_cache import invalidate_school_context
    invalidate_school_context(school_id)
    log_action('edit', 'academic_year', year_id,
               details=f'تفعيل العام "{year.name}" للمدرسة {school_id}')
    flash(f'تم تعيين "{year.name}" كالعام الدراسي الحالي.', 'success')
    return redirect(url_for('super_admin.school_detail', school_id=school_id))


@super_admin_bp.route('/schools/<int:school_id>/years/<int:year_id>/rollover', methods=['POST'])
@login_required
@super_admin_required
def rollover_year(school_id, year_id):
    """Copy structural master data from the previous year into *year_id*.

    Copies: Grades → Sections → Subjects → FeeTypes.
    Students' section_id is updated to point at the new sections so they
    remain visible and correctly placed in the new year.
    """
    target_year = AcademicYear.query.execution_options(bypass_tenant_scope=True)\
        .filter_by(id=year_id, school_id=school_id).first_or_404()

    # Source: the most-recent year that is NOT the target
    source_year = (
        AcademicYear.query.execution_options(bypass_tenant_scope=True)
        .filter(AcademicYear.school_id == school_id, AcademicYear.id != year_id)
        .order_by(AcademicYear.start_date.desc())
        .first()
    )
    if not source_year:
        flash('لا يوجد عام دراسي سابق لنسخ الهيكل منه.', 'danger')
        return redirect(url_for('super_admin.school_detail', school_id=school_id))

    # Prevent double-rollover: abort if target year already has grades
    existing = Grade.query.execution_options(bypass_tenant_scope=True)\
        .filter_by(school_id=school_id, academic_year_id=year_id).count()
    if existing:
        flash(
            f'يحتوي العام "{target_year.name}" بالفعل على صفوف دراسية. '
            'لن يُعاد النقل لتجنب التكرار.',
            'warning',
        )
        return redirect(url_for('super_admin.school_detail', school_id=school_id))

    # ── 1. Clone Grades ────────────────────────────────────────────────────────
    old_grades = Grade.query.execution_options(bypass_tenant_scope=True)\
        .filter_by(school_id=school_id, academic_year_id=source_year.id).all()

    grade_id_map = {}   # {old_grade_id: new_grade_id}
    for og in old_grades:
        ng = Grade(name=og.name, stage=og.stage, school_id=school_id, academic_year_id=year_id)
        db.session.add(ng)
        db.session.flush()
        grade_id_map[og.id] = ng.id

    # ── 2. Clone Sections ──────────────────────────────────────────────────────
    old_sections = Section.query.execution_options(bypass_tenant_scope=True)\
        .filter_by(school_id=school_id, academic_year_id=source_year.id).all()

    section_id_map = {}  # {old_section_id: new_section_id}
    for os_ in old_sections:
        new_grade_id = grade_id_map.get(os_.grade_id)
        if not new_grade_id:
            continue
        ns = Section(
            name             = os_.name,
            school_id        = school_id,
            academic_year_id = year_id,
            grade_id         = new_grade_id,
            teacher_id       = os_.teacher_id,
            capacity         = os_.capacity,
        )
        db.session.add(ns)
        db.session.flush()
        section_id_map[os_.id] = ns.id

    # ── 3. Clone Subjects ──────────────────────────────────────────────────────
    old_subjects = Subject.query.execution_options(bypass_tenant_scope=True)\
        .filter_by(school_id=school_id, academic_year_id=source_year.id).all()

    for osub in old_subjects:
        db.session.add(Subject(
            name             = osub.name,
            code             = osub.code,
            school_id        = school_id,
            academic_year_id = year_id,
            description      = osub.description,
            stage            = osub.stage,
            grade_id         = grade_id_map.get(osub.grade_id),
            total_marks      = osub.total_marks,
            pass_marks       = osub.pass_marks,
        ))

    # ── 4. Clone FeeTypes ──────────────────────────────────────────────────────
    old_fee_types = FeeType.query.execution_options(bypass_tenant_scope=True)\
        .filter_by(school_id=school_id, academic_year_id=source_year.id).all()

    for oft in old_fee_types:
        db.session.add(FeeType(
            name             = oft.name,
            school_id        = school_id,
            academic_year_id = year_id,
            description      = oft.description,
        ))

    # ── 5. Reassign student sections to new-year sections ─────────────────────
    students = (
        Student.query
        .execution_options(bypass_tenant_scope=True, include_all_years=True)
        .filter_by(school_id=school_id, status='active')
        .all()
    )
    reassigned = 0
    for stu in students:
        new_sec_id = section_id_map.get(stu.section_id)
        if new_sec_id:
            stu.section_id = new_sec_id
            reassigned += 1

    db.session.commit()

    log_action('edit', 'academic_year', year_id,
               details=(f'rollover from "{source_year.name}" to "{target_year.name}": '
                        f'{len(old_grades)} grades, {len(old_sections)} sections, '
                        f'{len(old_subjects)} subjects, {len(old_fee_types)} fee types, '
                        f'{reassigned} students reassigned'))
    flash(
        f'تم نقل هيكل العام "{source_year.name}" إلى "{target_year.name}" بنجاح: '
        f'{len(old_grades)} صف، {len(old_sections)} شعبة، '
        f'{len(old_subjects)} مادة، {len(old_fee_types)} نوع رسوم. '
        f'تم تحديث {reassigned} طالب.',
        'success',
    )
    return redirect(url_for('super_admin.school_detail', school_id=school_id))


# ─────────────────────────────────────────────────────────────────────────────
#  SCHOOL BILLING — add record
# ─────────────────────────────────────────────────────────────────────────────

@super_admin_bp.route('/schools/<int:school_id>/billing/add', methods=['POST'])
@login_required
@super_admin_required
def add_billing(school_id):
    school = School.query.get_or_404(school_id)

    try:
        amount_due = Decimal(request.form.get('amount_due', '0') or '0')
    except InvalidOperation:
        amount_due = Decimal('0')

    billing_type = request.form.get('billing_type', 'subscription')
    if billing_type not in SchoolBilling.BILLING_TYPES:
        billing_type = 'subscription'

    def _parse_date(field):
        raw = request.form.get(field, '').strip()
        if raw:
            try:
                return _dt.strptime(raw, '%Y-%m-%d').date()
            except ValueError:
                pass
        return None

    record = SchoolBilling(
        school_id    = school_id,
        amount_due   = amount_due,
        amount_paid  = Decimal('0'),
        description  = request.form.get('description', '').strip() or None,
        billing_type = billing_type,
        due_date     = _parse_date('due_date'),
        notes        = request.form.get('notes', '').strip() or None,
        status       = 'unpaid',
        created_by   = current_user.id,
    )
    db.session.add(record)
    db.session.commit()
    log_action('create', 'school_billing', record.id,
               details=f'school={school_id}, amount={amount_due}')
    flash(f'تم إضافة سجل فاتورة بقيمة {amount_due} لمدرسة "{school.school_name}".', 'success')
    return redirect(url_for('super_admin.school_detail', school_id=school_id))


# ─────────────────────────────────────────────────────────────────────────────
#  SCHOOL BILLING — record a payment
# ─────────────────────────────────────────────────────────────────────────────

@super_admin_bp.route('/billing/<int:billing_id>/pay', methods=['POST'])
@login_required
@super_admin_required
def record_payment(billing_id):
    record = SchoolBilling.query.get_or_404(billing_id)

    try:
        payment = Decimal(request.form.get('payment_amount', '0') or '0')
    except InvalidOperation:
        payment = Decimal('0')

    if payment <= 0:
        flash('مبلغ الدفع يجب أن يكون أكبر من صفر.', 'danger')
        return redirect(url_for('super_admin.school_detail', school_id=record.school_id))

    # Cap payment at remaining balance
    remaining = record.amount_due - record.amount_paid
    payment = min(payment, remaining)

    payment_date_raw = request.form.get('payment_date', '').strip()
    try:
        payment_date = _dt.strptime(payment_date_raw, '%Y-%m-%d').date()
    except (ValueError, AttributeError):
        payment_date = _date.today()

    record.amount_paid   += payment
    record.payment_date   = payment_date
    record.notes          = request.form.get('notes', record.notes or '').strip() or record.notes
    record.recompute_status()

    db.session.commit()
    log_action('edit', 'school_billing', billing_id,
               details=f'تسجيل دفعة {payment} للمدرسة {record.school_id}')
    flash(f'تم تسجيل دفعة بمبلغ {payment}.', 'success')
    return redirect(url_for('super_admin.school_detail', school_id=record.school_id))


# ─────────────────────────────────────────────────────────────────────────────
#  SCHOOL BILLING — delete record
# ─────────────────────────────────────────────────────────────────────────────

@super_admin_bp.route('/billing/<int:billing_id>/delete', methods=['POST'])
@login_required
@super_admin_required
def delete_billing(billing_id):
    record = SchoolBilling.query.get_or_404(billing_id)
    school_id = record.school_id
    db.session.delete(record)
    db.session.commit()
    log_action('delete', 'school_billing', billing_id,
               details=f'حذف سجل فاتورة للمدرسة {school_id}')
    flash('تم حذف سجل الفاتورة.', 'success')
    return redirect(url_for('super_admin.school_detail', school_id=school_id))


# ─────────────────────────────────────────────────────────────────────────────
#  GLOBAL BILLING OVERVIEW
# ─────────────────────────────────────────────────────────────────────────────

@super_admin_bp.route('/billing')
@login_required
@super_admin_required
def billing_overview():
    # Filters
    gov_filter    = request.args.get('governorate', '').strip()
    school_filter = request.args.get('school_id',   type=int)
    status_filter = request.args.get('status', '').strip()

    q = SchoolBilling.query.join(School, SchoolBilling.school_id == School.id)

    if gov_filter:
        if gov_filter == 'غير محدد':
            q = q.filter(School.governorate.is_(None))
        else:
            q = q.filter(School.governorate == gov_filter)

    if school_filter:
        q = q.filter(SchoolBilling.school_id == school_filter)

    if status_filter and status_filter in SchoolBilling.STATUS_TYPES:
        q = q.filter(SchoolBilling.status == status_filter)

    records = q.order_by(SchoolBilling.created_at.desc()).all()

    total_due  = sum((r.amount_due  or Decimal('0')) for r in records)
    total_paid = sum((r.amount_paid or Decimal('0')) for r in records)

    # Governorate list for filter dropdown
    gov_rows = db.session.query(School.governorate)\
        .filter(School.governorate.isnot(None))\
        .distinct().order_by(School.governorate).all()
    governorates = [r[0] for r in gov_rows]

    all_schools = School.query.execution_options(bypass_tenant_scope=True)\
        .order_by(School.school_name).all()

    return render_template(
        'super_admin/billing_overview.html',
        records       = records,
        total_due     = total_due,
        total_paid    = total_paid,
        total_remaining = total_due - total_paid,
        governorates  = governorates,
        all_schools   = all_schools,
        gov_filter    = gov_filter,
        school_filter = school_filter,
        status_filter = status_filter,
        status_types  = SchoolBilling.STATUS_TYPES,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  سلة المحذوفات  —  soft-deleted STUDENT DOCUMENTS of the selected school
# ─────────────────────────────────────────────────────────────────────────────
#  Scope: student documents only. This page does not restore any other kind of
#  record, and it never deletes anything permanently.
#
#  Authorization: @super_admin_required (role.name == 'super_admin' AND
#  school_id IS NULL — see User.is_super_admin). A school_admin never passes it.
#
#  School selection: the portal's existing switcher, i.e. session
#  ['active_school_id'] resolved server-side by get_current_school(). A
#  school_id in the request body is never accepted as authorization; every
#  query and every write is filtered by the resolved school's own id.

RECYCLE_BIN_PER_PAGE = 25


def _selected_portal_school():
    """The school selected in the portal switcher, or None for the global view.

    Uses the existing trusted mechanism (get_current_school() reads
    session['active_school_id'] for a super admin) — never a client-supplied
    school_id.
    """
    from app.utils.decorators import get_current_school
    return get_current_school()


def _deleted_document_query(school_id, search=''):
    """Soft-deleted student documents of ONE school, newest deletion first.

    School filtering is applied in the database query itself (not after
    fetching), and it also governs the row count and the pagination, so a
    search can never reach across schools. bypass_tenant_scope is explicit
    here because a super admin has no implicit ORM school scope — the school_id
    equality below is the only filter that matters.
    """
    from app.models import StudentDocument as _SD

    query = (_SD.query
             .execution_options(bypass_tenant_scope=True, include_all_years=True)
             .join(Student, Student.id == _SD.student_id)
             .filter(_SD.school_id == school_id,
                     Student.school_id == school_id,
                     _SD.deleted_at.isnot(None)))
    term = (search or '').strip()
    if term:
        like = f'%{term}%'
        query = query.filter(db.or_(Student.full_name.ilike(like),
                                    Student.student_id.ilike(like),
                                    _SD.document_type.ilike(like)))
    return query.order_by(_SD.deleted_at.desc(), _SD.id.desc())


@super_admin_bp.route('/recycle-bin')
@login_required
@super_admin_required
def recycle_bin():
    """List the selected school's soft-deleted student documents."""
    from app.blueprints.students import (_stored_document_filename,
                                         _stored_document_present)

    school = _selected_portal_school()
    search = (request.args.get('q') or '').strip()

    if school is None:
        # No school selected → nothing is queried and nothing is listed.
        return render_template('super_admin/recycle_bin.html',
                               school=None, rows=[], pagination=None,
                               search=search)

    page = request.args.get('page', 1, type=int)
    pagination = (_deleted_document_query(school.id, search)
                  .paginate(page=page, per_page=RECYCLE_BIN_PER_PAGE,
                            error_out=False))

    rows = []
    for doc in pagination.items:
        student = doc.student
        rows.append({
            'id':              doc.id,
            'document_type':   doc.document_type,
            'student_name':    student.full_name if student else '',
            'student_code':    student.student_id if student else '',
            'student_id':      doc.student_id,
            'deleted_at':      doc.deleted_at,
            'deleted_by_name': (doc.deleted_by.full_name
                                if doc.deleted_by else ''),
            'was_replaced':    doc.replaced_by_id is not None,
            'filename':        _stored_document_filename(doc.file_path),
            'file_present':    _stored_document_present(doc.file_path),
        })

    return render_template('super_admin/recycle_bin.html',
                           school=school, rows=rows, pagination=pagination,
                           search=search)


@super_admin_bp.route('/recycle-bin/documents/<int:doc_id>/restore',
                      methods=['POST'])
@login_required
@super_admin_required
@historical_guard
def restore_student_document(doc_id):
    """Reactivate ONE soft-deleted student document of the SELECTED school.

    Every check is server-side and against the resolved portal school:
      * a school must be selected (a bare doc_id is not enough),
      * the document must exist, be soft-deleted, and belong to that school,
      * its student must still exist and belong to that same school,
      * no ACTIVE document of the same type may exist (no silent overwrite),
      * the student's ACTIVE document count must stay within the limit.

    A stale form submitted after switching schools fails the school-equality
    check and is rejected. The document keeps its id, file_path and ownership;
    nothing is re-uploaded, moved or created.
    """
    from app.models import StudentDocument as _SD
    from app.blueprints.students import (MAX_STUDENT_DOCUMENTS,
                                         active_student_document_count,
                                         _stored_document_present)

    school = _selected_portal_school()
    if school is None:
        flash('يرجى اختيار المدرسة أولاً.', 'danger')
        return redirect(url_for('super_admin.recycle_bin'))

    # Scoped from the start: id + school + soft-deleted state.
    doc = (_SD.query
           .execution_options(bypass_tenant_scope=True, include_all_years=True)
           .filter(_SD.id == doc_id,
                   _SD.school_id == school.id,
                   _SD.deleted_at.isnot(None))
           .first())
    if doc is None:
        # Wrong school, unknown id, or already active — one safe answer.
        flash('المستمسك غير موجود في سلة محذوفات المدرسة المحددة.', 'danger')
        return redirect(url_for('super_admin.recycle_bin'))

    student = (Student.query
               .execution_options(bypass_tenant_scope=True,
                                  include_all_years=True)
               .filter_by(id=doc.student_id, school_id=school.id)
               .first())
    if student is None:
        # Never recreate a missing student/school automatically.
        flash('تعذّر الاسترجاع: سجل الطالب غير موجود أو لا ينتمي إلى '
              'المدرسة المحددة.', 'danger')
        return redirect(url_for('super_admin.recycle_bin'))

    if not _stored_document_present(doc.file_path):
        flash(f'تعذّر الاسترجاع: ملف المستمسك «{doc.document_type}» غير موجود '
              f'في المخزن.', 'danger')
        return redirect(url_for('super_admin.recycle_bin'))

    conflict = (_SD.query
                .execution_options(bypass_tenant_scope=True,
                                   include_all_years=True)
                .filter(_SD.student_id == student.id,
                        _SD.school_id == school.id,
                        _SD.document_type == doc.document_type,
                        _SD.deleted_at.is_(None))
                .first())
    if conflict is not None:
        flash(f'يوجد مستمسك فعّال من نوع «{doc.document_type}» لهذا الطالب. '
              f'لم يتم تغيير أي مستمسك.', 'danger')
        return redirect(url_for('super_admin.recycle_bin'))

    active_count = active_student_document_count(student)
    if active_count >= MAX_STUDENT_DOCUMENTS:
        flash(f'لا يمكن الاسترجاع: الطالب لديه {active_count} مستمسكات فعّالة '
              f'والحد الأقصى {MAX_STUDENT_DOCUMENTS}. لم يتم تغيير أي مستمسك.',
              'danger')
        return redirect(url_for('super_admin.recycle_bin'))

    doc_label, student_code = doc.document_type, student.student_id
    doc.deleted_at         = None
    doc.deleted_by_user_id = None
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash('تعذّر استرجاع المستمسك. لم يتم تغيير أي مستمسك.', 'danger')
        return redirect(url_for('super_admin.recycle_bin'))

    log_action('restore', 'student_document', doc.id,
               f'استرجاع المستمسك «{doc_label}» من سلة المحذوفات — '
               f'المدرسة {school.id}، الطالب {student.id} ({student_code}), '
               f'بواسطة مسؤول النظام {current_user.id}')
    flash(f'تم استرجاع المستمسك «{doc_label}» للطالب {student.full_name} '
          f'بنفس ملفه الأصلي.', 'success')
    return redirect(url_for('super_admin.recycle_bin'))

