"""
Per-school educational-stage configuration (``School.educational_stages``).

Three states, decided by the stored column:

  LEGACY  (NULL or empty) — the school predates this feature.  Nothing here
                    ever filters or provisions for it: its grades, sections,
                    subjects, default-setup buttons and public registration
                    behave exactly as they did before this feature existed.
                    Existing rows are never backfilled or inferred.

  MANAGED (valid value) — the Super Admin explicitly chose one or more stages.
                    Those stages govern AUTOMATIC default provisioning and the
                    public registration grade list.  They deliberately do NOT
                    restrict manual grade/section/subject CRUD, which keeps all
                    of its existing freedom.

  INVALID (non-empty but unparseable) — never silently downgraded to LEGACY.
                    Every consumer fails closed with ``InvalidStageConfiguration``
                    so a corrupt value can never quietly disable filtering; the
                    Super Admin repairs it by saving a valid selection, and that
                    repair deletes nothing.

Everything in this module is:
  * school-scoped — every query filters an explicit, already-authorized
    ``school_id``; nothing is ever resolved from client input here;
  * inert when the selection does not change — see ``apply_school_stages``;
  * fail-closed on removal — a stage is removed only when the code has proven,
    across EVERY academic year and through every real relationship, that
    nothing depends on it.  Student and academic data is never cascade-deleted,
    and only CURRENT-year structural rows are ever deleted.

None of the helpers commit.  The calling route owns the transaction so a
rejected change leaves the school and all of its records untouched.
"""
from __future__ import annotations

from app.utils.iraqi_grades import (IRAQI_STANDARD_GRADES,
                                    ensure_iraqi_standard_grades,
                                    _normalize)
from app.utils.iraqi_subjects import ensure_standard_subjects

# ── Canonical stage tokens — identical strings to Grade.stage / Subject.stage ──
STAGE_PRIMARY      = 'ابتدائية'
STAGE_INTERMEDIATE = 'متوسطة'
STAGE_PREPARATORY  = 'إعدادية'

# Display order == storage order.
ALL_STAGES: tuple[str, ...] = (STAGE_PRIMARY, STAGE_INTERMEDIATE, STAGE_PREPARATORY)

STAGE_LABELS: dict[str, str] = {
    STAGE_PRIMARY:      'المرحلة الابتدائية',
    STAGE_INTERMEDIATE: 'المرحلة المتوسطة',
    STAGE_PREPARATORY:  'المرحلة الإعدادية',
}

# The single section created for every automatically provisioned grade.
# Manual section creation is unaffected and unrestricted.
DEFAULT_SECTION_NAME = 'أ'

# ── Operator-facing Arabic messages ───────────────────────────────────────────
ERR_INVALID_STAGE  = 'قيمة المرحلة الدراسية غير صالحة.'
ERR_NO_STAGE       = 'يجب اختيار مرحلة دراسية واحدة على الأقل.'
ERR_STAGE_LINKED   = ('لا يمكن إزالة هذه المرحلة لوجود بيانات مرتبطة بها. '
                      'يرجى معالجة البيانات المرتبطة أولاً.')
ERR_STORED_INVALID = ('إعدادات المراحل الدراسية المحفوظة لهذه المدرسة غير صالحة. '
                      'يرجى إعادة تحديد المراحل الدراسية وحفظها لتصحيح الإعداد.')
ERR_AMBIGUOUS      = ('لا يمكن إزالة هذه المرحلة: توجد صفوف تصنيفها غير واضح بين '
                      'المرحلة المطلوب إزالتها ومرحلة أخرى مُبقاة. يرجى تصحيح تصنيف '
                      'هذه الصفوف أولاً.')


class InvalidStageConfiguration(ValueError):
    """``School.educational_stages`` is non-empty but is not a valid selection.

    Raised instead of returning ``[]`` so that a corrupt value can never be
    mistaken for LEGACY mode and silently turn stage filtering off.
    """


