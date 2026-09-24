"""Focused pre-round checks against the running isolated target.

Uses ONLY the two precheck schools (90, 91) — never the 10,000 load students —
so the timed round still starts with fresh students. All check-ins land on the
round's test date for the precheck students only.

Checks
  C1 synthetic device registers (reg ack + DB heartbeat)
  C2 fresh check-in creates exactly the expected attendance row
  C3 the linked parent retrieves it (pre-issued token AND a real /auth/login token)
  C4 replaying the exact event does not create/alter attendance or notify again
  C5 another school's parent gets 404 for this child
  C6 the same enrollid on another school's device is attributed to that school's student
  C7 no external push (devlog backend, no FCM 'sent' rows, no device tokens) and no
     unexpected server→device commands
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from aiface_client import AiFaceDevice  # noqa: E402

import psycopg2  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402  (tzdata installed in venv-gen)


def http(method, url, token=None, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--test-date', default=None)
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    cfg = common.load_config(root)
    sec = common.load_secrets(root)
    fx = json.load(open(os.path.join(root, 'run', 'fixtures.json'), encoding='utf-8'))
    tokens = json.load(open(os.path.join(root, 'secrets', 'tokens.json')))['tokens']
    test_date = a.test_date or dt.datetime.now(ZoneInfo(cfg["school_timezone"])).date().isoformat()
    base = f"http://127.0.0.1:{cfg['http_port']}/api/mobile/v1"
    ws_url = f"ws://127.0.0.1:{cfg['ws_port']}/"
    p90, p91 = fx['precheck']['90'], fx['precheck']['91']
    conn = psycopg2.connect(**common.pg_dsn(cfg, sec))
    conn.autocommit = True
    cur = conn.cursor()
    results = {'test_date': test_date, 'checks': {}, 'started_at': dt.datetime.now(dt.timezone.utc).isoformat()}

    def check(name, ok, **evidence):
        results['checks'][name] = {'pass': bool(ok), **evidence}
        print(('PASS ' if ok else 'FAIL ') + name, json.dumps(evidence, ensure_ascii=False, default=str))

    def att_rows(student_id):
        cur.execute('SELECT id, student_id, school_id, academic_year_id, date, status, check_in, check_out, '
                    'source, notes FROM student_attendance WHERE student_id=%s AND date=%s',
                    (student_id, test_date))
        return cur.fetchall()

    def push_count(user_id):
        cur.execute("SELECT count(*), count(*) FILTER (WHERE status='sent') FROM push_notifications "
                    "WHERE user_id=%s AND ntype='attendance'", (user_id,))
        return cur.fetchone()

    # C1 ─────────────────────────────────────────────────────────────────────
    cur.execute('SELECT last_sync_at FROM attendance_devices WHERE device_sn=%s', (p90['device_sn'],))
    before_sync = cur.fetchone()[0]
    dev90 = AiFaceDevice(ws_url, p90['device_sn'])
    reg_s = dev90.connect()
    time.sleep(1.5)
    cur.execute('SELECT last_sync_at FROM attendance_devices WHERE device_sn=%s', (p90['device_sn'],))
    after_sync = cur.fetchone()[0]
    check('C1_device_registers', after_sync is not None and after_sync != before_sync,
          reg_ack_ms=round(reg_s * 1000, 1), heartbeat_updated=after_sync != before_sync,
          server_polls_answered=dev90.stats['server_polls'])

    # C2 ─────────────────────────────────────────────────────────────────────
    if att_rows(p90['student_db_id']):
        raise SystemExit('precheck student already has attendance on the test date — use a fresh date')
    rec_time = '07:10:00'
    tag = f'AI Face {test_date} {rec_time}'
    push_before = push_count(p90['parent_user_id'])
    slot = dev90.begin_sendlog([AiFaceDevice.record(p90['enrollid'], test_date, rec_time)])
    ack = dev90.wait_ack(slot, 30)
    rows = []
    for _ in range(20):
        rows = att_rows(p90['student_db_id'])
        if rows:
            break
        time.sleep(0.25)
    ok2 = (len(rows) == 1 and rows[0][2] == p90['school_id'] and rows[0][3] == p90['year_id']
           and rows[0][5] == 'present' and rows[0][6] == dt.time(7, 10) and rows[0][7] is None
           and rows[0][8] == 'aiface' and rows[0][9] == tag)
    push_after = push_count(p90['parent_user_id'])
    check('C2_fresh_checkin_row', ok2 and push_after[0] == push_before[0] + 1,
          ack_result=ack.get('result'), rows=len(rows),
          row=None if not rows else {'school_ok': rows[0][2] == p90['school_id'],
                                     'year_ok': rows[0][3] == p90['year_id'], 'status': rows[0][5],
                                     'check_in': str(rows[0][6]), 'source': rows[0][8],
                                     'notes_ok': rows[0][9] == tag},
          push_rows_added=push_after[0] - push_before[0])

    # C3 ─────────────────────────────────────────────────────────────────────
    st, body = http('GET', f"{base}/parent/children/{p90['student_db_id']}/attendance", tokens[p90['username']])
    js = json.loads(body) if st == 200 else {}
    today = [r for r in js.get('records', []) if r['date'] == test_date]
    st_l, body_l = http('POST', f'{base}/auth/login', body={'username': p90['username'],
                                                            'password': sec['parent_password']})
    login_js = json.loads(body_l) if st_l == 200 else {}
    st2, body2 = http('GET', f"{base}/parent/children/{p90['student_db_id']}/attendance",
                      login_js.get('access_token'))
    js2 = json.loads(body2) if st2 == 200 else {}
    same_server_date = today != [] or dt.date.today().isoformat() != test_date
    check('C3_parent_reads_own_child',
          st == 200 and js.get('student_id') == p90['student_db_id'] and len(today) == 1
          and today[0]['check_in'] == '07:10' and st_l == 200 and st2 == 200
          and js2.get('records') == js.get('records'),
          status=st, today_records=len(today), real_login_status=st_l,
          real_login_children=[c['id'] for c in login_js.get('children') or []] == [p90['student_db_id']],
          server_date_matches_test_date=same_server_date)

    # C4 ─────────────────────────────────────────────────────────────────────
    slot = dev90.begin_sendlog([AiFaceDevice.record(p90['enrollid'], test_date, rec_time)])
    dev90.wait_ack(slot, 30)
    time.sleep(1.0)
    rows_r = att_rows(p90['student_db_id'])
    push_replay = push_count(p90['parent_user_id'])
    check('C4_replay_is_duplicate', len(rows_r) == 1 and rows_r[0] == rows[0] and push_replay == push_after,
          rows=len(rows_r), row_unchanged=rows_r[:1] == rows[:1], push_rows_added=push_replay[0] - push_after[0])

    # C5 ─────────────────────────────────────────────────────────────────────
    st5, body5 = http('GET', f"{base}/parent/children/{p90['student_db_id']}/attendance", tokens[p91['username']])
    st5b, body5b = http('GET', f"{base}/parent/children/{p91['student_db_id']}/attendance", tokens[p91['username']])
    leak = p90['student_code'].encode() in body5 or b'07:10' in body5
    check('C5_cross_school_parent_denied', st5 == 404 and not leak and st5b == 200,
          cross_status=st5, own_status=st5b, body_contains_child_data=leak)

    # C6 ─────────────────────────────────────────────────────────────────────
    dev91 = AiFaceDevice(ws_url, p91['device_sn'])
    dev91.connect()
    assert p91['enrollid'] == p90['enrollid']
    slot = dev91.begin_sendlog([AiFaceDevice.record(p91['enrollid'], test_date, '07:20:00')])
    dev91.wait_ack(slot, 30)
    rows91 = []
    for _ in range(20):
        rows91 = att_rows(p91['student_db_id'])
        if rows91:
            break
        time.sleep(0.25)
    rows90_again = att_rows(p90['student_db_id'])
    check('C6_same_enrollid_correct_school',
          len(rows91) == 1 and rows91[0][2] == p91['school_id'] and rows91[0][6] == dt.time(7, 20)
          and rows90_again == rows,
          enrollid=p91['enrollid'], school91_row=len(rows91), school91_row_school_ok=bool(rows91) and rows91[0][2] == p91['school_id'],
          school90_row_unchanged=rows90_again == rows)

    # C7 ─────────────────────────────────────────────────────────────────────
    iso = json.load(open(os.path.join(root, 'results', 'isolation_check.json')))
    cur.execute("SELECT count(*) FROM push_notifications WHERE status='sent'")
    sent_rows = cur.fetchone()[0]
    cur.execute('SELECT count(*) FROM mobile_device_tokens')
    tok_rows = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM users WHERE device_token IS NOT NULL AND device_token <> ''")
    legacy_tok = cur.fetchone()[0]
    unexpected = dev90.unexpected_commands + dev91.unexpected_commands
    check('C7_no_external_notification_or_device_command',
          iso['ISOLATION_OK'] and sent_rows == 0 and tok_rows == 0 and legacy_tok == 0 and not unexpected,
          fcm_service_enabled=iso['fcm_service_enabled'], backend=iso['notification_backend'],
          firebase_import=iso['firebase_admin_import'][:7], push_rows_status_sent=sent_rows,
          mobile_device_tokens=tok_rows, legacy_device_tokens=legacy_tok,
          unexpected_server_commands=unexpected,
          server_polls_seen=dev90.stats['server_polls'] + dev91.stats['server_polls'])

    dev90.close()
    dev91.close()
    results['all_pass'] = all(c['pass'] for c in results['checks'].values())
    results['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    json.dump(results, open(os.path.join(root, 'results', 'precheck.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=2, default=str)
    print('ALL PASS' if results['all_pass'] else 'PRECHECK FAILED')
    raise SystemExit(0 if results['all_pass'] else 1)


if __name__ == '__main__':
    main()
