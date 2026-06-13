"""
Mecha-School — Sub-feature (capability) registry and access-control helpers.

Features represent granular capabilities inside a module that a Super Admin can
enable or disable per school.  If a main module is disabled, all its features
are considered disabled regardless of their individual settings.

Usage in Python:
    from app.utils.features import FEATURES, is_feature_enabled, feature_required

Usage in Jinja2 templates (injected by context processor):
    {% if 'students.photo_upload' in enabled_features %} … {% endif %}

Route protection:
    @feature_required('students.delete')
    def delete(student_id): ...
"""
from __future__ import annotations
from functools import wraps

# ─── Feature registry ─────────────────────────────────────────────────────────
# Each key is "module_key.capability".  'module' must match a key in MODULES.
# 'label' is the Arabic display name for the Super Admin UI.

FEATURES: dict[str, dict] = {
    # ── Students ──────────────────────────────────────────────────────────────
    "students.view":             {"label": "عرض الطلاب",             "module": "students"},
    "students.create":           {"label": "إضافة طالب",             "module": "students"},
    "students.edit":             {"label": "تعديل بيانات طالب",      "module": "students"},
    "students.delete":           {"label": "حذف/أرشفة طالب",        "module": "students"},
    "students.photo_upload":     {"label": "رفع صورة الطالب",        "module": "students"},
    "students.documents_upload": {"label": "رفع مستندات الطالب",     "module": "students"},
    "students.export":           {"label": "تصدير بيانات الطلاب",    "module": "students"},

    # ── Sections ──────────────────────────────────────────────────────────────
    "sections.view":             {"label": "عرض الصفوف والشعب",      "module": "sections"},
    "sections.manage":           {"label": "إدارة الصفوف والشعب",    "module": "sections"},

    # ── Schedules ─────────────────────────────────────────────────────────────
    "schedules.view":            {"label": "عرض الجداول",            "module": "schedules"},
    "schedules.create":          {"label": "إنشاء جدول",             "module": "schedules"},
    "schedules.edit":            {"label": "تعديل جدول",             "module": "schedules"},
    "schedules.delete":          {"label": "حذف جدول",               "module": "schedules"},

    # ── Attendance ────────────────────────────────────────────────────────────
    "attendance.view":           {"label": "عرض الحضور",             "module": "attendance"},
    "attendance.manual_record":  {"label": "تسجيل حضور يدوي",        "module": "attendance"},
    "attendance.reports":        {"label": "تقارير الحضور",           "module": "attendance"},
    "attendance.suspensions":    {"label": "إدارة الإيقاف عن الدراسة","module": "attendance"},

    # ── Attendance Devices ────────────────────────────────────────────────────
    "attendance_devices.view":            {"label": "عرض أجهزة الحضور",      "module": "attendance_devices"},
    "attendance_devices.create":          {"label": "إضافة جهاز",            "module": "attendance_devices"},
    "attendance_devices.edit":            {"label": "تعديل جهاز",            "module": "attendance_devices"},
    "attendance_devices.delete":          {"label": "حذف جهاز",              "module": "attendance_devices"},
    "attendance_devices.test_connection": {"label": "اختبار الاتصال بالجهاز", "module": "attendance_devices"},
    "attendance_devices.sync":            {"label": "مزامنة الحضور",         "module": "attendance_devices"},
    "attendance_devices.mappings":        {"label": "ربط الطلاب بالأجهزة",   "module": "attendance_devices"},
    "attendance_devices.logs":            {"label": "عرض سجلات الأجهزة",    "module": "attendance_devices"},

    # ── Grades ────────────────────────────────────────────────────────────────
    "grades.view":               {"label": "عرض الدرجات",            "module": "grades"},
    "grades.create":             {"label": "إدخال درجات",            "module": "grades"},
    "grades.edit":               {"label": "تعديل درجات",            "module": "grades"},
    "grades.reports":            {"label": "تقارير الدرجات",          "module": "grades"},
    "grades.export":             {"label": "تصدير الدرجات",          "module": "grades"},

    # ── Fees ──────────────────────────────────────────────────────────────────
    "fees.view":                 {"label": "عرض الأقساط",            "module": "fees"},
    "fees.create":               {"label": "إضافة قسط",              "module": "fees"},
    "fees.pay":                  {"label": "تسجيل دفعة",             "module": "fees"},
    "fees.receipts":             {"label": "وصولات الدفع",           "module": "fees"},
    "fees.export":               {"label": "تصدير الأقساط",          "module": "fees"},

    # ── Finances ──────────────────────────────────────────────────────────────
    "finances.view":             {"label": "عرض الإيرادات والمصاريف", "module": "finances"},
    "finances.revenues_create":  {"label": "إضافة إيراد",            "module": "finances"},
    "finances.revenues_edit":    {"label": "تعديل إيراد",            "module": "finances"},
    "finances.revenues_delete":  {"label": "حذف إيراد",              "module": "finances"},
    "finances.expenses_create":  {"label": "إضافة مصروف",           "module": "finances"},
    "finances.expenses_edit":    {"label": "تعديل مصروف",           "module": "finances"},
    "finances.expenses_delete":  {"label": "حذف مصروف",             "module": "finances"},

    # ── Salaries ──────────────────────────────────────────────────────────────
    "salaries.view":             {"label": "عرض الرواتب",            "module": "salaries"},
    "salaries.create":           {"label": "إضافة راتب",             "module": "salaries"},
    "salaries.edit":             {"label": "تعديل راتب",             "module": "salaries"},
    "salaries.delete":           {"label": "حذف راتب",               "module": "salaries"},
    "salaries.approve":          {"label": "اعتماد الرواتب",         "module": "salaries"},
    "salaries.pay":              {"label": "صرف الرواتب",            "module": "salaries"},
    "salaries.settings":         {"label": "إعدادات الرواتب",        "module": "salaries"},
    "salaries.components":       {"label": "بنود الراتب",            "module": "salaries"},
    "salaries.statement":        {"label": "كشف حساب موظف",         "module": "salaries"},
    "salaries.export":           {"label": "تصدير الرواتب",          "module": "salaries"},

    # ── Employees ─────────────────────────────────────────────────────────────
    "employees.view":            {"label": "عرض الموظفين",           "module": "employees"},
    "employees.create":          {"label": "إضافة موظف",             "module": "employees"},
    "employees.edit":            {"label": "تعديل موظف",             "module": "employees"},
    "employees.documents":       {"label": "ملفات الموظفين",         "module": "employees"},

    # ── Evaluations ───────────────────────────────────────────────────────────
    "evaluations.view":          {"label": "عرض التقييمات",          "module": "evaluations"},
    "evaluations.create":        {"label": "إضافة تقييم",            "module": "evaluations"},
    "evaluations.edit":          {"label": "تعديل تقييم",            "module": "evaluations"},
    "evaluations.delete":        {"label": "حذف تقييم",              "module": "evaluations"},

    # ── Transport ─────────────────────────────────────────────────────────────
    "transport.view":            {"label": "عرض خطوط النقل",          "module": "transport"},
    "transport.create":          {"label": "إضافة خط نقل",            "module": "transport"},
    "transport.edit":            {"label": "تعديل خط نقل",            "module": "transport"},
    "transport.delete":          {"label": "حذف خط نقل",              "module": "transport"},
    "transport.students":        {"label": "ربط الطلاب بخطوط النقل",  "module": "transport"},
    "transport.reports":         {"label": "تقارير النقل",            "module": "transport"},

    # ── Inventory ─────────────────────────────────────────────────────────────
    "inventory.view":            {"label": "عرض المخزن",             "module": "inventory"},
    "inventory.items_create":    {"label": "إضافة مادة",             "module": "inventory"},
    "inventory.items_edit":      {"label": "تعديل مادة",             "module": "inventory"},
    "inventory.categories":      {"label": "إدارة التصنيفات",        "module": "inventory"},
    "inventory.movements":       {"label": "حركات المخزن",           "module": "inventory"},
    "inventory.counts":          {"label": "الجرد السنوي",            "module": "inventory"},
    "inventory.reports":         {"label": "تقارير المخزن",          "module": "inventory"},
    "inventory.export":          {"label": "تصدير المخزن",           "module": "inventory"},

    # ── Notifications ─────────────────────────────────────────────────────────
    "notifications.view":        {"label": "عرض الإشعارات",          "module": "notifications"},
    "notifications.create":      {"label": "إرسال إشعار",            "module": "notifications"},
    "notifications.delete":      {"label": "حذف إشعار",              "module": "notifications"},

    # ── Reports ───────────────────────────────────────────────────────────────
    "reports.view":              {"label": "عرض لوحة التقارير",       "module": "reports"},
    "reports.financial":         {"label": "التقارير المالية",        "module": "reports"},
    "reports.students":          {"label": "تقارير الطلاب",          "module": "reports"},
    "reports.fees":              {"label": "تقارير الأقساط",         "module": "reports"},
    "reports.attendance":        {"label": "تقارير الحضور",          "module": "reports"},
    "reports.salaries":          {"label": "تقارير الرواتب",         "module": "reports"},
    "reports.export_excel":      {"label": "تصدير Excel",            "module": "reports"},

    # ── Chat / Messaging ──────────────────────────────────────────────────────
    "chat.view":            {"label": "عرض المراسلات",             "module": "chat"},
    "chat.create_group":    {"label": "إنشاء مجموعة محادثة",       "module": "chat"},
    "chat.edit_group":      {"label": "تعديل مجموعة محادثة",       "module": "chat"},
    "chat.delete_group":    {"label": "حذف مجموعة محادثة",         "module": "chat"},
    "chat.send_message":    {"label": "إرسال رسائل",               "module": "chat"},
    "chat.close_chat":      {"label": "إغلاق محادثة",              "module": "chat"},
    "chat.reopen_chat":     {"label": "إعادة فتح محادثة",          "module": "chat"},
    "chat.assign_admin":    {"label": "تعيين مشرف مجموعة",         "module": "chat"},
    "chat.block_member":    {"label": "حظر عضو من المحادثة",       "module": "chat"},
    "chat.view_all_chats":  {"label": "عرض جميع المحادثات",        "module": "chat"},
    "chat.manage_schedule": {"label": "إدارة جدول أوقات الإرسال",  "module": "chat"},
    "chat.api_access":      {"label": "الوصول من تطبيق الجوال",   "module": "chat"},
}


