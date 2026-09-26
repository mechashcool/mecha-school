"""Attendance scope-manifest resolver — INERT, internal, pre-B2.

WHAT THIS IS
The set of journal scopes a given principal is allowed to synchronize for the
attendance pilot. Part B2 will key `change_journal` rows by `student:<id>`, so
the server must be able to answer "which student scopes may THIS principal
receive?" without ever consulting the journal — the journal is a change feed and
is never the authority on entitlement.

WHAT THIS IS NOT
There is no endpoint. Nothing in `app/blueprints/` imports this module, and
`tests/test_sync_scope_manifest.py` asserts that. Exposing it is B3 work, gated
behind SYNC_JOURNAL_ENABLED / SYNC_SIGNAL_ENABLED, which both default to false.

AUTHORIZATION SOURCE — derived, never invented
Every rule below mirrors an authorization check that the CURRENT mobile
attendance API already enforces. No role gains access it does not already have,
and no role in the pilot receives more than it already receives:

  * `app/blueprints/mobile_api/parent.py:59` `_assert_owns_student()` —
    a row in `parent_students` for (this parent, this student) AND
    `student.school_id == user.school_id`. Both conditions, not either.
  * `app/blueprints/mobile_api/utils.py:52` `jwt_required()` — re-loads the
    User row per request and rejects `not user.is_active`.
  * `app/blueprints/mobile_api/utils.py:100` `role_required()` — rejects a
    principal whose `role.name` is not in the allowed list.

WHY THE PILOT IS PARENT-ONLY
Teachers do have a narrow student-attendance read today: `teacher.py:479`
`teacher_student_profile` embeds a fixed last-30-day snapshot for a student in
one of the teacher's own sections (`_assert_student_access` ->
`_teacher_section_ids`: homeroom sections plus `teacher_subjects` assignments).
That access is deliberately NOT part of the pilot manifest — emitting no scope
for a teacher grants nothing new and fails closed. `teacher/my-attendance` is a
different resource entirely (`EmployeeAttendance`, the teacher's own record) and
produces no student scope. Admins, school managers and super admins have no
mobile student-attendance endpoint at all.

ACADEMIC YEAR
`Student` is intentionally a master record that persists across academic years
(`app/utils/scoping.py:59-61`), so student scopes are NOT filtered by year — a
year filter here would make a linked child disappear from a parent's manifest at
rollover. Year handling stays exactly where it already is: `StudentAttendance`
is year-scoped in the ORM, and the parent endpoint applies its own explicit
date-range window (default 30 days, hard-capped at 365) with
`include_all_years=True`. This module neither widens nor narrows that.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from app.models import Student, SyncPrincipalState, db, parent_students

# Scope namespace for the attendance pilot. One student == one scope.
SCOPE_STUDENT = 'student'

# Roles whose attendance scopes the pilot resolves. Parent only — see the
# module docstring. Adding a role here without a matching existing mobile
# endpoint would be granting new access.
PILOT_ROLES = frozenset({'parent'})


@dataclass(frozen=True)
class ScopeManifest:
    """Deterministic, PII-free description of what a principal may sync.

    `scopes` holds opaque `student:<id>` strings for students the principal is
    already authorized to read. No names, codes, photos, sections or grades
    appear here, and an identifier the principal may not access is never
    included — so the manifest itself can be handed to the client safely.
    """
    user_id: int
    school_id: int | None
    scopes_version: int
    scopes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        return not self.scopes

    def as_dict(self) -> dict:
        return {
            'scopes_version': self.scopes_version,
            'scopes': list(self.scopes),
        }


def read_scopes_version(user_id: int) -> int:
    """Durable per-principal version. ABSENCE MEANS 1, never an error.

    B1 creates no rows, so every existing principal resolves to 1 until a B2
    writer bumps them. Treating a missing row as an error would break every
    pre-existing account on the day sync is enabled.
    """
    row = (db.session.query(SyncPrincipalState.scopes_version)
           .filter(SyncPrincipalState.user_id == user_id)
           .one_or_none())
    return int(row[0]) if row is not None else 1


def resolve_attendance_scopes(user) -> ScopeManifest:
    """Return the attendance scopes `user` is authorized to synchronize.

    Fails closed: every rejection path yields an EMPTY manifest rather than an
    exception, so a caller that forgets to check cannot accidentally widen
    access. The version is still returned, because a client must be able to
    observe that its scopes changed to nothing (access revoked) — that is the
    signal that tells it to drop what it cached.
    """
    # ── Principal must be a real, active, role-bearing user ──────────────────
    if user is None or getattr(user, 'id', None) is None:
        return ScopeManifest(user_id=0, school_id=None, scopes_version=1)

    user_id = int(user.id)
    school_id = getattr(user, 'school_id', None)
    version = read_scopes_version(user_id)
    empty = ScopeManifest(user_id=user_id, school_id=school_id,
                          scopes_version=version)

    # jwt_required rejects an inactive account on every request; a manifest must
    # not outlive that. `is_active` defaults to None on an unsaved row, so test
    # for truthiness rather than `is False`.
    if not getattr(user, 'is_active', False):
        return empty

    role = getattr(user, 'role', None)
    role_name = getattr(role, 'name', None)
    if role_name not in PILOT_ROLES:
        return empty

    # A NULL school_id means super admin ("school_id = NULL -> super-admin" in
    # the User model). It must NEVER behave as a wildcard that matches every
    # school. Combined with the role check above this is already unreachable
    # for the pilot, but it is asserted explicitly so that adding a role later
    # cannot silently turn NULL into "all schools".
    if school_id is None:
        return empty

    # ── The authorization relationship itself ────────────────────────────────
    # A JOIN over the parent_students junction, exactly mirroring
    # _assert_owns_student: the link must exist AND the student must belong to
    # this parent's school. Deriving from an unrestricted Student.query is
    # forbidden — this query cannot return a student the parent is not linked
    # to, whatever the ORM scope happens to be.
    #
    # bypass_tenant_scope is deliberately NOT used: the ORM tenant criteria stay
    # active as defence in depth on top of the explicit school_id filter below.
    rows = db.session.execute(
        select(Student.id)
        .join(parent_students, parent_students.c.student_id == Student.id)
        .where(
            parent_students.c.user_id == user_id,
            Student.school_id == school_id,
        )
    ).all()

    # Sorted and de-duplicated: the manifest is compared across requests, so a
    # stable ordering is part of the contract, not a cosmetic detail.
    student_ids = sorted({int(r[0]) for r in rows})
    return ScopeManifest(
        user_id=user_id,
        school_id=school_id,
        scopes_version=version,
        scopes=tuple(f'{SCOPE_STUDENT}:{sid}' for sid in student_ids),
    )


def scope_for_student(student_id: int) -> str:
    """The journal scope key for one student. Single source of truth for B2."""
    return f'{SCOPE_STUDENT}:{int(student_id)}'
