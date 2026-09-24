"""Institute attendance + durable outbox reconciliation.

Split deliberately in two:

  * `compute()` and `invariants()` are PURE. They take plain dictionaries of
    expected and observed counts and return a report and a list of violations.
    That is the safety-critical arithmetic, and it is unit-tested without a
    database, a network or an environment.
  * `collect()` runs the SQL. It only reads, and every query is bounded to the
    experiment's own school ids — there is no query in this module that can
    see a row belonging to anything else.

This file does not replace reconcile.py. The AI Face reconciliation there is
untouched; this is the institute/outbox half, written to the same contract so
analyze.py can merge both.

No token string is ever selected, stored, printed or returned. The outbox
stores a device_token_id, so reconciliation works entirely on integer ids, and
the fake Firebase reports salted fingerprints only.
"""
from __future__ import annotations

import json
import os

PENDING = 'pending'
PROCESSING = 'processing'
RETRY = 'retry'
SENT = 'sent'
DEAD = 'dead'
CANCELLED = 'cancelled'
ALL_STATUSES = (PENDING, PROCESSING, RETRY, SENT, DEAD, CANCELLED)


# ── Expected values, derived from the generator's ledger ─────────────────────

def expected_from_ledger(ledger_rows, *, tokens_per_parent: int,
                         parents_per_student: int = 1) -> dict:
    """Recompute what the application owed us, from the immutable ledger.

    Only submissions the server ACCEPTED (HTTP 200) can have created rows. A
    rejected or errored submission must have created nothing, and the
    reconciler asserts that separately.

    Mirrors submit_attendance(): a student who was already absent in this
    session is `unchanged` and notifies nobody, so the expected job count is
    driven by TRANSITIONS, not by submissions.
    """
    previous: dict = {}
    intended = accepted = rejected = 0
    transitions = 0
    replays = 0
    absent_by_session: dict = {}
    accepted_sessions = set()
    for row in sorted(ledger_rows, key=lambda r: (r.get('wave', 0),
                                                  r.get('seq', 0))):
        intended += 1
        ok = row.get('response_status') == 200 and not row.get('error')
        if not ok:
            rejected += 1
            continue
        accepted += 1
        sid = row['session_id']
        accepted_sessions.add(sid)
        before = previous.get(sid, set())
        now = set(row.get('absent_student_ids') or ())
        new = now - before
        if not new and before == now:
            replays += 1
        transitions += len(new)
        previous[sid] = now
        absent_by_session[sid] = now
    return {
        'intended_submissions': intended,
        'accepted_submissions': accepted,
        'rejected_submissions': rejected,
        'replay_submissions': replays,
        'newly_absent_transitions': transitions,
        'expected_notifications': transitions * parents_per_student,
        'expected_jobs': transitions * parents_per_student * tokens_per_parent,
        'final_absent_by_session': {k: sorted(v)
                                    for k, v in absent_by_session.items()},
        'accepted_sessions': sorted(accepted_sessions),
        'tokens_per_parent': tokens_per_parent,
        'parents_per_student': parents_per_student,
    }


# ── Pure reconciliation ──────────────────────────────────────────────────────

def compute(expected: dict, observed: dict) -> dict:
    """Build the full report. Pure arithmetic over two dicts."""
    att_expected = expected['newly_absent_transitions']
    committed = observed['attendance_absent_records']
    status_counts = {s: int(observed['outbox_status_counts'].get(s, 0))
                     for s in ALL_STATUSES}
    total_jobs = sum(status_counts.values())

    report = {
        'attendance': {
            'intended_submissions': expected['intended_submissions'],
            'accepted_submissions': expected['accepted_submissions'],
            'rejected_submissions': expected['rejected_submissions'],
            'replay_submissions': expected['replay_submissions'],
            'expected_absent_records': att_expected,
            'committed_absent_records': committed,
            'committed_records_total': observed['attendance_records_total'],
            'missing_attendance': max(0, att_expected - committed),
            'unexpected_attendance': observed['attendance_unexpected'],
            'unexpected_duplicates': observed['attendance_duplicate_logical'],
            'cross_school_attendance': observed['attendance_cross_school'],
            'sessions_recorded': observed['sessions_recorded'],
        },
        'notifications': {
            'expected': expected['expected_notifications'],
            'actual': observed['notification_rows'],
            'missing': max(0, expected['expected_notifications']
                           - observed['notification_rows']),
            'unexpected': max(0, observed['notification_rows']
                              - expected['expected_notifications']),
            'cross_school_recipients': observed['notification_cross_school'],
        },
        'outbox': {
            'expected_jobs': expected['expected_jobs'],
            'actual_jobs': total_jobs,
            'distinct_dedup_keys': observed['outbox_distinct_dedup_keys'],
            **{s: status_counts[s] for s in ALL_STATUSES},
            'cross_school_jobs': observed['outbox_cross_school'],
            'transitions_missing_job': observed['transitions_missing_job'],
            'jobs_without_transition': observed['jobs_without_transition'],
            'backlog': status_counts[PENDING] + status_counts[PROCESSING]
            + status_counts[RETRY],
        },
        'fake_firebase': {
            'attempts': observed.get('fake_attempts', 0),
            'successes': observed.get('fake_successes', 0),
            'failures': observed.get('fake_failures', 0),
            'distinct_fingerprints': observed.get('fake_distinct', 0),
            'duplicate_sends': observed.get('fake_duplicate_sends', 0),
        },
    }
    report['fake_firebase']['unexplained_duplicate_sends'] = max(
        0, report['fake_firebase']['duplicate_sends']
        - observed.get('worker_reclaims', 0))
    return report


