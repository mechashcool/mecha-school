"""
packages.py
-----------
FeaturePackage management helpers.

A FeaturePackage is a named, reusable configuration bundle that a Super Admin
creates once and then applies to one or more schools.

Package application is a SNAPSHOT operation (Option B): when a package is
applied to a school, its settings are copied into the school's SchoolModule /
SchoolFeature rows, SchoolStudentFormConfig, and SchoolModuleConfig rows at
that moment.  Later edits to the package do NOT automatically update schools
that already received it.

Config JSON structure inside FeaturePackage.config::

    {
      "modules": {
          "students":   true,
          "employees":  false,
          ...
      },
      "features": {
          "students.create":         true,
          "attendance_devices.sync": false,
          ...
      },
      "module_configs": {
          "students": {
              "hidden_sections": ["attendance_device"],
              "hidden_fields":   ["nationality"],
              "required_fields": ["phone"],
              "disabled_actions": []
          },
          "employees": {
              "hidden_sections": ["system_account"],
              "hidden_fields":   ["base_salary"],
              "required_fields": [],
              "disabled_actions": ["delete"]
          },
          "employee_attendance": {
              "hidden_sections": [],
              "hidden_fields":   [],
              "required_fields": [],
              "disabled_actions": ["export_pdf"]
          },
          "attendance_devices": {
              "hidden_sections": [],
              "hidden_fields":   [],
              "required_fields": [],
              "disabled_actions": ["delete_device"]
          }
      }
    }

Keys missing from the JSON default to True/enabled (fail-open, backward compat).
"""
from __future__ import annotations

from app.utils.modules import MODULES, save_school_modules
from app.utils.features import FEATURES, save_school_features
from app.utils.student_form_config import ALL_SECTIONS as _SF_SECTIONS, ALL_FIELDS as _SF_FIELDS
from app.utils.school_config import MODULE_DEFS, CONFIGURABLE_MODULES, save_module_config


# ─── Default empty module config ──────────────────────────────────────────────

def _empty_module_cfg() -> dict:
    return {'hidden_sections': [], 'hidden_fields': [],
            'required_fields': [], 'disabled_actions': []}


# ─── Default config ───────────────────────────────────────────────────────────

def get_default_package_config() -> dict:
    """Return a config dict with every module/feature enabled and no restrictions."""
    module_configs: dict[str, dict] = {}

    # Student form default
    module_configs['students'] = {
        'hidden_sections': [],
        'hidden_fields':   [],
        'required_fields': [],
        'disabled_actions': [],
    }

    # All other configurable modules
    for mk in CONFIGURABLE_MODULES:
        module_configs[mk] = _empty_module_cfg()

    return {
        'modules':        {k: True for k in MODULES},
        'features':       {k: True for k in FEATURES},
        'module_configs': module_configs,
    }


# ─── Form → config helpers ────────────────────────────────────────────────────

def build_config_from_form(form) -> dict:
    """
    Build a package config dict from a Flask request.form object.

    Checkbox naming conventions:
      - module_<key>                      → module enabled flags
      - feature_<key>                     → feature enabled flags (dots → underscores)
      - sf_section_<key>                  → student form section visible
      - sf_field_<key>                    → student form field visible
      - sf_req_<key>                      → student form field required
      - mc_<module>_section_<section_key> → module section visible
      - mc_<module>_field_<field_key>     → module field visible
      - mc_<module>_req_<field_key>       → module field required
      - mc_<module>_action_<action_key>   → module action enabled
    """
    # ── Modules ───────────────────────────────────────────────────────────────
    modules = {key: bool(form.get(f'module_{key}')) for key in MODULES}

    # ── Features ──────────────────────────────────────────────────────────────
    features = {key: bool(form.get('feature_' + key.replace('.', '_')))
                for key in FEATURES}

    # ── Student form ──────────────────────────────────────────────────────────
    hidden_sections = [s for s in _SF_SECTIONS if not form.get(f'sf_section_{s}')]
    hidden_fields   = [f for f in _SF_FIELDS   if not form.get(f'sf_field_{f}')]
    required_fields = [f for f in _SF_FIELDS
                       if form.get(f'sf_req_{f}') and f not in hidden_fields]

    students_cfg = {
        'hidden_sections': hidden_sections,
        'hidden_fields':   hidden_fields,
        'required_fields': required_fields,
        'disabled_actions': [],
    }

    # ── Other modules (employees, employee_attendance, attendance_devices, …) ─
    module_configs: dict[str, dict] = {'students': students_cfg}

    for mk, mdef in MODULE_DEFS.items():
        mod_sections = mdef.get('sections', {})
        mod_fields   = mdef.get('fields', {})
        mod_actions  = mdef.get('actions', {})

        hidden_sec = [s for s in mod_sections if not form.get(f'mc_{mk}_section_{s}')]
        hidden_fld = [f for f in mod_fields   if not form.get(f'mc_{mk}_field_{f}')]
        req_fld    = [f for f in mod_fields
                      if form.get(f'mc_{mk}_req_{f}') and f not in hidden_fld]
        dis_act    = [a for a in mod_actions  if not form.get(f'mc_{mk}_action_{a}')]

        module_configs[mk] = {
            'hidden_sections':  hidden_sec,
            'hidden_fields':    hidden_fld,
            'required_fields':  req_fld,
            'disabled_actions': dis_act,
        }

    return {
        'modules':        modules,
        'features':       features,
        'module_configs': module_configs,
    }


