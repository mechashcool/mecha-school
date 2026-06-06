"""
Mecha-School ERP — Database seeder
===================================

Run after `flask create-db` to populate baseline data:
    * Permissions (including Phase 1 additions)
    * Roles      (admin, accountant, teacher, hr, reception, parent)
    * Default admin user (admin / Admin@1234)
    * Exam types (Monthly / Midterm / Final)
    * Revenue & expense categories (General Income + Expenses)
    * Default SchoolSettings row (white-label)
    * A sample RFID device (dev only — skipped if any device exists)

CLI entry points registered via `register_commands(app)`:
    flask create-db     — create all tables
    flask seed          — idempotent full seed
    flask reset-db      — DROP + CREATE + SEED (destructive, confirms)
    flask rotate-device-key <device_id>  — regenerate an ESP32 api_key
"""
import secrets
from datetime import date

import click
from flask.cli import with_appcontext

from app.models import (
    db, Role, Permission, User,
    ExamType, RevenueCategory, ExpenseCategory,
    SchoolSettings, Device, AcademicYear, School,
)


# ──────────────────────────────────────────────────────────────────────────
#  PERMISSIONS
# ──────────────────────────────────────────────────────────────────────────

ALL_PERMISSIONS = [
    # Students
    ('view_students',         'عرض الطلاب',                 'الطلاب'),
    ('add_student',           'إضافة طالب',                  'الطلاب'),
    ('edit_student',          'تعديل بيانات طالب',           'الطلاب'),
    ('delete_student',        'حذف طالب',                    'الطلاب'),
    ('manage_rfid',           'إدارة بطاقات RFID',            'الطلاب'),

    # Fees
    ('manage_fees',           'إدارة الرسوم الدراسية',       'المالية'),
    ('record_payments',       'تسجيل المدفوعات',              'المالية'),
    ('manage_revenues',       'إدارة الإيرادات العامة',       'المالية'),
    ('manage_expenses',       'إدارة المصروفات',              'المالية'),

    # HR
    ('manage_employees',      'إدارة الموظفين',               'الموارد البشرية'),
    ('manage_salaries',       'إدارة الرواتب',                'الموارد البشرية'),

    # Academic
    ('take_attendance',       'تسجيل الحضور والغياب',        'الأكاديمي'),
    ('enter_grades',          'إدخال الدرجات',                'الأكاديمي'),
    ('manage_schedules',      'إدارة الجداول',                'الأكاديمي'),
    ('print_schedules',       'طباعة الجداول',                'الأكاديمي'),

    # Reporting & Audit
    ('view_reports',          'عرض التقارير',                 'التقارير'),
    ('view_audit_log',        'عرض سجل التدقيق',              'التقارير'),

    # Notifications / Broadcast
    ('manage_notifications',  'إدارة الإشعارات',              'النظام'),
    ('send_broadcast',        'عرض سجل إعلانات أولياء الأمور السابقة', 'النظام'),

    # System / White-label
    ('manage_settings',       'إدارة إعدادات النظام',         'النظام'),
    ('manage_white_label',    'التخصيص (الاسم والشعار)',      'النظام'),
    ('manage_users',          'إدارة المستخدمين',             'النظام'),
    ('manage_devices',        'إدارة أجهزة RFID',             'النظام'),
    ('edit_user_profiles',    'تعديل بيانات ملفات المستخدمين', 'النظام'),
    ('manage_attendance_settings', 'إدارة إعدادات الحضور',       'النظام'),

    # Parent self-service (granted automatically to the 'parent' role)
    ('parent_view_child',     'ولي الأمر: عرض بيانات الأبناء', 'ولي الأمر'),
]


# ──────────────────────────────────────────────────────────────────────────
#  ROLES
# ──────────────────────────────────────────────────────────────────────────

