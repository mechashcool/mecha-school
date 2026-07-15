"""
Al-Muhandis School Management System
Application Factory  (Phase 6: Multi-Tenant)
"""
import os
from flask import Flask, request
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect, CSRFError

from app.models import db, bcrypt, User
from app.utils.scoping import register_tenant_guards
from app.utils.ratelimit import limiter
from config.settings import config

login_manager = LoginManager()
migrate = Migrate()
csrf = CSRFProtect()


def create_app(config_name=None):
    """Application factory."""
    if config_name is None:
        config_name = os.environ.get('FLASK_ENV', 'development')

    app = Flask(__name__)
    app.config.from_object(config[config_name])
    config[config_name].init_app(app)

    # ── Trusted reverse proxy (Nginx) ─────────────────────────────────────────
    # The app runs behind exactly ONE trusted proxy (Nginx / Render) that appends
    # the real client IP to X-Forwarded-For and sets X-Forwarded-Proto=$scheme.
    # ProxyFix rewrites request.remote_addr and request.scheme from the rightmost
    # (proxy-appended) values so per-IP rate limiting, login throttling, audit
    # logging, and HTTPS detection are accurate. x_for/x_proto=1 only — one hop.
    # x_host/x_port are intentionally left 0: Nginx delivers the real Host via the
    # standard Host header and does NOT emit X-Forwarded-Host, so trusting one
    # would let a client spoof it (Host-header injection). Do not raise the hop
    # counts unless the proxy topology actually changes.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_port=0)

    # ── Extensions ────────────────────────────────────────────────────────────
    db.init_app(app)
    bcrypt.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    register_tenant_guards(app)

    # ── Observability (P3) ────────────────────────────────────────────────────
    # Request timing + slow-request/slow-query WARNING logs + external-service
    # latency aggregates. Aggregates only — never bodies, query strings, bind
    # parameters, or tenant data. No-op when OBSERVABILITY_ENABLED=false.
    from app.utils.observability import init_app as init_observability
    init_observability(app)

    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'يرجى تسجيل الدخول للوصول إلى هذه الصفحة.'
    login_manager.login_message_category = 'warning'

    @app.teardown_appcontext
    def cleanup_db_session(exc=None):
        db.session.remove()

    @login_manager.user_loader
    def load_user(user_id):
        from sqlalchemy import select
        from sqlalchemy.orm import joinedload

        return db.session.execute(
            select(User)
            .options(joinedload(User.role))
            .where(User.id == int(user_id))
            .execution_options(bypass_tenant_scope=True)
        ).scalar_one_or_none()

    # ── CORS — mobile API only ─────────────────────────────────────────────────
    # after_request always fires, including for Flask's automatic OPTIONS responses.
    # JWT Bearer auth is stateless, so Allow-Origin: * is safe here.
    @app.after_request
    def _mobile_api_cors(response):
        if request.path.startswith('/api/mobile/v1/'):
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, PATCH, DELETE, OPTIONS'
        return response

    # ── Blueprints ────────────────────────────────────────────────────────────
    from app.blueprints.auth import auth_bp
    from app.blueprints.admin import admin_bp
    from app.blueprints.students import students_bp
    from app.blueprints.employees import employees_bp
    from app.blueprints.fees import fees_bp
    from app.blueprints.attendance import attendance_bp
    from app.blueprints.grades import grades_bp
    from app.blueprints.reports import reports_bp
    from app.blueprints.notifications import notifications_bp
    from app.blueprints.salaries import salaries_bp
    from app.blueprints.finances import finances_bp
    from app.blueprints.sections import sections_bp
    from app.blueprints.schedules import schedules_bp
    from app.blueprints.evaluations import evaluations_bp
    from app.blueprints.audit import audit_bp
    from app.blueprints.parent     import parent_bp
    from app.blueprints.api        import api_bp
    from app.blueprints.hardware   import hardware_bp
    from app.blueprints.broadcast  import broadcast_bp
    from app.blueprints.teacher    import teacher_bp
    # Phase 6 — multi-tenant
    from app.blueprints.schools      import schools_bp
    # Phase 7 — Super Admin portal
    from app.blueprints.super_admin  import super_admin_bp
    from app.blueprints.investor     import investor_bp
    # Transport routes
    from app.blueprints.transport    import transport_bp
    from app.blueprints.inventory    import inventory_bp
    # Mobile API — JWT-authenticated REST layer
    from app.blueprints.mobile_api        import mobile_api_bp
    from app.blueprints.attendance_devices import attendance_devices_bp
    from app.blueprints.school_calendar   import school_calendar_bp
    from app.blueprints.homework          import homework_bp
    from app.blueprints.chat              import chat_bp
    from app.blueprints.student_records   import student_records_bp
    from app.blueprints.buildings         import buildings_bp
    from app.blueprints.shifts            import shifts_bp
    # Live badge polling — lightweight JSON for in-page badge sync
    from app.blueprints.live              import live_bp
    # Media — serves locally-stored uploads independently of the nginx /static alias
    from app.blueprints.media             import media_bp
    # Public pages — no authentication required (e.g. /privacy)
    from app.blueprints.public_pages      import public_pages_bp
    # Ops — /ops/health liveness + guarded /ops/metrics & /ops/health/deep (P3)
    from app.blueprints.ops               import ops_bp

    app.register_blueprint(auth_bp,          url_prefix='/auth')
    app.register_blueprint(admin_bp,         url_prefix='/admin')
    app.register_blueprint(students_bp,      url_prefix='/students')
    app.register_blueprint(employees_bp,     url_prefix='/employees')
    app.register_blueprint(fees_bp,          url_prefix='/fees')
    app.register_blueprint(attendance_bp,    url_prefix='/attendance')
    app.register_blueprint(grades_bp,        url_prefix='/grades')
    app.register_blueprint(reports_bp,       url_prefix='/reports')
    app.register_blueprint(notifications_bp, url_prefix='/notifications')
    app.register_blueprint(salaries_bp,      url_prefix='/salaries')
    app.register_blueprint(finances_bp,      url_prefix='/finances')
    app.register_blueprint(sections_bp,      url_prefix='/sections')
    app.register_blueprint(schedules_bp,     url_prefix='/schedules')
    app.register_blueprint(evaluations_bp,   url_prefix='/evaluations')
    app.register_blueprint(audit_bp,         url_prefix='/audit')
    app.register_blueprint(parent_bp,        url_prefix='/parent')
    app.register_blueprint(api_bp,           url_prefix='/api')
    app.register_blueprint(hardware_bp,      url_prefix='/api/v1/hardware')
    app.register_blueprint(broadcast_bp,     url_prefix='/broadcast')
    app.register_blueprint(teacher_bp,       url_prefix='/teacher')
    app.register_blueprint(schools_bp,       url_prefix='/schools')
    app.register_blueprint(super_admin_bp,   url_prefix='/admin/super')
    app.register_blueprint(investor_bp,      url_prefix='/investor')
    app.register_blueprint(transport_bp,     url_prefix='/transport')
    app.register_blueprint(inventory_bp,     url_prefix='/inventory')
    app.register_blueprint(mobile_api_bp,          url_prefix='/api/mobile/v1')
    app.register_blueprint(attendance_devices_bp,  url_prefix='/attendance-devices')
    app.register_blueprint(school_calendar_bp,     url_prefix='/school-calendar')
    app.register_blueprint(homework_bp,            url_prefix='/homework')
    app.register_blueprint(chat_bp,                url_prefix='/chat')
    app.register_blueprint(student_records_bp,     url_prefix='/student-registration-records')
    app.register_blueprint(buildings_bp,           url_prefix='/buildings')
    app.register_blueprint(shifts_bp,              url_prefix='/attendance-shifts')
    app.register_blueprint(live_bp,                url_prefix='/live')
    app.register_blueprint(media_bp)          # route already includes the /media prefix
    app.register_blueprint(public_pages_bp)   # public routes — no login required
    app.register_blueprint(ops_bp)            # routes already include the /ops prefix

    # ── CSRF exemptions ───────────────────────────────────────────────────────
    # These blueprints authenticate via request headers (JWT bearer token or a
    # device API key), not session cookies, so they are not exposed to CSRF and
    # must be exempt — native/mobile clients do not send a CSRF token.
    csrf.exempt(api_bp)          # legacy mobile/parent JSON API
    csrf.exempt(mobile_api_bp)   # JWT-authenticated mobile API
    csrf.exempt(hardware_bp)     # X-Device-Key authenticated hardware endpoints

    # ── Jinja2 globals ────────────────────────────────────────────────────────
    from app.utils.helpers import resolve_photo_url
    from werkzeug.routing import BuildError as _BuildError

    def _safe_url_for(endpoint, **values):
        from flask import url_for as _url_for
        try:
            return _url_for(endpoint, **values)
        except (_BuildError, Exception):
            return '#'

    from app.utils.upload_access import protected_upload_url

    app.jinja_env.globals.update(enumerate=enumerate, zip=zip, len=len,
                                 resolve_photo_url=resolve_photo_url,
                                 safe_url_for=_safe_url_for,
                                 upload_url=protected_upload_url)

    # ── Jinja2 filters ────────────────────────────────────────────────────────
    from datetime import timezone as _tz, timedelta as _td
    _baghdad = _tz(_td(hours=3))  # Iraq is UTC+3, no DST

    def _to_baghdad(dt, fmt='%Y-%m-%d %H:%M'):
        if not dt:
            return ''
        return dt.replace(tzinfo=_tz.utc).astimezone(_baghdad).strftime(fmt)

    app.jinja_env.filters['to_baghdad'] = _to_baghdad

    # ── Root redirect ─────────────────────────────────────────────────────────
    from flask import redirect, url_for
    from flask_login import current_user as _cu

    @app.route('/')
    def index():
        if _cu.is_authenticated:
            if _cu.role and _cu.role.name == 'parent':
                return redirect(url_for('parent.dashboard'))
            if _cu.role and _cu.role.name == 'teacher':
                return redirect(url_for('teacher.dashboard'))
            return redirect(url_for('admin.dashboard'))
        return redirect(url_for('auth.login'))

    # ── Context processors ────────────────────────────────────────────────────
    @app.context_processor
    def inject_globals():
        from flask_login import current_user
        from datetime import date, datetime

        unread_count = 0
        current_school   = None
        active_year      = None   # VIEW year (may be historical); for display
        real_active_year = None   # CURRENT active year only; for write-guards
        all_schools      = []
        all_school_years = []     # all years for this school; for year-switcher
        sidebar_counts   = {'pending_complaints': 0, 'pending_leave_requests': 0, 'pending_employee_leave_requests': 0, 'unread_chat': 0}

        skip_db_context = (
            request.endpoint is None
            or request.endpoint == 'static'
            or request.path.startswith('/static/')
        )

        if not skip_db_context:
            if current_user.is_authenticated:
                try:
                    from app.models import AcademicYear, School, SchoolSettings
                    from app.utils.decorators import (
                        get_current_school, get_active_year, get_view_year,
                    )

                    current_school = get_current_school()
                    if current_school:
                        real_active_year = get_active_year(current_school.id)
                        active_year      = get_view_year(current_school.id)
                    # Super admin gets a list of all schools for the switcher widget
                    if current_user.is_super_admin:
                        all_schools = School.query.filter_by(is_active=True)\
                                                  .order_by(School.id).all()
                    # School managers get a list of all their school's years for the
                    # year-switcher widget in the sidebar.
                    elif current_school:
                        all_school_years = (
                            AcademicYear.query
                            .execution_options(bypass_tenant_scope=True)
                            .filter_by(school_id=current_school.id)
                            .order_by(AcademicYear.start_date.desc())
                            .all()
                        )

                    # Fallback to legacy SchoolSettings so old templates still work
                    # if no School rows exist yet (e.g., during a fresh install before seeding).
                    if current_school is None:
                        current_school = SchoolSettings.get()
                except Exception:
                    db.session.rollback()

            # Inject enabled_modules set for sidebar and template guards.
            try:
                from app.utils.modules import get_enabled_modules, MODULES
                if current_user.is_authenticated:
                    _school_id = (None if current_user.is_super_admin
                                  else getattr(current_user, 'school_id', None))
                    enabled_modules = get_enabled_modules(_school_id)
                else:
                    enabled_modules = set()
            except Exception:
                db.session.rollback()
                from app.utils.modules import MODULES
                enabled_modules = set(MODULES.keys())  # fail-open: show all

            if current_school is None:
                try:
                    from app.models import SchoolSettings
                    current_school = SchoolSettings.get()
                except Exception:
                    db.session.rollback()

            # Inject enabled_features for granular capability guards.
            try:
                from app.utils.features import get_enabled_features, FEATURES
                if current_user.is_authenticated:
                    _feat_school_id = (None if current_user.is_super_admin
                                       else getattr(current_user, 'school_id', None))
                    enabled_features = get_enabled_features(_feat_school_id)
                else:
                    enabled_features = set()
            except Exception:
                db.session.rollback()
                from app.utils.features import FEATURES
                enabled_features = set(FEATURES.keys())  # fail-open: show all

            # Inject school_cfg — per-school module config (sections/fields/actions).
            try:
                from app.utils.school_config import get_school_config
                if current_user.is_authenticated and not current_user.is_super_admin:
                    _cfg_school_id = getattr(current_user, 'school_id', None)
                else:
                    _cfg_school_id = None  # super admin: NullSchoolConfig (all open)
                school_cfg = get_school_config(_cfg_school_id)
            except Exception:
                db.session.rollback()
                from app.utils.school_config import NullSchoolConfig
                school_cfg = NullSchoolConfig()
        else:
            # Static file or no endpoint — no module context needed.
            enabled_modules  = set()
            enabled_features = set()
            from app.utils.school_config import NullSchoolConfig
            school_cfg = NullSchoolConfig()

        # ── Sidebar / topbar badge counts ──────────────────────────────────────
        # All four counters (notifications, complaints, leave requests, unread
        # chat) come from the shared helper so the live-polling endpoint and the
        # server-rendered page can never disagree on logic or isolation rules.
        # Page render uses the established 45 s badge_cache keys (live=False);
        # each count is individually guarded and falls back to 0 on error.
        if current_user.is_authenticated and not skip_db_context:
            from app.utils.sidebar_badges import get_badge_counts
            _counts = get_badge_counts(live=False)
            unread_count = _counts['unread_notifications']
            sidebar_counts = {
                'pending_complaints':              _counts['pending_complaints'],
                'pending_leave_requests':          _counts['pending_leave_requests'],
                'pending_employee_leave_requests': _counts['pending_employee_leave_requests'],
                'unread_chat':                     _counts['unread_chat'],
            }

        # True when a school user is viewing a historical (non-current) year.
        # Injected into all templates so they can hide write-action buttons.
        try:
            is_historical_year = (
                active_year is not None
                and real_active_year is not None
                and active_year.id != real_active_year.id
            )
        except Exception:
            is_historical_year = False

        from flask import session as _session
        return dict(
            unread_notifications = unread_count,
            today                = date.today(),
            now                  = datetime.utcnow(),
            school               = current_school,          # used by existing templates
            current_school       = current_school,          # alias for new templates
            active_year          = active_year,             # view year (may be historical)
            real_active_year     = real_active_year,        # always the current active year
            all_school_years     = all_school_years,        # for year-switcher widget
            all_schools          = all_schools,             # for super-admin switcher
            active_school_id     = _session.get('active_school_id'),
            is_historical_year   = is_historical_year,      # read-only guard for templates
            enabled_modules      = enabled_modules,         # set of enabled module keys
            enabled_features     = enabled_features,        # set of enabled feature keys
            school_cfg           = school_cfg,              # SchoolConfig for section/field/action guards
            sidebar_counts       = sidebar_counts,          # badge counters for sidebar nav items
        )

    # ── Module access guard ───────────────────────────────────────────────────
    @app.before_request
    def _check_module_access():
        """Block school users from blueprints whose module is disabled."""
        from flask_login import current_user
        if not current_user.is_authenticated or current_user.is_super_admin:
            return None
        blueprint = request.blueprint
        if not blueprint:
            return None
        from app.utils.modules import BLUEPRINT_MODULE, MODULES, is_module_enabled
        module_key = BLUEPRINT_MODULE.get(blueprint)
        if not module_key:
            return None
        school_id = getattr(current_user, 'school_id', None)
        if not is_module_enabled(school_id, module_key):
            from flask import render_template as _rt
            return _rt('shared/module_disabled.html',
                       module_label=MODULES[module_key]['label']), 403

    # ── Investor confinement guard ─────────────────────────────────────────────
    @app.before_request
    def _confine_investor():
        """Restrict read-only investor accounts to their own portal.

        The investor role holds no permissions, so permission-gated routes
        already return 403. This closes the remaining gap: routes protected by
        @login_required alone (e.g. /notifications/) would otherwise be
        reachable. Fail-closed — anything outside the investor's allowed surface
        is redirected (GET) or blocked (403). Only affects investor_viewer; no
        other role is touched. Web-session only: mobile API paths are skipped so
        the JWT-authenticated mobile endpoints (with their own role guards) are
        unaffected.
        """
        from flask_login import current_user
        if not current_user.is_authenticated:
            return None
        if not getattr(current_user, 'is_investor', False):
            return None

        # Mobile API authenticates per-request via JWT and guards its own routes.
        if request.path.startswith('/api/mobile/v1/'):
            return None

        endpoint = request.endpoint or ''
        blueprint = request.blueprint or ''

        # Allowed surface for the investor role.
        if (blueprint == 'investor'
                or endpoint in ('static', 'media.serve', 'auth.logout', 'auth.login')):
            return None

        from flask import redirect as _redirect, url_for as _url_for, abort as _abort
        if request.method == 'GET':
            return _redirect(_url_for('investor.dashboard'))
        _abort(403)

    # ── Accountant confinement guard ───────────────────────────────────────────
    # Blueprints/endpoints the finance-scoped accountant role may reach. Anything
    # else (dashboard, students, academic, HR list, employee manual attendance /
    # settings, evaluations, users, settings, notifications, reports, …) is
    # blocked below — independent of whatever permissions the role carries.
    _ACCOUNTANT_ALLOWED_BLUEPRINTS = frozenset({'fees', 'finances', 'salaries', 'auth'})
    _ACCOUNTANT_ALLOWED_ENDPOINTS = frozenset({
        'static', 'media.serve',
        'admin.switch_year',                              # year-switcher widget
        'employees.attendance_report',                    # employee attendance REPORT only
        'employees.attendance_report_detail',
        'employees.attendance_report_export_excel',
        'employees.attendance_report_export_pdf',
        'employees.attendance_report_employee_excel',
        'employees.attendance_report_employee_pdf',
    })

    @app.before_request
    def _confine_accountant():
        """Restrict the accountant role to accounting surfaces only.

        The accountant role is finance-scoped: fees/installments, revenues &
        expenses, payroll, and the employee-attendance REPORT (read-only). Every
        other staff surface is blocked here — independent of the role's
        permissions, so broad legacy permissions cannot be abused to reach the
        admin dashboard, employee management, manual attendance, settings, etc.
        Fail-closed: redirect (GET) or 403 (write/JSON). Only affects the
        accountant role; no other role is touched. Mobile API paths are skipped
        (they authenticate per-request via JWT with their own role guards).
        """
        from flask_login import current_user
        if not current_user.is_authenticated:
            return None
        if not getattr(current_user, 'is_accountant', False):
            return None

        if request.path.startswith('/api/mobile/v1/'):
            return None

        endpoint = request.endpoint or ''
        blueprint = request.blueprint or ''

        if blueprint in _ACCOUNTANT_ALLOWED_BLUEPRINTS:
            return None
        if endpoint in _ACCOUNTANT_ALLOWED_ENDPOINTS:
            return None

        from flask import redirect as _redirect, url_for as _url_for, abort as _abort
        if request.method == 'GET':
            return _redirect(_url_for('fees.index'))
        _abort(403)

    # ── Error handlers ────────────────────────────────────────────────────────
    def _render_error_page(template_name):
        try:
            db.session.rollback()
            return app.jinja_env.get_template(template_name).render()
        except Exception:
            return (
                '<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8">'
                '<title>خطأ</title><body><h1>حدث خطأ</h1></body></html>'
            )

    @app.errorhandler(403)
    def forbidden(e):
        return _render_error_page('shared/403.html'), 403

    @app.errorhandler(404)
    def not_found(e):
        return _render_error_page('shared/404.html'), 404

    @app.errorhandler(500)
    def server_error(e):
        return _render_error_page('shared/500.html'), 500

    @app.errorhandler(429)
    def too_many_requests(e):
        # API / AJAX callers (mobile app, fetch, XHR) get a JSON response.
        # Browser form submissions (web login) get the friendly Arabic page.
        from flask import jsonify as _jsonify
        is_api = (
            request.path.startswith('/api/')
            or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or 'application/json' in request.headers.get('Accept', '')
        )
        if is_api:
            return _jsonify({'ok': False, 'error': 'rate_limited', 'message': 'طلبات كثيرة، يرجى المحاولة لاحقاً.'}), 429
        return _render_error_page('shared/429.html'), 429

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        from flask import flash, redirect, url_for, jsonify as _jsonify
        msg = 'انتهت صلاحية الجلسة أو تم إرسال الطلب أكثر من مرة، يرجى المحاولة مجددًا.'
        is_ajax = (
            request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or 'application/json' in request.headers.get('Accept', '')
        )
        if is_ajax:
            return _jsonify({'ok': False, 'error': msg}), 400
        flash(msg, 'danger')
        return redirect(url_for('auth.login'))

    # ── Security response headers ─────────────────────────────────────────────
    @app.after_request
    def _set_security_headers(response):
        # Conservative, widely-compatible headers. No Content-Security-Policy is
        # set here because the UI relies on inline styles/scripts and CDN assets;
        # adding a strict CSP must be done deliberately to avoid breaking pages.
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        # Disable browser features the app never uses. Conservative and safe: it
        # does not restrict scripts/styles/CDN/inline handlers (that would be CSP),
        # so it cannot break the UI. No Content-Security-Policy is set here — a
        # strict CSP needs template work and is left as a deliberate follow-up.
        response.headers.setdefault(
            'Permissions-Policy',
            'camera=(), microphone=(), geolocation=(), payment=()',
        )
        if not app.debug and config_name == 'production':
            response.headers.setdefault(
                'Strict-Transport-Security',
                'max-age=31536000; includeSubDomains',
            )
        return response

    # ── No-store for authenticated, sensitive responses ───────────────────────
    @app.after_request
    def _no_store_for_authenticated(response):
        # Keep browsers and shared/proxy caches from retaining authenticated pages
        # that carry student / employee / attendance / finance / grades / fees /
        # reports / payroll / admin data. Tightly scoped so it never touches
        # cacheable public content:
        #   • only when the current user is authenticated,
        #   • never for /static or the 'static' endpoint,
        #   • never for the 'media' blueprint (public branding + signed private
        #     media set their own cache policy and must not be disturbed).
        # Public/login pages are served to anonymous users and are unaffected.
        try:
            from flask_login import current_user
            if not current_user.is_authenticated:
                return response
        except Exception:
            return response
        if request.endpoint == 'static' or request.path.startswith('/static/'):
            return response
        if request.blueprint == 'media':
            return response
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    # ── Badge cache invalidation on state-changing requests ───────────────────
    # When the logged-in user performs an action that can change one of their
    # own badge counts (read / reply / resolve / approve / reject / …), drop
    # their cached counts so the very next badge poll — or the page rendered
    # right after a redirect — reflects reality immediately on this worker,
    # rather than waiting out the cache TTL. Scoped to this user's keys only,
    # so it adds no recompute traffic for anyone else and leaves the read-only
    # navigation cache for unrelated users intact.
    @app.after_request
    def _invalidate_badges_after_write(response):
        if request.method in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
            return response
        if request.endpoint == 'live.badges':
            return response
        try:
            from flask_login import current_user
            if current_user.is_authenticated:
                from app.utils.sidebar_badges import invalidate_user_badges
                invalidate_user_badges(current_user)
        except Exception:
            pass
        return response

    # ── Upload folder ─────────────────────────────────────────────────────────
    uploads_dir = os.path.join(app.root_path, 'static', 'uploads')
    os.makedirs(uploads_dir, exist_ok=True)

    # ── Background services (web server only — skipped during CLI commands) ────
    # Flask CLI commands like `flask db upgrade`, `flask shell`, and `flask routes`
    # must not start background threads: they hold DB connections, interfere with
    # migrations, and fail when the schema is incomplete.
    import sys as _sys
    import logging as _logging
    _cli_cmd = _sys.argv[1] if len(_sys.argv) >= 2 else ''
    _skip_schedulers = _cli_cmd in ('db', 'shell', 'routes', 'digest', 'collect')

    _startup_log = _logging.getLogger('mecha.startup')
    _startup_log.info(
        '[startup] app=%s  env=%s  PORT=%s  WEB_CONCURRENCY=%s  '
        'AIFACE_WS_ENABLED=%s  AIFACE_WS_PORT=%s  '
        'ATTENDANCE_SCHEDULER_DISABLED=%s  FEE_REMINDER_SCHEDULER_DISABLED=%s  '
        'HIKVISION_AUTO_SYNC=%s  skip_background=%s',
        app.name, config_name,
        os.environ.get('PORT', '(not set)'),
        os.environ.get('WEB_CONCURRENCY', '(not set)'),
        os.environ.get('AIFACE_WS_ENABLED', '(not set)'),
        os.environ.get('AIFACE_WS_PORT', '(not set, default=7788)'),
        os.environ.get('ATTENDANCE_SCHEDULER_DISABLED', '(not set, default=false)'),
        os.environ.get('FEE_REMINDER_SCHEDULER_DISABLED', '(not set, default=false)'),
        os.environ.get('HIKVISION_AUTO_SYNC', '(not set, default=false)'),
        _skip_schedulers,
    )

    if not _skip_schedulers:
        from app.services.hikvision import start_auto_sync
        start_auto_sync(app)

        from app.services.ai_face_ws import start_ai_face_ws_server
        start_ai_face_ws_server(app)

        from app.services.auto_attendance import start_auto_attendance_scheduler
        start_auto_attendance_scheduler(app)

        from app.services.fee_reminder import start_fee_reminder_scheduler
        start_fee_reminder_scheduler(app)

        # Durable push-queue consumer (P3) — no-op unless REDIS_URL is set and
        # DURABLE_PUSH_QUEUE_ENABLED is true. Never blocks startup.
        try:
            from app.services.durable_queue import start_consumer
            start_consumer(app)
        except Exception:
            _logging.getLogger('mecha.durable_queue').exception(
                '[durable] consumer failed to start — pushes fall back to the '
                'in-process thread pool')

    return app
