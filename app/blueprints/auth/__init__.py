"""
Al-Muhandis – Authentication Blueprint
"""
from urllib.parse import urlparse, urljoin

from flask import (Blueprint, render_template, redirect,
                   url_for, flash, request, session)
from flask_login import login_user, logout_user, login_required, current_user
from app.models import db, User
from app.utils.audit import log_action
from app.utils.ratelimit import limiter, LOGIN_RATE_LIMIT
from app.utils.login_throttle import check_lockout, record_failed_attempt, reset_attempts, format_wait_ar
from datetime import datetime

auth_bp = Blueprint('auth', __name__, template_folder='../../templates/auth')


def _is_safe_redirect_target(target):
    """Allow redirects only to same-host relative paths (blocks open redirect)."""
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return (test_url.scheme in ('http', 'https')
            and ref_url.netloc == test_url.netloc)


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit(LOGIN_RATE_LIMIT, methods=['POST'])
def login():
    if current_user.is_authenticated:
        return _role_redirect(current_user)

    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))
        ip = request.remote_addr or '0.0.0.0'

        # ── Progressive lockout check ─────────────────────────────────────────
        # Evaluated before any DB query so locked-out requests cost nothing.
        # The lockout key encodes both IP and a hash of the username, so the
        # wait-time message cannot be used to confirm whether a username exists
        # (the attacker already knows what they submitted).
        locked, wait_seconds = check_lockout(ip, username)
        if locked:
            error = (
                f'تجاوزت الحد المسموح لمحاولات تسجيل الدخول. '
                f'يرجى المحاولة {format_wait_ar(wait_seconds)}.'
            )
            return render_template('auth/login.html', error=error)

        user = User.query.filter(
            (User.username == username) | (User.email == username)
        ).first()

        if user and user.check_password(password) and user.is_active:
            # ── Session fixation prevention ───────────────────────────────────
            # Clear any pre-login session data before establishing the
            # authenticated session. CSRF has already been validated by
            # Flask-WTF at this point, so clearing is safe. Flask-Login will
            # immediately repopulate the session with the new user context.
            session.clear()
            login_user(user, remember=remember)
            reset_attempts(ip, username)
            user.last_login = datetime.utcnow()
            db.session.commit()
            log_action('login', 'user', user.id, f'Login from {ip}')
            flash('تم تسجيل الدخول بنجاح.', 'success')
            next_page = request.args.get('next')
            if next_page and _is_safe_redirect_target(next_page):
                return redirect(next_page)
            return redirect(_role_redirect_url(user))
        else:
            # Record failure regardless of whether the username exists, so the
            # error message and throttle behaviour are identical for a wrong
            # username and a wrong password.
            record_failed_attempt(ip, username)
            # If this attempt crossed a lockout threshold show the lockout
            # message immediately — not only on the next submit.
            locked, wait_seconds = check_lockout(ip, username)
            if locked:
                error = (
                    f'تجاوزت الحد المسموح لمحاولات تسجيل الدخول. '
                    f'يرجى المحاولة {format_wait_ar(wait_seconds)}.'
                )
            else:
                error = 'اسم المستخدم أو كلمة المرور غير صحيحة.'

    return render_template('auth/login.html', error=error)


@auth_bp.route('/logout')
@login_required
def logout():
    log_action('logout', 'user', current_user.id)
    # logout_user() clears the Flask-Login session data and deletes the
    # remember-me cookie (sets it to empty with a past expiry date).
    logout_user()
    flash('تم تسجيل الخروج بنجاح.', 'success')
    return redirect(url_for('auth.login'))


_READONLY_ROLES = {'parent', 'teacher', 'investor_viewer'}


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
            # Require the current password before allowing a change, so a
            # hijacked session cannot silently lock out the real owner.
            current_password = request.form.get('current_password', '')
            if not current_password or not current_user.check_password(current_password):
                db.session.rollback()
                flash('كلمة المرور الحالية غير صحيحة.', 'danger')
                return redirect(url_for('auth.profile'))
            if len(new_password) < 8:
                db.session.rollback()
                flash('يجب أن تتكون كلمة المرور الجديدة من 8 أحرف على الأقل.', 'danger')
                return redirect(url_for('auth.profile'))
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
    if role == 'investor_viewer':
        return url_for('investor.dashboard')
    if role == 'accountant':
        # Finance-scoped role: no access to the admin dashboard. Land on the
        # first allowed accounting surface. Confinement is enforced by
        # _confine_accountant in app/__init__.py.
        return url_for('fees.index')
    # All remaining staff roles (admin, hr, reception, …) land on admin dashboard
    return url_for('admin.dashboard')


def _role_redirect(user):
    return redirect(_role_redirect_url(user))
