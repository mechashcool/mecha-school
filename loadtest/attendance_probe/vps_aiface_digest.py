"""Compact, copy-back digest of one VPS AI Face outbox round.

    python3 vps_aiface_digest.py --root /srv/attlt/attlt-aifx-20260925

Standard library only. Reads the round's evidence files and prints ONE JSON
block organised by the final report's sections. It never prints a secret, a
token, a DSN or a production host name/address: only counts, verdicts,
latencies, hashes, labels and timings. The full evidence stays on the VPS.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import json
import os


def load(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def rows(path):
    try:
        with open(path, newline='', encoding='utf-8') as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ts(v):
    try:
        return dt.datetime.fromisoformat(str(v).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def longest_run(samples, key, pred):
    """Longest continuous span (s) where pred(value) holds, from wall_utc samples."""
    best, cur_start = 0.0, None
    for r in samples:
        t, v = ts(r.get('wall_utc')), num(r.get(key))
        if t is None or v is None:
            continue
        if pred(v):
            cur_start = t if cur_start is None else cur_start
            best = max(best, t - cur_start)
        else:
            cur_start = None
    return round(best, 1)


def mx(samples, key):
    v = [num(r.get(key)) for r in samples if num(r.get(key)) is not None]
    return max(v) if v else None


def mn(samples, key):
    v = [num(r.get(key)) for r in samples if num(r.get(key)) is not None]
    return min(v) if v else None


def pick(d, *keys):
    return {k: (d or {}).get(k) for k in keys}


def stage(s, name):
    st = (s.get('stages') or {}).get(name) or {}
    drv = st.get('driver') or {}
    rec = st.get('reconciliation_cumulative') or {}
    return {
        'requested_rate_per_s': st.get('requested_rate_per_s'),
        'achieved_rate_send_window': drv.get('achieved_transitions_per_s_send_window'),
        'achieved_rate_until_last_ack': drv.get('achieved_transitions_per_s_until_last_ack'),
        'events': pick(drv, 'intended', 'sent', 'acked_ok', 'ack_failures', 'not_sent_due_to_stop',
                       'errors', 'devices_used', 'schools_used', 'max_concurrent_device_sessions'),
        'ack_latency_ms': drv.get('ack_latency'),
        'ack_latency_while_backlog_growing_ms': st.get('ack_latency_while_backlog_growing'),
        'schedule_lateness_ms': drv.get('schedule_lateness_ms'),
        'outbox_jobs_created': st.get('outbox_jobs_created'),
        'peak_backlog': st.get('peak_backlog_fine_0_5s'),
        'worker_jobs_per_s': {k: st.get(k) for k in (
            'worker_jobs_per_s_during_input', 'worker_jobs_per_s_after_input_stopped',
            'worker_jobs_per_s_first_job_to_last_completion')},
        'first_job_to_zero_s': st.get('first_job_to_zero_s'),
        'peak_to_zero_s': st.get('peak_to_zero_s'),
        'input_end_to_zero_s': st.get('input_end_to_zero_s'),
        'drained': st.get('drained'),
        'deliveries_after_their_ack': st.get('deliveries_completed_after_their_ack'),
        'deliveries_before_their_ack_race': st.get('deliveries_completed_before_their_ack_race'),
        'resources': pick(st.get('resources_watchdog'), 'host_cpu_pct_max', 'host_mem_available_pct_min',
                          'target_cpu_pct_max', 'target_rss_mb_max', 'db_connections_max_incl_harness',
                          'db_active_max', 'db_lock_waits_max', 'log_pool_timeout_max',
                          'log_db_operational_error_max', 'log_traceback_max'),
        'worker_process': pick(st.get('worker_process'), 'worker_cpu_pct_max', 'worker_rss_mb_max',
                               'worker_pids_seen'),
        'db_breakdown': st.get('db_breakdown'),
        'worker_restarted': st.get('worker_restarted'),
        'violations': rec.get('violations'),
        'PASS': st.get('PASS'),
        'STOPPED': st.get('STOPPED'),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    out_dir = os.path.join(root, 'results', 'aiface_load')
    cfg = load(os.path.join(root, 'experiment.json')) or {}
    idc = load(os.path.join(root, 'run', 'environment_identity.json')) or {}
    prep = load(os.path.join(root, 'run', 'prepared.json')) or {}
    backup = load(os.path.join(root, 'run', 'backup.json')) or {}
    s = load(os.path.join(out_dir, 'summary.json')) or {}
    sent = load(os.path.join(out_dir, 'sentinel_summary.json')) or {}
    wds = load(os.path.join(out_dir, 'watchdog_summary.json')) or {}
    wdc = load(os.path.join(out_dir, 'watchdog_config.json')) or {}
    runner = load(os.path.join(root, 'results', 'runner_status.json')) or {}
    ppre = load(os.path.join(root, 'results', 'prod_pre.json')) or {}
    ppost = load(os.path.join(root, 'results', 'prod_post.json')) or {}
    ftargets = load(os.path.join(root, 'results', 'forbidden_targets_summary.json')) or {}
    mon = rows(os.path.join(out_dir, 'monitor.csv'))
    smon = rows(os.path.join(out_dir, 'sentinel_monitor.csv'))
    iso = {}
    for p in sorted(glob.glob(os.path.join(root, 'results', 'isolation_*side_*.json'))):
        d = load(p) or {}
        iso[os.path.basename(p)[10:-5]] = {
            'PASS': d.get('PASS'), 'failures': d.get('failures'), 'interfaces': d.get('interfaces'),
            'probes': len(d.get('reachability_probes') or []),
            'probes_connected': sum(1 for x in d.get('reachability_probes') or [] if x.get('connected')),
            'targets_file_probes': d.get('targets_file_probes'),
            'production_names_checked': d.get('production_names_checked'),
            'dns_resolution': d.get('dns_resolution'),
            'host_test_port_listeners': d.get('host_test_port_listeners'),
            'production_health_status': d.get('production_health_status'),
            'separate_namespace': d.get('separate_namespace', d.get('namespace_is_separate'))}
    pf = s.get('preflight') or {}
    fin = s.get('final_reconciliation') or {}
    th = (wdc.get('thresholds') or {})
    health_ms = [num(r.get('production_health_ms')) for r in smon if num(r.get('production_health_ms'))]
    health_bad = [r.get('production_health_status') for r in smon
                  if r.get('production_health_status') not in ('200', None, '')]

    digest = {
        'A_identity': {
            'tested_commit': idc.get('app_source_commit'), 'expected_commit': (s.get('startup_gate') or {}).get('expected_commit'),
            'experiment_id': cfg.get('experiment_id'), 'run_id': cfg.get('run_id'), 'run_root': root,
            'preserved_root': cfg.get('preserved_root'),
            'db': f"{cfg.get('pg_host')}:{cfg.get('pg_port')}/{cfg.get('db_name')}",
            'preserved_dataset_reused': bool(prep.get('dataset')) and not prep.get('dataset', {}).get('problems'),
            'dataset': pick(prep.get('dataset'), 'load_schools', 'students_per_load_school',
                            'devices_per_load_school', 'active_mappings_per_load_school',
                            'fixture_records_verified', 'students_total', 'parent_links_total',
                            'attendance_rows_total', 'attendance_date_range', 'test_dates',
                            'attendance_rows_on_test_dates')},
        'B_database': {
            'identity': prep.get('identity'),
            'backup': pick(backup, 'verified', 'method', 'files', 'bytes', 'system_identifier', 'at_utc'),
            'migrations': pick(prep.get('migrations'), 'before', 'head', 'pending', 'applied', 'after',
                               'unexpected', 'tables_added', 'tables_removed',
                               'population_counts_unchanged', 'seconds'),
            'fake_tokens': prep.get('tokens'),
            'package_versions': {
                'production': load(os.path.join(root, 'results', 'prod_constraints_summary.json')),
                'test_venv_sqlalchemy': next((ln.split('==', 1)[1].strip() for ln in open(
                    os.path.join(root, 'results', 'venv_target_freeze.txt'), encoding='utf-8')
                    if ln.lower().startswith('sqlalchemy==')), None)
                if os.path.exists(os.path.join(root, 'results', 'venv_target_freeze.txt')) else None},
            'prepare_PASS': prep.get('PASS'), 'prepare_refused': prep.get('refused') or prep.get('error')},
        'C_isolation': {
            'proofs': iso, 'forbidden_targets': ftargets,
            'driver_network_proof': pf.get('network_proof'),
            'startup_gate': pick(s.get('startup_gate'), 'ok', 'failed', 'db_classification',
                                 'app_effective_db_host', 'app_effective_db_name',
                                 'production_targets_probed', 'production_targets_connected',
                                 'fake_firebase_active_in_worker', 'real_firebase_blocked_in_target',
                                 'app_fcm_service_enabled_in_target', 'port_7788_untouched',
                                 'alternate_ws_port_active', 'AIFACE_ATTENDANCE_OUTBOX_ENABLED_target',
                                 'AIFACE_ATTENDANCE_OUTBOX_ENABLED_worker', 'no_real_credentials_exposed',
                                 'unrelated_schedulers_disabled', 'watchdog_active', 'worker_active'),
            'ws': pf.get('ws'),
            'socket_audit_violations': len((s.get('socket_audit') or {}).get('violations') or []),
            'gates_checks': pick((pf.get('gates') or {}).get('checks'),
                                 'target_INSTITUTE_ATTENDANCE_OUTBOX_ENABLED', 'worker_role',
                                 'worker_batch_poll_lease_attempts', 'target_firebase_admin_import',
                                 'credential_files_mounted', 'runner_process_has_db_url')},
        'D_sanity': {k: (s.get('sanity') or {}).get(k) for k in
                     ('case1_same_school', 'case2_cross_school', 'drained', 'fixture_mapping_removed',
                      'PASS')},
        'E_stage_A': stage(s, 'A'),
        'F_idle_baseline': pick(s.get('idle_baseline'), 'duration_s', 'covered_s', 'monitor_samples',
                                'max_gap_between_samples_s', 'max_backlog', 'collector_failures',
                                'rows_violating', 'worker_pid_unchanged', 'total_jobs_unchanged',
                                'watchdog_stop_present', 'closing_job_states', 'PASS'),
        'G_stage_B': stage(s, 'B'),
        'H_reconciliation_final': {
            'attendance': fin.get('attendance'), 'outbox': fin.get('outbox'),
            'fake_firebase': fin.get('fake_firebase'),
            'push_notification_log': fin.get('push_notification_log'),
            'in_app_notification_rows_created': fin.get('in_app_notifications_rows'),
            'history_outside_test_dates': fin.get('history_outside_test_dates'),
            'violations': fin.get('violations'),
            'ack_commit_proof': s.get('ack_commit_proof'),
            'device_totals': {k: sum((d or {}).get(k, 0) for d in (s.get('device_stats') or {}).values())
                              for k in ('connects', 'reconnects', 'disconnects', 'unexpected_commands',
                                        'late_acks', 'unmatched_acks')},
            'logs': s.get('logs')},
        'I_safety': {
            'thresholds': pick(th, 'host_cpu_pct', 'host_cpu_sustain_s', 'mem_available_floor_pct',
                               'mem_sustain_s', 'db_connections_frac_of_max', 'error_rate',
                               'db_deadlocks_allowed', 'db_lock_wait_sustain_s', 'compliance_mode'),
            'host_cpu_pct_max_watchdog': mx(mon, 'host_cpu_pct'),
            'host_cpu_pct_max_sentinel': mx(smon, 'host_cpu_pct'),
            'longest_cpu_ge_80_s_watchdog': longest_run(mon, 'host_cpu_pct', lambda v: v >= 80),
            'longest_cpu_ge_80_s_sentinel': longest_run(smon, 'host_cpu_pct', lambda v: v >= 80),
            'mem_available_pct_min': min(x for x in (mn(mon, 'host_mem_available_pct'),
                                                     mn(smon, 'host_mem_available_pct'), 101) if x is not None),
            'swap_used_gb_max': mx(mon, 'host_swap_used_gb'),
            'target_cpu_pct_max': mx(mon, 'target_cpu_pct'), 'target_rss_mb_max': mx(mon, 'target_rss_mb'),
            'pg_cpu_pct_max': mx(mon, 'pg_cpu_pct'), 'pg_rss_mb_max': mx(mon, 'pg_rss_mb'),
            'worker_whole_round': s.get('worker_process_whole_round'),
            'db_connections_max': mx(mon, 'db_connections'), 'db_max_connections': mx(mon, 'db_max_connections'),
            'db_active_max': mx(mon, 'db_active'), 'db_lock_waits_max': mx(mon, 'db_lock_waits'),
            'db_deadlocks_since_start_max': mx(mon, 'db_deadlocks_since_start'),
            'disk_free_gb_min': mn(mon, 'disk_free_gb'), 'disk_free_gb_first': num((mon[0] if mon else {}).get('disk_free_gb')),
            'watchdog_fired': wds.get('fired'), 'watchdog_summary_written': bool(wds),
            'watchdog_stop_seen_by_driver': s.get('watchdog_stop'),
            'sentinel': pick(sent, 'samples', 'stop_written', 'terminated', 'host_mem_available_pct_min',
                             'host_cpu_pct_max', 'disk_free_gb_min', 'production_health_non_200',
                             'production_health_ms_max', 'thresholds'),
            'production_health_during': {'samples': len(smon), 'non_200': len(health_bad),
                                         'ms_max': max(health_ms) if health_ms else None,
                                         'ms_median': sorted(health_ms)[len(health_ms) // 2] if health_ms else None}},
        'J_production': {
            'pre_units': {u: pick(v, 'ActiveState', 'MainPID', 'NRestarts') for u, v in (ppre.get('units') or {}).items()},
            'production_unchanged': ppost.get('production_unchanged'),
            'differences': ppost.get('differences'),
            'port_7788_pre': ppre.get('port_7788'), 'port_7788_post': ppost.get('port_7788'),
            'prod_head_same': ppre.get('prod_head') == ppost.get('prod_head') if ppost else None,
            'prod_env_sha_same': ppre.get('prod_env_sha') == ppost.get('prod_env_sha') if ppost else None,
            'health_pre': ppre.get('health'), 'health_post': ppost.get('health')},
        'K_runner': runner,
        'L_verdict_driver': s.get('VERDICT'), 'errors': (s.get('errors') or [])[-3:],
    }
    # One section per line: easy to select and copy from a web console.
    print('##### AIFACE VPS DIGEST BEGIN #####')
    for key, value in digest.items():
        print(key + ' ' + json.dumps(value, separators=(',', ':'), default=str))
    print('##### AIFACE VPS DIGEST END #####')


if __name__ == '__main__':
    main()
