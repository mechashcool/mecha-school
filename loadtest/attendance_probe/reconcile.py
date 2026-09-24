"""Reconcile the generator's event manifest with the test database.

For every student index k in the round's schedule:
  scheduled → sent → acknowledged → verified committed (DB row)
and verify, per committed row: correct student (by construction of the
mapping), correct school, correct academic year, date, check_in time, status,
source, dedup tag written exactly once, no check_out; plus exactly one
parent notification row with the recipient's school and the right student.
Unsent students must have NO row. Rows for students that were never sent are
reported as unexpected (wrong attribution). History rows must be untouched.
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import glob
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

import psycopg2  # noqa: E402


def pct(v, p):
    if not v:
        return None
    s = sorted(v)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    root, out = os.path.abspath(a.root), os.path.abspath(a.out)
    cfg, sec = common.load_config(root), common.load_secrets(root)
    fx = json.load(open(os.path.join(root, 'run', 'fixtures.json'), encoding='utf-8'))
    start = json.load(open(os.path.join(out, 'round_start.json')))
    test_date = start['test_date']
    stage_limit = start['stage_limit']
    max_k = common.STAGES[stage_limit - 1][3]
    t0 = start['start_wall_epoch']

    events = {}
    for f in sorted(glob.glob(os.path.join(out, 'round_end_p*.json'))):
        for e in json.load(open(f))['events_final']:
            events[e['k']] = e
    if not events:   # fall back to the incremental log (generator killed before finish)
        for f in sorted(glob.glob(os.path.join(out, 'events_p*.jsonl'))):
            for line in open(f):
                e = json.loads(line)
                events[e['k']] = {**events.get(e['k'], {}), **e}

    load_school_ids = [fx['schools'][str(s)]['id'] for s in range(cfg['num_schools'])]
    conn = psycopg2.connect(**common.pg_dsn(cfg, sec))
    cur = conn.cursor()
    cur.execute("""SELECT id, student_id, school_id, academic_year_id, status, check_in, check_out, source, notes,
                          created_at FROM student_attendance WHERE date=%s AND school_id = ANY(%s)""",
                (test_date, load_school_ids))
    rows_by_student = collections.defaultdict(list)
    for r in cur.fetchall():
        rows_by_student[r[1]].append(r)
    # rows for load students recorded under a school other than their own
    cur.execute("""SELECT count(*) FROM student_attendance sa JOIN students s ON s.id=sa.student_id
                   WHERE sa.date=%s AND s.school_id = ANY(%s) AND sa.school_id <> s.school_id""",
                (test_date, load_school_ids))
    cross_school_rows = cur.fetchone()[0]
    cur.execute("""SELECT user_id, school_id, status, data_json, created_at FROM push_notifications
                   WHERE ntype='attendance' AND school_id = ANY(%s) AND created_at >= to_timestamp(%s) AT TIME ZONE 'UTC'""",
                (load_school_ids, t0 - 5))
    push_by_user = collections.defaultdict(list)
    for r in cur.fetchall():
        push_by_user[r[0]].append(r)
    cur.execute('SELECT count(*) FROM student_attendance WHERE date < %s AND school_id = ANY(%s)',
                (test_date, load_school_ids))
    history_rows_now = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM push_notifications WHERE status='sent'")
    push_sent_rows = cur.fetchone()[0]

    student_to_k = {s['student_db_id']: k for k, s in enumerate(fx['students']) if s}
    counts = collections.Counter()
    mismatches = []
    commit_lat, notif_delay, send_to_commit = [], [], []
    per_stage = collections.defaultdict(collections.Counter)

    for k in range(max_k):
        fxs = fx['students'][k]
        stage = common.stage_for_count(k)[0][0]
        e = events.get(k, {'status': 'missing_from_manifest'})
        st = e['status']
        per_stage[stage]['scheduled'] += 1
        counts['scheduled'] += 1
        sent = e.get('sent_rel') is not None
        acked = st == 'acked'
        counts[f'final_status.{st}'] += 1
        if sent:
            counts['sent'] += 1
            per_stage[stage]['sent'] += 1
        if acked:
            counts['acked'] += 1
            per_stage[stage]['acked'] += 1
        if e.get('retries'):
            counts['resent_after_disconnect'] += 1
        rows = rows_by_student.get(fxs['student_db_id'], [])
        if len(rows) > 1:
            mismatches.append({'k': k, 'problem': 'multiple_rows'})
        if not rows:
            if sent:
                counts['sent_not_committed'] += 1
                per_stage[stage]['sent_not_committed'] += 1
                if acked:
                    counts['acked_not_committed'] += 1
                    mismatches.append({'k': k, 'problem': 'acked_but_no_row'})
            continue
        r = rows[0]
        if not sent:
            counts['unexpected_row_unsent'] += 1
            mismatches.append({'k': k, 'problem': 'row_for_unsent_event'})
            continue
        exp_t = common.device_time_for(k)
        problems = []
        if r[2] != fxs['school_id']:
            problems.append('school')
        if r[3] != fxs['year_id']:
            problems.append('academic_year')
        if r[5] != exp_t:
            problems.append('check_in')
        if r[4] != common.expected_checkin_status(cfg, exp_t):
            problems.append('status')
        if r[6] is not None:
            problems.append('check_out')
        if r[7] != 'aiface':
            problems.append('source')
        tag = f"AI Face {test_date} {exp_t.strftime('%H:%M:%S')}"
        if (r[8] or '') != tag:
            problems.append('notes_tag')
        pushes = push_by_user.get(fxs['parent_user_id'], [])
        if len(pushes) != 1:
            problems.append(f'push_rows={len(pushes)}')
        else:
            p = pushes[0]
            data = json.loads(p[3] or '{}')
            if p[1] != fxs['school_id'] or str(data.get('student_id')) != str(fxs['student_db_id']):
                problems.append('push_attribution')
            if p[2] == 'sent':
                problems.append('push_status_sent')
            notif_delay.append((p[4] - r[9]).total_seconds() * 1000)
        if problems:
            mismatches.append({'k': k, 'problem': ','.join(problems)})
            counts['committed_with_mismatch'] += 1
        else:
            counts['verified_committed'] += 1
            per_stage[stage]['verified_committed'] += 1
        if not acked:
            counts['committed_not_acked'] += 1
        created_rel = r[9].replace(tzinfo=dt.timezone.utc).timestamp() - t0
        per_stage[stage]['commit_rel_max'] = max(per_stage[stage].get('commit_rel_max', 0), round(created_rel, 3))
        send_to_commit.append((created_rel - e['sent_rel']) * 1000)
        if e.get('ack_ms') is not None:
            commit_lat.append(e['ack_ms'])

    # rows for students outside the scheduled range, or not load students at all
    for sid, rows in rows_by_student.items():
        k = student_to_k.get(sid)
        if k is None or k >= max_k:
            counts['unexpected_row_outside_schedule'] += len(rows)
            mismatches.append({'student_db_id_hash': hash(sid) % 10**6, 'problem': 'row_outside_schedule'})
    extra_push = sum(len(v) for u, v in push_by_user.items()
                     if u not in {fx['students'][k]['parent_user_id'] for k in range(max_k)})

    res = {
        'test_date': test_date, 'stage_limit': stage_limit, 'counts': dict(counts),
        'per_stage': {str(k): dict(v) for k, v in sorted(per_stage.items())},
        'cross_school_rows': cross_school_rows,
        'history_rows_expected': fx['history_rows'], 'history_rows_now': history_rows_now,
        'history_untouched': history_rows_now == fx['history_rows'],
        'push_rows_status_sent_anywhere': push_sent_rows,
        'push_rows_for_unscheduled_parents': extra_push,
        'mismatch_count': len(mismatches),
        'ack_ms': {'n': len(commit_lat), 'p50': pct(commit_lat, 50), 'p95': pct(commit_lat, 95), 'p99': pct(commit_lat, 99)},
        'send_to_commit_ms_approx': {'n': len(send_to_commit), 'p50': pct(send_to_commit, 50),
                                     'p95': pct(send_to_commit, 95),
                                     'note': 'created_at (utcnow at INSERT build) minus generator send time; same host clock'},
        'attendance_to_push_row_ms': {'n': len(notif_delay), 'p50': pct(notif_delay, 50), 'p95': pct(notif_delay, 95),
                                      'max': max(notif_delay) if notif_delay else None},
        'correct': (len(mismatches) == 0 and cross_school_rows == 0 and history_rows_now == fx['history_rows']
                    and push_sent_rows == 0 and extra_push == 0),
    }
    json.dump(res, open(os.path.join(out, 'reconciliation.json'), 'w'), indent=2, default=str)
    with open(os.path.join(out, 'reconciliation_mismatches.csv'), 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=sorted({k2 for m in mismatches for k2 in m} or {'k', 'problem'}))
        w.writeheader()
        w.writerows(mismatches)
    print(json.dumps({k2: res[k2] for k2 in ('counts', 'cross_school_rows', 'history_untouched', 'mismatch_count', 'correct')}, indent=2))


if __name__ == '__main__':
    main()