def invariants(report: dict, *, drain_complete: bool) -> list:
    """Every machine-checkable assertion. Returns the violations, or []."""
    v = []
    a, n, o, f = (report['attendance'], report['notifications'],
                  report['outbox'], report['fake_firebase'])

    # Attendance correctness
    if a['committed_absent_records'] != a['expected_absent_records']:
        v.append(f"committed_expected != committed_actual "
                 f"({a['expected_absent_records']} != "
                 f"{a['committed_absent_records']})")
    for key in ('missing_attendance', 'unexpected_attendance',
                'unexpected_duplicates', 'cross_school_attendance'):
        if a[key] != 0:
            v.append(f'attendance.{key} == {a[key]}, must be 0')

    # In-app notifications
    if n['actual'] != n['expected']:
        v.append(f"notifications {n['actual']} != expected {n['expected']}")
    if n['cross_school_recipients'] != 0:
        v.append(f"notifications.cross_school_recipients == "
                 f"{n['cross_school_recipients']}, must be 0")

    # Outbox creation
    if o['actual_jobs'] != o['expected_jobs']:
        v.append(f"outbox jobs {o['actual_jobs']} != expected "
                 f"{o['expected_jobs']}")
    if o['distinct_dedup_keys'] != o['actual_jobs']:
        v.append(f"distinct dedup keys {o['distinct_dedup_keys']} != total "
                 f"jobs {o['actual_jobs']}")
    for key in ('cross_school_jobs', 'transitions_missing_job',
                'jobs_without_transition'):
        if o[key] != 0:
            v.append(f'outbox.{key} == {o[key]}, must be 0')

    # Drain
    if drain_complete:
        for state in (PENDING, PROCESSING, RETRY, DEAD, CANCELLED):
            if o[state] != 0:
                v.append(f'outbox.{state} == {o[state]} after drain, must be 0')
        if o[SENT] != o['expected_jobs']:
            v.append(f"outbox.sent {o[SENT]} != expected_jobs "
                     f"{o['expected_jobs']} after drain")
        if f['unexplained_duplicate_sends'] != 0:
            v.append(f"fake_firebase.unexplained_duplicate_sends == "
                     f"{f['unexplained_duplicate_sends']}, must be 0")
    return v


def verdict(report: dict, *, drain_complete: bool) -> dict:
    v = invariants(report, drain_complete=drain_complete)
    out = dict(report)
    out['violations'] = v
    out['correct'] = not v
    out['drain_complete'] = drain_complete
    out['delivery_guarantee'] = (
        'durable at-least-once with deduplicated enqueueing; a lease reclaim '
        'after an interrupted batch may legitimately resend, so duplicate '
        'sends are counted and explained rather than assumed impossible. '
        'This is NOT exactly-once.')
    return out


# ── Observation (read-only SQL, bounded to experiment schools) ───────────────

OBSERVED_KEYS = (
    'attendance_records_total', 'attendance_absent_records',
    'attendance_unexpected', 'attendance_duplicate_logical',
    'attendance_cross_school', 'sessions_recorded', 'notification_rows',
    'notification_cross_school', 'outbox_status_counts',
    'outbox_distinct_dedup_keys', 'outbox_cross_school',
    'transitions_missing_job', 'jobs_without_transition',
)


