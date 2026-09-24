"""Orchestrate ONE timed round.

  1. preconditions: target healthy + identity, isolation check OK, precheck all
     pass for the SAME test date, tokens valid, test date fresh, no existing
     test-date rows for load students, disk space
  2. snapshot of repository/application files (to prove they were not changed)
  3. baseline (watchdog --baseline, default 60 s)
  4. generator (Locust on Linux; thread_driver for local validation only)
  5. watchdog guarding the generator; hard timeout as a last resort
  6. reconciliation + analysis

Refuses to run a second full round into an existing output directory.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import manifest  # noqa: E402
import target as target_mod  # noqa: E402

IS_WINDOWS = os.name == 'nt'


def gen_python(root):
    return os.path.join(root, 'venv-gen', 'Scripts' if IS_WINDOWS else 'bin', 'python' + ('.exe' if IS_WINDOWS else ''))


def repo_snapshot(repo):
    st = subprocess.run(['git', '-C', repo, 'status', '--porcelain=v1', '-uall'], capture_output=True, text=True).stdout
    diff = subprocess.run(['git', '-C', repo, 'diff', 'HEAD'], capture_output=True).stdout
    head = subprocess.run(['git', '-C', repo, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    tracked_status = '\n'.join(l for l in st.splitlines() if 'loadtest/' not in l)
    return {'head': head, 'status_sha256': hashlib.sha256(tracked_status.encode()).hexdigest(),
            'diff_sha256': hashlib.sha256(diff).hexdigest(), 'taken_at': manifest.utcnow_iso()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--round-name', default='round1')
    ap.add_argument('--stage-limit', type=int, default=9)
    ap.add_argument('--baseline-seconds', type=int, default=60)
    ap.add_argument('--post-seconds', type=int, default=120)
    ap.add_argument('--driver', choices=['locust', 'thread'], default='thread' if IS_WINDOWS else 'locust')
    ap.add_argument('--live-health-url', default='')
    ap.add_argument('--min-mem-pct', type=float, default=20.0)
    ap.add_argument('--allow-degraded-host', action='store_true',
                    help='LOCAL VALIDATION ONLY: run despite baseline below the memory floor; '
                         'resource-guard compliance is recorded OVERRIDDEN, never passed')
    ap.add_argument('--outbox-monitor', action='store_true',
                    help='institute/outbox rounds only: sample notification_outbox '
                         'every watchdog cycle and enforce the outbox guard rules')
    ap.add_argument('--validation', action='store_true', help='label output as local tooling validation')
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    cfg = common.load_config(root)
    sec = common.load_secrets(root)
    out = os.path.join(root, 'results', a.round_name)
    if os.path.exists(os.path.join(out, 'round_start.json')):
        raise SystemExit(f'{out} already contains a round — refusing to run another into it')
    os.makedirs(out, exist_ok=True)
    manifest.write_owner_marker(out, cfg['experiment_id'])
    if a.driver == 'thread' and not a.validation:
        raise SystemExit('the thread driver is for local validation only (pass --validation)')

    # 1. preconditions
    tinfo = json.load(open(os.path.join(root, 'run', 'target.json')))
    if not (target_mod._alive(tinfo) and target_mod.http_ok(cfg)):
        raise SystemExit('target is not running/healthy')
    iso = json.load(open(os.path.join(root, 'results', 'isolation_check.json')))
    if not iso.get('ISOLATION_OK'):
        raise SystemExit('isolation check did not pass')
    from zoneinfo import ZoneInfo
    test_date = dt.datetime.now(ZoneInfo(cfg['school_timezone'])).date().isoformat()
    pre = json.load(open(os.path.join(root, 'results', 'precheck.json')))
    if not pre.get('all_pass') or pre.get('test_date') != test_date:
        raise SystemExit('precheck must pass for today\'s test date before the round')
    import psycopg2
    fx = json.load(open(os.path.join(root, 'run', 'fixtures.json'), encoding='utf-8'))
    conn = psycopg2.connect(**common.pg_dsn(cfg, sec))
    cur = conn.cursor()
    load_ids = [fx['schools'][str(s)]['id'] for s in range(cfg['num_schools'])]
    cur.execute('SELECT count(*) FROM student_attendance WHERE date=%s AND school_id = ANY(%s)', (test_date, load_ids))
    if cur.fetchone()[0]:
        raise SystemExit('load students already have attendance on the test date — fixtures are not fresh')
    conn.close()
    free_gb = shutil.disk_usage(root).free / 2**30
    if free_gb < 10:
        raise SystemExit(f'only {free_gb:.1f} GB free')
    run_meta = {'round': a.round_name, 'driver': a.driver, 'stage_limit': a.stage_limit, 'validation_only': a.validation,
                'test_date': test_date, 'target': tinfo, 'app_revision': cfg.get('app_revision_full'),
                'repo_snapshot_before': repo_snapshot(manifest.REPO_ROOT), 'host': os.environ.get('COMPUTERNAME') or os.uname().nodename}
    json.dump(run_meta, open(os.path.join(out, 'run_meta.json'), 'w'), indent=2, default=str)

    py = gen_python(root)
    tool = common.TOOL_DIR
    logs = os.path.join(root, 'logs')
    guard_args = ['--min-mem-pct', str(a.min_mem_pct)] + (['--allow-degraded-host'] if a.allow_degraded_host else [])
    # Outbox monitoring is opt-in and applies to the GUARD pass only: the
    # baseline and the startup gate never query the outbox, so an AI Face round
    # issues no outbox query at any point.
    watch_args = guard_args + (['--outbox-monitor'] if a.outbox_monitor else [])
    # 3. baseline
    print(f'baseline {a.baseline_seconds}s ...', flush=True)
    subprocess.run([py, os.path.join(tool, 'watchdog.py'), '--root', root, '--out', out,
                    '--baseline', str(a.baseline_seconds)], check=True)

    # 3b. startup safety gate — REJECT an unsafe host before generating any load
    print('startup safety gate ...', flush=True)
    gate = subprocess.run([py, os.path.join(tool, 'watchdog.py'), '--root', root, '--out', out,
                           '--check-gate'] + guard_args, capture_output=True, text=True)
    print(gate.stdout.strip(), flush=True)
    gate_res = json.load(open(os.path.join(out, 'startup_gate.json')))
    run_meta['startup_gate'] = gate_res
    run_meta['resource_guard_compliance'] = gate_res['compliance']
    json.dump(run_meta, open(os.path.join(out, 'run_meta.json'), 'w'), indent=2, default=str)
    if gate.returncode != 0:
        raise SystemExit(f'STARTUP GATE REJECTED THE ROUND: {gate_res["reasons"]}\n'
                         'The host is below the resource floor. Free resources, or (local '
                         'validation only) pass --allow-degraded-host to proceed with compliance OVERRIDDEN.')

    # 4. generator
    env = dict(os.environ, ATTLT_ROOT=root, ATTLT_OUT=out, ATTLT_STAGE_LIMIT=str(a.stage_limit), PYTHONUNBUFFERED='1')
    if a.driver == 'locust':
        locust = os.path.join(root, 'venv-gen', 'bin', 'locust')
        cmd = [locust, '-f', os.path.join(tool, 'locustfile.py'), '--headless',
               '--host', f"http://{cfg['target_host']}:{cfg['http_port']}",
               '--csv', os.path.join(out, 'locust'), '--csv-full-history', '--html', os.path.join(out, 'locust_report.html'),
               '--logfile', os.path.join(logs, f'locust_{a.round_name}.log'), '--loglevel', 'INFO', '--stop-timeout', '10']
    else:
        cmd = [py, os.path.join(tool, 'thread_driver.py'), '--root', root, '--out', out, '--stage-limit', str(a.stage_limit)]
    glog = open(os.path.join(logs, f'generator_{a.round_name}.log'), 'ab')
    gen = subprocess.Popen(cmd, cwd=tool, env=env, stdout=glog, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    gident = target_mod.proc_identity(gen.pid)
    manifest.add_resource(root, 'process', role=f'generator-{a.driver}', round=a.round_name, **gident)
    # 5. watchdog
    wlog = open(os.path.join(logs, f'watchdog_{a.round_name}.log'), 'ab')
    wd = subprocess.Popen([py, os.path.join(tool, 'watchdog.py'), '--root', root, '--out', out, '--generator-pid', str(gen.pid),
                           '--post-seconds', str(a.post_seconds)] + watch_args
                          + (['--live-health-url', a.live_health_url] if a.live_health_url else []),
                          cwd=tool, stdout=wlog, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    manifest.add_resource(root, 'process', role='watchdog', round=a.round_name, **target_mod.proc_identity(wd.pid))
    hard_limit = 30 + common.STAGES[a.stage_limit - 1][2] + 60 + 90
    t0 = time.monotonic()
    while gen.poll() is None:
        if wd.poll() is not None:
            print('watchdog exited unexpectedly — requesting halt', flush=True)
            json.dump({'mode': 'halt', 'reason': 'watchdog process exited'}, open(os.path.join(out, 'STOP.json'), 'w'))
        if time.monotonic() - t0 > hard_limit:
            print('hard time limit reached — terminating generator', flush=True)
            gen.terminate()
            try:
                gen.wait(15)
            except subprocess.TimeoutExpired:
                gen.kill()
            break
        time.sleep(2)
    print(f'generator exited code={gen.returncode}; waiting for recovery observation', flush=True)
    manifest.update_resource(root, 'process', {'pid': gen.pid}, stopped_at=manifest.utcnow_iso(), exit_code=gen.returncode)
    wd.wait(timeout=a.post_seconds + 120)
    manifest.update_resource(root, 'process', {'pid': wd.pid}, stopped_at=manifest.utcnow_iso())

    # 6. reconciliation + analysis
    subprocess.run([py, os.path.join(tool, 'reconcile.py'), '--root', root, '--out', out], check=False)
    subprocess.run([py, os.path.join(tool, 'analyze.py'), '--root', root, '--out', out], check=False)
    run_meta['repo_snapshot_after'] = repo_snapshot(manifest.REPO_ROOT)
    run_meta['repo_unchanged'] = (run_meta['repo_snapshot_after']['status_sha256'] == run_meta['repo_snapshot_before']['status_sha256']
                                  and run_meta['repo_snapshot_after']['diff_sha256'] == run_meta['repo_snapshot_before']['diff_sha256'])
    json.dump(run_meta, open(os.path.join(out, 'run_meta.json'), 'w'), indent=2, default=str)
    print('round complete:', out)


if __name__ == '__main__':
    main()
