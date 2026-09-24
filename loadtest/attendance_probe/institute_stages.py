"""The staged institute/outbox ladder: pure definitions and slot arithmetic.

Why this module exists
──────────────────────
institute_generator.build_plan() emits ONE entry per (wave, school, group).
Running it with waves > 1 and absent_fraction < 1.0 is not usable for
reconciliation, because absent_local_indices() ROTATES the absent set between
waves: a student marked absent in wave 0 and present in wave 1 contributes one
newly-absent transition but leaves no absent record behind, so

    outbox_reconcile.compute()'s
    expected_absent_records (= transitions)  ==  committed_absent_records

stops holding. The equality is correct arithmetic for a monotone absent set and
wrong for a rotating one.

So the ladder is built from a different primitive: every stage consumes whole
SESSION SLOTS that no other stage touches, and submits each slot exactly once
(waves=1). Within a slot the absent set is whatever absent_local_indices gives
for wave 0, so determinism and the existing fixture contract are preserved,
every absence is genuinely new, and the expected counts are exact.

That makes the total work a round can ever do a fixed budget:

    slots       = num_schools * GROUPS_PER_SCHOOL
    transitions = slots * STUDENTS_PER_GROUP          (at absent_per_session=20)
    jobs        = transitions * TOKENS_PER_PARENT

allocate() refuses to build a ladder that exceeds it, rather than letting a
late stage quietly submit re-absences that create no jobs.

Everything here is pure: no database, no network, no clock.
"""
from __future__ import annotations

import institute_common as ic

# Worker modes a stage runs under. These are descriptions of what the harness
# does to the worker, not application settings.
WORKER_RUNNING = 'running'        # normal worker, default batch
WORKER_STOPPED = 'stopped'        # Mode A: SIGTERM'd before the stage
WORKER_DRAINING = 'draining'      # Mode B: throttled worker, backlog falling
WORKER_KILL_CYCLE = 'kill-cycle'  # Mode C: claim, SIGKILL, restart, reclaim
WORKER_RETRY_PROBE = 'retry-probe'  # transient Firebase failure, then success


def slot_order(num_schools: int) -> list:
    """Every (school_idx, group_idx) session slot, group-major.

    Group-major so that the FIRST slots of a ladder spread across as many
    distinct schools as possible: a low-load stage that takes 10 slots then
    touches 10 different tenants, which is what makes cross-school leakage
    observable at the smallest scale.
    """
    return [(s, g) for g in range(ic.GROUPS_PER_SCHOOL)
            for s in range(num_schools)]


def absent_fraction_for(absent_per_session: int,
                        students_per_group: int = ic.STUDENTS_PER_GROUP
                        ) -> float:
    """The absent_fraction that makes absent_local_indices return exactly N.

    absent_local_indices computes n = max(1, round(len(enrolled) * f)), so the
    inverse is exact for every N in 1..len(enrolled).
    """
    if not 1 <= absent_per_session <= students_per_group:
        raise ValueError(f'absent_per_session must be 1..{students_per_group}')
    return absent_per_session / students_per_group


# ── The ladder ───────────────────────────────────────────────────────────────
# Ordered. A stage runs only if every previous stage passed.
#
# `transitions_per_session` is what one submission is worth; `target_tps` is
# the intended NEWLY-NOTIFIABLE TRANSITION rate, which is the unit the round is
# specified in. The request rate the driver paces to is target_tps divided by
# transitions_per_session.
#
# The three latency probes (LP1/LP2/LP3) are deliberately IDENTICAL in shape
# and rate and differ only in what the worker is doing. That is the only way to
# state whether ingestion latency depends on delivery throughput.

