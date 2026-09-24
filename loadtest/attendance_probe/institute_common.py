"""Deterministic, side-effect-free definitions for the institute outbox probe.

Everything the institute fixture seeder, the generator, the worker supervisor
and the reconciler must agree on lives here. Every function is pure: no
database, no network, no clock unless one is injected. That is what makes the
safety-critical parts unit-testable without an environment.

Naming contract
───────────────
Every synthetic row this probe creates carries the experiment tag, so a human
reading the database can tell at a glance what owns it, and so the reconciler
can assert that nothing outside the experiment was touched:

    institute subject   LT<tag>-SUBJ-<school_idx>
    study group         LT<tag>-GRP-<school_idx>-<group_idx>
    instructor employee LT<tag>-INS-<school_idx>
    instructor user     lt<tag>i<school_idx>
    device token        LT<tag>-TOK-<parent_k>-<device_idx>

The token strings are FAKE by construction. They are not derived from, and can
never collide with, a real FCM registration token: a real token is base64url
over a 152+ character alphabet and never contains the substring 'LT<tag>-TOK-'.
They are also never sent anywhere — the fake Firebase in fake_firebase/ is the
only consumer, and it refuses to load outside an experiment.
"""
from __future__ import annotations

import datetime as dt
import zlib

# ── Institute round shape ────────────────────────────────────────────────────
# Reuses the AI Face fixture population (schools, students, parents) rather
# than seeding a second one. Only institute-specific rows are added.

# Groups created per school. Several groups per school is what lets one stage
# submit concurrently to independent sessions inside the same tenant.
GROUPS_PER_SCHOOL = 3

# Students enrolled per group, taken from the school's existing synthetic
# students. Deliberately small relative to students_per_school: the probe
# measures notification fan-out, not enrollment volume.
STUDENTS_PER_GROUP = 20

# One session per group per test date. Sessions are materialized by the fixture
# step, never by a scheduler (the application only creates them when a human
# opens attendance, which the generator cannot do in bulk).
SESSIONS_PER_GROUP = 1

# Synthetic mobile devices per parent. 2 proves the fan-out is per ACTIVE
# token, not per parent — the single most common place an outbox count is
# silently wrong.
TOKENS_PER_PARENT = 2

# One token per parent is left INACTIVE, so the reconciler can prove that
# inactive tokens produce no job. Index of the inactive device, or None.
INACTIVE_TOKEN_INDEX = None

SESSION_START = dt.time(9, 0)
SESSION_END = dt.time(10, 30)

# The statuses the generator may submit. 'absent' is the only one that creates
# outbox jobs; the others exist to prove that a non-absent transition does not.
STATUS_PRESENT = 'present'
STATUS_ABSENT = 'absent'
STATUS_LATE = 'late'


# ── Deterministic identifiers ────────────────────────────────────────────────

def subject_code(tag: str, school_idx: int) -> str:
    return f'LT{tag}-SUBJ-{school_idx:02d}'


def group_name(tag: str, school_idx: int, group_idx: int) -> str:
    return f'LT{tag}-GRP-{school_idx:02d}-{group_idx}'


def instructor_employee_id(tag: str, school_idx: int) -> str:
    return f'LT{tag}-INS-{school_idx:02d}'


def instructor_username(tag: str, school_idx: int) -> str:
    return f'lt{tag}i{school_idx:02d}'


def device_token(tag: str, parent_k: int, device_idx: int) -> str:
    """A fake FCM token string. Never derived from a real token."""
    return f'LT{tag}-TOK-{parent_k:05d}-{device_idx}'


def is_experiment_token(tag: str, token: str) -> bool:
    return isinstance(token, str) and token.startswith(f'LT{tag}-TOK-')


def experiment_prefixes(tag: str) -> dict:
    """Every prefix this experiment owns, for identity files and safety gates."""
    return {
        'school': f'LT{tag}S',
        'device': f'LTAF-{tag}-',
        'student': f'LT{tag}-',
        'parent_user': f'lt{tag}p',
        'instructor_user': f'lt{tag}i',
        'subject': f'LT{tag}-SUBJ-',
        'group': f'LT{tag}-GRP-',
        'employee': f'LT{tag}-INS-',
        'device_token': f'LT{tag}-TOK-',
    }


