"""
school_config.py
----------------
Centralized per-school module configuration system.

Each school can have custom visibility rules for every module:
  - which SECTIONS appear (entire collapsible cards / page areas)
  - which FIELDS appear (individual form inputs)
  - which FIELDS are required
  - which ACTIONS are enabled (buttons, export links, etc.)

Config is stored in SchoolModuleConfig rows (one per school+module).
No row = everything visible/enabled (fail-open, backward compatible).

For the 'students' module, config is bridged from SchoolStudentFormConfig
(the existing dedicated table) for backward compatibility.

Usage in Jinja2 templates (via context processor — school_cfg is auto-injected)::

    {% if school_cfg.section_visible('employees', 'system_account') %}
    {% if school_cfg.field_visible('employees', 'base_salary') %}
    {% if school_cfg.field_required('employees', 'phone') %}
    {% if school_cfg.action_enabled('attendance_devices', 'sync') %}

Usage in Python routes::

    from app.utils.school_config import get_school_config
    cfg = get_school_config(school_id)
    if cfg.action_enabled('employee_attendance', 'export_excel'):
        ...
"""
from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
#  Module definitions — canonical keys + Arabic labels for Super Admin UI
# ─────────────────────────────────────────────────────────────────────────────

MODULE_DEFS: dict[str, dict] = {

    'employees': {
        'label': 'نموذج الموظف',
        'sections': {
            'photo':              'صورة الموظف',
            'system_account':     'حساب النظام',
            'teacher_assignment': 'إعدادات التدريسي',
        },
        'fields': {
            'job_title':     'المسمى الوظيفي',
            'department':    'القسم',
            'gender':        'الجنس',
            'nationality':   'الجنسية',
            'date_of_birth': 'تاريخ الميلاد',
            'phone':         'رقم الهاتف',
            'email':         'البريد الإلكتروني',
            'address':       'العنوان',
            'contract_type': 'نوع العقد',
            'hire_date':     'تاريخ التعيين',
            'base_salary':   'الراتب الأساسي',
        },
        'actions': {
            'create':       'إضافة موظف',
            'delete':       'حذف موظف',
            'export_excel': 'تصدير Excel',
        },
    },

    'employee_attendance': {
        'label': 'حضور الموظفين',
        'sections': {},
        'fields':   {},
        'actions': {
            'view_report':    'عرض صفحة تقرير الحضور',
            'export_excel':   'تصدير Excel (إجمالي)',
            'export_pdf':     'تصدير PDF (إجمالي)',
            'view_detail':    'تقرير تفصيلي للموظف',
            'employee_excel': 'تصدير Excel (للموظف)',
            'employee_pdf':   'تصدير PDF (للموظف)',
        },
    },

    'student_attendance': {
        'label': 'حضور الطلاب',
        'sections': {},
        'fields':   {},
        'actions': {
            'export_excel': 'تصدير Excel',
            'export_pdf':   'تصدير PDF',
        },
    },

    'attendance_devices': {
        'label': 'أجهزة الحضور',
        'sections': {
            'student_mappings': 'ربط الطلاب بالجهاز',
        },
        'fields': {},
        'actions': {
            'add_device':      'إضافة جهاز',
            'edit_device':     'تعديل جهاز',
            'delete_device':   'حذف جهاز',
            'sync':            'مزامنة الحضور',
            'test_connection': 'اختبار الاتصال',
            'view_logs':       'عرض السجلات الخام',
        },
    },

    'homework': {
        'label': 'الواجبات',
        'sections': {},
        'fields': {
            'title':        'عنوان الواجب',
            'subject':      'المادة',
            'grade':        'الصف',
            'section':      'الشعبة',
            'publish_date': 'تاريخ النشر',
            'due_date':     'تاريخ التسليم',
            'notes':        'الملاحظات / الوصف',
            'attachment':   'المرفق',
        },
        'actions': {
            'view':              'عرض الواجبات',
            'create':            'إضافة واجب',
            'edit':              'تعديل واجب',
            'delete':            'حذف واجب',
            'upload_attachment': 'رفع مرفق',
            'api_access':        'الوصول من التطبيق',
        },
    },

    'subjects': {
        'label': 'المواد الدراسية',
        'sections': {},
        'fields': {
            'name':        'اسم المادة',
            'code':        'الرمز',
            'stage':       'المرحلة',
            'grade':       'الصف',
            'total_marks': 'الدرجة الكلية',
            'pass_marks':  'درجة النجاح',
            'description': 'الوصف',
        },
        'actions': {
            'create': 'إضافة مادة',
            'edit':   'تعديل مادة',
            'delete': 'حذف مادة',
        },
    },

    'chat': {
        'label': 'المراسلات',
        'sections': {},
        'fields': {
            'allow_private_chats':               'السماح بالمحادثات الخاصة',
            'allow_group_chats':                 'السماح بمجموعات المحادثة',
            'allow_school_announcement_group':   'السماح بمجموعة الإعلانات المدرسية',
            'allow_parent_teacher_private_chat': 'محادثة خاصة: ولي الأمر ↔ المعلم',
            'allow_parent_admin_private_chat':   'محادثة خاصة: ولي الأمر ↔ الإدارة',
            'allow_file_attachments':            'السماح بالمرفقات',
            'allow_images':                      'السماح بإرسال الصور',
            'allow_pdf':                         'السماح بإرسال PDF',
            'allow_message_delete':              'السماح بحذف الرسائل',
            'allow_read_receipts':               'تفعيل إشعارات القراءة',
            'allow_admin_monitoring':            'السماح للإدارة بمراقبة المحادثات',
            'allow_chat_schedule':               'تفعيل جدول أوقات الإرسال',
            'allow_member_blocking':             'السماح بحظر الأعضاء',
            'allow_group_admins':                'السماح بتعيين مشرفي المجموعات',
            'max_attachment_size_mb':            'الحد الأقصى لحجم المرفق (MB)',
            'message_max_length':                'الحد الأقصى لطول الرسالة',
        },
        'actions': {
            'view':            'عرض المراسلات',
            'create_group':    'إنشاء مجموعة',
            'edit_group':      'تعديل مجموعة',
            'delete_group':    'حذف مجموعة',
            'send_message':    'إرسال رسالة',
            'close_chat':      'إغلاق محادثة',
            'reopen_chat':     'فتح محادثة',
            'assign_admin':    'تعيين مشرف',
            'block_member':    'حظر عضو',
            'unblock_member':  'إلغاء حظر عضو',
            'view_all_chats':  'عرض جميع المحادثات',
            'manage_schedule': 'إدارة أوقات التواصل',
            'api_access':      'الوصول من تطبيق الجوال',
        },
    },
}

