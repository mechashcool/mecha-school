"""
employee_classification.py
--------------------------
School-scoped employee classification: a fixed list of default job titles and a
default specialty ("القسم / الاختصاص") list per job title, plus per-school CUSTOM
job titles and custom specialties added through the "أخرى" option.

Design (smallest safe change):
  * The employee's chosen values are still stored as plain text in the existing
    ``Employee.job_title`` and ``Employee.department`` columns — no new FK, no
    schema change to the employees table.
  * The reusable per-school custom options are persisted in the EXISTING
    ``SchoolModuleConfig`` table (school-scoped, cascade-deleted with the school)
    under a dedicated ``module_key = 'employee_classification'`` row, so NO new
    table / migration is required.

Stored JSON shape (per school)::

    {
      "job_titles":  ["مشرف مختبر", ...],            # custom job titles only
      "specialties": {                               # keyed by job-title text
          "فني":        ["أجهزة إلكترونية", ...],    # custom specialties only
          "مشرف مختبر": [...]
      }
    }

Defaults are never stored — only school-added custom values are persisted.
Every read/write is scoped by ``school_id``; a value added by one school can
never appear for, or be validated against, another school.
"""
from __future__ import annotations

import re

# ─────────────────────────────────────────────────────────────────────────────
#  Constants — default classification
# ─────────────────────────────────────────────────────────────────────────────

MODULE_KEY = 'employee_classification'

# UI sentinel — never stored as a real employee value.
OTHER = 'أخرى'

# Default job titles (display order). 'أخرى' is a UI-only sentinel, not listed.
DEFAULT_JOB_TITLES: list[str] = [
    'معلم',
    'مدرس',
    'إداري',
    'محاسب',
    'عامل خدمة',
    'حارس',
    'سائق',
    'فني',
]

# Shared teacher subject list — 'معلم' and 'مدرس' use the SAME specialties.
_TEACHER_SPECIALTIES: list[str] = [
    'اللغة العربية',
    'اللغة الإنكليزية',
    'الرياضيات',
    'العلوم',
    'الفيزياء',
    'الكيمياء',
    'الأحياء',
    'الاجتماعيات',
    'التربية الإسلامية',
    'الحاسوب',
    'التربية الرياضية',
    'التربية الفنية',
    'الصفوف الأولية',
    'رياض الأطفال',
]

# Default specialty mapping per job title (without the 'أخرى' sentinel).
DEFAULT_SPECIALTIES: dict[str, list[str]] = {
    'معلم':      _TEACHER_SPECIALTIES,
    'مدرس':      _TEACHER_SPECIALTIES,
    'إداري':     ['الإدارة', 'شؤون الطلبة والتسجيل',
                  'السكرتارية والاستعلامات', 'الموارد البشرية'],
    'محاسب':     ['الحسابات والمالية'],
    'عامل خدمة': ['الخدمات'],
    'حارس':      ['الأمن والحراسة'],
    'سائق':      ['النقل'],
    'فني':       ['الصيانة', 'تقنية المعلومات'],
}


# ─────────────────────────────────────────────────────────────────────────────
#  Normalisation helpers
# ─────────────────────────────────────────────────────────────────────────────

def normalize_display(value: str | None) -> str:
    """Trim ends and collapse internal whitespace runs to a single space.

    This is the value actually stored/displayed. Rejects nothing — callers must
    check the result for emptiness.
    """
    return re.sub(r'\s+', ' ', (value or '').strip())


def _norm_key(value: str | None) -> str:
    """Normalised comparison key for duplicate detection.

    Collapses whitespace and case-folds (a no-op for Arabic, but guards mixed
    Latin custom values) so that "  فني  " and "فني" — or "IT" and "it" — are
    treated as the same option.
    """
    return normalize_display(value).casefold()


# ─────────────────────────────────────────────────────────────────────────────
#  Storage (SchoolModuleConfig, module_key='employee_classification')
# ─────────────────────────────────────────────────────────────────────────────

