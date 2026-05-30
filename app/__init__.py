"""
Al-Muhandis School Management System
Application Factory  (Phase 6: Multi-Tenant)
"""
import os
from flask import Flask, request
from flask_login import LoginManager
from flask_migrate import Migrate

from app.models import db, bcrypt, User
from app.utils.scoping import register_tenant_guards
from config.settings import config

login_manager = LoginManager()
migrate = Migrate()


def create_app(config_name=None):
    """Application factory."""
    if config_name is None:
        config_name = os.environ.get('FLASK_ENV', 'development')

    app = Flask(__name__)
    app.config.from_object(config[config_name])
    config[config_name].init_app(app)

    # ── Extensions ────────────────────────────────────────────────────────────
    db.init_app(app)
    bcrypt.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    register_tenant_guards(app)

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
    # Transport routes
    from app.blueprints.transport    import transport_bp
    from app.blueprints.inventory    import inventory_bp
    # Mobile API — JWT-authenticated REST layer
    from app.blueprints.mobile_api        import mobile_api_bp
    from app.blueprints.attendance_devices import attendance_devices_bp
    from app.blueprints.school_calendar   import school_calendar_bp
    from app.blueprints.homework          import homework_bp
    from app.blueprints.chat              import chat_bp

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
    app.register_blueprint(transport_bp,     url_prefix='/transport')
    app.register_blueprint(inventory_bp,     url_prefix='/inventory')
    app.register_blueprint(mobile_api_bp,          url_prefix='/api/mobile/v1')
    app.register_blueprint(attendance_devices_bp,  url_prefix='/attendance-devices')
    app.register_blueprint(school_calendar_bp,     url_prefix='/school-calendar')
    app.register_blueprint(homework_bp,            url_prefix='/homework')
    app.register_blueprint(chat_bp,                url_prefix='/chat')

    # ── Jinja2 globals ────────────────────────────────────────────────────────
    from app.utils.helpers import resolve_photo_url
    from werkzeug.routing import BuildError as _BuildError

    def _safe_url_for(endpoint, **values):
        from flask import url_for as _url_for
        try:
            return _url_for(endpoint, **values)
        except (_BuildError, Exception):
            return '#'

    app.jinja_env.globals.update(enumerate=enumerate, zip=zip, len=len,
                                 resolve_photo_url=resolve_photo_url,
                                 safe_url_for=_safe_url_for)

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

        skip_db_context = (
            request.endpoint is None
            or request.endpoint == 'static'
            or request.path.startswith('/static/')
        )

        if not skip_db_context:
            if current_user.is_authenticated:
                try:
                    from app.models import Notification, NotificationRead
                    from app.utils.notification_visibility import notification_visible_to
                    read_ids = db.session.query(NotificationRead.notification_id)\
                                 .filter_by(user_id=current_user.id).subquery()
                    unread_count = Notification.query\
                        .filter(notification_visible_to(current_user))\
                        .filter(Notification.id.notin_(read_ids))\
                        .count()
                except Exception:
                    db.session.rollback()
                    unread_count = 0

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

    # ── Upload folder ─────────────────────────────────────────────────────────
    uploads_dir = os.path.join(app.root_path, 'static', 'uploads')
    os.makedirs(uploads_dir, exist_ok=True)

    # ── Background services (web server only — skipped during CLI commands) ────
    # Flask CLI commands like `flask db upgrade`, `flask shell`, and `flask routes`
    # must not start background threads: they hold DB connections, interfere with
    # migrations, and fail when the schema is incomplete.
    import sys as _sys
    _cli_cmd = _sys.argv[1] if len(_sys.argv) >= 2 else ''
    _skip_schedulers = _cli_cmd in ('db', 'shell', 'routes', 'digest', 'collect')

    if not _skip_schedulers:
        from app.services.hikvision import start_auto_sync
        start_auto_sync(app)

        from app.services.ai_face_ws import start_ai_face_ws_server
        start_ai_face_ws_server(app)

        from app.services.auto_attendance import start_auto_attendance_scheduler
        start_auto_attendance_scheduler(app)

        from app.services.fee_reminder import start_fee_reminder_scheduler
        start_fee_reminder_scheduler(app)

    return app
