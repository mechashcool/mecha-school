"""Institute attendance + durable outbox END-TO-END SMOKE TEST.

Runs INSIDE the runner container on the internal-only compose network. One
submission, one newly-absent student, one replay, then one drain. This is not
a load test: there is no loop, no concurrency and no stage ladder.

Sequence, and it stops at the first failure rather than continuing:

  C  prove the network is internal; verify the environment identity
  D  seed the smallest synthetic dataset the fixture layer supports
  E  plan, and require the arithmetic to be deterministic
  F  ONE submission with the worker STOPPED  → jobs must sit pending
  G  the same submission again              → nothing new may appear
  H  start the worker                       → the backlog must drain to sent
  I  the collector must see backlog > 0 then == 0, and agree with the guards

Writes /exp/results/institute_smoke/report.json. That report contains counts,
verdicts and timings only — never a token, a credential or a payload.
"""
import datetime as dt
import json
import os
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

sys.path.insert(0, '/tooling')
os.environ['ATTLT_BIN_DIR'] = '/usr/local/bin'      # use the image interpreter

import common                      # noqa: E402
import environment_identity as ident   # noqa: E402
import guard_rules                 # noqa: E402
import institute_common as ic      # noqa: E402
import institute_generator as igen  # noqa: E402
import outbox_monitor as omon      # noqa: E402
import outbox_reconcile as orec    # noqa: E402
import safety_gates                # noqa: E402
import target as target_mod        # noqa: E402
import worker_control as wc        # noqa: E402

import psycopg2                    # noqa: E402

ROOT = '/exp'
OUT = os.path.join(ROOT, 'results', 'institute_smoke')
REPORT = {'started_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
          'parts': {}, 'errors': []}


class SmokeFailed(SystemExit):
    pass


def say(*a):
    print(*a, flush=True)


def part(name, payload):
    REPORT['parts'][name] = payload
    say(f'\n── {name} ' + '─' * max(0, 60 - len(name)))
    say(json.dumps(payload, indent=2, default=str))