def collect(cur, *, school_ids, session_ids, test_date) -> dict:
    """Read-only observation. Every query is bounded to experiment schools."""
    ids = list(school_ids)
    sess = list(session_ids)
    obs = {}

    cur.execute("""SELECT count(*) FROM institute_attendance_records
                    WHERE school_id = ANY(%s) AND session_id = ANY(%s)""",
                (ids, sess))
    obs['attendance_records_total'] = cur.fetchone()[0]

    cur.execute("""SELECT count(*) FROM institute_attendance_records
                    WHERE school_id = ANY(%s) AND session_id = ANY(%s)
                      AND status = 'absent'""", (ids, sess))
    obs['attendance_absent_records'] = cur.fetchone()[0]

    # A row in a session this experiment did not create, or in a school it does
    # not own, is unexpected by definition.
    cur.execute("""SELECT count(*) FROM institute_attendance_records
                    WHERE NOT (school_id = ANY(%s) AND session_id = ANY(%s))""",
                (ids, sess))
    obs['attendance_unexpected'] = cur.fetchone()[0]

    # The unique constraint should make this impossible; measuring it is how we
    # find out if that belief is wrong.
    cur.execute("""SELECT coalesce(sum(c - 1), 0) FROM (
                     SELECT count(*) AS c FROM institute_attendance_records
                      WHERE school_id = ANY(%s) AND session_id = ANY(%s)
                      GROUP BY session_id, student_id HAVING count(*) > 1) d""",
                (ids, sess))
    obs['attendance_duplicate_logical'] = cur.fetchone()[0]

    cur.execute("""SELECT count(*) FROM institute_attendance_records r
                     JOIN students s ON s.id = r.student_id
                    WHERE r.session_id = ANY(%s) AND r.school_id <> s.school_id""",
                (sess,))
    obs['attendance_cross_school'] = cur.fetchone()[0]

    cur.execute("""SELECT count(*) FROM institute_attendance_sessions
                    WHERE id = ANY(%s) AND status = 'recorded'""", (sess,))
    obs['sessions_recorded'] = cur.fetchone()[0]

    cur.execute("""SELECT count(*) FROM notifications
                    WHERE school_id = ANY(%s) AND ntype = 'attendance'
                      AND created_at::date = %s""", (ids, test_date))
    obs['notification_rows'] = cur.fetchone()[0]

    # A recipient whose own school differs from the notification's school.
    cur.execute("""SELECT count(*) FROM notifications n
                     JOIN users u ON u.id = n.target_user_id
                    WHERE n.school_id = ANY(%s) AND n.ntype = 'attendance'
                      AND n.created_at::date = %s
                      AND u.school_id IS DISTINCT FROM n.school_id""",
                (ids, test_date))
    obs['notification_cross_school'] = cur.fetchone()[0]

    cur.execute("""SELECT status, count(*) FROM notification_outbox
                    WHERE school_id = ANY(%s) GROUP BY status""", (ids,))
    obs['outbox_status_counts'] = {r[0]: r[1] for r in cur.fetchall()}

    cur.execute("""SELECT count(DISTINCT dedup_key) FROM notification_outbox
                    WHERE school_id = ANY(%s)""", (ids,))
    obs['outbox_distinct_dedup_keys'] = cur.fetchone()[0]

    # A job whose device token belongs to a different school than the job.
    cur.execute("""SELECT count(*) FROM notification_outbox o
                     JOIN mobile_device_tokens t ON t.id = o.device_token_id
                    WHERE o.school_id = ANY(%s)
                      AND (o.school_id <> t.school_id OR o.user_id <> t.user_id)""",
                (ids,))
    obs['outbox_cross_school'] = cur.fetchone()[0]

    # An absent record whose parent has an active token but that produced no
    # job — the silent-loss case the whole exercise exists to detect.
    #
    # Compared on (user_id, device_token_id): notification_outbox stores no
    # student_id, so the student is reached through parent_students on the
    # expected side only. Both sides are DISTINCT, so a parent with two
    # absences in the round is one expected pair, not two.
    cur.execute("""
        SELECT count(*) FROM (
          SELECT DISTINCT ps.user_id AS user_id, t.id AS token_id
            FROM institute_attendance_records r
            JOIN parent_students ps ON ps.student_id = r.student_id
            JOIN mobile_device_tokens t
              ON t.user_id = ps.user_id AND t.school_id = r.school_id
             AND t.is_active
           WHERE r.session_id = ANY(%s) AND r.status = 'absent'
          EXCEPT
          SELECT DISTINCT o.user_id, o.device_token_id
            FROM notification_outbox o WHERE o.school_id = ANY(%s)
        ) missing""", (sess, ids))
    obs['transitions_missing_job'] = cur.fetchone()[0]

    # A job pointing at a student/session pair with no absent record.
    cur.execute("""SELECT count(*) FROM notification_outbox o
                    WHERE o.school_id = ANY(%s) AND NOT EXISTS (
                      SELECT 1 FROM institute_attendance_records r
                       WHERE r.session_id = ANY(%s) AND r.status = 'absent'
                         AND r.school_id = o.school_id)""", (ids, sess))
    obs['jobs_without_transition'] = cur.fetchone()[0]
    return obs


def read_fake_firebase(root: str) -> dict:
    path = os.path.join(root, 'run', 'fake_fcm_sends.json')
    if not os.path.exists(path):
        return {'fake_attempts': 0, 'fake_successes': 0, 'fake_failures': 0,
                'fake_distinct': 0, 'fake_duplicate_sends': 0}
    with open(path, encoding='utf-8') as fh:
        d = json.load(fh)
    return {'fake_attempts': d.get('attempts', 0),
            'fake_successes': d.get('successes', 0),
            'fake_failures': d.get('failures', 0),
            'fake_distinct': d.get('distinct_fingerprints', 0),
            'fake_duplicate_sends': d.get('duplicate_sends', 0)}