ROLE_PRESETS = {
    'super_admin': {
        'label':       'Super Admin',
        'description': 'Full system owner access across all schools.',
        'is_admin':    True,
        'permissions': [],   # super_admin bypasses all permission checks
    },
    'school_admin': {
        'label':       'School Manager',
        'description': 'School-level manager scoped to one school.',
        'is_admin':    False,
        'permissions': [],   # school_admin bypasses school-level permission checks
    },
    'admin': {
        'label':       'مسؤول النظام',
        'description': 'صلاحيات كاملة على جميع الأقسام',
        'is_admin':    False,
        'permissions': [],
    },
    'accountant': {
        'label':       'محاسب',
        'description': 'يُدير الرسوم والإيرادات والمصروفات',
        'is_admin':    False,
        'permissions': [
            'view_students', 'manage_fees', 'record_payments',
            'manage_revenues', 'manage_expenses', 'view_reports',
            'view_audit_log',
        ],
    },
    'teacher': {
        'label':       'معلم',
        'description': 'يُسجّل الحضور ويُدخل الدرجات',
        'is_admin':    False,
        'permissions': ['view_students', 'take_attendance', 'enter_grades',
                        'print_schedules'],
    },
    'hr': {
        'label':       'موارد بشرية',
        'description': 'يُدير بيانات الموظفين والرواتب',
        'is_admin':    False,
        'permissions': ['manage_employees', 'manage_salaries', 'view_reports'],
    },
    'reception': {
        'label':       'موظف استقبال',
        'description': 'يُدير بيانات الطلاب والإشعارات',
        'is_admin':    False,
        'permissions': ['view_students', 'add_student', 'edit_student',
                        'manage_notifications', 'manage_rfid'],
    },
    'parent': {
        'label':       'ولي أمر',
        'description': 'يرى بيانات أبنائه فقط (بوابة أولياء الأمور)',
        'is_admin':    False,
        'permissions': ['parent_view_child'],
    },
}


# ──────────────────────────────────────────────────────────────────────────
#  REFERENCE DATA
# ──────────────────────────────────────────────────────────────────────────

DEFAULT_EXAM_TYPES = [
    ('شهري',        25),
    ('نصف الفصل',   35),
    ('نهائي',       40),
]

DEFAULT_REVENUE_CATEGORIES = [
    'رسوم دراسية',   # Student Fees
    'بطاقات هوية',   # ID Cards
    'كتب',           # Books
    'زي مدرسي',      # Uniforms
    'نقل',           # Transport
    'أنشطة',         # Activities
    'متفرقات',       # Miscellaneous
]

DEFAULT_EXPENSE_CATEGORIES = [
    ('إيجار',          False),   # Rent
    ('كهرباء',         False),   # Electricity
    ('ماء',            False),   # Water
    ('إنترنت',         False),   # Internet
    ('صيانة',          False),   # Maintenance
    ('قرطاسية',        False),   # Stationery
    ('نظافة',          False),   # Cleaning
    ('نقل',            False),   # Transport
    ('رواتب',          True),    # Salaries — SYSTEM (auto-created from payroll)
    ('مصاريف أخرى',    False),   # Other
]


# ──────────────────────────────────────────────────────────────────────────
#  SEED FUNCTIONS  (each is idempotent)
# ──────────────────────────────────────────────────────────────────────────

def seed_permissions_and_roles():
    """Create/refresh permissions, roles, and the default admin user."""
    click.echo('── Seeding permissions...')
    perm_map = {}
    for name, label, category in ALL_PERMISSIONS:
        p = Permission.query.filter_by(name=name).first()
        if not p:
            p = Permission(name=name, label=label, category=category)
            db.session.add(p)
            db.session.flush()
        else:
            # Refresh label/category in case they changed in code
            p.label = label
            p.category = category
        perm_map[name] = p

    click.echo('── Seeding roles...')
    role_map = {}
    for role_name, info in ROLE_PRESETS.items():
        r = Role.query.filter_by(name=role_name).first()
        if not r:
            r = Role(
                name=role_name,
                label=info['label'],
                description=info['description'],
                is_admin=info['is_admin'],
            )
            db.session.add(r)
            db.session.flush()
        else:
            r.label       = info['label']
            r.description = info['description']
            r.is_admin    = info['is_admin']
        r.permissions = [perm_map[p] for p in info['permissions'] if p in perm_map]
        role_map[role_name] = r

    click.echo('── Seeding default admin user...')
    legacy_admin = role_map.get('admin')
    if legacy_admin:
        User.query.filter(
            User.role_id == legacy_admin.id,
            User.school_id == None,
        ).update({'role_id': role_map['super_admin'].id}, synchronize_session=False)
        User.query.filter(
            User.role_id == legacy_admin.id,
            User.school_id != None,
        ).update({'role_id': role_map['school_admin'].id}, synchronize_session=False)

    admin = User.query.filter_by(username='admin').first()
    if not admin:
        admin = User(
            username='admin',
            email='admin@mecha-school.local',
            full_name='مسؤول النظام',
            role=role_map['super_admin'],
            school_id=None,
            is_active=True,
        )
        admin.set_password('Admin@1234')
        db.session.add(admin)
        click.echo('   ✓ Created admin / Admin@1234')
    else:
        admin.role = role_map['super_admin']
        admin.school_id = None
        admin.is_active = True
        click.echo('   ↷ Admin user already exists.')

    db.session.commit()