def save():
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, 'report.json'), 'w', encoding='utf-8') as fh:
        json.dump(REPORT, fh, indent=2, default=str, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════
#  C — isolation proof and identity
# ═══════════════════════════════════════════════════════════════════════════

def prove_internal_network() -> dict:
    """Positive proof that nothing here can leave the private network.

    Three probes, all of which MUST fail. This is the Docker-internal
    equivalent of netns_proof.py, and it is evidence rather than an assertion:
    if any probe succeeds, the network is not internal and the round stops.
    """
    results = {}
    ok = True

    # 1. DNS for a public name must not resolve (internal networks have no
    #    upstream resolver path).
    try:
        socket.setdefaulttimeout(5)
        socket.getaddrinfo('pypi.org', 443)
        results['dns_public'] = 'RESOLVED — network is not internal'
        ok = False
    except Exception as exc:
        results['dns_public'] = f'failed as required ({type(exc).__name__})'

    # 2. A raw outbound TCP connection to a public IP must not connect.
    for label, addr in (('tcp_8888_dns', ('8.8.8.8', 53)),
                        ('tcp_cloudflare', ('1.1.1.1', 443))):
        try:
            s = socket.create_connection(addr, timeout=5)
            s.close()
            results[label] = 'CONNECTED — network is not internal'
            ok = False
        except Exception as exc:
            results[label] = f'failed as required ({type(exc).__name__})'

    # 3. The production AI Face port must be unreachable by name or by route.
    try:
        s = socket.create_connection(('127.0.0.1', 7788), timeout=2)
        s.close()
        results['local_7788'] = 'SOMETHING IS LISTENING ON 7788'
        ok = False
    except Exception as exc:
        results['local_7788'] = f'nothing listening ({type(exc).__name__})'

    # 4. The database must be reachable — isolation is not the same as broken.
    try:
        s = socket.create_connection((os.environ['ATTLT_PG_HOST'], 5432),
                                     timeout=5)
        s.close()
        results['db_reachable'] = 'yes (required)'
    except Exception as exc:
        results['db_reachable'] = f'NO — {type(exc).__name__}'
        ok = False
    socket.setdefaulttimeout(None)
    results['internal_network_proven'] = ok
    return results


def part_c():
    proof = prove_internal_network()
    if not proof['internal_network_proven']:
        part('C_isolation', proof)
        raise SmokeFailed('network isolation NOT proven — stopping')

    cfg = common.load_config(ROOT)
    identity = ident.verify(ROOT, experiment_id=cfg['experiment_id'],
                            tag=common.tag(cfg))
    printable = {
        'experiment_tag': identity['experiment_tag'],
        'experiment_id': identity['experiment_id'],
        'source_commit': identity['app_source_commit'],
        'database_name': identity['database_name'],
        'database_host': identity['database_host'],
        'database_classification': identity['database_host_classification'],
        'database_isolated': identity['database_isolated'],
        'network_isolation': 'internal docker network, proven from inside',
        'firebase_mode': identity['firebase_mode'],
        'http_port': identity['target_http_port'],
        'ws_port': identity['target_ws_port'],
        'production_reachable': identity['production_network_reachable'],
        'app_process_role': 'web (isolated target)',
        'worker_process_role': 'outbox-worker',
    }
    assert identity['database_host_classification'] in ('container', 'loopback')
    assert identity['firebase_mode'] == 'fake-local'
    assert identity['production_network_reachable'] is False
    assert identity['target_ws_port'] != 7788
    part('C_isolation', {'network_proof': proof, 'identity': printable})
    return cfg


def run_gates(cfg, sec, netns_ok):
    """Both processes' gates, before a single row is written."""
    prefixes = ic.experiment_prefixes(common.tag(cfg))
    identity = ident.load(ROOT)
    target_env = target_mod.build_env(cfg, sec, ws_enabled=False,
                                      outbox_enabled=True)
    worker_env = wc.build_worker_env(cfg, sec)
    wc.write_fake_credential(ROOT)
    out = {}
    out['target'] = safety_gates.run_all(
        env=target_env, cfg=cfg, identity=identity, prefixes=prefixes,
        role='target', netns_proof_ok=netns_ok)
    out['worker'] = safety_gates.run_all(
        env=worker_env, cfg=cfg, identity=identity, prefixes=prefixes,
        role='worker', netns_proof_ok=netns_ok)
    out['aiface_disabled_for_target'] = target_env['AIFACE_WS_ENABLED']
    out['outbox_enabled_for_target'] = target_env[
        'INSTITUTE_ATTENDANCE_OUTBOX_ENABLED']
    out['aiface_disabled_for_worker'] = worker_env['AIFACE_WS_ENABLED']
    part('C_gates', out)


# ═══════════════════════════════════════════════════════════════════════════
#  D — seed
# ═══════════════════════════════════════════════════════════════════════════

def sh(cmd, **kw):
    say('  $', ' '.join(cmd))
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        raise SmokeFailed(f'command failed ({r.returncode}): {" ".join(cmd)}')
    return r


def part_d(cfg, sec):
    env = target_mod.build_env(cfg, sec, ws_enabled=False,
                               outbox_enabled=True)
    app_src = os.path.join(ROOT, 'app_src')
    # Ownership marker first: seed.py refuses without it.
    conn = psycopg2.connect(**common.pg_dsn(cfg, sec))
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute('CREATE TABLE IF NOT EXISTS attlt_owner (experiment_id text)')
    cur.execute('SELECT count(*) FROM attlt_owner')
    if cur.fetchone()[0] == 0:
        cur.execute('INSERT INTO attlt_owner VALUES (%s)',
                    (cfg['experiment_id'],))
    conn.close()

    t0 = time.time()
    sh(['/usr/local/bin/flask', 'db', 'upgrade'], cwd=app_src, env=env)
    migrate_s = round(time.time() - t0, 1)
    sh(['/usr/local/bin/python', '/tooling/seed.py', '--root', ROOT,
        '--inside'], cwd=app_src, env=env)
    sh(['/usr/local/bin/python', '/tooling/institute_fixtures.py', '--root',
        ROOT, '--inside'], cwd=app_src, env=env)

    inst = json.load(open(os.path.join(ROOT, 'run', 'institute_fixtures.json'),
                          encoding='utf-8'))
    counts = row_counts(cfg, sec)
    part('D_seed', {'migrations_seconds': migrate_s,
                    'config': {k: cfg[k] for k in
                               ('num_schools', 'students_per_school',
                                'devices_per_school', 'history_calendar_days')},
                    'institute_totals': inst['totals'],
                    'row_counts': counts,
                    'test_date': inst['test_date']})
    return inst


def row_counts(cfg, sec) -> dict:
    conn = psycopg2.connect(**common.pg_dsn(cfg, sec))
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor()
    out = {}
    for table in ('schools', 'students', 'users', 'employees',
                  'institute_study_groups', 'institute_group_enrollments',
                  'institute_attendance_sessions',
                  'institute_attendance_records', 'mobile_device_tokens',
                  'notifications', 'notification_outbox', 'parent_students'):
        cur.execute(f'SELECT count(*) FROM {table}')
        out[table] = cur.fetchone()[0]
    conn.close()
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  E — plan
# ═══════════════════════════════════════════════════════════════════════════

def part_e(cfg, inst):
    """One wave, one group, one absent student. Determinism is required."""
    plan_a = igen.build_plan(cfg, inst, waves=1, rate=1.0, absent_fraction=1.0)
    plan_b = igen.build_plan(cfg, inst, waves=1, rate=1.0, absent_fraction=1.0)
    if json.dumps(plan_a, sort_keys=True) != json.dumps(plan_b, sort_keys=True):
        raise SmokeFailed('the plan is not deterministic — stopping')
    plan = [e for e in plan_a if e['student_ids']]
    if len(plan) != 1:
        raise SmokeFailed(f'expected exactly 1 non-empty submission, '
                          f'got {len(plan)}')
    entry = plan[0]
    expected = igen.expected_outbox_jobs(plan, inst, cfg)
    if len(entry['absent_student_ids']) != 1:
        raise SmokeFailed(f'expected exactly 1 absent student, '
                          f'got {len(entry["absent_student_ids"])}')
    payload = {
        'deterministic': True,
        'students_touched': len(entry['student_ids']),
        'newly_absent_transitions': expected['newly_absent_transitions'],
        'expected_notification_rows': expected['expected_notifications'],
        'expected_outbox_jobs': expected['expected_jobs'],
        'tokens_per_parent': expected['active_tokens_per_parent'],
        'parents_per_student': expected['parents_per_student'],
        'session_id': entry['session_id'],
        'transition_id': entry['transition_id'],
    }
    part('E_plan', payload)
    return entry, expected


# ═══════════════════════════════════════════════════════════════════════════
#  F / G — submissions
# ═══════════════════════════════════════════════════════════════════════════

def submit(cfg, entry, token) -> dict:
    base = f"http://127.0.0.1:{cfg['http_port']}"
    url = (f"{base}/api/mobile/v1/teacher/institute/sessions/"
           f"{entry['session_id']}/attendance")
    body = json.dumps({'records': [
        {'student_id': sid, 'status': st}
        for sid, st in sorted(entry['intended_status_map'].items())]
    }).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Authorization', 'Bearer ' + token)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode('utf-8') or '{}')
            return {'status': resp.status,
                    'latency_ms': round((time.time() - t0) * 1000, 1),
                    'body': payload}
    except urllib.error.HTTPError as exc:
        return {'status': exc.code,
                'latency_ms': round((time.time() - t0) * 1000, 1),
                'body': exc.read().decode('utf-8')[:400]}


def outbox_snapshot(cfg, sec, school_ids, session_ids, test_date) -> dict:
    conn = psycopg2.connect(**common.pg_dsn(cfg, sec))
    conn.set_session(readonly=True, autocommit=True)
    try:
        obs = orec.collect(conn.cursor(), school_ids=school_ids,
                           session_ids=session_ids, test_date=test_date)
    finally:
        conn.close()
    obs.update(orec.read_fake_firebase(ROOT))
    obs['worker_reclaims'] = 0
    return obs


def reconcile(expected_ledger, obs, *, drain_complete) -> dict:
    return orec.verdict(orec.compute(expected_ledger, obs),
                        drain_complete=drain_complete)


def ledger_expected(rows, tokens_per_parent):
    return orec.expected_from_ledger(rows, tokens_per_parent=tokens_per_parent)


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    os.makedirs(OUT, exist_ok=True)
    cfg = part_c()
    sec = common.load_secrets(ROOT)
    run_gates(cfg, sec, netns_ok=True)

    inst = part_d(cfg, sec)
    school_ids = [s['school_id'] for s in inst['schools'].values()]
    session_ids = [g['session_id'] for s in inst['schools'].values()
                   for g in s['groups']]
    test_date = inst['test_date']

    # Target app: no AI Face, no scheduler, loopback only.
    target_mod.start(cfg, sec, ws_enabled=False, outbox_enabled=True)
    sh(['/usr/local/bin/python', '/tooling/institute_generator.py',
        'issue-tokens', '--root', ROOT, '--issue-tokens-inside'],
       cwd=os.path.join(ROOT, 'app_src'),
       env=target_mod.build_env(cfg, sec, ws_enabled=False,
                                outbox_enabled=True))
    with open(os.path.join(ROOT, igen.TOKENS_FILE), encoding='utf-8') as fh:
        tokens = json.load(fh)['tokens']

    entry, expected = part_e(cfg, inst)
    token = tokens[entry['instructor_username']]
    tpp = expected['active_tokens_per_parent']

    sampler = omon.sampler_for_experiment(ROOT, cfg, sec, school_ids)
    th = guard_rules.build_thresholds()

    # ── F: worker stopped, exactly one submission ──────────────────────────
    worker_status = wc.status(cfg)
    if worker_status.get('running'):
        raise SmokeFailed('worker is running — F requires it stopped')
    first = submit(cfg, entry, token)
    obs1 = outbox_snapshot(cfg, sec, school_ids, session_ids, test_date)
    ledger1 = [{'seq': 0, 'wave': 0, 'session_id': entry['session_id'],
                'absent_student_ids': entry['absent_student_ids'],
                'response_status': first['status'], 'error': None}]
    v1 = reconcile(ledger_expected(ledger1, tpp), obs1, drain_complete=False)
    sample_stopped = sampler.sample(window_s=th['outbox_no_drain_window_s'])
    breaches_stopped = guard_rules.outbox_breaches(
        sample_stopped, th, draining=False, sustained=None)
    part('F_first_submission', {
        'http': {k: first[k] for k in ('status', 'latency_ms')},
        'response': first['body'],
        'worker_running': False,
        'attendance_records': obs1['attendance_records_total'],
        'absent_records': obs1['attendance_absent_records'],
        'notification_rows': obs1['notification_rows'],
        'outbox_status_counts': obs1['outbox_status_counts'],
        'outbox_distinct_dedup_keys': obs1['outbox_distinct_dedup_keys'],
        'cross_school': {
            'attendance': obs1['attendance_cross_school'],
            'notifications': obs1['notification_cross_school'],
            'outbox': obs1['outbox_cross_school']},
        'fake_firebase_sends': obs1['fake_attempts'],
        'reconciliation': v1,
        'collector_sample': sample_stopped,
        'guard_reasons': breaches_stopped,
    })
    if first['status'] != 200:
        raise SmokeFailed(f'submission returned {first["status"]}')
    if not v1['correct']:
        raise SmokeFailed(f'reconciliation FAILED after F: {v1["violations"]}')
    if obs1['fake_attempts'] != 0:
        raise SmokeFailed('fake Firebase was called with the worker stopped')
    if sample_stopped['outbox_backlog'] <= 0:
        raise SmokeFailed('collector did not observe a backlog')
    if breaches_stopped:
        raise SmokeFailed(f'guards fired on a healthy backlog: '
                          f'{breaches_stopped}')

    # ── G: replay ──────────────────────────────────────────────────────────
    second = submit(cfg, entry, token)
    obs2 = outbox_snapshot(cfg, sec, school_ids, session_ids, test_date)
    replay_clean = (
        obs2['attendance_records_total'] == obs1['attendance_records_total']
        and obs2['notification_rows'] == obs1['notification_rows']
        and obs2['outbox_status_counts'] == obs1['outbox_status_counts']
        and obs2['outbox_distinct_dedup_keys']
        == obs1['outbox_distinct_dedup_keys'])
    part('G_replay', {
        'http': {k: second[k] for k in ('status', 'latency_ms')},
        'response': second['body'],
        'attendance_records': obs2['attendance_records_total'],
        'notification_rows': obs2['notification_rows'],
        'outbox_status_counts': obs2['outbox_status_counts'],
        'outbox_distinct_dedup_keys': obs2['outbox_distinct_dedup_keys'],
        'unchanged_reported_by_api': second['body'].get('unchanged')
        if isinstance(second['body'], dict) else None,
        'notified_reported_by_api': second['body'].get('notified')
        if isinstance(second['body'], dict) else None,
        'replay_created_nothing_new': replay_clean,
    })
    if not replay_clean:
        raise SmokeFailed('replay created new rows — stopping')

    # ── H: start the worker and drain ──────────────────────────────────────
    wc.start(cfg, sec, netns_proof_ok=True, batch_size=20, poll_seconds=1.0)
    drain_t0 = time.time()
    backlog_seen, final_sample = [], None
    for _ in range(60):
        time.sleep(1.0)
        final_sample = sampler.sample(window_s=th['outbox_no_drain_window_s'])
        if not final_sample.get('collector_ok'):
            raise SmokeFailed(f'collector failed during drain: '
                              f'{final_sample.get("collector_error")}')
        backlog_seen.append(final_sample['outbox_backlog'])
        if final_sample['outbox_backlog'] == 0 and final_sample['outbox_sent']:
            break
    drain_s = round(time.time() - drain_t0, 2)
    obs3 = outbox_snapshot(cfg, sec, school_ids, session_ids, test_date)
    ledger3 = ledger1
    v3 = reconcile(ledger_expected(ledger3, tpp), obs3, drain_complete=True)
    breaches_drained = guard_rules.outbox_breaches(
        final_sample, th, draining=True, sustained=None)
    wstat = wc.status(cfg)
    worker_log = tail_log(os.path.join(ROOT, 'logs', 'worker.log'))
    part('H_drain', {
        'drain_seconds': drain_s,
        'backlog_series': backlog_seen,
        'outbox_status_counts': obs3['outbox_status_counts'],
        'fake_firebase': {k: obs3[k] for k in
                          ('fake_attempts', 'fake_successes', 'fake_failures',
                           'fake_distinct', 'fake_duplicate_sends')},
        'worker_running': wstat.get('running'),
        'worker_pid': wstat.get('pid'),
        'reconciliation': v3,
        'collector_sample': final_sample,
        'guard_reasons': breaches_drained,
        'worker_log_tail': worker_log,
        'worker_started_no_background_services': background_service_check(
            worker_log),
    })
    if not v3['correct']:
        raise SmokeFailed(f'reconciliation FAILED after drain: '
                          f'{v3["violations"]}')
    if breaches_drained:
        raise SmokeFailed(f'guards fired after a clean drain: '
                          f'{breaches_drained}')

    part('I_collector', {
        'backlog_with_worker_stopped': sample_stopped['outbox_backlog'],
        'backlog_after_drain': final_sample['outbox_backlog'],
        'hard_ceiling': th['outbox_backlog_ceiling'],
        'ceiling_not_tripped':
            sample_stopped['outbox_backlog'] < th['outbox_backlog_ceiling'],
        'guards_agree_stopped': breaches_stopped == [],
        'guards_agree_drained': breaches_drained == [],
        'unplanned_worker_restarts':
            final_sample.get('unplanned_worker_restarts'),
    })

    REPORT['VERDICT'] = 'PASS'
    say('\nINSTITUTE OUTBOX SMOKE TEST PASS')


def tail_log(path, n=40):
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            return [l.rstrip() for l in fh.readlines()[-n:]]
    except OSError:
        return []


def port_bound(port: int) -> bool:
    try:
        s = socket.create_connection(('127.0.0.1', port), timeout=1)
        s.close()
        return True
    except Exception:
        return False


def background_service_check(lines) -> dict:
    """True == the service did NOT start.

    Matches START markers only. An earlier version searched for the service
    NAME, which flagged the startup banner's own
    "AIFACE_WS_ENABLED=false  HIKVISION_AUTO_SYNC=false" — configuration
    echoes that prove the opposite of what they were being read as. The
    authoritative evidence is the lifecycle line, reproduced verbatim below,
    plus a live check that nothing is listening on the WS port.
    """
    blob = '\n'.join(lines).lower()
    markers = {
        'auto_attendance_scheduler': 'auto-attendance scheduler started',
        'ai_face_ws': 'ai face ws server',
        'fee_reminder': 'fee-reminder scheduler started',
        'hikvision': 'auto-sync thread started',
        'durable_push_consumer': 'durable queue consumer started',
    }
    out = {name: marker not in blob for name, marker in markers.items()}
    out['lifecycle_line'] = next(
        (l for l in lines if 'lifecycle role=' in l), None)
    line = out['lifecycle_line'] or ''
    out['background_services_no'] = ('background_services=no' in line
                                     and 'start_background=False' in line)
    out['nothing_listening_on_ws_port'] = not port_bound(18188)
    out['nothing_listening_on_7788'] = not port_bound(7788)
    return out


if __name__ == '__main__':
    code = 0
    try:
        main()
    except SmokeFailed as exc:
        REPORT['VERDICT'] = 'FAIL'
        REPORT['errors'].append(str(exc))
        say(f'\nSMOKE TEST FAILED: {exc}')
        code = 1
    except Exception:
        REPORT['VERDICT'] = 'ERROR'
        REPORT['errors'].append(traceback.format_exc())
        say('\nSMOKE TEST ERROR:\n' + traceback.format_exc())
        code = 2
    finally:
        try:
            cfg = common.load_config(ROOT)
            wc.stop(cfg)
            target_mod.stop(cfg)
        except Exception as exc:
            REPORT['errors'].append(f'teardown: {exc}')
        REPORT['finished_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
        save()
        say(f'\nreport written to {os.path.join(OUT, "report.json")}')
    sys.exit(code)