# ─── Feature presets ──────────────────────────────────────────────────────────

FEATURE_PRESETS: dict[str, dict] = {
    "attendance_grades": {
        "label": "باقة الحضور والدرجات",
        "features": [
            "students.view", "students.create", "students.edit",
            "students.photo_upload", "students.export",
            "sections.view", "sections.manage",
            "attendance.view", "attendance.manual_record",
            "attendance.reports", "attendance.suspensions",
            "attendance_devices.view", "attendance_devices.sync",
            "attendance_devices.mappings", "attendance_devices.logs",
            "grades.view", "grades.create", "grades.edit",
            "grades.reports", "grades.export",
            "notifications.view", "notifications.create",
        ],
    },
    "full": {
        "label": "الباقة الكاملة",
        "features": list(FEATURES.keys()),
    },
    "chat_basic": {
        "label": "باقة المراسلات الأساسية",
        "features": [
            "chat.view", "chat.create_group", "chat.edit_group",
            "chat.send_message", "chat.close_chat", "chat.reopen_chat",
            "chat.view_all_chats", "chat.api_access",
        ],
    },
}


# ─── DB helpers ───────────────────────────────────────────────────────────────

def is_feature_enabled(school_id: int | None, feature_key: str) -> bool:
    """Return True if feature_key is enabled for school_id.

    Rules:
    - school_id=None (super admin global view) → always True.
    - Unknown feature_key → True (fail-open for unknown capabilities).
    - Parent module disabled → False (module gate takes priority).
    - No SchoolFeature row → True (backward compat for existing schools).
    - Row exists → return its is_enabled flag.
    """
    if school_id is None:
        return True
    if feature_key not in FEATURES:
        return True
    # Module gate: if parent module is disabled, all its features are disabled.
    module_key = FEATURES[feature_key]["module"]
    from app.utils.modules import is_module_enabled
    if not is_module_enabled(school_id, module_key):
        return False
    from app.models import SchoolFeature
    row = (SchoolFeature.query
           .execution_options(bypass_tenant_scope=True)
           .filter_by(school_id=school_id, feature_key=feature_key)
           .first())
    if row is None:
        return True  # No configuration row → enabled by default
    return bool(row.is_enabled)