def seed_exam_types():
    """Seed default exam types if none exist."""
    if ExamType.query.count() > 0:
        return
    click.echo('── Seeding exam types...')
    for name, weight in DEFAULT_EXAM_TYPES:
        db.session.add(ExamType(name=name, weight=weight))
    db.session.commit()


def seed_default_categories():
    """Seed default finance categories for every school."""
    schools = School.query.all()
    if not schools:
        return

    click.echo('── Seeding revenue categories...')
    for school in schools:
        for name in DEFAULT_REVENUE_CATEGORIES:
            cat = (
                RevenueCategory.query.execution_options(bypass_tenant_scope=True)
                .filter_by(name=name, school_id=school.id)
                .first()
            )
            if not cat:
                db.session.add(RevenueCategory(name=name, school_id=school.id))

    click.echo('── Seeding expense categories...')
    for school in schools:
        for name, is_system in DEFAULT_EXPENSE_CATEGORIES:
            cat = (
                ExpenseCategory.query.execution_options(bypass_tenant_scope=True)
                .filter_by(name=name, school_id=school.id)
                .first()
            )
            if not cat:
                db.session.add(ExpenseCategory(
                    name=name,
                    school_id=school.id,
                    is_system=is_system,
                ))
            else:
                cat.is_system = is_system
    db.session.commit()


def seed_school_settings():
    """Ensure the single SchoolSettings row exists (legacy fallback)."""
    if SchoolSettings.query.count() == 0:
        click.echo('── Seeding default SchoolSettings (white-label) ...')
        db.session.add(SchoolSettings(
            school_name='Mecha-School',
            school_name_ar='مدرسة المهندس',
            primary_color='#0d6efd',
            currency_code='IQD',
            currency_symbol='د.ع',
            timezone='Asia/Baghdad',
            locale='ar',
            receipt_footer='شكراً لثقتكم بنا — Mecha-School ERP',
        ))
        db.session.commit()


def seed_default_school():
    """
    Ensure at least one School row exists.
    If SchoolSettings has data, mirror it into the first school.
    """
    if School.query.count() > 0:
        return
    click.echo('── Seeding default School (multi-tenant root)...')
    # Try to import settings from legacy SchoolSettings
    settings = SchoolSettings.query.first()
    school = School(
        school_name     = (settings.school_name if settings else 'Mecha-School'),
        school_name_ar  = (settings.school_name_ar if settings else 'مدرسة المهندس'),
        capacity        = 0,
        primary_color   = (settings.primary_color if settings else '#0d6efd'),
        currency_code   = (settings.currency_code if settings else 'IQD'),
        currency_symbol = (settings.currency_symbol if settings else 'د.ع'),
        timezone        = (settings.timezone if settings else 'Asia/Baghdad'),
        locale          = (settings.locale if settings else 'ar'),
        logo_path       = (settings.logo_path if settings else None),
        receipt_footer  = (settings.receipt_footer if settings else 'شكراً لثقتكم بنا — Mecha-School ERP'),
        is_active       = True,
    )
    db.session.add(school)
    db.session.commit()

    # Link academic years and users to this school if they have no school_id yet
    AcademicYear.query.filter_by(school_id=None).update({'school_id': school.id})
    # Don't change admin user's school_id (it should stay NULL)
    from app.models import User
    User.query.filter(User.school_id == None,
                      User.username != 'admin').update({'school_id': school.id})
    db.session.commit()
    click.echo(f'   ✓ Created default school id={school.id} "{school.school_name}"')


