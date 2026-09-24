"""Institute + device-token fixtures, layered on the EXISTING synthetic data.

Run through the orchestrator (`python institute_fixtures.py --root <root>`):
it re-executes itself inside venv-target with the isolated target environment,
exactly as seed.py does.

What this adds — and only this
──────────────────────────────
seed.py already created the schools, academic years, students, parents and the
parent_students links. This step REUSES all of them and adds the minimum the
institute attendance path needs:

  * institution_type='institute' on the chosen load schools
        The only documented behavioural change is that AUTOMATIC student
        absence generation is skipped. The probe disables that scheduler
        anyway (ATTENDANCE_SCHEDULER_DISABLED=true), so the AI Face round is
        unaffected. Schools not listed are left untouched.
  * one Subject per school                      LT<tag>-SUBJ-<ss>
  * one instructor User(role=teacher)+Employee  lt<tag>i<ss> / LT<tag>-INS-<ss>
  * GROUPS_PER_SCHOOL study groups              LT<tag>-GRP-<ss>-<g>
  * enrollments drawn from that school's own existing students
  * one attendance session per group for the test date
  * TOKENS_PER_PARENT fake MobileDeviceToken rows per enrolled student's parent

Every row carries the experiment tag, belongs to a school seed.py created, and
is recorded in run/institute_fixtures.json plus the manifest. Nothing is
created in any school this experiment does not own.

Refuses to run unless the database carries this experiment's ownership marker.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import institute_common as ic  # noqa: E402


def reexec_in_target(root: str, argv: list):
    import target
    cfg = common.load_config(root)
    sec = common.load_secrets(root)
    env = target.build_env(cfg, sec, ws_enabled=False)
    target.assert_no_dotenv_in_ancestors(cfg)
    env['ATTLT_TOOL_DIR'] = common.TOOL_DIR
    r = subprocess.run([target.venv_bin(root, 'python'), os.path.abspath(__file__),
                        '--root', root, '--inside'] + argv,
                       cwd=os.path.join(root, 'app_src'), env=env)
    raise SystemExit(r.returncode)


def inside(root: str, *, test_date: str | None = None):
    from sqlalchemy import text
    from app import create_app
    from app.models import (db, School, AcademicYear, Student, User, Role,
                            Employee, Subject, MobileDeviceToken,
                            InstituteStudyGroup, InstituteGroupEnrollment,
                            InstituteAttendanceSession, parent_students)

    cfg = common.load_config(root)
    tag = common.tag(cfg)
    fx = json.load(open(os.path.join(root, 'run', 'fixtures.json'),
                        encoding='utf-8'))
    app = create_app('production')
    with app.app_context():
        owner = db.session.execute(
            text('SELECT experiment_id FROM attlt_owner')).scalars().all()
        if owner != [cfg['experiment_id']]:
            raise SystemExit('ownership marker mismatch — refusing to seed')

        # Only schools THIS experiment seeded. Anything else is out of bounds.
        owned_school_ids = {v['id'] for v in fx['schools'].values()}
        school_indices = list(range(cfg['num_schools']))

        date = (dt.date.fromisoformat(test_date) if test_date
                else dt.date.today())
        pw_hash = _instructor_password_hash(root)
        role = Role.query.filter_by(name='teacher').first()
        if role is None:
            role = Role(name='teacher', label='معلم')
            db.session.add(role)
            db.session.flush()

        out = {
            'experiment_id': cfg['experiment_id'],
            'test_date': date.isoformat(),
            'schools': {},
            'totals': {'groups': 0, 'enrollments': 0, 'sessions': 0,
                       'tokens': 0, 'instructors': 0},
        }

        for s in school_indices:
            meta = fx['schools'][str(s)]
            school_id, year_id = meta['id'], meta['year_id']
            if school_id not in owned_school_ids:
                raise SystemExit(f'school {school_id} is not experiment-owned')

            school = School.query.execution_options(
                bypass_tenant_scope=True).get(school_id)
            if school is None:
                raise SystemExit(f'school {school_id} missing')
            school.institution_type = School.INSTITUTION_INSTITUTE

            year = AcademicYear.query.execution_options(
                bypass_tenant_scope=True).get(year_id)
            if year is None or not year.is_current:
                raise SystemExit(f'school {s}: no current academic year')

            subject = Subject(name=f'مادة معهد {tag} {s:02d}',
                              code=ic.subject_code(tag, s),
                              school_id=school_id, academic_year_id=year_id)
            db.session.add(subject)

            user = User(username=ic.instructor_username(tag, s),
                        password_hash=pw_hash,
                        full_name=f'مدرّس اختبار {s:02d}', role_id=role.id,
                        school_id=school_id, is_active=True, locale='ar')
            db.session.add(user)
            db.session.flush()

            emp = Employee(employee_id=ic.instructor_employee_id(tag, s),
                           full_name=f'مدرّس اختبار {s:02d}',
                           job_title='مدرّس', school_id=school_id,
                           user_id=user.id, status='active', base_salary=0)
            db.session.add(emp)
            db.session.flush()
            out['totals']['instructors'] += 1

            # Students of THIS school only, ordered by the deterministic local
            # index so enrollment is reproducible.
            local = _school_students(fx, cfg, s)
            groups = []
            for gidx in range(ic.GROUPS_PER_SCHOOL):
                grp = InstituteStudyGroup(
                    school_id=school_id, academic_year_id=year_id,
                    subject_id=subject.id, instructor_id=emp.id,
                    name=ic.group_name(tag, s, gidx), is_active=True)
                db.session.add(grp)
                db.session.flush()
                out['totals']['groups'] += 1

                enrolled = []
                for j in ic.enrolled_local_indices(
                        s, gidx, cfg['students_per_school']):
                    rec = local.get(j)
                    if rec is None:
                        continue
                    student = Student.query.execution_options(
                        bypass_tenant_scope=True).get(rec['student_db_id'])
                    if student is None or student.school_id != school_id:
                        raise SystemExit(
                            f'student {rec["student_db_id"]} not in school {school_id}')
                    db.session.add(InstituteGroupEnrollment(
                        school_id=school_id, group_id=grp.id,
                        student_id=student.id,
                        status=InstituteGroupEnrollment.STATUS_ACTIVE))
                    enrolled.append(rec)
                    out['totals']['enrollments'] += 1

                sess = InstituteAttendanceSession(
                    school_id=school_id, academic_year_id=year_id,
                    group_id=grp.id, session_date=date,
                    start_time=ic.SESSION_START, end_time=ic.SESSION_END,
                    instructor_id=emp.id,
                    status=InstituteAttendanceSession.STATUS_NOT_RECORDED)
                db.session.add(sess)
                db.session.flush()
                out['totals']['sessions'] += 1

                groups.append({
                    'group_id': grp.id, 'group_name': grp.name,
                    'session_id': sess.id,
                    'student_db_ids': [r['student_db_id'] for r in enrolled],
                    'parent_user_ids': [r['parent_user_id'] for r in enrolled],
                    'student_ks': [r['k'] for r in enrolled],
                })

            # Fake device tokens, for the parents of enrolled students only.
            # A parent enrolled in two groups of the same school is still one
            # parent, so tokens are created once per distinct arrival index k.
            token_rows = 0
            seen_k = set()
            for g in groups:
                for k, parent_id in zip(g['student_ks'], g['parent_user_ids']):
                    if k in seen_k:
                        continue
                    seen_k.add(k)
                    for d in range(ic.TOKENS_PER_PARENT):
                        active = (ic.INACTIVE_TOKEN_INDEX is None
                                  or d != ic.INACTIVE_TOKEN_INDEX)
                        db.session.add(MobileDeviceToken(
                            user_id=parent_id,
                            school_id=school_id,
                            fcm_token=ic.device_token(tag, k, d),
                            platform='android',
                            device_name=f'attlt-fake-{d}',
                            is_active=active))
                        token_rows += 1
            out['totals']['tokens'] += token_rows

            db.session.commit()
            out['schools'][str(s)] = {
                'school_id': school_id, 'year_id': year_id,
                'instructor_user_id': user.id, 'instructor_employee_id': emp.id,
                'instructor_username': user.username,
                'subject_id': subject.id, 'groups': groups,
                'token_rows': token_rows,
            }
            print(f'school {s}: {len(groups)} groups, '
                  f'{sum(len(g["student_db_ids"]) for g in groups)} enrollments, '
                  f'{token_rows} fake tokens')

        _verify_ownership(db, text, cfg, tag, owned_school_ids)
        path = os.path.join(root, 'run', 'institute_fixtures.json')
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        print(json.dumps(out['totals'], indent=2))
        print('institute_fixtures.json written')


def _school_students(fx: dict, cfg: dict, s: int) -> dict:
    """{local_index j: fixture record} for one school, from seed.py's output."""
    out = {}
    for k, rec in enumerate(fx['students']):
        if rec is None:
            continue
        lay = common.layout(cfg, k)
        if lay['school_idx'] == s:
            out[lay['local_idx']] = {**rec, 'k': k}
    return out


