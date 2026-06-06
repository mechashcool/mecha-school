"""
Standard Iraqi school subjects helper.

Creates standard subjects per grade for each school/academic year.
Subjects are linked to their respective grade via Subject.grade_id (one-to-many).

Running multiple times is safe — existing subjects are never modified or deleted.
No sections are ever created.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Canonical subject map: grade name → ordered list of subject names.
# Scientific and literary preparatory grades have separate subject lists.
# ---------------------------------------------------------------------------
STANDARD_SUBJECTS_BY_GRADE: dict[str, list[str]] = {
    # ── Primary / ابتدائية ──────────────────────────────────────────────────
    'الصف الأول الابتدائي': [
        'التربية الإسلامية',
        'اللغة العربية',
        'اللغة الإنكليزية',
        'الرياضيات',
        'العلوم',
        'التربية الأخلاقية',
    ],
    'الصف الثاني الابتدائي': [
        'التربية الإسلامية',
        'اللغة العربية',
        'اللغة الإنكليزية',
        'الرياضيات',
        'العلوم',
        'التربية الأخلاقية',
    ],
    'الصف الثالث الابتدائي': [
        'التربية الإسلامية',
        'اللغة العربية',
        'اللغة الإنكليزية',
        'الرياضيات',
        'العلوم',
    ],
    'الصف الرابع الابتدائي': [
        'التربية الإسلامية',
        'اللغة العربية',
        'اللغة الإنكليزية',
        'الرياضيات',
        'العلوم',
        'الاجتماعيات',
    ],
    'الصف الخامس الابتدائي': [
        'التربية الإسلامية',
        'اللغة العربية',
        'اللغة الإنكليزية',
        'الرياضيات',
        'العلوم',
        'الاجتماعيات',
    ],
    'الصف السادس الابتدائي': [
        'التربية الإسلامية',
        'اللغة العربية',
        'اللغة الإنكليزية',
        'الرياضيات',
        'العلوم',
        'الاجتماعيات',
    ],

    # ── Intermediate / متوسطة ────────────────────────────────────────────────
    'الصف الأول المتوسط': [
        'التربية الإسلامية',
        'اللغة العربية',
        'اللغة الإنكليزية',
        'الرياضيات',
        'الاجتماعيات',
        'الأحياء',
        'الكيمياء',
        'الفيزياء',
        'الحاسوب',
        'التربية الأخلاقية',
        'اللغة الفرنسية',
    ],
    'الصف الثاني المتوسط': [
        'التربية الإسلامية',
        'اللغة العربية',
        'اللغة الإنكليزية',
        'الرياضيات',
        'الاجتماعيات',
        'الأحياء',
        'الكيمياء',
        'الفيزياء',
        'الحاسوب',
        'التربية الأخلاقية',
        'اللغة الفرنسية',
    ],
    'الصف الثالث المتوسط': [
        'التربية الإسلامية',
        'اللغة العربية',
        'اللغة الإنكليزية',
        'الرياضيات',
        'الاجتماعيات',
        'الأحياء',
        'الكيمياء',
        'الفيزياء',
        'اللغة الفرنسية',
    ],

    # ── Preparatory Scientific / إعدادية علمي ───────────────────────────────
    'الصف الرابع الإعدادي العلمي': [
        'التربية الإسلامية',
        'اللغة العربية',
        'اللغة الإنكليزية',
        'الرياضيات',
        'الفيزياء',
        'الكيمياء',
        'الأحياء',
        'الحاسوب',
        'اللغة الفرنسية',
        'اللغة الكردية',
        'جرائم حزب البعث',
    ],
    'الصف الخامس الإعدادي العلمي': [
        'التربية الإسلامية',
        'اللغة العربية',
        'اللغة الإنكليزية',
        'الرياضيات',
        'الفيزياء',
        'الكيمياء',
        'الأحياء',
        'علم الأرض',
        'الحاسوب',
        'اللغة الفرنسية',
        'اللغة الكردية',
    ],
    'الصف السادس الإعدادي العلمي': [
        'التربية الإسلامية',
        'اللغة العربية',
        'اللغة الإنكليزية',
        'الرياضيات',
        'الفيزياء',
        'الكيمياء',
        'الأحياء',
    ],

    # ── Preparatory Literary / إعدادية أدبي ─────────────────────────────────
    'الصف الرابع الإعدادي الأدبي': [
        'التربية الإسلامية',
        'اللغة العربية',
        'اللغة الإنكليزية',
        'الرياضيات',
        'التاريخ',
        'أسس الجغرافية',
        'علم الاجتماع',
        'الحاسوب',
        'اللغة الفرنسية',
        'اللغة الكردية',
        'جرائم حزب البعث',
    ],
    'الصف الخامس الإعدادي الأدبي': [
        'التربية الإسلامية',
        'اللغة العربية',
        'اللغة الإنكليزية',
        'الرياضيات',
        'التاريخ',
        'الجغرافية',
        'الاقتصاد',
        'الفلسفة وعلم النفس',
        'الحاسوب',
        'اللغة الفرنسية',
        'اللغة الكردية',
        'البلاغة',
    ],
    'الصف السادس الإعدادي الأدبي': [
        'التربية الإسلامية',
        'اللغة العربية',
        'اللغة الإنكليزية',
        'الرياضيات',
        'التاريخ',
        'الجغرافية',
        'الاقتصاد',
        'الأدب والنصوص',
        'النقد',
    ],
}

_WS_RE = re.compile(r'\s+')


def _normalize(name: str) -> str:
    """Collapse runs of whitespace to a single space and strip ends."""
    return _WS_RE.sub(' ', name).strip()


def ensure_standard_subjects(school_id: int, academic_year_id: int) -> dict:
    """
    Add missing standard subjects for each standard grade in the given school+year.
    Each subject is linked to its grade via Subject.grade_id.

    Returns {
        'created_subjects': int,
        'skipped_subjects': int,
        'skipped_grades':   list[str],  # grade names not found in this school/year
    }.

    Rules:
    - One Subject record is created per grade per subject name.
    - Existing subjects are matched by normalized name within the same grade.
    - Grades not found in the school/year are silently skipped and listed in
      skipped_grades (normal for schools that haven't run grade setup yet).
    - Custom subjects and grades are never modified or deleted.
    - No sections are created.
    - Does NOT commit — the caller is responsible for db.session.commit().
    """
    from app.models import db, Grade, Subject

    # Build a lookup: normalized grade name → Grade instance
    all_grades = (
        Grade.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(school_id=school_id, academic_year_id=academic_year_id)
        .all()
    )
    grade_map: dict[str, Grade] = {_normalize(g.name): g for g in all_grades}

    created = 0
    skipped = 0
    skipped_grades: list[str] = []

    for grade_name, subject_names in STANDARD_SUBJECTS_BY_GRADE.items():
        grade = grade_map.get(_normalize(grade_name))
        if grade is None:
            skipped_grades.append(grade_name)
            continue

        # Fetch existing subject names already linked to this specific grade
        existing_rows = (
            Subject.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(
                school_id        = school_id,
                academic_year_id = academic_year_id,
                grade_id         = grade.id,
            )
            .with_entities(Subject.name)
            .all()
        )
        existing_names = {_normalize(row.name) for row in existing_rows}

        for name in subject_names:
            if _normalize(name) in existing_names:
                skipped += 1
                continue
            db.session.add(Subject(
                name             = name,
                school_id        = school_id,
                academic_year_id = academic_year_id,
                grade_id         = grade.id,
                stage            = grade.stage,
            ))
            created += 1

    return {
        'created_subjects': created,
        'skipped_subjects': skipped,
        'skipped_grades':   skipped_grades,
    }
