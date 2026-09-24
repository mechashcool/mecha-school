"""Locust adapter for the timed attendance round (run on Linux: VPS or a separate host).

    ATTLT_ROOT=<experiment root> ATTLT_OUT=<root>/results/round1 \
    <root>/venv-gen/bin/locust -f locustfile.py --headless \
        --host http://127.0.0.1:<http_port> \
        --csv <out>/locust --csv-full-history --html <out>/locust_report.html \
        --logfile <root>/logs/locust.log

Normally started by run_round.py (which also runs the baseline, the watchdog,
reconciliation and analysis). The staged shape, device schedule and all
accounting live in probe_core.Round; this file only binds them to Locust.
Optional multi-process partitioning: ATTLT_PART_INDEX / ATTLT_PART_COUNT (one
independent locust process per partition, same experiment root).
"""
import os
import time

from locust import FastHttpUser, LoadTestShape, User, constant, events, task  # gevent patched first
from locust.exception import StopUser

import probe_core

ROOT = os.environ['ATTLT_ROOT']
OUT = os.environ.get('ATTLT_OUT', os.path.join(ROOT, 'results', 'round1'))
CORE = probe_core.Round(ROOT, OUT, part_index=int(os.environ.get('ATTLT_PART_INDEX', '0')),
                        part_count=int(os.environ.get('ATTLT_PART_COUNT', '1')),
                        stage_limit=int(os.environ.get('ATTLT_STAGE_LIMIT', '9')),
                        expect_watchdog=os.environ.get('ATTLT_EXPECT_WATCHDOG', '1') == '1')
WARMUP_MAX_S = 30.0
_warm_start = None


def _fire(request_type, name, ms, exc):
    events.request.fire(request_type=request_type, name=name, response_time=ms or 0, response_length=0,
                        exception=exc, context={})


@events.test_stop.add_listener
def _on_stop(**_kw):
    CORE.finish()


class DeviceUser(User):
    """One persistent AI Face connection per simulated device."""
    fixed_count = len(CORE.devices)
    wait_time = constant(1)

    def on_start(self):
        self.slot = CORE.next_device_slot()

    @task
    def drive(self):
        # Holds the persistent connection until the round finishes, then stops
        # this user cleanly (StopUser => Locust will not re-run the task, so no
        # second connection is opened and teardown is not blocked).
        CORE.run_device(self.slot, on_request=_fire, sleep=time.sleep)
        raise StopUser()


class ParentUser(FastHttpUser):
    """One parent session reading its own child's attendance every ~30 s."""
    weight = 1
    wait_time = constant(1)
    network_timeout = 30.0
    connection_timeout = 10.0

    def on_start(self):
        self.slot = CORE.next_parent_slot()
        CORE.parent_started()
        self._counted = True

    def on_stop(self):
        if getattr(self, '_counted', False):
            self._counted = False
            CORE.parent_stopped()

    def _get(self, path, token, name):
        t0 = time.perf_counter()
        with self.client.get(path, headers={'Authorization': f'Bearer {token}'}, name=name,
                             catch_response=True) as resp:
            ms = (time.perf_counter() - t0) * 1000
            status = resp.status_code
            body = resp.content or b''
            err = None
            if status == 0:
                err = type(resp.error).__name__ if resp.error else 'ConnectionError'
                resp.failure(err)
            elif name == 'parent_cross_school_denied':
                resp.success() if status == 404 else resp.failure(f'expected 404, got {status}')
            elif status != 200:
                resp.failure(f'HTTP {status}')
            return status, body, ms, err

    @task
    def drive(self):
        try:
            CORE.run_parent(self.slot, self._get, sleep=time.sleep)
        finally:
            self.on_stop()
        raise StopUser()


class ProbeShape(LoadTestShape):
    def tick(self):
        global _warm_start
        n_dev = len(CORE.devices)
        if CORE.t0 is None:
            if _warm_start is None:
                _warm_start = time.monotonic()
            if not CORE.devices_ready() and time.monotonic() - _warm_start < WARMUP_MAX_S:
                return n_dev, 50            # setup: connect + register devices (not timed)
            CORE.start()
        if CORE.finished():
            return None
        return n_dev + CORE.target_parent_sessions(), CORE.spawn_rate_hint()
