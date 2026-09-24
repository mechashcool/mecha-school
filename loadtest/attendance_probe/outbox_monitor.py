"""Sample the durable outbox so the watchdog can act on it.

This is the collector the guard rules were written against. guard_rules.py
already knows what an unsafe outbox looks like; until now nothing measured it.

Three design rules
──────────────────
 1. FAIL CLOSED. A sample that could not be taken is not a healthy sample. The
    collector returns collector_ok=False with the reason, and
    guard_rules.outbox_breaches() turns that into a stop reason. A database
    that has gone away must halt the round, not read as "backlog 0".
 2. PURE WHERE IT MATTERS. Deciding whether the backlog is falling, and
    whether a restart was planned, is arithmetic over recorded history. Both
    are module-level functions with no I/O, so they are tested directly.
 3. NOTHING SENSITIVE. The only columns read are `status` and `school_id`.
    Titles, bodies, data_json, dedup keys and device_token_id are never
    selected, so no payload, token or credential can reach a log or a report.

Every query is bounded to the experiment's own school ids.
"""
from __future__ import annotations

import json
import os
import time
from collections import deque

STATUSES = ('pending', 'processing', 'retry', 'sent', 'dead', 'cancelled')
BACKLOG_STATUSES = ('pending', 'processing', 'retry')

# How much backlog history to keep, in seconds. Must cover the longest window
# any rule asks about (outbox_no_drain_window_s).
HISTORY_SECONDS = 600


# ── Pure helpers ─────────────────────────────────────────────────────────────

def backlog_is_falling(history, *, window_s: float, now: float | None = None
                       ) -> bool:
    """True when the backlog is lower than it was at the start of the window.

    `history` is an iterable of (timestamp, backlog) in chronological order.
    Strictly lower, not "not higher": a backlog that is merely flat is NOT
    falling, which is what makes the stuck-drain rule fire.

    With fewer than two samples, or no sample old enough to compare against,
    the answer is False — absence of evidence of progress is not evidence of
    progress.
    """
    pts = [(t, b) for t, b in history]
    if len(pts) < 2:
        return False
    now = pts[-1][0] if now is None else now
    cutoff = now - window_s
    older = [b for t, b in pts if t <= cutoff]
    baseline = older[-1] if older else pts[0][1]
    return pts[-1][1] < baseline


def unplanned_restarts(observed_identities, planned_starts: int) -> int:
    """Restarts the harness did not ask for.

    `observed_identities` is the ordered list of distinct (pid, create_time)
    pairs the sampler has seen. Each new identity after the first is one
    restart. The harness's own starts are subtracted, so Mode C (kill then
    start) reads as zero unplanned restarts while a worker that died and was
    resurrected by something else does not.
    """
    seen = []
    for ident in observed_identities:
        if not seen or seen[-1] != ident:
            seen.append(ident)
    observed = max(0, len(seen) - 1)
    return max(0, observed - max(0, planned_starts - 1))


def derive(counts: dict) -> dict:
    """Totals and backlog from a status→count mapping. Pure."""
    c = {s: int(counts.get(s, 0)) for s in STATUSES}
    c['total_jobs'] = sum(c[s] for s in STATUSES)
    c['outbox_backlog'] = sum(c[s] for s in BACKLOG_STATUSES)
    for s in STATUSES:
        c['outbox_' + s] = c[s]
    return c


# ── Collector ────────────────────────────────────────────────────────────────

COUNTS_SQL = ("SELECT status, count(*) FROM notification_outbox "
              "WHERE school_id = ANY(%s) GROUP BY status")


