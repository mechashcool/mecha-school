"""
student_form_config.py
----------------------
Per-school student form field/section visibility and required-field rules.

Usage in blueprint routes::

    from app.utils.student_form_config import get_student_form_config
    form_cfg = get_student_form_config(school.id)

Usage in templates::

    {% if form_cfg.section_visible('guardian_info') %}...{% endif %}
    {% if form_cfg.field_visible('nationality') %}...{% endif %}
    {% if form_cfg.field_required('phone') %}required{% endif %}
"""

# ---------------------------------------------------------------------------
# Canonical keys
# ---------------------------------------------------------------------------

# Whole cards that can be shown/hidden
ALL_SECTIONS = [
    'guardian_info',          # بيانات ولي الأمر
    'link_existing_parent',   # ربط بولي أمر موجود
    'create_parent_account',  # إنشاء حساب ولي أمر
    'attendance_device',      # بيانات جهاز الحضور
    'student_photo',          # صورة الطالب
    'student_documents',      # مستندات الطالب
    'notes',                  # ملاحظات
    'class_section',          # تعيين الصف والشعبة (inside basic_info card)
]

# Individual fields that can be shown/hidden or made required
ALL_FIELDS = [
    # inside basic_info card
    'date_of_birth',
    'gender',
    'nationality',
    'phone',
    'address',
    # inside guardian_info card
    'guardian_name',
    'guardian_phone',
    'guardian_email',
    'guardian_relation',
]

# Readable Arabic labels for the Super Admin UI
SECTION_LABELS = {
    'guardian_info':         'قسم بيانات ولي الأمر',
    'link_existing_parent':  'ربط بولي أمر موجود',
    'create_parent_account': 'إنشاء حساب ولي أمر جديد',
    'attendance_device':     'بيانات جهاز الحضور',
    'student_photo':         'صورة الطالب',
    'student_documents':     'مستندات الطالب',
    'notes':                 'الملاحظات',
    'class_section':         'تعيين المرحلة / الصف / الشعبة',
}

FIELD_LABELS = {
    'date_of_birth':    'تاريخ الميلاد',
    'gender':           'الجنس',
    'nationality':      'الجنسية',
    'phone':            'رقم الهاتف',
    'address':          'العنوان',
    'guardian_name':    'اسم ولي الأمر',
    'guardian_phone':   'هاتف ولي الأمر',
    'guardian_email':   'بريد ولي الأمر',
    'guardian_relation': 'صلة القرابة',
}


# ---------------------------------------------------------------------------
# Config wrapper
# ---------------------------------------------------------------------------

class StudentFormConfig:
    """
    Wraps a SchoolStudentFormConfig DB row (or None for defaults).

    All three sets default to empty when no row exists, which means:
      - every section/field is visible
      - no fields are made extra-required (only full_name stays hardcoded required)
    """

    def __init__(self, row=None):
        self._hidden_sections = set(row.hidden_sections or []) if row else set()
        self._hidden_fields   = set(row.hidden_fields   or []) if row else set()
        self._required_fields = set(row.required_fields or []) if row else set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def section_visible(self, key: str) -> bool:
        return key not in self._hidden_sections

    def field_visible(self, key: str) -> bool:
        return key not in self._hidden_fields

    def field_required(self, key: str) -> bool:
        return key in self._required_fields

    # Convenience: validate a POST dict against the current config.
    # Returns a list of Arabic error messages (empty = ok).
    def validate(self, form_data: dict) -> list:
        errors = []
        for field in self._required_fields:
            if field in self._hidden_fields:
                continue  # required but hidden → skip (should not happen if admin is sensible)
            value = form_data.get(field, '').strip()
            if not value:
                label = FIELD_LABELS.get(field, field)
                errors.append(f'الحقل "{label}" مطلوب.')
        return errors

    # Serialisable snapshot for passing to templates
    def as_dict(self):
        return {
            'hidden_sections': list(self._hidden_sections),
            'hidden_fields':   list(self._hidden_fields),
            'required_fields': list(self._required_fields),
        }


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def get_student_form_config(school_id: int) -> StudentFormConfig:
    """
    Load the StudentFormConfig for the given school.
    Returns defaults (all visible, nothing extra required) when no row exists.
    """
    from app.models import SchoolStudentFormConfig
    row = SchoolStudentFormConfig.query.filter_by(school_id=school_id).first()
    return StudentFormConfig(row)
