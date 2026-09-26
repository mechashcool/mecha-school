"""Prepare the PRESERVED 10-school × 1,000-student VPS database for the AI Face
outbox round — verify first, change only what the round needs, record everything.

Runs INSIDE the private network namespace, as the unprivileged test account,
with the TARGET interpreter (app requirements + alembic):

    <root>/venv-target/bin/python vps_prepare_db.py --root <root> \
        --preserved-root /srv/attlt/attlt-20260917-7b7cb5-vps \
        --system-identifier <pg_controldata value> \
        --allowed-migrations a,b,c

Order — every step fails closed, and nothing is written before step 4:
  1  identity: the connected server IS the preserved experiment cluster
     (127.0.0.1:<port>, database name, data_directory, pg_control system
     identifier, ownership marker, database comment, track_commit_timestamp)
  2  dataset: the preserved population is structurally intact and every one of
     seed.py's 10,000 fixture records maps through its OWN school's device;
     the three test dates hold no attendance at all
  3  backup precondition: a verified cold copy of the cluster exists
  4  schema: ONLY the allow-listed pending Alembic revisions are applied,
     through the application's own `flask db upgrade`; nothing else runs.
     Row counts of the population tables must be identical afterwards
  5  fake tokens: 2 LT<tag>-TOK-* tokens per parent of the 600 load students,
     the same fan-out the validated round used. The preserved seed has none,
     and without them the outbox path under test would stage zero jobs
Writes <root>/run/prepared.json. No credential, token string or DSN is printed.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import institute_common as ic  # noqa: E402
import manifest  # noqa: E402
import target  # noqa: E402

import psycopg2  # noqa: E402

SELECTED_STUDENTS = 600
TOKENS_PER_PARENT = 2
PRECHECK_SCHOOLS = (90, 91)
# Must equal aiface_load.PRESERVED_TABLES: the driver re-counts these and
# refuses to start if anything changed between this script and the round.
PRESERVED_TABLES = ('schools', 'students', 'users', 'parent_students', 'attendance_devices',
                    'device_student_mappings', 'mobile_device_tokens', 'student_attendance',
                    'notifications', 'push_notifications', 'notification_outbox')
POPULATION_TABLES = ('schools', 'students', 'users', 'parent_students', 'attendance_devices',
                     'device_student_mappings', 'student_attendance', 'academic_years',
                     'grades', 'sections', 'notifications', 'push_notifications')

OUT = {'started_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'PASS': False}


class Refused(SystemExit):
    pass


def say(*a):
    print(*a, flush=True)


def save(root):
    path = os.path.join(root, 'run', 'prepared.json')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(OUT, fh, indent=2, default=str)
    os.replace(tmp, path)


def connect(cfg, sec):
    conn = psycopg2.connect(**dict(common.pg_dsn(cfg, sec), application_name='attlt-prepare'))
    conn.autocommit = True
    return conn


def one(cur, sql, args=None):
    cur.execute(sql, args)
    return cur.fetchone()[0]


def table_exists(cur, name):
    return one(cur, 'SELECT to_regclass(%s) IS NOT NULL', (f'public.{name}',))


def counts(cur, tables):
    return {t: (one(cur, f'SELECT count(*) FROM {t}') if table_exists(cur, t) else None)
            for t in tables}


def test_dates(cfg):
    import pytz
    today = dt.datetime.now(pytz.timezone(cfg['school_timezone'])).date()
    return [(today - dt.timedelta(days=n)).isoformat() for n in (2, 1, 0)]


# ── 1 identity ────────────────────────────────────────────────────────────────

def step_identity(cur, cfg, a):
    cur.execute("SELECT current_database(), host(inet_server_addr()), inet_server_port(), "
                "current_setting('data_directory'), current_setting('track_commit_timestamp'), "
                "current_setting('max_connections')::int, current_setting('server_version'), "
                "(SELECT system_identifier::text FROM pg_control_system()), "
                "shobj_description((SELECT oid FROM pg_database WHERE datname = current_database()), "
                "'pg_database')")
    db, addr, port, data_dir, tct, maxc, ver, sysid, comment = cur.fetchone()
    cur.execute('SELECT experiment_id FROM attlt_owner')
    owner = [r[0] for r in cur.fetchall()]
    want_dir = os.path.realpath(os.path.join(a.preserved_root, 'pgdata'))
    res = {'database': db, 'server_addr': addr, 'server_port': port,
           'data_directory_is_preserved_pgdata': os.path.realpath(data_dir) == want_dir,
           'system_identifier_matches_pg_controldata': sysid == a.system_identifier,
           'owner_marker': owner, 'database_comment_matches': comment == f"attlt:{cfg['experiment_id']}",
           'track_commit_timestamp': tct, 'max_connections': maxc, 'server_version': ver}
    OUT['identity'] = res
    say(json.dumps(res, indent=2))
    bad = []
    if db != cfg['db_name'] or db != 'core_school_attendance_load_test':
        bad.append('database name')
    if addr != '127.0.0.1' or port != cfg['pg_port']:
        bad.append('server address/port')
    if not res['data_directory_is_preserved_pgdata']:
        bad.append('data_directory is not the preserved pgdata')
    if not res['system_identifier_matches_pg_controldata']:
        bad.append('pg_control system identifier differs from the pre-start pg_controldata')
    if owner != [cfg['experiment_id']]:
        bad.append('ownership marker')
    if not res['database_comment_matches']:
        bad.append('database comment')
    if tct != 'on':
        bad.append('track_commit_timestamp is not on')
    if bad:
        raise Refused('identity REFUSED: ' + '; '.join(bad))


# ── 2 dataset ────────────────────────────────────────────────────────────────

def step_dataset(cur, cfg, fx):
    tag = common.tag(cfg)
    problems = []
    n_sch, per = cfg['num_schools'], cfg['students_per_school']
    if (n_sch, per, cfg['devices_per_school']) != (10, 1000, 2):
        problems.append(f'experiment.json shape is not 10×1000×2: {(n_sch, per, cfg["devices_per_school"])}')
    cur.execute('SELECT id, code FROM schools WHERE code LIKE %s', (f'LT{tag}S%',))
    by_code = {code: sid for sid, code in cur.fetchall()}
    for s in list(range(n_sch)) + list(PRECHECK_SCHOOLS):
        want = fx['schools'].get(str(s), {}).get('id')
        if by_code.get(common.school_code(cfg, s)) != want or want is None:
            problems.append(f'school {s}: code/id does not match fixtures')
    load_ids = [fx['schools'][str(s)]['id'] for s in range(n_sch)]
    cur.execute('SELECT school_id, count(*) FROM students WHERE school_id = ANY(%s) GROUP BY 1',
                (load_ids,))
    stu_per = dict(cur.fetchall())
    cur.execute('SELECT school_id, count(*) FROM attendance_devices WHERE school_id = ANY(%s) '
                'GROUP BY 1', (load_ids,))
    dev_per = dict(cur.fetchall())
    cur.execute('SELECT school_id, count(*) FROM device_student_mappings '
                'WHERE school_id = ANY(%s) AND is_active GROUP BY 1', (load_ids,))
    map_per = dict(cur.fetchall())
    for sid in load_ids:
        if stu_per.get(sid) != per:
            problems.append(f'school id {sid}: {stu_per.get(sid)} students, expected {per}')
        if dev_per.get(sid) != 2:
            problems.append(f'school id {sid}: {dev_per.get(sid)} devices, expected 2')
        if map_per.get(sid) != per:
            problems.append(f'school id {sid}: {map_per.get(sid)} active mappings, expected {per}')

    cur.execute('SELECT id, school_id FROM students')
    student_school = dict(cur.fetchall())
    cur.execute('SELECT id, school_id FROM users')
    user_school = dict(cur.fetchall())
    cur.execute('SELECT user_id, student_id FROM parent_students')
    parents_of = collections.defaultdict(set)
    for uid, sid in cur.fetchall():
        parents_of[sid].add(uid)
    cur.execute('SELECT device_sn, id, school_id FROM attendance_devices')
    devices = {sn: (i, s) for sn, i, s in cur.fetchall()}
    cur.execute('SELECT device_id, employee_no_string, student_id, school_id, is_active '
                'FROM device_student_mappings')
    maps = collections.defaultdict(list)
    for d, e, s, sc, act in cur.fetchall():
        maps[(d, e)].append((s, sc, act))

    recs = [r for r in fx['students'] if r is not None]
    bad_records = 0
    for k, rec in enumerate(fx['students']):
        if rec is None:
            continue
        lay = common.layout(cfg, k)
        dev = devices.get(rec['device_sn'])
        m = maps.get((dev[0], str(rec['enrollid']))) if dev else None
        ok = (student_school.get(rec['student_db_id']) == rec['school_id']
              and dev is not None and dev[1] == rec['school_id']
              and rec['device_sn'] == common.device_sn(cfg, lay['school_idx'], lay['device_idx'])
              and rec['enrollid'] == lay['enrollid']
              and m is not None and len(m) == 1 and m[0][0] == rec['student_db_id']
              and m[0][1] == rec['school_id'] and m[0][2]
              and parents_of.get(rec['student_db_id']) == {rec['parent_user_id']}
              and user_school.get(rec['parent_user_id']) == rec['school_id'])
        if not ok:
            bad_records += 1
    if len(recs) != n_sch * per:
        problems.append(f'fixtures.json has {len(recs)} student records, expected {n_sch * per}')
    if bad_records:
        problems.append(f'{bad_records} fixture record(s) do not map through their own school')

    cur.execute("SELECT v FROM attlt_seed WHERE k = 'done'")
    seed_done = cur.fetchone()
    history_rows = one(cur, "SELECT v FROM attlt_seed WHERE k = 'history_rows'")
    cur.execute('SELECT count(*), min(date), max(date) FROM student_attendance')
    att_n, att_min, att_max = cur.fetchone()
    cross = one(cur, 'SELECT count(*) FROM student_attendance sa JOIN students s '
                     'ON s.id = sa.student_id WHERE sa.school_id <> s.school_id')
    dates = test_dates(cfg)
    on_dates = one(cur, 'SELECT count(*) FROM student_attendance WHERE date = ANY(%s::date[])',
                   (dates,))
    if not seed_done:
        problems.append('attlt_seed has no done marker')
    if cross:
        problems.append(f'{cross} cross-school attendance rows already present')
    if on_dates:
        problems.append(f'{on_dates} attendance rows already exist on the test dates {dates}')

    ds = {'experiment_tag': tag, 'load_schools': n_sch, 'precheck_schools': len(PRECHECK_SCHOOLS),
          'students_per_load_school': sorted(set(stu_per.values())),
          'devices_per_load_school': sorted(set(dev_per.values())),
          'active_mappings_per_load_school': sorted(set(map_per.values())),
          'fixture_student_records': len(recs), 'fixture_records_verified': len(recs) - bad_records,
          'students_total': len(student_school), 'users_total': len(user_school),
          'parent_links_total': sum(len(v) for v in parents_of.values()),
          'attendance_rows_total': att_n, 'attendance_date_range': [att_min, att_max],
          'seed_history_rows_recorded': history_rows, 'seed_done_at': seed_done[0] if seed_done else None,
          'cross_school_attendance_rows': cross, 'test_dates': dates,
          'attendance_rows_on_test_dates': on_dates, 'problems': problems}
    OUT['dataset'] = ds
    OUT['test_dates'] = dates
    say(json.dumps(ds, indent=2, default=str))
    if problems:
        raise Refused('preserved dataset REFUSED: ' + '; '.join(problems))
    return student_school, user_school, parents_of


# ── 4 schema ─────────────────────────────────────────────────────────────────

def pending_revisions(app_src, current):
    from alembic.script import ScriptDirectory
    script = ScriptDirectory(os.path.join(app_src, 'migrations'))
    heads = script.get_heads()
    if len(heads) != 1:
        raise Refused(f'migration tree has {len(heads)} heads: {heads}')
    if not current:
        raise Refused('database has no alembic revision — refusing to migrate an unknown schema')
    revs = [r.revision for r in script.iterate_revisions(heads[0], current)]
    return heads[0], list(reversed(revs))


def step_schema(cur, cfg, sec, a, root):
    cur.execute('SELECT version_num FROM alembic_version')
    current = [r[0] for r in cur.fetchall()]
    if len(current) != 1:
        raise Refused(f'alembic_version holds {len(current)} rows: {current}')
    head, pending = pending_revisions(os.path.join(root, 'app_src'), current[0])
    allowed = [x for x in a.allowed_migrations.split(',') if x]
    unexpected = [r for r in pending if r not in allowed]
    mig = {'before': current, 'head': head, 'pending': pending, 'allowed': allowed,
           'unexpected': unexpected, 'applied': [], 'after': current}
    OUT['migrations'] = mig
    say(json.dumps(mig, indent=2))
    if unexpected:
        raise Refused(f'pending migrations outside the reviewed allow-list: {unexpected}')
    if not pending:
        return

    # ── 3 backup precondition (checked here, right before the first write) ──
    bpath = os.path.join(root, 'run', 'backup.json')
    try:
        backup = json.load(open(bpath, encoding='utf-8'))
    except (OSError, ValueError):
        backup = {}
    OUT['backup'] = backup
    if not (backup.get('verified') is True and os.path.realpath(backup.get('source', '')) ==
            os.path.realpath(os.path.join(a.preserved_root, 'pgdata'))):
        raise Refused('no verified cold backup of the preserved cluster — refusing to migrate')

    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    tables_before = sorted(r[0] for r in cur.fetchall())
    counts_before = counts(cur, POPULATION_TABLES)
    env = target.build_env(cfg, sec, ws_enabled=False)
    target.assert_no_dotenv_in_ancestors(cfg)
    t0 = time.time()
    r = subprocess.run([target.venv_bin(root, 'flask'), 'db', 'upgrade'],
                       cwd=os.path.join(root, 'app_src'), env=env,
                       capture_output=True, text=True, timeout=1800)
    with open(os.path.join(root, 'logs', 'migrate_aifx.log'), 'w', encoding='utf-8') as fh:
        fh.write(r.stdout + r.stderr)
    if r.returncode != 0:
        raise Refused('flask db upgrade FAILED — see logs/migrate_aifx.log '
                      '(the verified cold backup is intact)')
    cur.execute('SELECT version_num FROM alembic_version')
    after = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    tables_after = sorted(r[0] for r in cur.fetchall())
    counts_after = counts(cur, POPULATION_TABLES)
    mig.update({'applied': pending, 'after': after, 'seconds': round(time.time() - t0, 1),
                'tables_added': sorted(set(tables_after) - set(tables_before)),
                'tables_removed': sorted(set(tables_before) - set(tables_after)),
                'population_counts_unchanged': counts_before == counts_after,
                'population_counts': counts_after})
    say(json.dumps(mig, indent=2, default=str))
    if after != [head]:
        raise Refused(f'after upgrade alembic_version is {after}, expected {head}')
    if mig['tables_removed'] or counts_before != counts_after:
        raise Refused('the upgrade removed a table or changed population row counts')
    manifest.add_resource(root, 'schema_migration', database=cfg['db_name'],
                          server=f"{cfg['pg_host']}:{cfg['pg_port']}", applied=pending,
                          before=current, after=after,
                          note='allow-listed additive migrations; cold backup in run/backup.json')


# ── 5 fake tokens ────────────────────────────────────────────────────────────

def step_tokens(cur, cfg, fx, root):
    tag = common.tag(cfg)
    want = set()
    for k, rec in enumerate(fx['students'][:SELECTED_STUDENTS]):
        for d in range(TOKENS_PER_PARENT):
            want.add((rec['parent_user_id'], rec['school_id'], ic.device_token(tag, k, d)))
    cur.execute('SELECT user_id, school_id, fcm_token, is_active FROM mobile_device_tokens')
    have = cur.fetchall()
    action = 'reused'
    if not have:
        now = dt.datetime.utcnow()
        cur.execute('BEGIN')
        try:
            cur.executemany(
                'INSERT INTO mobile_device_tokens (user_id, school_id, fcm_token, platform, '
                'device_name, is_active, created_at, last_seen_at) '
                "VALUES (%s, %s, %s, 'android', %s, true, %s, %s)",
                [(u, s, t, f'attlt-fake-{t.rsplit("-", 1)[-1]}', now, now)
                 for (u, s, t) in sorted(want)])
            cur.execute('COMMIT')
        except Exception:
            cur.execute('ROLLBACK')
            raise
        action = 'inserted'
        manifest.add_resource(root, 'db_rows', table='mobile_device_tokens', count=len(want),
                              token_prefix=f'LT{tag}-TOK-', school_scope='load schools only',
                              note='fake FCM tokens for the parents of the 600 load students; '
                                   'never sent anywhere (fake firebase_admin only)')
        cur.execute('SELECT user_id, school_id, fcm_token, is_active FROM mobile_device_tokens')
        have = cur.fetchall()
    got = {(u, s, t) for (u, s, t, act) in have if act}
    res = {'action': action, 'rows': len(have), 'expected': len(want),
           'exact_match': got == want and len(have) == len(want),
           'all_tagged': all(ic.is_experiment_token(tag, t) for (_u, _s, t, _a) in have),
           'tokens_per_parent': TOKENS_PER_PARENT, 'parents': len({u for (u, _s, _t) in want})}
    OUT['tokens'] = res
    say(json.dumps(res, indent=2))
    if not (res['exact_match'] and res['all_tagged']):
        raise Refused('mobile_device_tokens does not hold exactly the expected fake tokens '
                      '(pre-existing tokens are never modified or removed)')


# ── 6 settle ─────────────────────────────────────────────────────────────────

def step_settle(cur):
    """Start the round from a settled storage state. Non-destructive: VACUUM
    changes no row. Without it, the first full read of freshly COPY-loaded (or
    rewritten) history sets hint bits and autovacuum/the checkpointer flush the
    resulting dirty pages DURING the load stages — I/O the round would then
    wrongly attribute to the AI Face path."""
    cur.execute("SELECT relname, n_dead_tup FROM pg_stat_user_tables WHERE relname = ANY(%s)",
                (list(POPULATION_TABLES),))
    dead_before = dict(cur.fetchall())
    t0 = time.time()
    cur.execute('VACUUM (ANALYZE)')
    t1 = time.time()
    cur.execute('CHECKPOINT')
    t2 = time.time()
    res = {'vacuum_analyze_seconds': round(t1 - t0, 1), 'checkpoint_seconds': round(t2 - t1, 1),
           'dead_tuples_before': dead_before,
           'note': 'VACUUM (ANALYZE) + CHECKPOINT on the TEST database only; no row changed'}
    OUT['settle'] = res
    say(json.dumps(res, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--preserved-root', required=True)
    ap.add_argument('--system-identifier', required=True)
    ap.add_argument('--allowed-migrations', required=True)
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    cfg, sec = common.load_config(root), common.load_secrets(root)
    with open(os.path.join(root, 'run', 'fixtures.json'), encoding='utf-8') as fh:
        fx = json.load(fh)
    code = 0
    conn = connect(cfg, sec)
    cur = conn.cursor()
    try:
        step_identity(cur, cfg, a)
        step_dataset(cur, cfg, fx)
        step_schema(cur, cfg, sec, a, root)
        step_tokens(cur, cfg, fx, root)
        step_settle(cur)
        OUT['row_counts_after_prepare'] = counts(cur, PRESERVED_TABLES)
        OUT['PASS'] = True
    except Refused as exc:
        OUT['refused'] = str(exc)
        say(f'PREPARE REFUSED: {exc}')
        code = 1
    except Exception as exc:
        OUT['error'] = f'{type(exc).__name__}: {str(exc)[:500]}'
        say(f'PREPARE ERROR: {OUT["error"]}')
        code = 2
    finally:
        conn.close()
        OUT['finished_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
        save(root)
    say('PREPARE ' + ('PASS' if OUT['PASS'] else 'FAILED'))
    raise SystemExit(code)


if __name__ == '__main__':
    main()