class OutboxSampler:
    """Takes one sample per call and remembers just enough history.

    `fetch_counts` is injected: in production it runs COUNTS_SQL against the
    experiment database; in tests it is a plain function. That is what lets
    the fail-closed behaviour be tested without breaking a real database.
    """

    def __init__(self, fetch_counts, *, school_ids, worker_state=None,
                 clock=time.time, history_seconds: float = HISTORY_SECONDS):
        self._fetch = fetch_counts
        self._school_ids = list(school_ids)
        self._worker_state = worker_state or (lambda: {})
        self._clock = clock
        self._history_seconds = history_seconds
        self.history = deque()
        self.identities = []
        self.consecutive_failures = 0

    def sample(self, *, window_s: float) -> dict:
        now = self._clock()
        try:
            counts = self._fetch(self._school_ids)
            if not isinstance(counts, dict):
                raise TypeError(f'fetch_counts returned {type(counts).__name__}')
        except Exception as exc:
            # Fail closed. No counts are invented, and no key that a guard rule
            # reads is set to a healthy-looking value.
            self.consecutive_failures += 1
            return {
                'sampled_at': now,
                'collector_ok': False,
                'collector_error': f'{type(exc).__name__}: {exc}',
                'consecutive_collector_failures': self.consecutive_failures,
            }
        self.consecutive_failures = 0

        sample = derive(counts)
        sample['sampled_at'] = now
        sample['collector_ok'] = True

        self.history.append((now, sample['outbox_backlog']))
        while self.history and now - self.history[0][0] > self._history_seconds:
            self.history.popleft()
        sample['outbox_backlog_falling'] = backlog_is_falling(
            self.history, window_s=window_s, now=now)

        try:
            state = self._worker_state() or {}
        except Exception as exc:
            # The worker view failing is also unsafe: an unnoticed crash is
            # exactly what this field exists to catch.
            sample['collector_ok'] = False
            sample['collector_error'] = (
                f'worker state unavailable: {type(exc).__name__}: {exc}')
            return sample

        sample['worker_pid'] = state.get('pid')
        sample['worker_alive'] = bool(state.get('alive'))
        ident = (state.get('pid'), state.get('create_time'))
        if state.get('alive') and ident != (None, None):
            if not self.identities or self.identities[-1] != ident:
                self.identities.append(ident)
        sample['unplanned_worker_restarts'] = unplanned_restarts(
            self.identities, int(state.get('planned_starts', 0)))
        sample['worker_restarts_observed'] = max(0, len(self.identities) - 1)
        sample['planned_worker_starts'] = int(state.get('planned_starts', 0))
        return sample


# ── Wiring to a live experiment ──────────────────────────────────────────────

def db_fetch_counts(conn_factory):
    """A fetch_counts that reads the experiment database. Read-only."""
    def _fetch(school_ids):
        conn = conn_factory()
        try:
            cur = conn.cursor()
            cur.execute(COUNTS_SQL, (list(school_ids),))
            return {row[0]: row[1] for row in cur.fetchall()}
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return _fetch


LIFECYCLE_FILE = 'worker_lifecycle.json'


def read_worker_state(root: str) -> dict:
    """The worker's pid/liveness plus how many starts the harness asked for."""
    state = {'pid': None, 'create_time': None, 'alive': False,
             'planned_starts': 0}
    lifecycle_path = os.path.join(root, 'run', LIFECYCLE_FILE)
    if os.path.exists(lifecycle_path):
        with open(lifecycle_path, encoding='utf-8') as fh:
            state['planned_starts'] = int(json.load(fh).get('planned_starts', 0))
    pid_path = os.path.join(root, 'run', 'worker.json')
    if not os.path.exists(pid_path):
        return state
    with open(pid_path, encoding='utf-8') as fh:
        info = json.load(fh)
    state['pid'] = info.get('pid')
    state['create_time'] = info.get('create_time')
    try:
        import psutil
        p = psutil.Process(info['pid'])
        state['alive'] = abs(p.create_time() - info['create_time']) < 1.0
    except Exception:
        state['alive'] = False
    return state


def record_lifecycle_event(root: str, action: str) -> None:
    """Called by worker_control so a harness-initiated restart is not a crash."""
    path = os.path.join(root, 'run', LIFECYCLE_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {'planned_starts': 0, 'planned_stops': 0, 'planned_kills': 0,
            'events': []}
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as fh:
                data.update(json.load(fh))
        except (OSError, ValueError):
            pass
    key = {'start': 'planned_starts', 'stop': 'planned_stops',
           'kill': 'planned_kills'}.get(action)
    if key:
        data[key] = int(data.get(key, 0)) + 1
    data.setdefault('events', []).append({'action': action, 'at': time.time()})
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def sampler_for_experiment(root: str, cfg: dict, sec: dict, school_ids):
    """Build a sampler wired to the isolated experiment database."""
    import psycopg2
    import common

    def conn_factory():
        conn = psycopg2.connect(**common.pg_dsn(cfg, sec))
        conn.set_session(readonly=True, autocommit=True)
        return conn

    return OutboxSampler(db_fetch_counts(conn_factory), school_ids=school_ids,
                         worker_state=lambda: read_worker_state(root))
