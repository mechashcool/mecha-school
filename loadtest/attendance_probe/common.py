"""Shared, deterministic definitions for the attendance load probe.

Everything the fixture seeder, generator, watchdog and reconciler must agree on
lives here: experiment configuration, fixture naming, arrival order, the timed
schedule, synthetic device times, and the expected attendance history. All
expected values are pure functions, so correctness can be verified exactly
without storing a second copy of the data.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import zlib

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Timed round definition (seconds from round start) ────────────────────────
# (stage_no, start_s, end_s, cumulative_students_target)
STAGES = [
    (1,   0,  60,    10),
    (2,  60, 120,    50),
    (3, 120, 180,   100),
    (4, 180, 240,   250),
    (5, 240, 300,   500),
    (6, 300, 360,  1000),
    (7, 360, 420,  2500),
    (8, 420, 480,  5000),
    (9, 480, 540, 10000),
]
RECOVERY = (10, 540, 600, 10)      # stage_no, start, end, parent sessions kept
RAMP_SECONDS = 45.0
ROUND_SECONDS = 600
PARENT_READ_INTERVAL = 30.0
MAX_STUDENTS = 10000

VALID_STATUSES = {'present', 'late', 'absent', 'on_leave', 'excused'}


# ── Experiment config ────────────────────────────────────────────────────────

def load_config(root: str) -> dict:
    with open(os.path.join(root, 'experiment.json'), encoding='utf-8') as fh:
        cfg = json.load(fh)
    cfg['root'] = root
    # Optional host overrides — used only for the Linux-container driver check,
    # where the target/PG run on the Windows host reachable as host.docker.internal.
    # They never change what is bound or seeded; they only change where the
    # generator connects. Recorded in run_meta via the caller.
    import os as _os
    if _os.environ.get('ATTLT_TARGET_HOST'):
        cfg['target_host'] = _os.environ['ATTLT_TARGET_HOST']
    if _os.environ.get('ATTLT_PG_HOST'):
        cfg['pg_host'] = _os.environ['ATTLT_PG_HOST']
    return cfg


def load_secrets(root: str) -> dict:
    with open(os.path.join(root, 'secrets', 'secrets.json'), encoding='utf-8') as fh:
        return json.load(fh)


def tag(cfg: dict) -> str:
    """Short experiment tag used in every synthetic identifier."""
    return cfg['experiment_id'].rsplit('-', 1)[-1]


def pg_dsn(cfg: dict, sec: dict, dbname: str | None = None) -> dict:
    return dict(host=cfg['pg_host'], port=cfg['pg_port'], user=sec['pg_user'],
                password=sec['pg_password'], dbname=dbname or cfg['db_name'],
                connect_timeout=10, application_name='attlt-tool')


def sqlalchemy_url(cfg: dict, sec: dict) -> str:
    from urllib.parse import quote
    return (f"postgresql://{quote(sec['pg_user'])}:{quote(sec['pg_password'])}"
            f"@{cfg['pg_host']}:{cfg['pg_port']}/{cfg['db_name']}")


# ── Fixture layout ───────────────────────────────────────────────────────────
# Global arrival index k (0..MAX_STUDENTS-1) → (school s, local index j, device d, enrollid)
# Schools are interleaved so every stage spreads check-ins across all schools
# and all devices. The same enrollid exists on every device of every school,
# which exercises attribution by (device, enrollid) at full scale.

def layout(cfg: dict, k: int) -> dict:
    n_sch = cfg['num_schools']
    dps = cfg['devices_per_school']
    s = k % n_sch
    j = k // n_sch
    d = j % dps
    enroll = j // dps + 1
    return {'k': k, 'school_idx': s, 'local_idx': j, 'device_idx': d, 'enrollid': enroll}


def school_code(cfg: dict, s: int) -> str:
    return f"LT{tag(cfg)}S{s:02d}"          # ≤ 20 chars (schools.code)


def device_sn(cfg: dict, s: int, d: int) -> str:
    return f"LTAF-{tag(cfg)}-S{s:02d}-D{d}"


def student_code(cfg: dict, s: int, j: int) -> str:
    return f"LT{tag(cfg)}-{s:02d}-{j:04d}"


def parent_username(cfg: dict, k: int) -> str:
    return f"lt{tag(cfg)}p{k:05d}"


def device_index(cfg: dict, s: int, d: int) -> int:
    return s * cfg['devices_per_school'] + d


# ── Schedule ─────────────────────────────────────────────────────────────────

def stage_for_count(k: int):
    prev = 0
    for st in STAGES:
        if k < st[3]:
            return st, prev
        prev = st[3]
    raise ValueError(k)


def arrival_offset(k: int) -> float:
    """Seconds from round start at which student k is introduced."""
    (no, start, _end, target), prev = stage_for_count(k)
    n = target - prev
    return start + RAMP_SECONDS * (k - prev + 0.5) / n


def stage_at(t: float):
    for st in STAGES:
        if st[1] <= t < st[2]:
            return st
    if RECOVERY[1] <= t < RECOVERY[2]:
        return RECOVERY
    return None


def target_students_at(t: float) -> int:
    """Cumulative students scheduled to have been introduced by time t."""
    prev = 0
    for no, start, end, target in STAGES:
        if t < start:
            return prev
        if t < end:
            frac = min(1.0, (t - start) / RAMP_SECONDS)
            return prev + int((target - prev) * frac)
        prev = target
    return prev


def intended_event_rate(stage) -> float:
    no, start, end, target = stage
    prev = 0 if no == 1 else STAGES[no - 2][3]
    return (target - prev) / RAMP_SECONDS


def _h(*parts) -> int:
    return zlib.crc32(':'.join(str(p) for p in parts).encode())


def parent_stagger(k: int) -> float:
    return (_h('stagger', k) % 30000) / 1000.0


def device_time_for(k: int) -> dt.time:
    """Synthetic school-local morning scan time (06:45:00–08:44:59)."""
    sec = (k * 7919) % 7200
    base = dt.datetime(2000, 1, 1, 6, 45, 0) + dt.timedelta(seconds=sec)
    return base.time()


def expected_checkin_status(cfg: dict, t: dt.time) -> str:
    late = dt.time.fromisoformat(cfg['att_late_threshold'])
    return 'late' if t >= late else 'present'


# ── Deterministic attendance history ─────────────────────────────────────────

WEEKEND = {4, 5}   # Friday, Saturday (Python weekday numbers)


def history_dates(cfg: dict) -> list[dt.date]:
    end = dt.date.fromisoformat(cfg['history_end_date'])
    out = []
    for i in range(cfg['history_calendar_days']):
        d = end - dt.timedelta(days=i)
        if d.weekday() not in WEEKEND:
            out.append(d)
    return sorted(out)


def expected_history(cfg: dict, s: int, j: int, d: dt.date) -> dict:
    h = _h('hist', cfg['experiment_id'], s, j, d.isoformat())
    r = h % 100
    if r < 80:
        status = 'present'
        ci = dt.time(7, h % 30, (h >> 8) % 60)
    elif r < 90:
        status = 'late'
        ci = dt.time(7, 50 + h % 10, (h >> 8) % 60)
    elif r < 97:
        status = 'absent'
        ci = None
    else:
        status = 'excused'
        ci = None
    co = dt.time(13, (h >> 4) % 30, (h >> 12) % 60) if ci else None
    return {'status': status, 'check_in': ci, 'check_out': co}


def hhmm(t: dt.time | None) -> str | None:
    return t.strftime('%H:%M') if t else None