def _load(school_id: int | None) -> dict:
    """Return the school's stored custom-options dict (never None).

    Shape is always ``{'job_titles': [...], 'specialties': {title: [...]}}``.
    """
    empty = {'job_titles': [], 'specialties': {}}
    if not school_id:
        return empty
    from app.models import SchoolModuleConfig
    row = (SchoolModuleConfig.query
           .filter_by(school_id=school_id, module_key=MODULE_KEY)
           .first())
    cfg = (row.config if row else None) or {}
    return {
        'job_titles':  list(cfg.get('job_titles') or []),
        'specialties': dict(cfg.get('specialties') or {}),
    }


def _save(school_id: int, data: dict) -> None:
    """Upsert the school's custom-options row. Does NOT commit — caller commits.

    A fresh dict is assigned to ``config`` so SQLAlchemy detects the change
    (in-place JSON mutation would not be tracked).
    """
    from app.models import db, SchoolModuleConfig
    from datetime import datetime as _dt
    row = (SchoolModuleConfig.query
           .filter_by(school_id=school_id, module_key=MODULE_KEY)
           .first())
    if row is None:
        row = SchoolModuleConfig(school_id=school_id, module_key=MODULE_KEY)
        db.session.add(row)
    row.config = {
        'job_titles':  list(data.get('job_titles') or []),
        'specialties': dict(data.get('specialties') or {}),
    }
    row.updated_at = _dt.utcnow()


# ─────────────────────────────────────────────────────────────────────────────
#  Read helpers
# ─────────────────────────────────────────────────────────────────────────────

def custom_job_titles(school_id: int | None) -> list[str]:
    return _load(school_id)['job_titles']


def selectable_job_titles(school_id: int | None) -> list[str]:
    """Default job titles followed by the school's custom titles (deduped)."""
    out: list[str] = list(DEFAULT_JOB_TITLES)
    seen = {_norm_key(t) for t in out}
    for t in custom_job_titles(school_id):
        if _norm_key(t) not in seen:
            seen.add(_norm_key(t))
            out.append(t)
    return out


def specialties_for_title(school_id: int | None, job_title: str) -> list[str]:
    """Default specialties for the title + the school's custom specialties.

    Excludes the 'أخرى' sentinel. Order: defaults first, then custom.
    """
    out: list[str] = list(DEFAULT_SPECIALTIES.get(job_title, []))
    seen = {_norm_key(s) for s in out}
    for s in _load(school_id)['specialties'].get(job_title, []):
        if _norm_key(s) not in seen:
            seen.add(_norm_key(s))
            out.append(s)
    return out


def classification_map(school_id: int | None) -> dict[str, list[str]]:
    """Full {job_title: [specialties..., 'أخرى']} map used by the dependent
    dropdown JS. Every title (default + custom) always ends with 'أخرى'."""
    m: dict[str, list[str]] = {}
    for t in selectable_job_titles(school_id):
        m[t] = specialties_for_title(school_id, t) + [OTHER]
    return m


# ─────────────────────────────────────────────────────────────────────────────
#  Validation helpers (server-side — never trust the browser dropdown)
# ─────────────────────────────────────────────────────────────────────────────

def job_title_exists(school_id: int | None, value: str) -> bool:
    n = _norm_key(value)
    return any(_norm_key(t) == n for t in selectable_job_titles(school_id))


def specialty_exists(school_id: int | None, job_title: str, value: str) -> bool:
    n = _norm_key(value)
    return any(_norm_key(s) == n for s in specialties_for_title(school_id, job_title))


def is_valid_job_title(school_id: int | None, value: str,
                       preserved: str | None = None) -> bool:
    """True when ``value`` is a default title, a custom title of THIS school, or
    equals the employee's preserved (grandfathered) title."""
    if job_title_exists(school_id, value):
        return True
    return bool(preserved and _norm_key(preserved) == _norm_key(value))