# ── Which students are enrolled where ────────────────────────────────────────

def enrolled_local_indices(school_idx: int, group_idx: int,
                           students_per_school: int) -> list[int]:
    """Local student indices (0-based within the school) enrolled in a group.

    Disjoint across the groups of one school, so a single submission can never
    be double-counted, and stable across runs so a reconciliation can be
    recomputed from configuration alone.
    """
    start = group_idx * STUDENTS_PER_GROUP
    end = min(start + STUDENTS_PER_GROUP, students_per_school)
    return list(range(start, max(start, end)))


def group_capacity(students_per_school: int) -> int:
    """How many students the institute fixtures will actually enroll."""
    return sum(len(enrolled_local_indices(0, g, students_per_school))
               for g in range(GROUPS_PER_SCHOOL))


# ── Submission plan ──────────────────────────────────────────────────────────

def absent_local_indices(school_idx: int, group_idx: int, wave: int,
                         students_per_school: int, absent_fraction: float = 0.5
                         ) -> list[int]:
    """Which enrolled students are marked ABSENT in a given submission wave.

    Deterministic and wave-dependent: wave 0 marks one deterministic subset
    absent, wave 1 marks a different subset, so a second submission produces
    genuine absent→present and present→absent transitions instead of a replay.
    Only NEW absences create outbox jobs, which is exactly the property the
    reconciler must be able to predict.
    """
    enrolled = enrolled_local_indices(school_idx, group_idx, students_per_school)
    if not enrolled:
        return []
    n = max(1, int(round(len(enrolled) * absent_fraction)))
    h = zlib.crc32(f'absent:{school_idx}:{group_idx}:{wave}'.encode())
    start = h % len(enrolled)
    return sorted(enrolled[(start + i) % len(enrolled)] for i in range(n))


def newly_absent(previous: set, current: set) -> set:
    """The transitions that create notifications: absent now, not absent before.

    Mirrors app/services/institute_attendance.py — a record only enters
    newly_absent when it is created as absent, or changes INTO absent. A
    byte-identical re-submission produces the empty set.
    """
    return set(current) - set(previous)


def expected_jobs_for_transitions(newly_absent_student_ids,
                                  parents_of_student: dict,
                                  active_tokens_of_parent: dict) -> int:
    """Outbox jobs the application must create for a set of new absences.

    Mirrors notification_outbox.stage_absence_deliveries():
      * one job per ACTIVE device token,
      * of each parent linked to the student,
      * restricted to the student's own school (the caller supplies only
        same-school parents/tokens, which the reconciler verifies separately).
    A parent with no active token produces an in-app Notification but no job.
    """
    total = 0
    for sid in newly_absent_student_ids:
        for parent_id in parents_of_student.get(sid, ()):
            total += len(active_tokens_of_parent.get(parent_id, ()))
    return total


def expected_notifications_for_transitions(newly_absent_student_ids,
                                           parents_of_student: dict) -> int:
    """In-app Notification rows: one per linked parent, tokens irrelevant."""
    return sum(len(parents_of_student.get(sid, ()))
               for sid in newly_absent_student_ids)


# ── Event ledger ─────────────────────────────────────────────────────────────

LEDGER_FIELDS = (
    'experiment_id', 'seq', 'school_idx', 'school_id', 'group_id', 'session_id',
    'wave', 'student_ids', 'absent_student_ids', 'intended_status_map',
    'transition_id', 'submitted_at', 'response_status', 'response_body_keys',
    'latency_ms', 'attempt', 'retried', 'error',
)


def transition_id(session_id: int, wave: int, student_ids) -> str:
    """Stable identifier for one logical submission, independent of retries."""
    payload = f'{session_id}:{wave}:' + ','.join(str(s) for s in sorted(student_ids))
    return f'{session_id}-{wave}-{zlib.crc32(payload.encode()):08x}'


def ledger_record(**fields) -> dict:
    """Build one ledger row, rejecting anything that is not a declared field.

    Credentials and JWTs are structurally impossible here: the field list has
    no slot for them, and an unexpected key raises.
    """
    unknown = set(fields) - set(LEDGER_FIELDS)
    if unknown:
        raise ValueError(f'ledger field(s) not permitted: {sorted(unknown)}')
    return {k: fields.get(k) for k in LEDGER_FIELDS}