def seed_sample_device():
    """Create one sample RFID device in dev so the hardware endpoint is testable."""
    if Device.query.count() > 0:
        return
    click.echo('── Seeding sample RFID device (dev-only)...')
    api_key = secrets.token_urlsafe(32)
    school = School.query.first()
    dev = Device(
        device_id='ESP32-MAIN-GATE-01',
        name='Main Gate Reader',
        location='Building A — Main Gate',
        api_key=api_key,
        purpose='attendance',
        is_active=True,
        firmware='1.0.0',
        school_id=school.id if school else None,
    )
    db.session.add(dev)
    db.session.commit()
    click.echo(f'   ✓ Device ESP32-MAIN-GATE-01 created.')
    click.echo(f'   🔑 API key (save this — shown once): {api_key}')


def seed_default_academic_year():
    """Create a current academic year for the default school if none exists."""
    if AcademicYear.query.count() > 0:
        return
    # Get (or create) the default school first
    school = School.query.first()
    if not school:
        return  # seed_default_school() hasn't run yet
    today = date.today()
    # If we're past August, current year is this year → next; else prev → this
    if today.month >= 8:
        start_year, end_year = today.year, today.year + 1
    else:
        start_year, end_year = today.year - 1, today.year
    click.echo(f'── Seeding default academic year {start_year}-{end_year}...')
    db.session.add(AcademicYear(
        school_id  = school.id,
        name       = f'{start_year}-{end_year}',
        start_date = date(start_year, 8, 1),
        end_date   = date(end_year, 6, 30),
        is_current = True,
    ))
    db.session.commit()


# ──────────────────────────────────────────────────────────────────────────
#  SEQUENCE SYNC  (PostgreSQL-only, no-op on other engines)
# ──────────────────────────────────────────────────────────────────────────

def sync_sequences():
    """
    Reset every auto-increment sequence to MAX(id)+1 so that the next
    INSERT never collides with rows that were inserted with explicit IDs
    (e.g. during seeding or a data restore).

    Introspects pg_sequences for sequences whose name follows the SQLAlchemy
    convention  <tablename>_id_seq  and that are attached to a table that
    exists in the current database.  Safe to call multiple times.
    """
    from sqlalchemy import inspect as sa_inspect
    dialect = db.engine.dialect.name
    if dialect != 'postgresql':
        return  # sequences are a PostgreSQL concept

    click.echo('── Syncing PostgreSQL sequences...')
    inspector = sa_inspect(db.engine)
    table_names = inspector.get_table_names()

    with db.engine.connect() as conn:
        for table in table_names:
            seq_name = f'{table}_id_seq'
            # Check the sequence actually exists before trying to set it
            exists = conn.execute(
                db.text(
                    "SELECT 1 FROM pg_sequences WHERE schemaname='public' AND sequencename=:seq"
                ),
                {'seq': seq_name},
            ).fetchone()
            if not exists:
                continue
            conn.execute(
                db.text(
                    f"SELECT setval('{seq_name}',"
                    f" COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
                )
            )
            click.echo(f'   ✓ {seq_name}')
        conn.commit()


# ──────────────────────────────────────────────────────────────────────────
#  TOP-LEVEL SEED (idempotent)
# ──────────────────────────────────────────────────────────────────────────

def seed_all():
    seed_permissions_and_roles()
    seed_exam_types()
    seed_school_settings()
    seed_default_school()          # Phase 6: must come before academic year
    seed_default_categories()
    seed_default_academic_year()
    seed_sample_device()
    sync_sequences()
    click.echo('✓ Full seed complete.')


# Back-compat alias used by older code paths
def seed_database():
    seed_permissions_and_roles()


# ──────────────────────────────────────────────────────────────────────────
#  CLI COMMANDS
# ──────────────────────────────────────────────────────────────────────────