# ─── Apply package to school ──────────────────────────────────────────────────

def apply_package_to_school(school_id: int, package) -> None:
    """
    Snapshot a FeaturePackage onto a school.

    Copies the package config into:
      - SchoolModule rows  (via save_school_modules)
      - SchoolFeature rows (via save_school_features)
      - SchoolStudentFormConfig row  (via module_configs.students)
      - SchoolModuleConfig rows      (via save_module_config for other modules)

    Does NOT commit — caller must call db.session.commit().
    """
    from app.models import db, SchoolStudentFormConfig
    from datetime import datetime as dt

    config = package.config or {}

    # ── Modules ───────────────────────────────────────────────────────────────
    modules_cfg = config.get('modules', {})
    if modules_cfg:
        enabled_modules = [k for k, v in modules_cfg.items() if v]
        save_school_modules(school_id, enabled_modules)
    else:
        save_school_modules(school_id, list(MODULES.keys()))  # fail-open

    # ── Features ──────────────────────────────────────────────────────────────
    features_cfg = config.get('features', {})
    if features_cfg:
        enabled_features = [k for k, v in features_cfg.items() if v]
        save_school_features(school_id, enabled_features)
    else:
        save_school_features(school_id, list(FEATURES.keys()))  # fail-open

    # ── Module configs ────────────────────────────────────────────────────────
    module_configs = config.get('module_configs', {})

    # Backward compat: old packages had 'student_form' key instead of module_configs.students
    students_cfg = (module_configs.get('students')
                    or config.get('student_form', {}))

    # Save student form config to SchoolStudentFormConfig (legacy table)
    row = (SchoolStudentFormConfig.query
           .filter_by(school_id=school_id)
           .first())
    if row is None:
        row = SchoolStudentFormConfig(school_id=school_id)
        db.session.add(row)

    row.hidden_sections = students_cfg.get('hidden_sections') or None
    row.hidden_fields   = students_cfg.get('hidden_fields')   or None
    row.required_fields = students_cfg.get('required_fields') or None
    row.updated_at      = dt.utcnow()

    # Save other module configs to SchoolModuleConfig table
    for mk in CONFIGURABLE_MODULES:
        if mk in module_configs:
            save_module_config(school_id, mk, module_configs[mk])


# ─── Config summary helpers ────────────────────────────────────────────────────

def config_summary(config: dict) -> dict:
    """Return human-readable counts for a config dict."""
    if not config:
        return {
            'enabled_modules':  len(MODULES),
            'disabled_modules': 0,
            'enabled_features': len(FEATURES),
            'disabled_features': 0,
            'hidden_sections':  0,
            'hidden_fields':    0,
            'required_fields':  0,
            'disabled_actions': 0,
        }
    modules  = config.get('modules', {})
    features = config.get('features', {})

    enabled_m  = sum(1 for v in modules.values()  if v)
    disabled_m = sum(1 for v in modules.values()  if not v)
    enabled_f  = sum(1 for v in features.values() if v)
    disabled_f = sum(1 for v in features.values() if not v)

    # Aggregate across all module configs
    mc = config.get('module_configs', {})
    total_hidden_sec = sum(len(c.get('hidden_sections', []))  for c in mc.values())
    total_hidden_fld = sum(len(c.get('hidden_fields', []))    for c in mc.values())
    total_req_fld    = sum(len(c.get('required_fields', []))  for c in mc.values())
    total_dis_act    = sum(len(c.get('disabled_actions', [])) for c in mc.values())

    # Also include legacy student_form key for backward compat
    sf = config.get('student_form', {})
    total_hidden_sec += len(sf.get('hidden_sections', []))
    total_hidden_fld += len(sf.get('hidden_fields', []))
    total_req_fld    += len(sf.get('required_fields', []))

    return {
        'enabled_modules':   enabled_m,
        'disabled_modules':  disabled_m,
        'enabled_features':  enabled_f,
        'disabled_features': disabled_f,
        'hidden_sections':   total_hidden_sec,
        'hidden_fields':     total_hidden_fld,
        'required_fields':   total_req_fld,
        'disabled_actions':  total_dis_act,
    }