# ═════════════════════════════════════════════════════════════════════════════
#  Parsing / serialization
# ═════════════════════════════════════════════════════════════════════════════

def parse_stages(raw) -> list[str]:
    """Normalize a selection into canonical order, de-duplicated.

    Accepts the stored comma-separated string, a list (``request.form.getlist``)
    or None.  Raises ``ValueError`` on any token that is not a known stage —
    an unknown value is rejected, never silently dropped.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        tokens = [t.strip() for t in raw.split(',')]
    else:
        tokens = [str(t).strip() for t in raw]
    tokens = [t for t in tokens if t]

    unknown = [t for t in tokens if t not in ALL_STAGES]
    if unknown:
        raise ValueError(ERR_INVALID_STAGE)
    return [s for s in ALL_STAGES if s in set(tokens)]


def serialize_stages(stages) -> str | None:
    """Canonical storage form; None when nothing is selected (legacy)."""
    ordered = parse_stages(stages)
    return ','.join(ordered) if ordered else None


def school_stages(school) -> list[str]:
    """Configured stages of a School.

    Returns ``[]`` for LEGACY (stored value NULL or blank).
    Raises ``InvalidStageConfiguration`` when the stored value is non-empty but
    does not parse to at least one known stage — callers must fail closed
    rather than treat that as legacy.
    """
    raw = getattr(school, 'educational_stages', None)
    if raw is None:
        return []
    text = raw if isinstance(raw, str) else str(raw)
    if not text.strip():
        return []                       # LEGACY
    try:
        stages = parse_stages(text)
    except ValueError:
        raise InvalidStageConfiguration(ERR_STORED_INVALID)
    if not stages:                      # e.g. ",,," — non-empty but selects nothing
        raise InvalidStageConfiguration(ERR_STORED_INVALID)
    return stages


def stage_grade_names(stages) -> list[str]:
    """Canonical grade names of the given stages, in canonical order.

    For ``إعدادية`` this is all SIX preparatory grades — the scientific and the
    literary track of years four, five and six — exactly as they appear in
    ``IRAQI_STANDARD_GRADES``.  Nothing is renamed or merged.
    """
    wanted = set(parse_stages(stages))
    return [name for name, stage in IRAQI_STANDARD_GRADES if stage in wanted]


def _stage_grade_name_set(stages) -> set[str]:
    return {_normalize(n) for n in stage_grade_names(stages)}


def grade_matches_stages(grade, stages) -> bool:
    """A grade belongs to ``stages`` by canonical name OR by its stage column."""
    wanted = set(parse_stages(stages))
    if not wanted:
        return False
    if (grade.stage or '').strip() in wanted:
        return True
    return _normalize(grade.name or '') in _stage_grade_name_set(wanted)


# ═════════════════════════════════════════════════════════════════════════════
#  Row lookup — always school-scoped, explicitly
# ═════════════════════════════════════════════════════════════════════════════

def _school_grades(school_id: int, academic_year_id: int | None = None):
    """Every grade of ONE school.  ``bypass_tenant_scope`` disables both the
    school and the year criteria, so both are re-applied explicitly here."""
    from app.models import Grade
    q = (Grade.query
         .execution_options(bypass_tenant_scope=True)
         .filter(Grade.school_id == school_id))
    if academic_year_id is not None:
        q = q.filter(Grade.academic_year_id == academic_year_id)
    return q.all()


def stage_grades(school_id: int, stages, academic_year_id: int | None = None):
    """Grades of this school that belong to the given stages."""
    wanted = parse_stages(stages)
    if not wanted:
        return []
    return [g for g in _school_grades(school_id, academic_year_id)
            if grade_matches_stages(g, wanted)]


def detect_configured_stages(school_id: int) -> list[str]:
    """Stages the school's EXISTING data already represents (any year).

    Consulted ONLY when a legacy school explicitly enters managed mode, so that
    a stage its data already uses cannot be dropped without running the
    dependency guard.  A school that is already managed derives removals from
    its STORED configuration alone — see ``apply_school_stages``.

    Grades are not the only way a stage is represented.  A stage is also
    present when the school has a stage-only subject (``grade_id IS NULL`` with
    a ``stage`` value) or a stage-scoped chat room (``ChatRoom.stage``, a bare
    string with no foreign key).  Detecting by grades alone would leave those
    stages out of ``removed``, letting them skip the rejection guard entirely.
    This is dependency detection only — nothing here reads or changes chat
    behaviour.
    """
    from app.models import Subject, ChatRoom

    present = set()
    for g in _school_grades(school_id):
        for stage in ALL_STAGES:
            if grade_matches_stages(g, [stage]):
                present.add(stage)
                break

    stage_only_subjects = (Subject.query
                           .execution_options(bypass_tenant_scope=True)
                           .filter(Subject.school_id == school_id,
                                   Subject.grade_id.is_(None),
                                   Subject.stage.in_(ALL_STAGES))
                           .with_entities(Subject.stage).distinct().all())
    present.update(row.stage for row in stage_only_subjects)

    stage_rooms = (ChatRoom.query
                   .execution_options(bypass_tenant_scope=True)
                   .filter(ChatRoom.school_id == school_id,
                           ChatRoom.stage.in_(ALL_STAGES))
                   .with_entities(ChatRoom.stage).distinct().all())
    present.update(row.stage for row in stage_rooms)

    return [s for s in ALL_STAGES if s in present]


def ambiguous_stage_grades(school_id: int, removed, kept) -> list:
    """Grades that match BOTH a removed and a retained stage.

    Example: a row named ``الصف الأول الابتدائي`` whose ``stage`` column was
    manually set to ``متوسطة``.  Removing متوسطة while keeping ابتدائية would
    make it a deletion candidate even though its name belongs to a retained
    stage.  Rather than silently renaming or reclassifying operator data, the
    whole edit is rejected and the operator fixes the classification.
    """
    rem = parse_stages(removed)
    keep = parse_stages(kept)
    if not rem or not keep:
        return []
    return [g for g in _school_grades(school_id)
            if grade_matches_stages(g, rem) and grade_matches_stages(g, keep)]


# ═════════════════════════════════════════════════════════════════════════════
#  Candidate set — ONE definition shared by the guard and the deletion
# ═════════════════════════════════════════════════════════════════════════════

def removal_candidates(school_id: int, stages, academic_year_id: int | None = None,
                       *, lock: bool = False) -> dict:
    """Structural rows attributable to ``stages`` for ONE school.

    ``academic_year_id is None`` → every academic year.  Used by the dependency
    guard, so historical linked data blocks a removal too.
    ``academic_year_id = <id>``  → that year only.  Used by the deletion, so
    historical grade/section/subject rows are preserved.

    The deletion set is therefore always a subset of the guarded set: same
    classification rules, narrower year filter.

    Subjects are attributable two ways — linked to one of the stage's grades,
    or a stage-only subject (``grade_id IS NULL``) whose ``stage`` column names
    a stage being removed.  A subject with neither is never attributed and
    never deleted.

    ``lock=True`` takes ``SELECT … FOR UPDATE`` on every candidate row.  Adding
    a dependent row requires ``FOR KEY SHARE`` on its parent, which conflicts,
    so a concurrent INSERT cannot slip between the guard and the delete.
    """
    from app.models import db, Grade, Section, Subject

    wanted = parse_stages(stages)
    empty = {'grades': [], 'sections': [], 'subjects': []}
    if not wanted:
        return empty

    gq = (Grade.query
          .execution_options(bypass_tenant_scope=True)
          .filter(Grade.school_id == school_id))
    if academic_year_id is not None:
        gq = gq.filter(Grade.academic_year_id == academic_year_id)
    if lock:
        gq = gq.with_for_update()
    grades = [g for g in gq.all() if grade_matches_stages(g, wanted)]
    grade_ids = [g.id for g in grades]

    sections = []
    if grade_ids:
        sq = (Section.query
              .execution_options(bypass_tenant_scope=True)
              .filter(Section.school_id == school_id,
                      Section.grade_id.in_(grade_ids)))
        if academic_year_id is not None:
            sq = sq.filter(Section.academic_year_id == academic_year_id)
        if lock:
            sq = sq.with_for_update()
        sections = sq.all()

    subject_clauses = [db.and_(Subject.grade_id.is_(None),
                               Subject.stage.in_(wanted))]
    if grade_ids:
        subject_clauses.append(Subject.grade_id.in_(grade_ids))
    subq = (Subject.query
            .execution_options(bypass_tenant_scope=True)
            .filter(Subject.school_id == school_id,
                    db.or_(*subject_clauses)))
    if academic_year_id is not None:
        subq = subq.filter(Subject.academic_year_id == academic_year_id)
    if lock:
        subq = subq.with_for_update()
    subjects = subq.all()

    return {'grades': grades, 'sections': sections, 'subjects': subjects}


# ═════════════════════════════════════════════════════════════════════════════
#  Provisioning  (create-only, idempotent, no commit)
# ═════════════════════════════════════════════════════════════════════════════

def _ensure_default_sections(school_id: int, academic_year_id: int, grades) -> int:
    """Create the single ``أ`` section for each grade that lacks one.

    Matched on the normalized name so a pre-existing ``أ`` (any spacing) is
    reused — never duplicated.  Other sections are never touched, and manual
    section creation stays completely unrestricted.
    """
    from app.models import db, Section

    if not grades:
        return 0
    grade_ids = [g.id for g in grades]
    existing = (Section.query
                .execution_options(bypass_tenant_scope=True)
                .filter(Section.school_id == school_id,
                        Section.academic_year_id == academic_year_id,
                        Section.grade_id.in_(grade_ids))
                .with_entities(Section.grade_id, Section.name)
                .all())
    have = {row.grade_id for row in existing
            if _normalize(row.name or '') == _normalize(DEFAULT_SECTION_NAME)}

    created = 0
    for g in grades:
        if g.id in have:
            continue
        db.session.add(Section(
            name             = DEFAULT_SECTION_NAME,
            school_id        = school_id,
            academic_year_id = academic_year_id,
            grade_id         = g.id,
        ))
        created += 1
    return created


def provision_stages(school_id: int, academic_year_id: int, stages) -> dict:
    """Create the missing grades / ``أ`` sections / standard subjects of
    ``stages`` for ONE school + academic year.

    Called only for stages that are NEWLY added (or for a brand-new school).
    Fully idempotent: existing rows are reused, nothing is renamed, nothing is
    deleted, and grades outside ``stages`` are not touched.  Does NOT commit.
    """
    from app.models import db

    wanted = parse_stages(stages)
    if not wanted or not academic_year_id:
        return {'grades_created': 0, 'sections_created': 0, 'subjects_created': 0}

    grade_result = ensure_iraqi_standard_grades(school_id, academic_year_id,
                                                only_stages=wanted)
    db.session.flush()   # grade ids must exist before sections/subjects link

    grades = stage_grades(school_id, wanted, academic_year_id)
    sections_created = _ensure_default_sections(school_id, academic_year_id, grades)

    subject_result = ensure_standard_subjects(
        school_id, academic_year_id,
        only_grade_names=stage_grade_names(wanted),
    )
    db.session.flush()

    return {
        'grades_created':   grade_result['created'],
        'sections_created': sections_created,
        'subjects_created': subject_result['created_subjects'],
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Removal guard — every real relationship, across every academic year
# ═════════════════════════════════════════════════════════════════════════════

def stage_dependency_counts(school_id: int, stages, *, lock: bool = False) -> dict:
    """Count everything that depends on ``stages`` in ONE school, across ALL of
    its academic years, so historical links block a removal too.

    Covers every foreign key that actually references grades/sections/subjects
    in the schema, plus the one stage reference that is a bare string:

      stage string ← ChatRoom.stage            (scope='stage' rooms have no grade_id)
      grades       ← Schedule.grade_id, ChatRoom.grade_id,
                     StudentRegistrationRequest.desired_grade_id
      sections     ← Student.section_id, Exam.section_id, Schedule.section_id,
                     Homework.section_id, ChatRoom.section_id,
                     teacher_subjects.section_id
      subjects     ← Exam.subject_id, Schedule.subject_id, Homework.subject_id,
                     ChatRoom.subject_id, teacher_subjects.subject_id

    Section.grade_id and Subject.grade_id are the structural children the
    removal itself deletes, so they are not blockers.  Any non-zero count here
    rejects the whole stage change.
    """
    from app.models import (db, Student, Exam, Schedule, Homework, ChatRoom,
                            StudentRegistrationRequest, teacher_subjects)

    counts = {
        'students': 0, 'exams': 0, 'schedules': 0, 'homework': 0,
        'chat_rooms': 0, 'registration_requests': 0, 'teacher_assignments': 0,
    }

    wanted = parse_stages(stages)
    if not wanted:
        return counts

    cand = removal_candidates(school_id, wanted, None, lock=lock)
    grade_ids   = [g.id for g in cand['grades']]
    section_ids = [s.id for s in cand['sections']]
    subject_ids = [s.id for s in cand['subjects']]

    def _count(model, column, ids):
        if not ids:
            return 0
        return (model.query
                .execution_options(bypass_tenant_scope=True)
                .filter(model.school_id == school_id, column.in_(ids))
                .count())

    # ── grade-level dependents ────────────────────────────────────────────────
    counts['schedules'] += _count(Schedule, Schedule.grade_id, grade_ids)
    counts['registration_requests'] += _count(
        StudentRegistrationRequest,
        StudentRegistrationRequest.desired_grade_id, grade_ids)

    # ── section-level dependents ──────────────────────────────────────────────
    counts['students'] += _count(Student, Student.section_id, section_ids)
    counts['exams'] += _count(Exam, Exam.section_id, section_ids)
    counts['schedules'] += _count(Schedule, Schedule.section_id, section_ids)
    counts['homework'] += _count(Homework, Homework.section_id, section_ids)

    # ── subject-level dependents ──────────────────────────────────────────────
    counts['exams'] += _count(Exam, Exam.subject_id, subject_ids)
    counts['schedules'] += _count(Schedule, Schedule.subject_id, subject_ids)
    counts['homework'] += _count(Homework, Homework.subject_id, subject_ids)

    # ── chat rooms: one query over every way a room can reference the stage ───
    # ChatRoom.stage is a bare string with no FK, so a scope='stage' room blocks
    # the removal even when the stage has no grade rows at all.
    chat_clauses = [ChatRoom.stage.in_(wanted)]
    if grade_ids:
        chat_clauses.append(ChatRoom.grade_id.in_(grade_ids))
    if section_ids:
        chat_clauses.append(ChatRoom.section_id.in_(section_ids))
    if subject_ids:
        chat_clauses.append(ChatRoom.subject_id.in_(subject_ids))
    counts['chat_rooms'] = (ChatRoom.query
                            .execution_options(bypass_tenant_scope=True)
                            .filter(ChatRoom.school_id == school_id,
                                    db.or_(*chat_clauses))
                            .count())

    # ── teacher ↔ subject ↔ section assignment junction (no school_id column) ─
    if section_ids or subject_ids:
        clauses = []
        if section_ids:
            clauses.append(teacher_subjects.c.section_id.in_(section_ids))
        if subject_ids:
            clauses.append(teacher_subjects.c.subject_id.in_(subject_ids))
        counts['teacher_assignments'] = (
            db.session.query(db.func.count())
            .select_from(teacher_subjects)
            .filter(db.or_(*clauses))
            .scalar()) or 0

    return counts


def stage_removal_blocked(school_id: int, stages, *, lock: bool = False) -> bool:
    return any(stage_dependency_counts(school_id, stages, lock=lock).values())


def remove_stage_structures(school_id: int, stages, academic_year_id: int) -> dict:
    """Delete the proven-unused grades / sections / subjects of ``stages`` in
    the CURRENT academic year only.

    MUST be called only after ``stage_dependency_counts`` proved every count is
    zero across every year.  Historical-year rows are deliberately preserved:
    they are the structural context of past records and deleting them is
    irreversible.  No student, attendance, fee, exam, homework, schedule or
    chat row is ever touched.  Does NOT commit.
    """
    from app.models import db

    if not academic_year_id:
        return {'grades': 0, 'sections': 0, 'subjects': 0}

    cand = removal_candidates(school_id, stages, academic_year_id)

    for row in cand['subjects']:
        db.session.delete(row)
    for row in cand['sections']:
        db.session.delete(row)
    for row in cand['grades']:
        db.session.delete(row)
    db.session.flush()

    return {'grades': len(cand['grades']), 'sections': len(cand['sections']),
            'subjects': len(cand['subjects'])}


# ═════════════════════════════════════════════════════════════════════════════
#  The one entry point used by the Super-Admin routes
# ═════════════════════════════════════════════════════════════════════════════

def apply_school_stages(school, requested, academic_year_id=None) -> tuple[bool, str | None]:
    """Validate the COMPLETE requested stage change, then apply it.

    Returns ``(ok, error_message)``.  On ``ok is False`` nothing was changed
    beyond the current (uncommitted) session — the caller rolls back, which
    preserves the previous configuration and every existing record exactly.

    An UNCHANGED selection is completely inert: no write, no provisioning, no
    deletion.  Saving an unrelated field on a managed school therefore cannot
    recreate a deliberately deleted default, nor remove a manually added
    out-of-stage grade, section or subject.

    Never commits.
    """
    from app.models import AcademicYear

    # 1 ── validate the request itself BEFORE touching anything ──────────────
    try:
        wanted = parse_stages(requested)
    except ValueError as exc:
        return False, str(exc)
    if not wanted:
        return False, ERR_NO_STAGE

    try:
        current = school_stages(school)
        stored_valid = True
    except InvalidStageConfiguration:
        current, stored_valid = [], False

    # 2 ── unchanged selection → do absolutely nothing ───────────────────────
    if stored_valid and set(wanted) == set(current):
        return True, None

    if academic_year_id is None:
        year = (AcademicYear.query
                .execution_options(bypass_tenant_scope=True)
                .filter(AcademicYear.school_id == school.id,
                        AcademicYear.is_current.is_(True))
                .first())
        academic_year_id = year.id if year else None

    # 3 ── repairing a corrupt stored value is never destructive ─────────────
    # The previous selection is unknowable, so nothing may be treated as
    # "removed".  Store the valid selection and provision it; delete nothing.
    if not stored_valid:
        school.educational_stages = serialize_stages(wanted)
        if academic_year_id:
            provision_stages(school.id, academic_year_id, wanted)
        return True, None

    # 4 ── what is actually being added / removed ────────────────────────────
    if current:
        # Already managed: removals come from the STORED configuration only, so
        # manually added out-of-stage rows are never swept up by a stage edit.
        effective_current = current
    else:
        # Legacy school explicitly entering managed mode: fail closed on the
        # stages its existing data already represents.
        effective_current = detect_configured_stages(school.id)

    removed = [s for s in effective_current if s not in wanted]
    added   = [s for s in wanted if s not in current]

    # 5 ── guard the removal across ALL academic years ───────────────────────
    if removed:
        if ambiguous_stage_grades(school.id, removed, wanted):
            return False, ERR_AMBIGUOUS
        if stage_removal_blocked(school.id, removed, lock=True):
            return False, ERR_STAGE_LINKED

    # 6 ── apply ─────────────────────────────────────────────────────────────
    school.educational_stages = serialize_stages(wanted)

    if added and academic_year_id:
        provision_stages(school.id, academic_year_id, added)

    if removed and academic_year_id:
        remove_stage_structures(school.id, removed, academic_year_id)

    return True, None
