"""OS-thread adapter for LOCAL TOOLING VALIDATION ONLY.

Runs the same probe_core.Round (schedule, protocol, validation, accounting,
stop protocol) without gevent/Locust, for environments where Locust cannot run
(e.g. greenlet blocked by Windows Application Control). One thread per device
connection and one thread per active parent session, with a keep-alive HTTP
connection each. Intended for small stage limits; its numbers are NOT a
capacity measurement and must never be reported as one.
"""
from __future__ import annotations

import argparse
import http.client
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_core  # noqa: E402

MAX_THREADS_GUARD = 600


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--stage-limit', type=int, default=3)
    ap.add_argument('--no-watchdog', action='store_true')
    a = ap.parse_args()
    core = probe_core.Round(os.path.abspath(a.root), os.path.abspath(a.out), stage_limit=a.stage_limit,
                            expect_watchdog=not a.no_watchdog)
    if core.max_k > MAX_THREADS_GUARD:
        raise SystemExit('thread driver is for small validation runs only (use Locust for the real round)')
    host, port = core.cfg['target_host'], core.cfg['http_port']

    dev_threads = []
    for _ in core.devices:
        slot = core.next_device_slot()
        t = threading.Thread(target=core.run_device, args=(slot,), daemon=True, name=f'dev{slot}')
        t.start()
        dev_threads.append(t)
    warm = time.monotonic()
    while not core.devices_ready() and time.monotonic() - warm < 30:
        time.sleep(0.2)
    print(f'devices registered: {len(core.registered)}/{len(core.devices)} in {time.monotonic() - warm:.1f}s')
    core.start()

    parents = []   # (thread, stop_event)

    def parent_body(slot, stop_evt):
        conn = http.client.HTTPConnection(host, port, timeout=30)

        def get(path, token, name):
            t0 = time.perf_counter()
            try:
                conn.request('GET', path, headers={'Authorization': f'Bearer {token}'})
                r = conn.getresponse()
                body = r.read()
                return r.status, body, (time.perf_counter() - t0) * 1000, None
            except Exception as exc:
                conn.close()
                return 0, b'', (time.perf_counter() - t0) * 1000, type(exc).__name__
        core.parent_started()
        try:
            core.run_parent(slot, get, stopped=stop_evt.is_set)
        finally:
            core.parent_stopped()
            conn.close()

    while not core.finished():
        target = core.target_parent_sessions()
        alive = [(t, e) for t, e in parents if t.is_alive() and not e.is_set()]
        while len(alive) < target:
            e = threading.Event()
            t = threading.Thread(target=parent_body, args=(core.next_parent_slot(), e), daemon=True)
            t.start()
            parents.append((t, e))
            alive.append((t, e))
        for t, e in alive[target:]:
            e.set()
        time.sleep(0.2)
    for _, e in parents:
        e.set()
    for t, _ in parents:
        t.join(timeout=35)
    core.finish()
    print('round finished: mode', core.mode, core.mode_reason)


if __name__ == '__main__':
    main()