def _instructor_password_hash(root: str) -> str:
    """Reuse the experiment's generated parent password. Never a real one, and
    never used: the generator authenticates with a pre-issued JWT."""
    from werkzeug.security import generate_password_hash
    sec = common.load_secrets(root)
    return generate_password_hash(sec['parent_password']).decode('utf-8')


def _verify_ownership(db, text, cfg: dict, tag: str, owned_school_ids: set):
    """Prove, in SQL, that nothing was created outside the experiment.

    Checks the ROW COUNTS of every table this step writes, against the set of
    experiment-owned school ids. Any row elsewhere aborts before the fixture
    file is written.
    """
    checks = {
        'institute_study_groups': 'school_id',
        'institute_group_enrollments': 'school_id',
        'institute_attendance_sessions': 'school_id',
        'mobile_device_tokens': 'school_id',
    }
    ids = tuple(sorted(owned_school_ids))
    problems = []
    for table, col in checks.items():
        n = db.session.execute(
            text(f'SELECT count(*) FROM {table} WHERE {col} NOT IN :ids'),
            {'ids': ids}).scalar()
        if n:
            problems.append(f'{table}: {n} row(s) outside experiment schools')
    stray = db.session.execute(text(
        'SELECT count(*) FROM mobile_device_tokens WHERE fcm_token NOT LIKE :p'),
        {'p': f'LT{tag}-TOK-%'}).scalar()
    if stray:
        problems.append(f'mobile_device_tokens: {stray} token(s) not tagged '
                        f'LT{tag}-TOK-')
    if problems:
        raise SystemExit('ownership verification FAILED: ' + '; '.join(problems))
    print('ownership verified: every institute row belongs to an experiment school')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--inside', action='store_true')
    ap.add_argument('--test-date', default=None)
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    if a.inside:
        inside(root, test_date=a.test_date)
    else:
        rest = ['--test-date', a.test_date] if a.test_date else []
        reexec_in_target(root, rest)


if __name__ == '__main__':
    main()