# Keys for which module_configs exist (excludes 'students' — uses SchoolStudentFormConfig)
CONFIGURABLE_MODULES = list(MODULE_DEFS.keys())


# ─────────────────────────────────────────────────────────────────────────────
#  Config wrapper
# ─────────────────────────────────────────────────────────────────────────────

class SchoolConfig:
    """
    Wraps per-school configuration for ALL modules.

    Lazy-loads from DB on first access per module.
    Returns True/enabled for everything when no config row exists (fail-open).

    For the 'students' module, delegates to the SchoolStudentFormConfig system.
    """

    def __init__(self, school_id: int | None):
        self._school_id = school_id
        self._cache: dict[str, dict | None] = {}  # module_key -> config dict or None

    # ── Internal loader ───────────────────────────────────────────────────────

    def _get_config(self, module_key: str) -> dict | None:
        if module_key not in self._cache:
            if not self._school_id:
                self._cache[module_key] = None
            elif module_key == 'students':
                self._cache[module_key] = self._load_students_config()
            else:
                self._cache[module_key] = self._load_module_config(module_key)
        return self._cache[module_key]

    def _load_students_config(self) -> dict | None:
        from app.models import SchoolStudentFormConfig
        row = SchoolStudentFormConfig.query.filter_by(school_id=self._school_id).first()
        if not row:
            return None
        return {
            'hidden_sections': row.hidden_sections or [],
            'hidden_fields':   row.hidden_fields   or [],
            'required_fields': row.required_fields or [],
            'disabled_actions': [],
        }

    def _load_module_config(self, module_key: str) -> dict | None:
        from app.models import SchoolModuleConfig
        row = (SchoolModuleConfig.query
               .filter_by(school_id=self._school_id, module_key=module_key)
               .first())
        return row.config if row else None

    # ── Public API ────────────────────────────────────────────────────────────

    def section_visible(self, module_key: str, section_key: str) -> bool:
        cfg = self._get_config(module_key)
        if not cfg:
            return True
        return section_key not in cfg.get('hidden_sections', [])

    def field_visible(self, module_key: str, field_key: str) -> bool:
        cfg = self._get_config(module_key)
        if not cfg:
            return True
        return field_key not in cfg.get('hidden_fields', [])

    def field_required(self, module_key: str, field_key: str) -> bool:
        cfg = self._get_config(module_key)
        if not cfg:
            return False
        return field_key in cfg.get('required_fields', [])

    def action_enabled(self, module_key: str, action_key: str) -> bool:
        cfg = self._get_config(module_key)
        if not cfg:
            return True
        return action_key not in cfg.get('disabled_actions', [])

    def as_dict(self, module_key: str) -> dict:
        """Serialisable snapshot of a module's config for templates."""
        cfg = self._get_config(module_key)
        return cfg or {
            'hidden_sections':  [],
            'hidden_fields':    [],
            'required_fields':  [],
            'disabled_actions': [],
        }


# ─────────────────────────────────────────────────────────────────────────────
#  Null config — super admin or unauthenticated: everything open
# ─────────────────────────────────────────────────────────────────────────────

class NullSchoolConfig(SchoolConfig):
    """All methods return the permissive default (super admin sees everything)."""

    def __init__(self):
        super().__init__(school_id=None)

    def section_visible(self, *_):  return True
    def field_visible(self, *_):    return True
    def field_required(self, *_):   return False
    def action_enabled(self, *_):   return True


# ─────────────────────────────────────────────────────────────────────────────
#  Factory
# ─────────────────────────────────────────────────────────────────────────────

def get_school_config(school_id: int | None) -> SchoolConfig:
    """
    Return a SchoolConfig for the given school.
    Pass None (super admin / unauthenticated) to get a NullSchoolConfig that
    always returns True/False (no restrictions).
    """
    if not school_id:
        return NullSchoolConfig()
    return SchoolConfig(school_id)


# ─────────────────────────────────────────────────────────────────────────────
#  DB save helper
# ─────────────────────────────────────────────────────────────────────────────

def save_module_config(school_id: int, module_key: str, config: dict) -> None:
    """
    Upsert a SchoolModuleConfig row.  Does NOT commit — caller must commit.

    config keys: hidden_sections, hidden_fields, required_fields, disabled_actions
    Empty lists are stored as-is (explicit "nothing hidden" differs from no row).
    """
    from app.models import db, SchoolModuleConfig
    from datetime import datetime as dt

    row = (SchoolModuleConfig.query
           .filter_by(school_id=school_id, module_key=module_key)
           .first())
    if row is None:
        row = SchoolModuleConfig(school_id=school_id, module_key=module_key)
        db.session.add(row)

    row.config     = config
    row.updated_at = dt.utcnow()
