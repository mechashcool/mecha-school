"""
Mecha-School — Complete permission catalog for the existing roles system.

This module does NOT introduce a new permission architecture. It only:

  * defines the complete, organized catalog of grantable permissions
    (PERMISSION_GROUPS) covering every active feature surface, reusing every
    existing seeded permission name and the existing permissions /
    role_permissions tables;
  * syncs that catalog into the permissions table (sync_catalog_permissions),
    so the existing role create/edit screens can offer it;
  * defines the ordered landing-page list (LANDING_PAGES) used after login to
    send a user to the FIRST page their role's permissions authorize — the
    dashboard only when view_dashboard is granted.

Enforcement stays with the existing helpers: route decorators
(permission_required / any_permission_required) and template checks
(current_user.has_permission). Admin tiers (super_admin / school_admin)
bypass permission checks exactly as before via User.has_permission().

Permissions are pure CAPABILITY flags: they never widen data scope. School,
tenant and academic-year isolation is enforced independently by the ORM
tenant guards (app/utils/scoping.py) and per-route ownership checks.
"""
from __future__ import annotations

# Built-in role names. Role management may edit their permission sets (except
# the protected admin tiers) but can never rename, recreate, or delete them.
BUILTIN_ROLE_NAMES = frozenset({
    'super_admin', 'school_admin', 'admin', 'accountant', 'teacher',
    'hr', 'reception', 'parent', 'investor_viewer',
})


# ─── Permission catalog ──────────────────────────────────────────────────────
# (group_label, [(permission_name, permission_label), ...])
# Reuses every existing seeded permission name; new names extend coverage to
# every active feature surface of the platform. Order defines display order in
# the role editor.

PERMISSION_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ('لوحة التحكم', [
        ('view_dashboard',          'عرض لوحة التحكم والإحصائيات'),
    ]),
    ('الطلاب', [
        ('view_students',           'عرض الطلاب والبحث والتصدير وسجل القيد'),
        ('view_student_records',    'سجل قيد الطالب'),
        ('add_student',             'إضافة طالب'),
        ('edit_student',            'تعديل بيانات طالب'),
        ('delete_student',          'حذف / أرشفة طالب'),
        ('manage_residential_areas', 'إدارة مناطق السكن'),
        ('manage_rfid',             'إدارة بطاقات RFID'),
    ]),
    ('الصفوف والشعب والمواد', [
        ('view_sections',           'عرض الصفوف والشعب'),
        ('manage_sections',         'إدارة الصفوف والشعب (إضافة/تعديل/حذف)'),
        ('view_subjects',           'عرض المواد الدراسية'),
        ('manage_subjects',         'إدارة المواد الدراسية (إضافة/تعديل/حذف)'),
    ]),
    ('الجداول الدراسية', [
        ('view_schedules',          'عرض الجداول الدراسية'),
        ('manage_schedules',        'إدارة الجداول (إضافة/حذف)'),
        ('print_schedules',         'طباعة الجداول'),
    ]),
    ('الحضور والانضباط', [
        ('take_attendance',         'تسجيل الحضور والغياب وعرض تقاريره'),
        ('manage_suspensions',      'إيقاف الطلاب (إدارة الإيقافات)'),
        ('manage_attendance_settings', 'إدارة إعدادات الحضور والشفتات'),
        ('manage_calendar',         'إدارة التقويم الدراسي والعطل'),
        ('view_attendance_devices', 'عرض أجهزة الحضور وسجلاتها'),
        ('manage_attendance_devices', 'إدارة أجهزة الحضور (إضافة/تعديل/حذف/مزامنة)'),
    ]),
    ('الدرجات والاختبارات', [
        ('enter_grades',            'إدخال الدرجات وإدارة الاختبارات والتقارير'),
    ]),
    ('الواجبات', [
        ('manage_homework',         'إدارة الواجبات المدرسية'),
    ]),
    ('المالية', [
        ('manage_fees',             'إدارة الرسوم الدراسية والأقساط'),
        ('record_payments',         'تسجيل المدفوعات وطباعة الإيصالات'),
        ('manage_revenues',         'إدارة الإيرادات العامة'),
        ('manage_expenses',         'إدارة المصروفات'),
    ]),
    ('الموارد البشرية', [
        ('manage_employees',        'إدارة الموظفين والتقييمات وحضور الموظفين'),
        ('manage_salaries',         'إدارة الرواتب'),
    ]),
    ('الخدمات المدرسية', [
        ('manage_transport',        'إدارة خطوط النقل'),
        ('view_inventory',          'عرض المخازن وتقاريرها'),
        ('manage_inventory',        'إدارة المخازن (أصناف/حركات/جرد)'),
        ('manage_buildings',        'إدارة البنايات'),
    ]),
    ('التواصل', [
        ('view_notifications',      'عرض الإشعارات الواردة'),
        ('manage_notifications',    'إنشاء وحذف الإشعارات'),
        ('send_broadcast',          'إعلانات أولياء الأمور (البث)'),
        ('use_chat',                'المشاركة في المراسلات (غرفه فقط)'),
        ('manage_chat',             'إدارة المراسلات (الغرف والأعضاء والإعدادات)'),
        ('manage_complaints',       'إدارة الشكاوى والرد عليها'),
        ('manage_school_board',     'إدارة لوحة المدرسة (فيديوهات/إعلانات)'),
    ]),
    ('طلبات الإجازة', [
        ('manage_leave_requests',   'إدارة طلبات إجازات الطلاب والموظفين'),
    ]),
    ('التقارير والتدقيق', [
        ('view_reports',            'عرض التقارير والإحصائيات'),
        ('view_audit_log',          'عرض سجل العمليات'),
    ]),
    # Academic-year management deliberately stays admin-tier (no grantable
    # permission): changing year structure affects every year-scoped record.
    ('النظام', [
        ('manage_users',            'إدارة مستخدمي المدرسة'),
        ('edit_user_profiles',      'تعديل بيانات ملفات المستخدمين'),
        ('manage_white_label',      'هوية المدرسة (الاسم والشعار)'),
        ('manage_settings',         'إدارة إعدادات النظام'),
        ('manage_devices',          'إدارة أجهزة RFID'),
    ]),
]

