"""
Al-Muhandis – Authentication Blueprint
"""
from flask import (Blueprint, render_template, redirect,
                   url_for, flash, request, session)
from flask_login import login_user, logout_user, login_required, current_user
from app.models import db, User
from app.utils.audit import log_action
from datetime import datetime

auth_bp = Blueprint('auth', __name__, template_folder='../../templates/auth')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return _role_redirect(current_user)

    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))

        user = User.query.filter(
            (User.username == username) | (User.email == username)
        ).first()

        if user and user.check_password(password) and user.is_active:
            login_user(user, remember=remember)
            user.last_login = datetime.utcnow()
            db.session.commit()
            log_action('login', 'user', user.id, f'Login from {request.remote_addr}')
            next_page = request.args.get('next')
            return redirect(next_page or _role_redirect_url(user))
        else:
            error = 'اسم المستخدم أو كلمة المرور غير صحيحة.'

    return render_template('auth/login.html', error=error)


@auth_bp.route('/logout')
@login_required
def logout():
    log_action('logout', 'user', current_user.id)
    logout_user()
    flash('تم تسجيل الخروج بنجاح.', 'success')
    return redirect(url_for('auth.login'))


_READONLY_ROLES = {'parent', 'teacher'}


@auth_bp.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    role_name = current_user.role.name if current_user.role else ''
    readonly  = role_name in _READONLY_ROLES

    if request.method == 'POST':
        if readonly:
            # Hard block — prevents direct POST manipulation by these roles
            flash('لا يمكنك تعديل بياناتك الشخصية. يرجى التواصل مع إدارة المدرسة.', 'warning')
            return redirect(url_for('auth.profile'))

        full_name = request.form.get('full_name', '').strip()
        phone     = request.form.get('phone', '').strip()
        if full_name:
            current_user.full_name = full_name
        current_user.phone = phone

        new_password = request.form.get('new_password', '')
        if new_password:
            current_user.set_password(new_password)

        db.session.commit()
        flash('تم تحديث البيانات بنجاح.', 'success')
        return redirect(url_for('auth.profile'))

    return render_template('auth/profile.html', readonly=readonly)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _role_redirect_url(user):
    role = user.role.name if user.role else ''
    if role == 'parent':
        return url_for('parent.dashboard')
    if role == 'teacher':
        return url_for('teacher.dashboard')
    # All staff roles (admin, accountant, hr, reception, …) land on admin dashboard
    return url_for('admin.dashboard')


def _role_redirect(user):
    return redirect(_role_redirect_url(user))
