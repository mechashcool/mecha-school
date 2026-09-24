"""Schema + synthetic fixtures for the attendance probe (test database ONLY).

Run through the orchestrator (`python seed.py --root <root>` from ANY python):
it re-executes itself inside venv-target with the isolated target environment.

Creates (all identifiers carry the experiment tag):
  * schema via the real Alembic migrations (`flask db upgrade`) → production indexes
  * 10 load schools × 1000 students, 2 AI Face devices per school,
    1 parent per student (parent_students link), device mappings
  * 2 precheck schools with 1 device / 1 student / 1 parent each, using the
    SAME enrollid, for the attribution and cross-school checks
  * deterministic attendance history (common.expected_history) for load students
Refuses to run unless the database carries this experiment's ownership marker.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

PRECHECK_SCHOOLS = (90, 91)


def reexec_in_target(root: str, argv: list[str]):
    import target
    cfg = common.load_config(root)
    sec = common.load_secrets(root)
    env = target.build_env(cfg, sec, ws_enabled=False)
    target.assert_no_dotenv_in_ancestors(cfg)
    app_src = os.path.join(root, 'app_src')
    # 1) schema through the real migrations
    t0 = time.time()
    r = subprocess.run([target.venv_bin(root, 'flask'), 'db', 'upgrade'], cwd=app_src, env=env,
                       capture_output=True, text=True)
    open(os.path.join(root, 'logs', 'migrate.log'), 'w', encoding='utf-8').write(r.stdout + r.stderr)
    if r.returncode != 0:
        print(r.stderr[-4000:])
        raise SystemExit('flask db upgrade failed — see logs/migrate.log')
    print(f'migrations applied in {time.time() - t0:.1f}s')
    env['ATTLT_TOOL_DIR'] = common.TOOL_DIR
    r = subprocess.run([target.venv_bin(root, 'python'), os.path.abspath(__file__), '--root', root,
                        '--inside'] + argv, cwd=app_src, env=env)
    raise SystemExit(r.returncode)


def inside(root: str):
    from sqlalchemy import insert, text
    from app import create_app
    from app.models import (db, School, AcademicYear, Grade, Section, Student, User, Role,
                            AttendanceDevice, DeviceStudentMapping, parent_students)
    from flask_bcrypt import generate_password_hash

    cfg = common.load_config(root)
    sec = common.load_secrets(root)
    tag = common.tag(cfg)
    app = create_app('production')
    with app.app_context():
        owner = db.session.execute(text('SELECT experiment_id FROM attlt_owner')).scalars().all()
        if owner != [cfg['experiment_id']]:
            raise SystemExit('ownership marker mismatch — refusing to seed')
        db.session.execute(text('CREATE TABLE IF NOT EXISTS attlt_seed (k text PRIMARY KEY, v text)'))
        if db.session.execute(text("SELECT v FROM attlt_seed WHERE k='done'")).scalar():
            print('fixtures already seeded — nothing to do')
            return
        # Some data migrations insert rows with explicit ids (e.g. schools.id=1)
        # without advancing the sequence. Align every owned sequence with its
        # table's max(id) — test database only, before inserting fixtures.
        seqs = db.session.execute(text("""
            SELECT c.relname AS tbl, a.attname AS col, pg_get_serial_sequence(c.relname, a.attname) AS seq
            FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid
            WHERE c.relkind = 'r' AND c.relnamespace = 'public'::regnamespace
              AND a.attnum > 0 AND NOT a.attisdropped AND pg_get_serial_sequence(c.relname, a.attname) IS NOT NULL""")).all()
        for tbl, col, seq in seqs:
            db.session.execute(text(
                f'SELECT setval(:seq, GREATEST((SELECT COALESCE(MAX("{col}"), 0) FROM "{tbl}"), 1))'),
                {'seq': seq})
        db.session.commit()
        pre = db.session.execute(text('SELECT count(*) FROM schools')).scalar()
        print(f'sequences aligned ({len(seqs)}); pre-existing migration rows in schools: {pre}')
        tz = common.dt  # noqa
        import pytz
        local_today = dt.datetime.now(pytz.timezone(cfg['school_timezone'])).date()
        history_end = local_today - dt.timedelta(days=1)
        cfg_disk = json.load(open(os.path.join(root, 'experiment.json'), encoding='utf-8'))
        cfg_disk['history_end_date'] = history_end.isoformat()
        json.dump(cfg_disk, open(os.path.join(root, 'experiment.json'), 'w', encoding='utf-8'), indent=2)
        cfg['history_end_date'] = history_end.isoformat()

        role = Role.query.filter_by(name='parent').first()
        if not role:
            role = Role(name='parent', label='ولي أمر')
            db.session.add(role)
            db.session.flush()
        pw_hash = generate_password_hash(sec['parent_password']).decode('utf-8')
        t = lambda s: dt.time.fromisoformat(s)  # noqa: E731

        fixtures = {'experiment_id': cfg['experiment_id'], 'schools': {}, 'devices': {},
                    'students': [None] * (cfg['num_schools'] * cfg['students_per_school']),
                    'precheck': {}}
        school_ids = list(range(cfg['num_schools'])) + list(PRECHECK_SCHOOLS)
        for s in school_ids:
            sch = School(school_name=f'مدرسة اختبار الحمل {tag} {s:02d}', code=common.school_code(cfg, s),
                         timezone=cfg['school_timezone'], att_start_time=t(cfg['att_start_time']),
                         att_late_threshold=t(cfg['att_late_threshold']),
                         att_absence_threshold=t(cfg['att_absence_threshold']),
                         att_departure_time=t(cfg['att_departure_time']))
            db.session.add(sch)
            db.session.flush()
            year = AcademicYear(school_id=sch.id, name=f'LT {history_end.year}',
                                start_date=history_end - dt.timedelta(days=150),
                                end_date=history_end + dt.timedelta(days=200), is_current=True)
            db.session.add(year)
            db.session.flush()
            sections = []
            for g in range(5):
                gr = Grade(name=f'الصف {g + 1}', stage='primary', school_id=sch.id, academic_year_id=year.id)
                db.session.add(gr)
                db.session.flush()
                for sc in range(4):
                    sec_row = Section(name=f'{chr(65 + sc)}', school_id=sch.id, academic_year_id=year.id,
                                      grade_id=gr.id, capacity=60)
                    db.session.add(sec_row)
                    db.session.flush()
                    sections.append(sec_row.id)
            n_dev = 1 if s in PRECHECK_SCHOOLS else cfg['devices_per_school']
            dev_ids = []
            for d in range(n_dev):
                sn = (f'LTAF-{tag}-PC{s}-D{d}' if s in PRECHECK_SCHOOLS else common.device_sn(cfg, s, d))
                dev = AttendanceDevice(school_id=sch.id, name=f'LT device {s:02d}-{d}', device_type='hikvision',
                                       device_scope='students', ip_address=f'192.0.2.{(s % 250) + 1}',
                                       port=80, username='attlt', password='attlt-unused', device_sn=sn,
                                       is_active=True, notes=f'attlt:{cfg["experiment_id"]}')
                db.session.add(dev)
                db.session.flush()
                dev_ids.append(dev.id)
                fixtures['devices'][sn] = {'id': dev.id, 'school_idx': s, 'school_id': sch.id, 'device_idx': d}
            fixtures['schools'][str(s)] = {'id': sch.id, 'year_id': year.id, 'code': sch.code,
                                          'device_ids': dev_ids}
            db.session.commit()

            n_students = 1 if s in PRECHECK_SCHOOLS else cfg['students_per_school']
            stu_rows, usr_rows = [], []
            for j in range(n_students):
                code = (f'LT{tag}-PC{s}-0000' if s in PRECHECK_SCHOOLS else common.student_code(cfg, s, j))
                k = None if s in PRECHECK_SCHOOLS else s + j * cfg['num_schools']
                uname = (f'lt{tag}pc{s}' if s in PRECHECK_SCHOOLS else common.parent_username(cfg, k))
                stu_rows.append(dict(student_id=code, full_name=f'طالب اختبار {s:02d}-{j:04d}', school_id=sch.id,
                                     academic_year_id=year.id, section_id=sections[j % len(sections)],
                                     gender='m' if j % 2 else 'f', status='active',
                                     enrollment_date=history_end - dt.timedelta(days=150)))
                usr_rows.append(dict(username=uname, password_hash=pw_hash, full_name=f'ولي أمر اختبار {s:02d}-{j:04d}',
                                     role_id=role.id, school_id=sch.id, is_active=True, locale='ar'))
            db.session.execute(insert(Student.__table__), stu_rows)
            db.session.execute(insert(User.__table__), usr_rows)
            db.session.commit()
            stu_ids = dict(db.session.execute(text(
                'SELECT student_id, id FROM students WHERE school_id=:sid'), {'sid': sch.id}).all())
            usr_ids = dict(db.session.execute(text(
                'SELECT username, id FROM users WHERE school_id=:sid'), {'sid': sch.id}).all())
            links, maps = [], []
            for j in range(n_students):
                if s in PRECHECK_SCHOOLS:
                    code, uname, d, enroll = f'LT{tag}-PC{s}-0000', f'lt{tag}pc{s}', 0, 1
                else:
                    k = s + j * cfg['num_schools']
                    lay = common.layout(cfg, k)
                    assert lay['school_idx'] == s and lay['local_idx'] == j
                    code, uname = common.student_code(cfg, s, j), common.parent_username(cfg, k)
                    d, enroll = lay['device_idx'], lay['enrollid']
                sid, uid = stu_ids[code], usr_ids[uname]
                links.append(dict(user_id=uid, student_id=sid, relation='guardian'))
                maps.append(dict(school_id=sch.id, device_id=dev_ids[d], employee_no_string=str(enroll),
                                 student_id=sid, is_active=True))
                rec = {'student_db_id': sid, 'parent_user_id': uid, 'school_id': sch.id, 'year_id': year.id,
                       'device_sn': (f'LTAF-{tag}-PC{s}-D0' if s in PRECHECK_SCHOOLS
                                     else common.device_sn(cfg, s, d)),
                       'enrollid': enroll, 'username': uname, 'student_code': code}
                if s in PRECHECK_SCHOOLS:
                    fixtures['precheck'][str(s)] = rec
                else:
                    fixtures['students'][k] = rec
            db.session.execute(insert(parent_students), links)
            db.session.execute(insert(DeviceStudentMapping.__table__), maps)
            db.session.commit()
            print(f'school {s}: {n_students} students seeded')

        # ── history via COPY ──────────────────────────────────────────────────
        dates = common.history_dates(cfg)
        raw = db.engine.raw_connection()
        cur = raw.cursor()
        total = 0
        for s in range(cfg['num_schools']):
            buf = io.StringIO()
            for j in range(cfg['students_per_school']):
                k = s + j * cfg['num_schools']
                rec = fixtures['students'][k]
                for d in dates:
                    e = common.expected_history(cfg, s, j, d)
                    ci = e['check_in'].isoformat() if e['check_in'] else r'\N'
                    co = e['check_out'].isoformat() if e['check_out'] else r'\N'
                    src = 'aiface' if e['check_in'] else 'manual'
                    notes = (f"AI Face {d.isoformat()} {e['check_in'].isoformat()}" if e['check_in'] else r'\N')
                    created = f"{d.isoformat()} 04:00:00"
                    buf.write(f"{rec['student_db_id']}\t{rec['school_id']}\t{rec['year_id']}\t{d.isoformat()}\t"
                              f"{e['status']}\t{ci}\t{co}\t{src}\t{notes}\t{created}\n")
                    total += 1
            buf.seek(0)
            cur.copy_expert('COPY student_attendance (student_id, school_id, academic_year_id, date, status, '
                            'check_in, check_out, source, notes, created_at) FROM STDIN', buf)
            raw.commit()
        cur.execute('ANALYZE')
        raw.commit()
        raw.close()
        db.session.execute(text("INSERT INTO attlt_seed (k, v) VALUES ('done', :v), ('history_rows', :h)"),
                           {'v': dt.datetime.utcnow().isoformat(), 'h': str(total)})
        db.session.commit()
        fixtures['history_rows'] = total
        fixtures['history_days'] = len(dates)
        json.dump(fixtures, open(os.path.join(root, 'run', 'fixtures.json'), 'w', encoding='utf-8'))
        print(f'history rows: {total} over {len(dates)} school days; fixtures.json written')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--inside', action='store_true')
    a, rest = ap.parse_known_args()
    root = os.path.abspath(a.root)
    if a.inside:
        inside(root)
    else:
        reexec_in_target(root, rest)


if __name__ == '__main__':
    main()
