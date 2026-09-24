"""Reset synthetic round data so another round can start with fresh check-ins.

Deletes, in the experiment-owned database only (ownership marker verified):
  * student_attendance rows ON THE GIVEN DATE for the 10 load schools
  * push_notifications rows with ntype='attendance' for load-school parents
    created at/after the given round start
History rows (dates before the test date), precheck schools, and every other
table are untouched. Dry run unless --execute. Each reset is appended to
<root>/results/resets.jsonl.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

import psycopg2  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--round-out', required=True, help='results/<round> whose data should be removed')
    ap.add_argument('--execute', action='store_true')
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    cfg, sec = common.load_config(root), common.load_secrets(root)
    fx = json.load(open(os.path.join(root, 'run', 'fixtures.json'), encoding='utf-8'))
    start = json.load(open(os.path.join(os.path.abspath(a.round_out), 'round_start.json')))
    test_date, t0 = start['test_date'], start['start_wall_epoch']
    ids = [fx['schools'][str(s)]['id'] for s in range(cfg['num_schools'])]
    conn = psycopg2.connect(**common.pg_dsn(cfg, sec))
    cur = conn.cursor()
    cur.execute('SELECT experiment_id FROM attlt_owner')
    if [r[0] for r in cur.fetchall()] != [cfg['experiment_id']]:
        raise SystemExit('ownership marker mismatch — refusing')
    cur.execute('SELECT count(*) FROM student_attendance WHERE date=%s AND school_id = ANY(%s)', (test_date, ids))
    n_att = cur.fetchone()[0]
    cur.execute("""SELECT count(*) FROM push_notifications WHERE ntype='attendance' AND school_id = ANY(%s)
                   AND created_at >= to_timestamp(%s) AT TIME ZONE 'UTC'""", (ids, t0 - 60))
    n_push = cur.fetchone()[0]
    plan = {'round_out': a.round_out, 'test_date': test_date, 'attendance_rows': n_att, 'push_rows': n_push,
            'executed': a.execute, 'at_utc': dt.datetime.now(dt.timezone.utc).isoformat()}
    if a.execute:
        cur.execute('DELETE FROM student_attendance WHERE date=%s AND school_id = ANY(%s)', (test_date, ids))
        cur.execute("""DELETE FROM push_notifications WHERE ntype='attendance' AND school_id = ANY(%s)
                       AND created_at >= to_timestamp(%s) AT TIME ZONE 'UTC'""", (ids, t0 - 60))
        conn.commit()
        with open(os.path.join(root, 'results', 'resets.jsonl'), 'a') as fh:
            fh.write(json.dumps(plan) + '\n')
    print(json.dumps(plan, indent=2))


if __name__ == '__main__':
    main()