def get_enabled_features(school_id: int | None) -> set:
    """Return the set of enabled feature keys for a school.

    - school_id=None → all feature keys (super admin bypass).
    - No rows in school_features for this school → all keys (backward compat).
    - Rows exist → only keys that are enabled AND whose parent module is enabled.
    """
    all_keys = set(FEATURES.keys())
    if school_id is None:
        return all_keys

    from app.utils.request_cache import request_memo

    def _load():
        from app.models import SchoolFeature
        rows = (SchoolFeature.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=school_id)
                .all())
        if not rows:
            return all_keys  # Existing school with no feature config → grant all
        # Apply parent-module filter to enabled rows. Module flags are loaded
        # ONCE for the whole school; the per-feature check is in memory only —
        # same semantics as is_module_enabled (missing row → enabled).
        from app.utils.modules import MODULES, get_module_flags
        flags = get_module_flags(school_id)

        def _module_on(module_key: str) -> bool:
            if module_key not in MODULES:
                return True
            if module_key not in flags:
                return True
            return flags[module_key]

        enabled = set()
        for r in rows:
            if not r.is_enabled:
                continue
            module_key = FEATURES.get(r.feature_key, {}).get("module", "")
            if not module_key or _module_on(module_key):
                enabled.add(r.feature_key)
        return enabled

    return request_memo(('enabled_features', school_id), _load)