def is_valid_specialty(school_id: int | None, job_title: str, value: str,
                       preserved: str | None = None) -> bool:
    """True when ``value`` is empty (specialty optional), a valid specialty for
    ``job_title`` in THIS school, or equals the employee's preserved specialty
    (only pass ``preserved`` when the job title is unchanged)."""
    if not value:
        return True
    if specialty_exists(school_id, job_title, value):
        return True
    return bool(preserved and _norm_key(preserved) == _norm_key(value))


# ─────────────────────────────────────────────────────────────────────────────
#  Write helpers — add a custom option for one school (no commit)
# ─────────────────────────────────────────────────────────────────────────────

def add_custom_job_title(school_id: int, value: str) -> None:
    """Persist ``value`` as a custom job title for this school if it is not
    already a default or existing custom title. Caller commits."""
    if not school_id:
        return
    display = normalize_display(value)
    if not display or job_title_exists(school_id, display):
        return
    data = _load(school_id)
    data['job_titles'].append(display)
    _save(school_id, data)


def add_custom_specialty(school_id: int, job_title: str, value: str) -> None:
    """Persist ``value`` as a custom specialty under ``job_title`` for this
    school if it is not already a default/custom specialty of that title.
    Caller commits."""
    if not school_id:
        return
    display = normalize_display(value)
    job_title = normalize_display(job_title)
    if not display or not job_title:
        return
    if specialty_exists(school_id, job_title, display):
        return
    data = _load(school_id)
    data['specialties'].setdefault(job_title, []).append(display)
    _save(school_id, data)


# ─────────────────────────────────────────────────────────────────────────────
#  Employee-list filter data
# ─────────────────────────────────────────────────────────────────────────────

def build_filter_context(school_id: int | None,
                         db_job_titles: list[str],
                         db_pairs: list[tuple[str, str]]) -> dict:
    """Build the option lists for the employee-list filters.

    ``db_job_titles`` — distinct non-empty job_title values used by employees of
    this school. ``db_pairs`` — distinct (job_title, department) pairs used.

    Returns::

        {
          'job_titles':      [...],                 # defaults + custom + used
          'spec_map':        {title: [specialties]},# for client narrowing
          'all_specialties': [...],                 # union for "no title" case
        }
    """
    # Job titles: defaults + custom, then any extra title actually used in DB.
    titles = selectable_job_titles(school_id)
    seen_t = {_norm_key(t) for t in titles}
    for t in db_job_titles:
        if t and _norm_key(t) not in seen_t:
            seen_t.add(_norm_key(t))
            titles.append(t)

    # Per-title specialty map (defaults + custom).
    spec_map: dict[str, list[str]] = {
        t: specialties_for_title(school_id, t) for t in titles
    }
    # Augment with department values actually used with each title in DB.
    for jt, dep in db_pairs:
        if not jt or not dep:
            continue
        bucket = spec_map.setdefault(jt, list(DEFAULT_SPECIALTIES.get(jt, [])))
        if all(_norm_key(dep) != _norm_key(s) for s in bucket):
            bucket.append(dep)
        if jt and _norm_key(jt) not in seen_t:
            seen_t.add(_norm_key(jt))
            titles.append(jt)

    # Union of every specialty (for the "no job title selected" filter case).
    all_specs: list[str] = []
    seen_s: set[str] = set()
    for bucket in spec_map.values():
        for s in bucket:
            if _norm_key(s) not in seen_s:
                seen_s.add(_norm_key(s))
                all_specs.append(s)
    # Include any department used in DB that is not attached to a known title.
    for _jt, dep in db_pairs:
        if dep and _norm_key(dep) not in seen_s:
            seen_s.add(_norm_key(dep))
            all_specs.append(dep)

    return {
        'job_titles':      titles,
        'spec_map':        spec_map,
        'all_specialties': all_specs,
    }