LADDER = [
    dict(name='A_low_load', slots=10, transitions_per_session=1,
         worker=WORKER_RUNNING, target_tps=0.5, concurrency=1,
         purpose='lowest practical multi-school correctness stage'),

    dict(name='B_modest_concurrent', slots=20, transitions_per_session=5,
         worker=WORKER_RUNNING, target_tps=1.7, concurrency=4,
         purpose='multi-school concurrent stage at roughly AI Face stage-3 scale'),

    dict(name='LP1_worker_running', slots=12, transitions_per_session=20,
         worker=WORKER_RUNNING, target_tps=20.0, concurrency=4,
         purpose='latency reference: worker delivering normally'),

    dict(name='C_worker_stopped', slots=20, transitions_per_session=20,
         worker=WORKER_STOPPED, target_tps=20.0, concurrency=4,
         purpose='Mode A: durable backlog accumulates while the worker is down'),

    dict(name='LP2_worker_stopped', slots=12, transitions_per_session=20,
         worker=WORKER_STOPPED, target_tps=20.0, concurrency=4,
         purpose='latency under a growing backlog, identical shape to LP1'),

    dict(name='LP3_worker_draining', slots=12, transitions_per_session=20,
         worker=WORKER_DRAINING, target_tps=20.0, concurrency=4,
         purpose='latency while the throttled worker drains, identical to LP1'),

    dict(name='E_kill_reclaim', slots=5, transitions_per_session=20,
         worker=WORKER_KILL_CYCLE, target_tps=20.0, concurrency=4,
         purpose='Mode C: SIGKILL mid-batch, lease reclaim, eventual delivery'),

    dict(name='P1_11_tps', slots=20, transitions_per_session=20,
         worker=WORKER_RUNNING, target_tps=11.0, concurrency=4,
         purpose='progressive throughput: ~11 transitions/s'),

    dict(name='P2_22_tps', slots=30, transitions_per_session=20,
         worker=WORKER_RUNNING, target_tps=22.0, concurrency=6,
         purpose='progressive throughput: ~22 transitions/s'),

    dict(name='P3_50_tps', slots=25, transitions_per_session=20,
         worker=WORKER_RUNNING, target_tps=50.0, concurrency=8,
         purpose='short burst: ~50 transitions/s'),
]


# The FINAL validation ladder. Deliberately NOT the full ladder: the stages it
# leaves out (low load, modest concurrency, the stopped-worker burst, the
# throttled drain and the three latency probes) have already passed in an
# earlier isolated round, and repeating them proves nothing new. What is left
# is what has never completed end to end.
#
# Mode C is sized DOWN on purpose. The question is whether a killed worker's
# claimed rows come back, which one real claimed batch answers; a large backlog
# would only make the stage slower and the reclaim window harder to observe.

FINAL_LADDER = [
    dict(name='MC_kill_reclaim', slots=5, transitions_per_session=20,
         worker=WORKER_KILL_CYCLE, target_tps=20.0, concurrency=4,
         purpose='Mode C: SIGKILL mid-batch, lease reclaim, eventual delivery'),

    dict(name='R_transient_retry', slots=1, transitions_per_session=1,
         worker=WORKER_RETRY_PROBE, target_tps=1.0, concurrency=1,
         purpose='transient Firebase failure -> retry -> sent, same worker'),

    dict(name='P1_11_tps', slots=20, transitions_per_session=20,
         worker=WORKER_RUNNING, target_tps=11.0, concurrency=4,
         purpose='progressive throughput: ~11 transitions/s'),

    dict(name='P2_22_tps', slots=30, transitions_per_session=20,
         worker=WORKER_RUNNING, target_tps=22.0, concurrency=6,
         purpose='progressive throughput: ~22 transitions/s'),
]

LADDERS = {'full': LADDER, 'final': FINAL_LADDER}


def ladder_for(name: str) -> list:
    """Pick a named ladder. Unknown names fail rather than silently defaulting."""
    try:
        return LADDERS[name]
    except KeyError:
        raise ValueError(f'unknown ladder {name!r}; known: {sorted(LADDERS)}')


# ── Retry-probe readiness (pure) ─────────────────────────────────────────────
# Outbox status counts are CUMULATIVE across a round. A probe that waits on an
# absolute `sent` count is satisfied instantly by whatever an earlier stage
# already delivered, so it reconciles before the retry it is meant to observe
# has fired. Both predicates below are therefore DELTAS against a baseline
# captured immediately before the probe's worker starts.


def more_in_retry(base: dict, now: dict, expected_jobs: int) -> bool:
    """The probe's jobs have failed their first attempt and parked in retry."""
    return int(now.get('retry', 0)) >= int(base.get('retry', 0)) + expected_jobs