# Every catalog permission name (fast membership checks).
CATALOG_PERMISSION_NAMES: frozenset = frozenset(
    name for _g, perms in PERMISSION_GROUPS for name, _l in perms
)


# ─── Post-login landing order ────────────────────────────────────────────────
# Ordered (any-of-permissions, endpoint, optional module gate). The first
# entry whose permission the user holds — and whose module is enabled for the
# school — is the landing page. view_dashboard is deliberately first: the
# dashboard appears only when explicitly granted; otherwise the user lands on
# the first page they are actually authorized to open.

LANDING_PAGES: list[tuple[tuple, str, str | None]] = [
    (('view_dashboard',),                        'admin.dashboard',          None),
    (('view_students',),                         'students.index',           'students'),
    (('manage_residential_areas',),              'students.residential_areas', 'students'),
    (('take_attendance',),                       'attendance.index',         'attendance'),
    (('manage_suspensions',),                    'attendance.suspensions',   'attendance'),
    (('manage_attendance_settings',),            'admin.attendance_settings', 'attendance'),
    (('manage_calendar',),                       'school_calendar.index',    'attendance'),
    (('view_attendance_devices',
      'manage_attendance_devices'),              'attendance_devices.index', 'attendance_devices'),
    (('enter_grades',),                          'grades.index',             'grades'),
    (('manage_homework',),                       'homework.index',           'homework'),
    (('view_sections', 'manage_sections'),       'sections.index',           'sections'),
    (('view_subjects', 'manage_subjects'),       'sections.subjects',        'subjects'),
    (('view_schedules', 'manage_schedules',
      'print_schedules'),                        'schedules.index',          'schedules'),
    (('manage_fees',),                           'fees.index',               'fees'),
    (('manage_revenues', 'manage_expenses'),     'finances.index',           'finances'),
    (('manage_salaries',),                       'salaries.index',           'salaries'),
    (('manage_employees',),                      'employees.index',          'employees'),
    (('manage_transport',),                      'transport.index',          'transport'),
    (('view_inventory', 'manage_inventory'),     'inventory.index',          'inventory'),
    (('manage_buildings',),                      'buildings.index',          None),
    (('manage_chat',),                           'chat.index',               'chat'),
    (('use_chat',),                              'chat.user_index',          'chat'),
    (('manage_complaints',),                     'admin.complaints_list',    None),
    (('manage_leave_requests',),                 'admin.leave_requests_list', None),
    (('manage_school_board',),                   'admin.school_board_videos', None),
    (('send_broadcast',),                        'broadcast.index',          'notifications'),
    (('view_notifications',
      'manage_notifications'),                   'notifications.index',      'notifications'),
    (('view_reports',),                          'reports.index',            'reports'),
    (('view_audit_log',),                        'audit.index',              None),
    (('manage_users',),                          'admin.users_list',         None),
    (('manage_white_label', 'manage_settings'),  'admin.school_settings',    None),
]


def get_landing_endpoint(user) -> str | None:
    """First endpoint the user's role permissions authorize, or None.

    Uses the existing User.has_permission() helper — admin tiers therefore
    always land on the dashboard (has_permission bypass), and every other
    role lands on its first granted page. Module gates respect the school's
    enabled modules.
    """
    try:
        if not user or not getattr(user, 'is_authenticated', True):
            return None
        from app.utils.modules import get_enabled_modules
        enabled = get_enabled_modules(getattr(user, 'school_id', None))
    except Exception:
        enabled = None

    for perms, endpoint, module in LANDING_PAGES:
        try:
            if module is not None and enabled is not None and module not in enabled:
                continue
            if any(user.has_permission(p) for p in perms):
                return endpoint
        except Exception:
            continue
    return None


# ─── DB sync ─────────────────────────────────────────────────────────────────

def sync_catalog_permissions():
    """Idempotently upsert every catalog permission into the existing
    permissions table (refreshing label/category). Does NOT commit — caller
    must. Never deletes rows and never touches role_permissions, so existing
    roles and legacy permissions (e.g. parent_view_child) are unaffected."""
    from app.models import db, Permission
    existing = {p.name: p for p in Permission.query.all()}
    for category, perms in PERMISSION_GROUPS:
        for name, label in perms:
            row = existing.get(name)
            if row is None:
                db.session.add(Permission(name=name, label=label,
                                          category=category))
            else:
                row.label = label
                row.category = category
    db.session.flush()