def save_school_features(school_id: int, enabled_keys: list) -> None:
    """Upsert SchoolFeature rows for school_id.

    Sets is_enabled=True for keys in enabled_keys, False for all others.
    Creates missing rows; updates existing ones.  Does NOT commit — caller must.
    """
    from app.models import db, SchoolFeature
    existing = {
        r.feature_key: r
        for r in (SchoolFeature.query
                  .execution_options(bypass_tenant_scope=True)
                  .filter_by(school_id=school_id)
                  .all())
    }
    enabled_set = set(enabled_keys)
    for key in FEATURES:
        if key in existing:
            existing[key].is_enabled = (key in enabled_set)
        else:
            db.session.add(SchoolFeature(
                school_id   = school_id,
                feature_key = key,
                is_enabled  = (key in enabled_set),
            ))
    db.session.flush()


# ─── Route decorator ──────────────────────────────────────────────────────────

def feature_required(feature_key: str):
    """Decorator: return 403 if the current school has feature_key disabled.

    Super admins are never blocked.  Must be placed after @login_required.
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            from flask_login import current_user
            if not current_user.is_authenticated or current_user.is_super_admin:
                return f(*args, **kwargs)
            school_id = getattr(current_user, 'school_id', None)
            if not is_feature_enabled(school_id, feature_key):
                label = FEATURES.get(feature_key, {}).get('label', feature_key)
                from flask import render_template as _rt
                return _rt('shared/feature_disabled.html',
                           feature_label=label,
                           feature_key=feature_key), 403
            return f(*args, **kwargs)
        return wrapped
    return decorator
