"""Pure, dependency-free guard rules (no psutil / no clock of their own).

Extracted from watchdog.py so the safety logic can be unit-tested with simulated
monitoring samples and an injected clock, without touching real resources.

Two things live here:
  * threshold construction + the startup safety gate (baseline precondition)
  * a SustainedBreach detector (condition must hold continuously for N seconds)

Design decisions after the s3c finding:
  * The memory floor is a FIXED percentage (default 20%, the requested
    guardrail). There is NO silent auto-lowering to fit an unhealthy host.
  * If the baseline is already below the floor, the round is REJECTED at
    startup unless `allow_degraded_host=True` is passed explicitly. That flag
    is for local functional validation only; it stamps compliance OVERRIDDEN
    and drops the floor only to an absolute exhaustion guard, never to "off".
"""
from __future__ import annotations

# Resource-guard compliance states (kept separate from functional/latency verdicts)
COMPLIANCE_ENFORCED = 'ENFORCED'       # full guardrails active
COMPLIANCE_OVERRIDDEN = 'OVERRIDDEN'   # degraded-host override used; safety NOT certifiable

ABSOLUTE_MEM_EXHAUSTION_PCT = 2.0      # floor used only under an explicit override
DEFAULT_MIN_MEM_PCT = 20.0             # the requested, non-lowerable MemAvailable guardrail


def build_thresholds(*, min_mem_pct: float = 20.0, allow_degraded_host: bool = False,
                     mem_total_gb: float | None = None, live_health_url: str | None = None) -> dict:
    """Return the threshold dict and the compliance mode. No baseline adaptation."""
    if allow_degraded_host:
        mem_floor = ABSOLUTE_MEM_EXHAUSTION_PCT
        compliance = COMPLIANCE_OVERRIDDEN
        note = (f'DEGRADED-HOST OVERRIDE: memory floor set to the absolute exhaustion guard '
                f'{ABSOLUTE_MEM_EXHAUSTION_PCT}% for 10 s (requested guardrail is {min_mem_pct}%). '
                f'Resource-guard compliance is OVERRIDDEN — safety is NOT certified.')
    else:
        mem_floor = min_mem_pct
        compliance = COMPLIANCE_ENFORCED
        note = f'MemAvailable < {min_mem_pct}% for 10 s'
    th = {
        'parent_read_p95_ms': 2000, 'parent_read_sustain_s': 20,
        'ack_p95_ms': 5000, 'ack_sustain_s': 20,
        'min_samples_for_percentile': 20,
        'error_rate': 0.01, 'min_samples_for_error_rate': 100,
        'host_cpu_pct': 85.0, 'host_cpu_sustain_s': 20,
        'mem_available_floor_pct': mem_floor, 'mem_sustain_s': 10, 'mem_note': note,
        'mem_requested_guardrail_pct': min_mem_pct, 'mem_total_gb': mem_total_gb,
        'disk_free_floor_gb': 5.0, 'disk_decline_gb': 2.0,
        'log_growth_mb_per_min': 50.0,
        'io_time_ratio': 1.5, 'io_sustain_s': 20,
        'swap_growth_gb': 1.0,
        'db_connections_frac_of_max': 0.9,
        'event_backlog': 50, 'backlog_growth_window_s': 20,
        'send_delay_p95_s': 10.0,
        'generator_cpu_pct_of_one_core': 90.0, 'generator_sustain_s': 20,
        'generator_stale_s': 20, 'halt_ignored_s': 45,
        'missed_read_fraction': 0.05,
        'compliance_mode': compliance,
        'live_service_health_url': live_health_url or None,
    }
    th.update(OUTBOX_THRESHOLDS)
    return th


# ── Durable-notification-outbox guardrails ───────────────────────────────────
# Added for the institute outbox probe. These are PURELY ADDITIVE: no value
# above is read, relaxed or recomputed here, so the CPU, memory, DB-connection,
# latency and error-rate guardrails keep the limits they already had.

OUTBOX_THRESHOLDS = {
    # Hard ceiling on pending+processing+retry. A backlog larger than this
    # means the producer has outrun the worker by more than the round intends
    # to prove, and nothing further is learned by continuing.
    'outbox_backlog_ceiling': 2000,
    # The backlog must actually fall once the worker is meant to be draining.
    'outbox_no_drain_window_s': 60,
    # Any job reaching a terminal failure state is unexpected in a success-mode
    # round: the fake Firebase always succeeds, so `dead` can only mean a real
    # defect in the worker or the database.
    'outbox_dead_jobs_allowed': 0,
    'outbox_cancelled_jobs_allowed': 0,
    # A worker restart the harness did not ask for is a crash.
    'outbox_unplanned_worker_restarts_allowed': 0,
    # Database symptoms that must stop a round immediately rather than be
    # averaged into an error rate.
    'db_operational_errors_allowed': 0,
    'db_pool_timeouts_allowed': 0,
    'worker_tracebacks_allowed': 0,
    # Isolation is binary. One violation ends the round.
    'isolation_violations_allowed': 0,
}


