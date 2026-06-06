"""
Iraqi standard school grades helper.

Iraqi public education has 12 standard grades across three stages:
  - ابتدائية  (Primary)     : grades 1–6
  - متوسطة   (Intermediate) : grades 7–9
  - إعدادية  (Preparatory)  : grades 10–12

ensure_iraqi_standard_grades(school_id, academic_year_id) creates any
missing grades for the given school+year and returns a result dict.
It is fully idempotent — safe to call multiple times.
No sections are ever created.
"""
from __future__ import annotations

import re

# Canonical list — (name, stage).  Order defines visual display order.
IRAQI_STANDARD_GRADES: list[tuple[str, str]] = [
    ('الصف الأول الابتدائي',   'ابتدائية'),
    ('الصف الثاني الابتدائي',  'ابتدائية'),
    ('الصف الثالث الابتدائي',  'ابتدائية'),
    ('الصف الرابع الابتدائي',  'ابتدائية'),
    ('الصف الخامس الابتدائي',  'ابتدائية'),
    ('الصف السادس الابتدائي',  'ابتدائية'),
    ('الصف الأول المتوسط',     'متوسطة'),
    ('الصف الثاني المتوسط',    'متوسطة'),
    ('الصف الثالث المتوسط',    'متوسطة'),
    ('الصف الرابع الإعدادي',   'إعدادية'),
    ('الصف الخامس الإعدادي',   'إعدادية'),
    ('الصف السادس الإعدادي',   'إعدادية'),
]

_WS_RE = re.compile(r'\s+')


def _normalize(name: str) -> str:
    """Collapse runs of whitespace to a single space and strip ends."""
    return _WS_RE.sub(' ', name).strip()


def ensure_iraqi_standard_grades(school_id: int, academic_year_id: int) -> dict:
    """
    Add any missing standard Iraqi grades to the given school + academic year.

    Returns {'created': int, 'skipped': int}.

    Rules:
    - Existing grades are matched by normalized Arabic name.
    - Missing grades are added; existing ones are never modified or deleted.
    - Custom (non-standard) grades are never touched.
    - No sections are created.
    - Does NOT commit — the caller is responsible for db.session.commit().
    """
    from app.models import db, Grade

    existing_rows = (
        Grade.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(school_id=school_id, academic_year_id=academic_year_id)
        .with_entities(Grade.name)
        .all()
    )
    existing_names = {_normalize(row.name) for row in existing_rows}

    created = 0
    skipped = 0

    for name, stage in IRAQI_STANDARD_GRADES:
        if _normalize(name) in existing_names:
            skipped += 1
            continue
        db.session.add(Grade(
            name             = name,
            stage            = stage,
            school_id        = school_id,
            academic_year_id = academic_year_id,
        ))
        created += 1

    return {'created': created, 'skipped': skipped}