def retry_completed(base: dict, now: dict, expected_jobs: int) -> bool:
    """The probe's jobs reached `sent` AND nothing is left waiting to retry.

    Both halves matter: `sent` alone can be reached by other jobs, and an
    empty `retry` alone could mean the rows went dead instead.
    """
    return (int(now.get('sent', 0)) >= int(base.get('sent', 0)) + expected_jobs
            and int(now.get('retry', 0)) <= int(base.get('retry', 0)))


def stage_transitions(stage: dict) -> int:
    return stage['slots'] * stage['transitions_per_session']


def stage_jobs(stage: dict, tokens_per_parent: int = ic.TOKENS_PER_PARENT
               ) -> int:
    return stage_transitions(stage) * tokens_per_parent


def request_rate(stage: dict) -> float:
    """Requests per second the driver paces to."""
    return stage['target_tps'] / stage['transitions_per_session']


def stage_seconds(stage: dict) -> float:
    return round(stage_transitions(stage) / stage['target_tps'], 1)


def allocate(num_schools: int, ladder: list = None) -> dict:
    """Assign disjoint session slots to each stage, in ladder order.

    Raises if the fixtures cannot supply enough slots: a ladder that overruns
    its budget would silently re-submit sessions that are already fully absent,
    which creates no transitions and would read as "the application lost jobs".
    """
    ladder = LADDER if ladder is None else ladder
    pool = slot_order(num_schools)
    needed = sum(s['slots'] for s in ladder)
    if needed > len(pool):
        raise ValueError(
            f'ladder needs {needed} session slots but {num_schools} schools '
            f'supply only {len(pool)}; raise --schools to at least '
            f'{-(-needed // ic.GROUPS_PER_SCHOOL)}')
    out, i = {}, 0
    for stage in ladder:
        out[stage['name']] = pool[i:i + stage['slots']]
        i += stage['slots']
    return out


def budget(num_schools: int, ladder: list = None) -> dict:
    """What the ladder costs against what the fixtures provide. Pure."""
    ladder = LADDER if ladder is None else ladder
    slots = num_schools * ic.GROUPS_PER_SCHOOL
    used = sum(s['slots'] for s in ladder)
    return {
        'sessions_available': slots,
        'sessions_used': used,
        'sessions_spare': slots - used,
        'transition_budget': slots * ic.STUDENTS_PER_GROUP,
        'transitions_planned': sum(stage_transitions(s) for s in ladder),
        'jobs_planned': sum(stage_jobs(s) for s in ladder),
        'per_stage': [{
            'name': s['name'],
            'slots': s['slots'],
            'absent_per_session': s['transitions_per_session'],
            'transitions': stage_transitions(s),
            'jobs': stage_jobs(s),
            'worker': s['worker'],
            'target_transitions_per_s': s['target_tps'],
            'request_rate_per_s': round(request_rate(s), 3),
            'planned_seconds': stage_seconds(s),
            'concurrency': s['concurrency'],
        } for s in ladder],
    }


def peak_stopped_backlog(ladder: list = None,
                         tokens_per_parent: int = ic.TOKENS_PER_PARENT) -> int:
    """Jobs that accumulate across every consecutive worker-stopped stage.

    The watchdog's ceiling is a hard stop, so the ladder must be provably under
    it before anything runs rather than discovering it at sample time.
    """
    ladder = LADDER if ladder is None else ladder
    peak = run = 0
    for stage in ladder:
        if stage['worker'] == WORKER_STOPPED:
            run += stage_jobs(stage, tokens_per_parent)
            peak = max(peak, run)
        else:
            run = 0
    return peak


def session_id_for(inst_fx: dict, slot) -> int:
    """The session a (school_idx, group_idx) slot names, from the fixtures."""
    school_idx, group_idx = slot
    return inst_fx['schools'][str(school_idx)]['groups'][group_idx]['session_id']


def entries_for(plan: list, inst_fx: dict, slots) -> list:
    """Filter a build_plan() result down to one stage's slots, in slot order.

    Matched on session_id rather than on position in the plan: the session is
    the thing a slot actually names, and it is the same key the reconciler and
    the ledger use, so a change in build_plan's iteration order cannot silently
    hand a stage the wrong sessions.
    """
    index = {e['session_id']: e for e in plan}
    out = []
    for slot in slots:
        sid = session_id_for(inst_fx, slot)
        entry = index.get(sid)
        if entry is None:
            raise KeyError(f'no plan entry for slot {slot} (session {sid})')
        out.append(entry)
    return out