def register_commands(app):
    @app.cli.command('seed')
    @with_appcontext
    def seed_cmd():
        """Seed the database with initial data (idempotent)."""
        seed_all()

    @app.cli.command('create-db')
    @with_appcontext
    def create_db_cmd():
        """Create all database tables."""
        db.create_all()
        click.echo('✓ Tables created.')

    @app.cli.command('reset-db')
    @with_appcontext
    def reset_db_cmd():
        """Drop and recreate all tables (DANGEROUS)."""
        if click.confirm('⚠️  This will DELETE all data. Continue?'):
            db.drop_all()
            db.create_all()
            seed_all()
            click.echo('✓ Database reset and seeded.')

    @app.cli.command('fix-admin')
    @with_appcontext
    def fix_admin_cmd():
        """Ensure the admin role has is_admin=True and the admin user is active."""
        super_role = Role.query.filter_by(name='super_admin').first()
        if not super_role:
            click.echo('super_admin role not found - run: flask seed')
            return
        user = User.query.filter_by(username='admin').first()
        if user:
            user.role = super_role
            user.school_id = None
            user.is_active = True
            db.session.commit()
            click.echo('[DONE] admin user now has role=super_admin and school_id=NULL.')
        else:
            click.echo('No user with username=admin found')
        return
        role = Role.query.filter_by(name='admin').first()
        if not role:
            click.echo('✗ Admin role not found — run: flask seed')
            return
        if False:
            role.name = role.name
            click.echo('   [OK] Set admin role is_admin=True')
        else:
            click.echo('   [--] admin role already has is_admin=True')

        user = User.query.filter_by(username='admin').first()
        if user:
            if not user.is_active:
                user.is_active = True
                click.echo('   [OK] Re-activated admin user')
            actual_role = user.role
            if False:
                actual_role.name = actual_role.name
                click.echo('   [OK] Set is_admin=True on user\'s actual role: %r' % actual_role.name)
            click.echo('   [--] Admin user: %r / role: %r / is_admin: %r / active: %r' % (
                user.username, actual_role.name if actual_role else None,
                actual_role.name if actual_role else None, user.is_active))
        else:
            click.echo('   ✗ No user with username=admin found')

        db.session.commit()
        click.echo('[DONE] fix-admin complete.')

    @app.cli.command('sync-sequences')
    @with_appcontext
    def sync_sequences_cmd():
        """Reset all PostgreSQL sequences to MAX(id)+1 (run after bulk inserts)."""
        sync_sequences()
        click.echo('✓ Sequences synced.')

    @app.cli.command('cleanup-chat-test-schools')
    @click.option('--execute', is_flag=True, default=False,
                  help='Actually delete matched schools. Default is dry-run.')
    @with_appcontext
    def cleanup_chat_test_schools_cmd(execute):
        """Find and optionally delete test schools created by automated chat tests.

        Targets schools whose names match:
          "Chat Admin School A <hex-suffix>"
          "Chat Admin School B <hex-suffix>"

        Dry-run is the default. Pass --execute to perform deletion.
        Schools that contain students, employees, fees, or attendance records
        are reported but never auto-deleted.
        """
        from sqlalchemy import or_
        from app.utils.school_cleanup import cleanup_school_cascade
        from app.models import (
            ChatRoom, User, Student, Employee, AcademicYear,
            FeeRecord, StudentAttendance, EmployeeAttendance,
        )

        _REAL_MODELS = [
            ('Users',               User),
            ('Students',            Student),
            ('Employees',           Employee),
            ('Academic years',      AcademicYear),
            ('Fee records',         FeeRecord),
            ('Student attendance',  StudentAttendance),
            ('Employee attendance', EmployeeAttendance),
        ]
        _REAL_KEYS = {label for label, _ in _REAL_MODELS}

        schools = (
            School.query
            .execution_options(bypass_tenant_scope=True)
            .filter(
                or_(
                    School.school_name.like('Chat Admin School A %'),
                    School.school_name.like('Chat Admin School B %'),
                )
            )
            .order_by(School.id)
            .all()
        )

        if not schools:
            click.echo('No test schools found matching "Chat Admin School A/B *".')
            return

        click.echo(f'\nFound {len(schools)} matched school(s):\n')

        safe = []
        unsafe = []

        for school in schools:
            counts = {}
            for label, model in _REAL_MODELS:
                n = (
                    model.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(school_id=school.id)
                    .count()
                )
                if n:
                    counts[label] = n

            chat_rooms = (
                ChatRoom.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=school.id)
                .count()
            )

            click.echo(f'  School ID  : {school.id}')
            click.echo(f'  Name       : {school.school_name}')
            click.echo(f'  Created    : {school.created_at or "unknown"}')
            click.echo(f'  Chat rooms : {chat_rooms}')
            if counts:
                click.echo(f'  Linked     : {", ".join(f"{k}: {v}" for k, v in counts.items())}')
            else:
                click.echo(f'  Linked     : none')

            real_keys = {'Students', 'Employees', 'Fee records',
                         'Student attendance', 'Employee attendance'}
            has_real = bool(real_keys & set(counts.keys()))
            if has_real:
                click.echo(f'  Status     : UNSAFE - real data present, will NOT be deleted\n')
                unsafe.append(school)
            else:
                click.echo(f'  Status     : SAFE - only test/chat records\n')
                safe.append(school)

        click.echo(f'Matched  : {len(schools)}')
        click.echo(f'Safe     : {len(safe)}')
        click.echo(f'Skipped  : {len(unsafe)} (real data present)')

        if not execute:
            click.echo('\n[DRY RUN] No changes made.')
            click.echo('  Dry-run : flask cleanup-chat-test-schools')
            click.echo('  Execute : flask cleanup-chat-test-schools --execute')
            return

        if not safe:
            click.echo('\nNothing safe to delete.')
            return

        click.echo(f'\nDeleting {len(safe)} school(s)...')
        for school in safe:
            sid, name = school.id, school.school_name
            try:
                cleanup_school_cascade(sid)
                db.session.commit()
                click.echo(f'  ✓ Deleted id={sid}  {name!r}')
            except Exception as exc:
                db.session.rollback()
                click.echo(f'  ✗ FAILED  id={sid}  {name!r}: {exc}')

        click.echo(f'\nDone. {len(safe)} school(s) processed.')

    @app.cli.command('cleanup-test-data')
    @click.option('--execute', is_flag=True, default=False,
                  help='Actually delete matched records. Default is dry-run.')
    @with_appcontext
    def cleanup_test_data_cmd(execute):
        """Find and optionally delete test schools and test students.

        Test school patterns (name must match exactly):
          "Chat Admin School A <hex>", "Chat Admin School B <hex>"
          "Parent Request A <hex>",    "Parent Request B <hex>"

        Test student patterns (strict prefix match):
          full_name starts with "Own Student " or "Other Student "
          student_id starts with "PRA-" or "PRB-"

        Dry-run is the default. Pass --execute to perform deletion.
        Schools with real-looking student data are skipped automatically.
        """
        import re as _re
        from sqlalchemy import or_, text as sql_text
        from app.utils.school_cleanup import cleanup_school_cascade
        from app.models import (
            ChatRoom, User, Student, Employee,
            FeeRecord, StudentAttendance, Complaint, LeaveRequest,
            Exam, Notification, StudentRegistrationRecord,
        )

        _SCHOOL_PATTERNS = [
            'Chat Admin School A %',
            'Chat Admin School B %',
            'Parent Request A %',
            'Parent Request B %',
            'RegRec School A %',
            'RegRec School B %',
        ]
        _STUDENT_NAME_PREFIXES = ('Own Student ', 'Other Student ')
        _STUDENT_CODE_PREFIXES = ('PRA-', 'PRB-')

        # Student names generated by RegRec test fixtures:
        #   "Ahmed Test <hex>"  and  "School B Student <hex>"
        # Hex suffix must be 6–32 lowercase hex chars — prevents matching real names.
        _REGREC_STUDENT_RE = _re.compile(
            r'^(?:Ahmed Test|School B Student) [0-9a-f]{6,32}$',
            _re.IGNORECASE,
        )

        def _is_test_student_record(stu):
            name = stu.full_name or ''
            code = stu.student_id or ''
            return (
                any(name.startswith(p) for p in _STUDENT_NAME_PREFIXES) or
                any(code.startswith(p) for p in _STUDENT_CODE_PREFIXES) or
                bool(_REGREC_STUDENT_RE.match(name))
            )

        def _count(model, school_id):
            return (
                model.query
                .execution_options(bypass_tenant_scope=True)
                .filter(model.school_id == school_id)
                .count()
            )

        mode = 'EXECUTE' if execute else 'DRY RUN'
        click.echo(f'\n{"=" * 64}')
        click.echo(f' cleanup-test-data -- {mode}')
        click.echo(f'{"=" * 64}\n')

        # ── Phase 1: test schools ────────────────────────────────────────────
        school_filter = or_(*[School.school_name.like(p) for p in _SCHOOL_PATTERNS])
        test_schools = (
            School.query
            .execution_options(bypass_tenant_scope=True)
            .filter(school_filter)
            .order_by(School.id)
            .all()
        )

        click.echo(f'PHASE 1 -- Test schools ({len(test_schools)} matched)\n')

        safe_school_ids = []
        unsafe_school_ids = []

        for school in test_schools:
            sid = school.id
            students = (
                Student.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=sid)
                .all()
            )
            real_students = [s for s in students if not _is_test_student_record(s)]

            click.echo(f'  School ID  : {sid}')
            click.echo(f'  Name       : {school.school_name}')
            click.echo(f'  Created    : {school.created_at or "unknown"}')
            click.echo(f'  Users      : {_count(User, sid)}')
            click.echo(f'  Students   : {len(students)}  (real-looking: {len(real_students)})')
            click.echo(f'  Reg records: {_count(StudentRegistrationRecord, sid)}')
            click.echo(f'  Employees  : {_count(Employee, sid)}')
            click.echo(f'  Chat rooms : {_count(ChatRoom, sid)}')
            click.echo(f'  Complaints : {_count(Complaint, sid)}')
            click.echo(f'  Leave reqs : {_count(LeaveRequest, sid)}')
            click.echo(f'  Attendance : {_count(StudentAttendance, sid)}')
            click.echo(f'  Fees       : {_count(FeeRecord, sid)}')
            click.echo(f'  Exams      : {_count(Exam, sid)}')
            click.echo(f'  Notifs     : {_count(Notification, sid)}')

            if real_students:
                names = ', '.join(repr(s.full_name) for s in real_students[:3])
                click.echo(f'  Status     : UNSAFE -- real-looking students: {names}')
                unsafe_school_ids.append(sid)
            else:
                click.echo(f'  Status     : SAFE')
                safe_school_ids.append(sid)
            click.echo()

        all_test_school_ids = {s.id for s in test_schools}

        click.echo(f'  Matched  : {len(test_schools)}')
        click.echo(f'  Safe     : {len(safe_school_ids)}')
        click.echo(f'  Skipped  : {len(unsafe_school_ids)} (real data detected)')

        # ── Phase 2: orphaned test students not in any matched test school ───
        student_filter = or_(
            Student.full_name.like('Own Student %'),
            Student.full_name.like('Other Student %'),
            Student.student_id.like('PRA-%'),
            Student.student_id.like('PRB-%'),
        )
        orphan_q = (
            Student.query
            .execution_options(bypass_tenant_scope=True)
            .filter(student_filter)
        )
        if all_test_school_ids:
            orphan_q = orphan_q.filter(Student.school_id.notin_(all_test_school_ids))
        orphaned = orphan_q.order_by(Student.id).all()

        click.echo(f'\nPHASE 2 -- Orphaned test students ({len(orphaned)} found)\n')

        orphan_ids = []

        for s in orphaned:
            c_n  = (Complaint.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(student_id=s.id).count())
            lr_n = (LeaveRequest.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(student_id=s.id).count())
            att_n = (StudentAttendance.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter_by(student_id=s.id).count())
            fee_n = (FeeRecord.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter_by(student_id=s.id).count())
            par_n = db.session.execute(
                sql_text('SELECT COUNT(*) FROM parent_students WHERE student_id = :sid'),
                {'sid': s.id},
            ).scalar()

            click.echo(f'  PK        : {s.id}')
            click.echo(f'  Code      : {s.student_id}')
            click.echo(f'  Name      : {s.full_name}')
            click.echo(f'  School ID : {s.school_id}')
            click.echo(f'  Parents   : {par_n}')
            click.echo(f'  Complaints: {c_n}')
            click.echo(f'  Leave reqs: {lr_n}')
            click.echo(f'  Attendance: {att_n}')
            click.echo(f'  Fees      : {fee_n}')
            click.echo(f'  Status    : SAFE (matches strict test pattern)')
            click.echo()
            orphan_ids.append(s.id)

        # ── Summary ──────────────────────────────────────────────────────────
        click.echo(f'Summary:')
        click.echo(f'  Safe schools to delete  : {len(safe_school_ids)}')
        click.echo(f'  Skipped schools         : {len(unsafe_school_ids)}')
        click.echo(f'  Orphaned students       : {len(orphan_ids)}')

        if not execute:
            click.echo('\n[DRY RUN] No changes made.')
            click.echo('  Dry-run : flask cleanup-test-data')
            click.echo('  Execute : flask cleanup-test-data --execute')
            return

        # ── Execute: schools ──────────────────────────────────────────────────
        click.echo(f'\nDeleting {len(safe_school_ids)} school(s)...')
        for sid in safe_school_ids:
            school_obj = (School.query
                          .execution_options(bypass_tenant_scope=True)
                          .get(sid))
            name = school_obj.school_name if school_obj else f'id={sid}'
            try:
                cleanup_school_cascade(sid)
                db.session.commit()
                click.echo(f'  OK   school id={sid}  {name!r}')
            except Exception as exc:
                db.session.rollback()
                click.echo(f'  ERR  school id={sid}  {name!r}: {exc}')

        # ── Execute: orphaned students ────────────────────────────────────────
        click.echo(f'\nDeleting {len(orphan_ids)} orphaned student(s)...')
        for sid in orphan_ids:
            try:
                db.session.execute(sql_text(
                    'DELETE FROM parent_students WHERE student_id = :sid'), {'sid': sid})
                db.session.execute(sql_text(
                    'DELETE FROM complaints WHERE student_id = :sid'), {'sid': sid})
                db.session.execute(sql_text(
                    'DELETE FROM leave_requests WHERE student_id = :sid'), {'sid': sid})
                db.session.execute(sql_text(
                    'DELETE FROM student_attendance WHERE student_id = :sid'), {'sid': sid})
                db.session.execute(sql_text(
                    'DELETE FROM fee_installments'
                    ' WHERE fee_record_id IN'
                    ' (SELECT id FROM fee_records WHERE student_id = :sid)'),
                    {'sid': sid})
                db.session.execute(sql_text(
                    'DELETE FROM fee_records WHERE student_id = :sid'), {'sid': sid})
                db.session.execute(sql_text(
                    'DELETE FROM exam_results WHERE student_id = :sid'), {'sid': sid})
                db.session.execute(sql_text(
                    'DELETE FROM student_documents WHERE student_id = :sid'), {'sid': sid})
                db.session.execute(sql_text(
                    'DELETE FROM student_suspensions WHERE student_id = :sid'), {'sid': sid})
                db.session.execute(sql_text(
                    'DELETE FROM students WHERE id = :sid'), {'sid': sid})
                db.session.commit()
                click.echo(f'  OK   student id={sid}')
            except Exception as exc:
                db.session.rollback()
                click.echo(f'  ERR  student id={sid}: {exc}')

        click.echo('\nDone.')

    @app.cli.command('setup-iraqi-grades')
    @click.option('--school-id', required=True, type=int,
                  help='School ID to create standard Iraqi grades for.')
    @click.option('--year-id', default=None, type=int,
                  help='Academic year ID (default: current active year for the school).')
    @with_appcontext
    def setup_iraqi_grades_cmd(school_id, year_id):
        """Create the 12 standard Iraqi school grades for a school's academic year."""
        from app.utils.iraqi_grades import ensure_iraqi_standard_grades

        school = (School.query
                  .execution_options(bypass_tenant_scope=True)
                  .get(school_id))
        if not school:
            click.echo(f'✗ School id={school_id} not found.')
            return

        if year_id:
            year = (AcademicYear.query
                    .execution_options(bypass_tenant_scope=True)
                    .get(year_id))
        else:
            year = (AcademicYear.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(school_id=school_id, is_current=True)
                    .first())
            if not year:
                year = (AcademicYear.query
                        .execution_options(bypass_tenant_scope=True)
                        .filter_by(school_id=school_id)
                        .order_by(AcademicYear.start_date.desc())
                        .first())

        if not year:
            click.echo(f'✗ No academic year found for school_id={school_id}. '
                       f'Create one first or pass --year-id.')
            return

        click.echo(f'School : {school.school_name} (id={school_id})')
        click.echo(f'Year   : {year.name} (id={year.id})')

        result = ensure_iraqi_standard_grades(school_id, year.id)
        db.session.commit()

        click.echo(f'✓ Created : {result["created"]}')
        click.echo(f'  Skipped : {result["skipped"]} (already existed)')

    @app.cli.command('rotate-device-key')
    @click.argument('device_id')
    @with_appcontext
    def rotate_device_key_cmd(device_id):
        """Regenerate the api_key for an RFID device."""
        dev = Device.query.filter_by(device_id=device_id).first()
        if not dev:
            click.echo(f'✗ No device with device_id={device_id!r}')
            return
        new_key = secrets.token_urlsafe(32)
        dev.api_key = new_key
        db.session.commit()
        click.echo(f'✓ New api_key for {device_id}: {new_key}')
