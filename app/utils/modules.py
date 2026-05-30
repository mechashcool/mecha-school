"""
Mecha-School — Module (feature) registry and access-control helpers.

Modules represent optional feature sets that a Super Admin can enable or
disable per school.  School managers cannot change module configuration.

Usage in Python:
    from app.utils.modules import MODULES, PRESETS, is_module_enabled, get_enabled_modules

Usage in Jinja2 templates (injected by context processor):
    {% if 'fees' in enabled_modules %} … {% endif %}

Route protection:
    A global before_request hook in app/__init__.py reads BLUEPRINT_MODULE and
    blocks any request whose blueprint maps to a disabled module.
"""
from __future__ import annotations

# ─── Module registry ──────────────────────────────────────────────────────────
# Each key is a logical module name.  It must match the corresponding entry in
# BLUEPRINT_MODULE so that the before_request guard knows which blueprint to block.

MODULES: dict[str, dict] = {
    "students": {
        "label":       "الطلاب",
        "icon":        "bi-people-fill",
        "description": "إدارة بيانات الطلاب وملفاتهم الشخصية",
    },
    "sections": {
        "label":       "الصفوف والشعب",
        "icon":        "bi-grid-3x3-gap-fill",
        "description": "إدارة الصفوف الدراسية والشعب",
    },
    "schedules": {
        "label":       "الجداول الدراسية",
        "icon":        "bi-table",
        "description": "تنظيم الجداول الأسبوعية للصفوف",
    },
    "attendance": {
        "label":       "الحضور والانصراف",
        "icon":        "bi-calendar-check-fill",
        "description": "تسجيل وإدارة حضور الطلاب يومياً",
    },
    "attendance_devices": {
        "label":       "أجهزة الحضور",
        "icon":        "bi-camera-video-fill",
        "description": "ربط وإدارة أجهزة Hikvision لتسجيل الحضور تلقائياً",
    },
    "grades": {
        "label":       "الدرجات والاختبارات",
        "icon":        "bi-patch-check-fill",
        "description": "إدخال نتائج الاختبارات وإدارة درجات الطلاب",
    },
    "fees": {
        "label":       "الأقساط والرسوم",
        "icon":        "bi-cash-coin",
        "description": "إدارة رسوم الطلاب وتسجيل المدفوعات",
    },
    "finances": {
        "label":       "الإيرادات والمصروفات",
        "icon":        "bi-graph-up-arrow",
        "description": "متابعة الحسابات المالية العامة للمدرسة",
    },
    "salaries": {
        "label":       "الرواتب",
        "icon":        "bi-wallet-fill",
        "description": "حساب وصرف رواتب الموظفين",
    },
    "employees": {
        "label":       "الموظفون",
        "icon":        "bi-person-badge-fill",
        "description": "إدارة بيانات وملفات الموظفين",
    },
    "evaluations": {
        "label":       "تقييم الموظفين",
        "icon":        "bi-star-fill",
        "description": "تقييم أداء الموظفين بشكل دوري",
    },
    "transport": {
        "label":       "خطوط النقل",
        "icon":        "bi-bus-front-fill",
        "description": "إدارة خطوط وحافلات نقل الطلاب",
    },
    "inventory": {
        "label":       "المخازن",
        "icon":        "bi-box-seam-fill",
        "description": "إدارة مخزون المستلزمات والأجهزة",
    },
    "notifications": {
        "label":       "الإشعارات",
        "icon":        "bi-bell-fill",
        "description": "إرسال إشعارات وإعلانات للطلاب وأولياء الأمور",
    },
    "reports": {
        "label":       "التقارير",
        "icon":        "bi-bar-chart-fill",
        "description": "عرض وطباعة التقارير والإحصائيات",
    },
    "homework": {
        "label":       "الواجبات",
        "icon":        "bi-journal-text",
        "description": "إدارة الواجبات المدرسية وتعيينها للطلاب",
    },
    "subjects": {
        "label":       "المواد الدراسية",
        "icon":        "bi-book-fill",
        "description": "إدارة المواد الدراسية وتنظيمها بحسب المراحل والصفوف",
    },
}

# Maps Flask blueprint name → module key.
# Blueprints absent from this dict (auth, admin, schools, super_admin, parent,
# teacher, api, hardware, broadcast, audit) are never blocked by the module guard.
BLUEPRINT_MODULE: dict[str, str] = {
    "students":           "students",
    "sections":           "sections",
    "schedules":          "schedules",
    "attendance":         "attendance",
    "attendance_devices": "attendance_devices",
    "grades":             "grades",
    "fees":               "fees",
    "finances":           "finances",
    "salaries":           "salaries",
    "employees":          "employees",
    "evaluations":        "evaluations",
    "transport":          "transport",
    "inventory":          "inventory",
    "notifications":      "notifications",
    "reports":            "reports",
    "homework":           "homework",
}

# Package presets — quick-select buttons shown in the school creation form.
PRESETS: dict[str, dict] = {
    "attendance_grades": {
        "label":   "باقة الحضور والدرجات",
        "modules": [
            "students", "sections", "attendance", "attendance_devices",
            "grades", "notifications",
        ],
    },
    "full": {
        "label":   "الباقة الكاملة",
        "modules": list(MODULES.keys()),
    },
}


# ─── DB helpers ───────────────────────────────────────────────────────────────

def is_module_enabled(school_id: int | None, module_key: str) -> bool:
    """Return True if module_key is enabled for school_id.

    Rules:
    - school_id=None (super admin global view) → always True.
    - Unknown module_key (not in MODULES) → True (never block unknown routes).
    - No SchoolModule row exists → True (backward compat for existing schools).
    - Row exists → return its is_enabled flag.
    """
    if school_id is None:
        return True
    if module_key not in MODULES:
        return True
    from app.models import SchoolModule
    row = (SchoolModule.query
           .execution_options(bypass_tenant_scope=True)
           .filter_by(school_id=school_id, module_key=module_key)
           .first())
    if row is None:
        return True  # No configuration row → enabled by default
    return bool(row.is_enabled)


def get_enabled_modules(school_id: int | None) -> set:
    """Return the set of enabled module keys for a school.

    - school_id=None → all module keys (super admin bypass).
    - No rows in school_modules for this school → all keys (backward compat).
    - Rows exist → return only the enabled subset.
    """
    all_keys = set(MODULES.keys())
    if school_id is None:
        return all_keys
    from app.models import SchoolModule
    rows = (SchoolModule.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school_id)
            .all())
    if not rows:
        return all_keys  # Existing school with no module config → grant all
    return {r.module_key for r in rows if r.is_enabled}


def save_school_modules(school_id: int, enabled_keys: list) -> None:
    """Upsert SchoolModule rows for school_id.

    Sets is_enabled=True for keys in enabled_keys, False for all others.
    Creates missing rows; updates existing ones.  Does NOT commit — caller must.
    """
    from app.models import db, SchoolModule
    existing = {
        r.module_key: r
        for r in (SchoolModule.query
                  .execution_options(bypass_tenant_scope=True)
                  .filter_by(school_id=school_id)
                  .all())
    }
    enabled_set = set(enabled_keys)
    for key in MODULES:
        if key in existing:
            existing[key].is_enabled = (key in enabled_set)
        else:
            db.session.add(SchoolModule(
                school_id  = school_id,
                module_key = key,
                is_enabled = (key in enabled_set),
            ))
    db.session.flush()
