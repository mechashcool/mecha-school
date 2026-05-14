"""
Al-Muhandis School Management System
Application Factory  (Phase 6: Multi-Tenant)
"""
import os
from flask import Flask
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

    # ── Jinja2 globals ────────────────────────────────────────────────────────
    app.jinja_env.globals.update(enumerate=enumerate, zip=zip, len=len)

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
        from app.models import School, SchoolSettings
        from app.utils.decorators import get_current_school, get_active_year

        unread_count = 0
        if current_user.is_authenticated:
            from app.models import Notification, NotificationRead
            from app.utils.notification_visibility import notification_visible_to
            read_ids = db.session.query(NotificationRead.notification_id)\
                         .filter_by(user_id=current_user.id).subquery()
            unread_count = Notification.query\
                .filter(notification_visible_to(current_user))\
                .filter(Notification.id.notin_(read_ids))\
                .count()

        # Current school for the request.
        # `school` keeps the same name used throughout all existing templates
        # (school.school_name, school.logo_path, etc.).
        current_school   = None
        active_year      = None   # VIEW year (may be historical); for display
        real_active_year = None   # CURRENT active year only; for write-guards
        all_schools      = []
        all_school_years = []     # all years for this school; for year-switcher

        try:
            if current_user.is_authenticated:
                from app.utils.decorators import get_view_year
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
                    from app.models import AcademicYear
                    all_school_years = (
                        AcademicYear.query
                        .execution_options(bypass_tenant_scope=True)
                        .filter_by(school_id=current_school.id)
                        .order_by(AcademicYear.start_date.desc())
                        .all()
                    )
        except Exception:
            pass

        # Fallback to legacy SchoolSettings so old templates still work
        # if no School rows exist yet (e.g., during a fresh install before seeding).
        if current_school is None:
            try:
                current_school = SchoolSettings.get()
            except Exception:
                pass

        # True when a school user is viewing a historical (non-current) year.
        # Injected into all templates so they can hide write-action buttons.
        is_historical_year = (
            active_year is not None
            and real_active_year is not None
            and active_year.id != real_active_year.id
        )

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
        )

    # ── Error handlers ────────────────────────────────────────────────────────
    @app.errorhandler(403)
    def forbidden(e):
        from flask import render_template
        return render_template('shared/403.html'), 403

    @app.errorhandler(404)
    def not_found(e):
        from flask import render_template
        return render_template('shared/404.html'), 404

    @app.errorhandler(500)
    def server_error(e):
        from flask import render_template
        return render_template('shared/500.html'), 500

    # ── Upload folder ─────────────────────────────────────────────────────────
    uploads_dir = os.path.join(app.root_path, 'static', 'uploads')
    os.makedirs(uploads_dir, exist_ok=True)

    return app