def outbox_breaches(sample: dict, th: dict, *, draining: bool,
                    sustained=None) -> list:
    """Evaluate the outbox guardrails against one monitoring sample.

    Pure: `sample` is a plain dict of observed counters and `sustained` is the
    caller's SustainedBreach instance (or None to skip the time-based rule).
    Returns the list of stop reasons; empty means healthy.

    FAIL CLOSED: a sample that was not taken, or was taken and failed, is a
    stop reason in itself. An absent `collector_ok` key means no collector ran,
    which is treated exactly like a failed one — a round must never read as
    healthy because nothing was watching.
    """
    reasons = []
    if not sample.get('collector_ok', False):
        err = sample.get('collector_error') or 'no outbox sample was taken'
        reasons.append(f'outbox collector unavailable — {err}')
        n = int(sample.get('consecutive_collector_failures', 0) or 0)
        if n > 1:
            reasons.append(f'outbox collector has failed {n} times in a row')
        # Every rule below reads a counter this sample does not have. Returning
        # now prevents a missing key from being read as a healthy zero.
        return reasons
    backlog = int(sample.get('outbox_backlog', 0))
    if backlog > th['outbox_backlog_ceiling']:
        reasons.append(f"outbox backlog {backlog} > ceiling "
                       f"{th['outbox_backlog_ceiling']}")
    if int(sample.get('outbox_dead', 0)) > th['outbox_dead_jobs_allowed']:
        reasons.append(f"outbox dead jobs {sample.get('outbox_dead')} — "
                       'unexpected in a success-mode round')
    if int(sample.get('outbox_cancelled', 0)) > th['outbox_cancelled_jobs_allowed']:
        reasons.append(f"outbox cancelled jobs {sample.get('outbox_cancelled')}")
    if int(sample.get('unplanned_worker_restarts', 0)) > \
            th['outbox_unplanned_worker_restarts_allowed']:
        reasons.append('outbox worker restarted without the harness asking')
    for key, limit, label in (
            ('db_operational_errors', 'db_operational_errors_allowed',
             'database OperationalError'),
            ('db_pool_timeouts', 'db_pool_timeouts_allowed',
             'database pool timeout'),
            ('worker_tracebacks', 'worker_tracebacks_allowed',
             'traceback in the worker log'),
            ('isolation_violations', 'isolation_violations_allowed',
             'ISOLATION VIOLATION')):
        n = int(sample.get(key, 0))
        if n > th[limit]:
            reasons.append(f'{label}: {n}')
    if draining and sustained is not None:
        stuck = backlog > 0 and not sample.get('outbox_backlog_falling', False)
        if sustained('outbox_no_drain', stuck, th['outbox_no_drain_window_s']):
            reasons.append(
                f"outbox backlog stuck at {backlog} for "
                f"{th['outbox_no_drain_window_s']}s while the worker should be "
                'draining')
    return reasons


def startup_gate(baseline: dict, *, min_mem_pct: float = 20.0, min_disk_gb: float = 10.0,
                 allow_degraded_host: bool = False) -> dict:
    """Decide whether it is safe to START a round, given the baseline sample.

    Returns {'ok', 'compliance', 'reasons', 'checked'}. When the host is below a
    safety floor the round is rejected unless allow_degraded_host is set, in
    which case it may proceed but compliance is OVERRIDDEN.
    """
    host = (baseline or {}).get('host', {})
    mem = host.get('mem_available_pct_min')
    disk = host.get('disk_free_gb')
    reasons, checked = [], {}
    unsafe = []
    if mem is not None:
        checked['baseline_mem_available_pct_min'] = mem
        if mem < min_mem_pct:
            unsafe.append(f'baseline MemAvailable {mem:.2f}% < required {min_mem_pct}%')
    else:
        unsafe.append('baseline memory not measured')
    if disk is not None:
        checked['baseline_disk_free_gb'] = disk
        if disk < min_disk_gb:
            unsafe.append(f'baseline disk free {disk:.1f} GB < required {min_disk_gb} GB')
    if not unsafe:
        return {'ok': True, 'compliance': COMPLIANCE_ENFORCED, 'reasons': [], 'checked': checked}
    if allow_degraded_host:
        return {'ok': True, 'compliance': COMPLIANCE_OVERRIDDEN,
                'reasons': ['PROCEEDING UNDER OVERRIDE despite: ' + '; '.join(unsafe)], 'checked': checked}
    return {'ok': False, 'compliance': COMPLIANCE_ENFORCED,
            'reasons': ['unsafe startup rejected: ' + '; '.join(unsafe)], 'checked': checked}


class SustainedBreach:
    """Fires True once `cond` has held continuously for `secs`, using an injected
    clock (so tests drive it deterministically). Resets the moment cond is False.
    """
    def __init__(self, clock):
        self._clock = clock
        self._since: dict = {}

    def __call__(self, key: str, cond: bool, secs: float) -> bool:
        now = self._clock()
        if cond:
            self._since.setdefault(key, now)
            return now - self._since[key] >= secs
        self._since.pop(key, None)
        return False

    def reset(self, key: str) -> None:
        self._since.pop(key, None)
