# -*- coding: utf-8 -*-
"""Student create/edit parent picker — column projection must be invisible.

`students.create()` and `students.edit()` now load the `available_parents`
list with load_only(id, username, full_name, school_id, phone) instead of the
full 15-column User row. Everything else is unchanged.

The baseline is the REAL pre-change code: the students blueprint source of
commit 48b7d14 is executed as a separate module and its `create` / `edit` view
functions are swapped into the app for the baseline run, so both versions run
through the same routes, decorators, templates, tenant scope, before_flush
guard and database — on identical fixtures with identical ids.

Pinned here:
  * the working source differs from 48b7d14 ONLY by the two projections;
  * GET create / edit: byte-identical HTML and status (control: two runs of
    the same version are byte-identical), the list SELECT carries 5 columns
    instead of 15 with identical WHERE / ORDER BY / parameters, every other
    statement identical, same statement count and pool checkouts;
  * POST create (link a parent, another school's parent, a new parent, fee +
    payment → Notification / PushNotification) — apart from the later,
    intentional duplicate-name re-check (exactly 2 statements after the
    commit, see _strip_duplicate_name_check), POST edit (link, new parent,
    another school's parent), unlink + edit page, the fee-error rollback →
    _re_render path: identical responses, flashes, normalised database state
    and statement sequence (no extra lazy load / N+1);
  * access: anonymous, parent, other-school admin, super admin by
    active_school_id;
  * client↔server bytes through the loopback relay (printed).

Runs against the isolated local PostgreSQL test database only.
"""
import difflib
import os
import re
import statistics
import subprocess
import types
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import event, text

from app import create_app
from app.models import (db, AcademicYear, FeeType, Grade, Role, School, Section,
                        Student, User, parent_students)
from app.utils import badge_cache, code_generator, ttl_cache

REPO = Path(__file__).resolve().parents[1]
BASE_COMMIT = '48b7d148841f1088c09177b74bd5e1fdf6955c88'
STUDENTS_SRC = 'app/blueprints/students/__init__.py'
OPTS = {'bypass_tenant_scope': True}
_RCP = re.compile(r'RCP-\d{8}-[0-9A-F]{6}')
# The CSRF token is a per-session random signed value: the only part of a
# page that legitimately differs between two identical requests.
_CSRF = re.compile(rb'(name="csrf[-_]token"[^>]*?(?:content|value)=")[^"]+')
_LIST_ORDER = 'ORDER BY users.full_name'
FULL_USER_COLUMNS = 15


# ═════════════════════════════════════════════════════════════════════════════
#  Frozen baseline — the students blueprint exactly as committed at 48b7d14.
# ═════════════════════════════════════════════════════════════════════════════

def _baseline_source():
    out = subprocess.run(['git', 'show', f'{BASE_COMMIT}:{STUDENTS_SRC}'],
                         cwd=REPO, capture_output=True, text=True, encoding='utf-8')
    if out.returncode != 0:
        raise unittest.SkipTest(f'baseline {BASE_COMMIT[:7]} not available: {out.stderr}')
    return out.stdout


def _baseline_module(source):
    mod = types.ModuleType('app.blueprints._students_baseline_48b7d14')
    mod.__file__ = str(REPO / STUDENTS_SRC)
    exec(compile(source, f'<{BASE_COMMIT[:7]}:{STUDENTS_SRC}>', 'exec'), mod.__dict__)
    return mod


# ═════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═════════════════════════════════════════════════════════════════════════════

def _flat(statement):
    return ' '.join(statement.split())


def _is_list(statement):
    s = _flat(statement)
    return s.startswith('SELECT ') and ' FROM users ' in s and s.endswith(_LIST_ORDER)


def _select_cols(statement):
    s = _flat(statement)
    return [c.strip() for c in s[len('SELECT '):s.index(' FROM ')].split(',')]


def _tail(statement):
    s = _flat(statement)
    return s[s.index(' FROM '):]


class _Capture:
    def __init__(self, engine):
        self.engine, self.stmts, self.checkouts = engine, [], 0

    def _before(self, conn, cursor, statement, params, context, many):
        self.stmts.append([statement, params, None])

    def _after(self, conn, cursor, statement, params, context, many):
        self.stmts[-1][2] = cursor.rowcount

    def _checkout(self, *a):
        self.checkouts += 1

    def __enter__(self):
        event.listen(self.engine, 'before_cursor_execute', self._before)
        event.listen(self.engine, 'after_cursor_execute', self._after)
        event.listen(self.engine, 'checkout', self._checkout)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, 'before_cursor_execute', self._before)
        event.remove(self.engine, 'after_cursor_execute', self._after)
        event.remove(self.engine, 'checkout', self._checkout)
        return False


# key → table whose generated ids differ between runs and are mapped to labels
_ID_KEYS = {'student_id': 'students', 'user_id': 'users', 'target_user_id': 'users',
            'parent_id': 'users', 'collected_by': 'users', 'created_by': 'users',
            'created_by_user_id': 'users', 'recorded_by': 'users',
            'fee_record_id': 'fee_records', 'installment_id': 'fee_installments',
            'category_id': 'revenue_categories', 'fee_installment_id': 'fee_installments'}
# Generated per run: surrogate keys, wall-clock stamps (incl. a push's
# sent_at), the salted bcrypt hash and the audited row's surrogate id.
_DROP = {'id', 'created_at', 'updated_at', 'sent_at', 'password_hash', 'entity_id',
         'resource_id'}
# Dev-mode FCM message ids are derived from the wall clock (FCM is blocked).
_DEVLOG = re.compile(r'devlog-\d+')


# ═════════════════════════════════════════════════════════════════════════════
#  Tests
# ═════════════════════════════════════════════════════════════════════════════

class ParentPickerProjectionTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.base_src = _baseline_source()
        cls.base_mod = _baseline_module(cls.base_src)
        with cls.app.app_context():
            cls.roles = {n: Role.query.filter_by(name=n).first().id
                         for n in ('school_admin', 'parent', 'super_admin')}

    # ── Fixture ──────────────────────────────────────────────────────────────

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        ids = self.ids = {}
        with self.app.app_context():
            for tag in ('a', 'b'):
                school = School(school_name=f'PP {tag} {self.sfx}',
                                code=f'PP{tag.upper()}{self.sfx[:6]}', capacity=0,
                                is_active=True, timezone='Asia/Baghdad')
                db.session.add(school)
                db.session.flush()
                year = AcademicYear(school_id=school.id, name=f'Y {tag} {self.sfx}',
                                    start_date=date.today() - timedelta(days=100),
                                    end_date=date.today() + timedelta(days=200),
                                    is_current=True)
                db.session.add(year)
                db.session.flush()
                grade = Grade(school_id=school.id, academic_year_id=year.id,
                              name=f'G{tag}{self.sfx[:4]}')
                db.session.add(grade)
                db.session.flush()
                section = Section(school_id=school.id, academic_year_id=year.id,
                                  grade_id=grade.id, name=f'S{tag}{self.sfx[:4]}',
                                  capacity=40)
                db.session.add(section)
                db.session.flush()
                fee_type = FeeType(school_id=school.id, academic_year_id=year.id,
                                   name=f'رسوم {tag} {self.sfx}')
                db.session.add(fee_type)
                db.session.flush()
                ids.update({f'school_{tag}': school.id, f'year_{tag}': year.id,
                            f'section_{tag}': section.id, f'fee_type_{tag}': fee_type.id})

            def user(key, role, tag, full_name, phone=None, active=True):
                u = User(username=f'pp_{key}_{self.sfx}',
                         email=f'pp_{key}_{self.sfx}@example.test',
                         full_name=full_name, role_id=self.roles[role],
                         school_id=ids[f'school_{tag}'] if tag else None,
                         is_active=active, phone=phone,
                         last_login=datetime(2031, 1, 2, 3, 4, 5),
                         device_token=f'legacy-token-{key}')
                u.set_password('Password123')
                db.session.add(u)
                db.session.flush()
                ids[key] = u.id

            user('admin_a', 'school_admin', 'a', 'Admin A')
            user('admin_b', 'school_admin', 'b', 'Admin B')
            user('super', 'super_admin', None, 'Super')
            user('p_dup1', 'parent', 'a', 'أحمد علي', '07700000001')
            user('p_dup2', 'parent', 'a', 'أحمد علي', None)               # NULL phone
            user('p_html', 'parent', 'a', 'Zed <b>&"x\'', '0780')        # escaping
            user('p_inactive', 'parent', 'a', 'باسم', '0781', active=False)
            user('p_link', 'parent', 'a', 'سارة', '0782')
            user('p_multi', 'parent', 'a', 'مريم', '0783')
            user('p_free', 'parent', 'a', 'ياسر', '0790')
            user('p_b', 'parent', 'b', 'أحمد علي', '07700000001')         # same name, B

            for key, tag in (('s1', 'a'), ('s2', 'a'), ('sb', 'b')):
                st = Student(student_id=f'PP-{key}-{self.sfx}', full_name=f'طالب {key}',
                             date_of_birth=date(2015, 1, 1), gender='male',
                             school_id=ids[f'school_{tag}'],
                             academic_year_id=ids[f'year_{tag}'],
                             section_id=ids[f'section_{tag}'], status='active',
                             guardian_name='ولي')
                db.session.add(st)
                db.session.flush()
                ids[key] = st.id
            self.links = [(ids['p_link'], ids['s1']), (ids['p_multi'], ids['s1']),
                          (ids['p_multi'], ids['s2'])]
            for uid, sid in self.links:
                db.session.execute(parent_students.insert().values(
                    user_id=uid, student_id=sid, relation='father'))
            db.session.commit()
            self.fixture_users = [v for k, v in ids.items()
                                  if k.startswith(('admin_', 'super', 'p_'))]
            self.fixture_students = [ids['s1'], ids['s2'], ids['sb']]
            self.student_rows = {
                r['id']: dict(r) for r in db.session.execute(text(
                    'SELECT * FROM students WHERE id = ANY(:s)'),
                    {'s': self.fixture_students}).mappings()}
            gen = code_generator.generate_parent_username()
        self.gen_user, self.gen_pass = gen, 'AB482951'
        self.schools = [ids['school_a'], ids['school_b']]

    def tearDown(self):
        self._reset()
        with self.app.app_context():
            s = self.schools
            for sql in (
                'DELETE FROM parent_students WHERE student_id IN '
                '(SELECT id FROM students WHERE school_id = ANY(:s))',
                'DELETE FROM students WHERE school_id = ANY(:s)',
                'DELETE FROM users WHERE school_id = ANY(:s) OR id = ANY(:u)',
                'DELETE FROM fee_types WHERE school_id = ANY(:s)',
                'DELETE FROM sections WHERE school_id = ANY(:s)',
                'DELETE FROM grades WHERE school_id = ANY(:s)',
                'DELETE FROM academic_years WHERE school_id = ANY(:s)',
                'DELETE FROM schools WHERE id = ANY(:s)',
            ):
                db.session.execute(text(sql), {'s': s, 'u': self.fixture_users})
            db.session.commit()

    def _reset(self):
        """Back to the exact fixture state (everything a request created goes)."""
        with self.app.app_context():
            p = {'s': self.schools, 'u': self.fixture_users, 'st': self.fixture_students}
            for sql in (
                'DELETE FROM push_notifications WHERE school_id = ANY(:s)',
                'DELETE FROM notification_outbox WHERE school_id = ANY(:s)',
                'DELETE FROM notification_reads WHERE notification_id IN '
                '(SELECT id FROM notifications WHERE school_id = ANY(:s))',
                'DELETE FROM notifications WHERE school_id = ANY(:s)',
                'DELETE FROM revenues WHERE school_id = ANY(:s)',
                'DELETE FROM fee_installments WHERE school_id = ANY(:s)',
                'DELETE FROM fee_records WHERE school_id = ANY(:s)',
                'DELETE FROM revenue_categories WHERE school_id = ANY(:s)',
                'DELETE FROM audit_logs WHERE school_id = ANY(:s) OR user_id = ANY(:u)',
                'DELETE FROM parent_students WHERE student_id IN '
                '(SELECT id FROM students WHERE school_id = ANY(:s))',
                'DELETE FROM students WHERE school_id = ANY(:s) AND NOT (id = ANY(:st))',
                'DELETE FROM users WHERE school_id = ANY(:s) AND NOT (id = ANY(:u))',
            ):
                db.session.execute(text(sql), p)
            for uid, sid in self.links:
                db.session.execute(parent_students.insert().values(
                    user_id=uid, student_id=sid, relation='father'))
            for row in self.student_rows.values():
                cols = [c for c in row if c != 'id']
                db.session.execute(text(
                    f"UPDATE students SET {', '.join(f'{c} = :{c}' for c in cols)} "
                    'WHERE id = :id'), row)
            db.session.commit()
        badge_cache.clear()
        ttl_cache.clear()

    # ── State snapshot (normalised: generated ids → stable labels) ──────────

    def _snapshot(self):
        with self.app.app_context():
            q = lambda sql: [dict(r) for r in db.session.execute(  # noqa: E731
                text(sql), {'s': self.schools}).mappings()]
            tables = {
                'students': q('SELECT * FROM students WHERE school_id = ANY(:s)'),
                'users': q('SELECT * FROM users WHERE school_id = ANY(:s)'),
                'parent_students': q(
                    'SELECT ps.* FROM parent_students ps JOIN students s '
                    'ON s.id = ps.student_id WHERE s.school_id = ANY(:s)'),
                'fee_records': q('SELECT * FROM fee_records WHERE school_id = ANY(:s)'),
                'fee_installments': q(
                    'SELECT * FROM fee_installments WHERE school_id = ANY(:s)'),
                'revenues': q('SELECT * FROM revenues WHERE school_id = ANY(:s)'),
                'revenue_categories': q(
                    'SELECT * FROM revenue_categories WHERE school_id = ANY(:s)'),
                'notifications': q('SELECT * FROM notifications WHERE school_id = ANY(:s)'),
                'push_notifications': q(
                    'SELECT * FROM push_notifications WHERE school_id = ANY(:s)'),
                'notification_outbox': q(
                    'SELECT * FROM notification_outbox WHERE school_id = ANY(:s)'),
                'audit_logs': q('SELECT * FROM audit_logs WHERE school_id = ANY(:s)'),
            }
            db.session.remove()
        students = {r['id']: f"S:{r['full_name']}" for r in tables['students']}
        labels = {
            'students': students,
            'users': {r['id']: f"U:{r['username']}" for r in tables['users']},
            'fee_records': {r['id']: f"F:{students.get(r['student_id'])}:{r['total_amount']}"
                            for r in tables['fee_records']},
            'fee_installments': {r['id']: f"I:{r['installment_no']}"
                                 for r in tables['fee_installments']},
            'revenue_categories': {r['id']: f"C:{r['name']}"
                                   for r in tables['revenue_categories']},
        }
        out = {}
        for name, rows in tables.items():
            norm = []
            for r in rows:
                n = {}
                for k, v in r.items():
                    if k in _DROP:
                        continue
                    if k in _ID_KEYS and isinstance(v, int) and v in labels[_ID_KEYS[k]]:
                        v = labels[_ID_KEYS[k]][v]
                    if isinstance(v, str):
                        v = _DEVLOG.sub('devlog-*', _RCP.sub('RCP-*', v))
                        for key, table in _ID_KEYS.items():
                            v = re.sub(rf'("{key}":\s*"?)(\d+)',
                                       lambda m, t=table: m.group(1) + str(
                                           labels[t].get(int(m.group(2)), m.group(2))),
                                       v)
                    n[k] = v
                norm.append(n)
            out[name] = sorted(norm, key=repr)
        return out

    @staticmethod
    def _state_diff(a, b):
        """(table, column) pairs whose values differ — for a readable failure."""
        out = []
        for table in a:
            ra, rb_ = a[table], b[table]
            if len(ra) != len(rb_):
                out.append((table, f'rows {len(ra)} != {len(rb_)}'))
                continue
            for x, y in zip(ra, rb_):
                out += [(table, k, x.get(k), y.get(k)) for k in sorted(set(x) | set(y))
                        if x.get(k) != y.get(k)]
        return out

    # ── Request runner ──────────────────────────────────────────────────────

    def _request(self, impl, user_key, method, url, data=None, active_school=None):
        """One real request through the app with the chosen students code."""
        badge_cache.clear()
        ttl_cache.clear()
        client = self.app.test_client()
        if user_key:
            with client.session_transaction() as sess:
                sess['_user_id'] = str(self.ids[user_key])
                sess['_fresh'] = True
                if active_school:
                    sess['active_school_id'] = active_school
        with self.app.app_context():
            engine = db.engine
        views = ({'students.create': self.base_mod.create,
                  'students.edit': self.base_mod.edit} if impl == 'baseline' else {})
        with patch.dict(self.app.view_functions, views), \
                patch.object(code_generator, 'generate_parent_username',
                             return_value=self.gen_user), \
                patch.object(code_generator, 'generate_parent_password',
                             return_value=self.gen_pass), \
                _Capture(engine) as cap:
            if method == 'GET':
                resp = client.get(url)
            else:
                resp = client.post(url, data=data or {})
        with client.session_transaction() as sess:
            flashes = [(c, _RCP.sub('RCP-*', m)) for c, m in sess.get('_flashes', [])]
        location = resp.headers.get('Location', '')
        for sid in sorted(set(self._new_student_ids()) - set(self.fixture_students)):
            location = re.sub(rf'/{sid}(?=[/?]|$)', '/<new>', location)
        return {'status': resp.status_code,
                'body': _CSRF.sub(rb'\1<csrf>', resp.get_data()),
                'location': location, 'flashes': flashes,
                'stmts': cap.stmts, 'checkouts': cap.checkouts}

    def _new_student_ids(self):
        with self.app.app_context():
            ids = [r[0] for r in db.session.execute(text(
                'SELECT id FROM students WHERE school_id = ANY(:s)'), {'s': self.schools})]
            db.session.remove()
        return ids

    def _compare_sql(self, base, new):
        """Same statements in the same order; only the list SELECT may differ,
        and only in its column list. Returns (base_list, new_list)."""
        self.assertEqual(base['checkouts'], new['checkouts'], 'pool checkouts')
        b_flat = [_flat(s)[:200] for s, _, _ in base['stmts']]
        n_flat = [_flat(s)[:200] for s, _, _ in new['stmts']]
        self.assertEqual(len(b_flat), len(n_flat),
                         {'only_baseline': [x for x in b_flat if x not in n_flat],
                          'only_optimised': [x for x in n_flat if x not in b_flat]})
        lists = ([], [])
        for (bs, bp, br), (ns, np_, nr) in zip(base['stmts'], new['stmts']):
            if _is_list(bs):
                self.assertTrue(_is_list(ns), ns)
                self.assertEqual(_tail(bs), _tail(ns))          # WHERE + ORDER BY
                self.assertEqual(bp, np_)
                self.assertEqual(br, nr)                        # same row count
                lists[0].append(bs)
                lists[1].append(ns)
                continue
            if _flat(bs).startswith('SELECT ') and ' FROM users ' in _flat(bs) \
                    and 'WHERE users.id = ' in _flat(bs):
                # A post-rollback refresh of an expired picker row: same count,
                # same row, possibly a narrower column list.
                self.assertTrue(' FROM users ' in _flat(ns)
                                and 'WHERE users.id = ' in _flat(ns), ns)
                self.assertEqual(_tail(bs), _tail(ns))
                self.assertEqual(br, nr)
                continue
            self.assertEqual(_flat(bs), _flat(ns))
            if not re.match(r'(INSERT|UPDATE|DELETE)', _flat(bs)):
                self.assertEqual(br, nr, _flat(bs)[:160])
        return lists

    def _strip_duplicate_name_check(self, new, method, url):
        """Remove the post-save duplicate-name re-check that students.create
        gained after the 48b7d14 baseline — and nothing else.

        A successful create (302) issues exactly two intentional extra
        statements, right after its commit: one refresh of the School row (it
        expired at the commit; the lookup's building scope reads it) and one
        name lookup on students. Both are matched by exact shape and must occur
        exactly once, so every other statement is still compared one-to-one
        with the baseline and any extra query (N+1, repeated lookup) fails.
        Re-render paths (200) must not run the lookup at all.
        """
        stmts = new['stmts']
        lookups = [i for i, (s, _, _) in enumerate(stmts)
                   if _flat(s).startswith('SELECT students.')
                   and 'lower(translate(regexp_replace(students.full_name' in _flat(s)]
        is_create_ok = (method == 'POST' and url == '/students/create'
                        and new['status'] == 302)
        if not is_create_ok:
            self.assertEqual(lookups, [], 'duplicate-name lookup outside a saved create')
            return
        self.assertEqual(len(lookups), 1, 'duplicate-name lookup must run exactly once')
        i = lookups[0]
        # The School refresh comes first; the lookup's teacher check may pull
        # the current user's own expired-row refresh (users.id = …) in between.
        # The baseline issues that same refresh later, so it stays compared.
        j = i - 1
        while j >= 0 and i - j <= 2 and _flat(stmts[j][0]).startswith('SELECT users.') \
                and ' FROM users ' in _flat(stmts[j][0]) \
                and 'WHERE users.id = ' in _flat(stmts[j][0]):
            j -= 1
        refresh = _flat(stmts[j][0]) if j >= 0 else ''
        self.assertTrue(refresh.startswith('SELECT schools.')
                        and ' FROM schools WHERE schools.id = ' in refresh, refresh[:160])
        new['stmts'] = stmts[:j] + stmts[j + 1:i] + stmts[i + 1:]

    def _parity(self, user_key, method, url, data=None, active_school=None):
        self._reset()
        base = self._request('baseline', user_key, method, url, data, active_school)
        base_state = self._snapshot()
        self._reset()
        new = self._request('optimised', user_key, method, url, data, active_school)
        new_state = self._snapshot()
        self._strip_duplicate_name_check(new, method, url)
        self.assertEqual(base['status'], new['status'])
        self.assertEqual(base['location'], new['location'])
        self.assertEqual(base['flashes'], new['flashes'])
        if method == 'GET' or base['status'] == 200:
            self.assertEqual(base['body'], new['body'], 'rendered HTML differs')
        self.assertEqual(self._state_diff(base_state, new_state), [],
                         'database state differs')
        self.assertEqual(base_state, new_state, 'database state differs')
        lists = self._compare_sql(base, new)
        return base, new, lists, new_state

    def _assert_projection(self, lists):
        for b, n in zip(*lists):
            self.assertEqual(len(_select_cols(b)), FULL_USER_COLUMNS, _select_cols(b))
            self.assertEqual([c.split(' AS ')[0] for c in _select_cols(n)],
                             ['users.id', 'users.username', 'users.full_name',
                              'users.school_id', 'users.phone'])

    def _options(self, body):
        html = body.decode('utf-8')
        m = re.search(r'<select name="link_existing_parent_id"[^>]*>(.*?)</select>',
                      html, re.S)
        return re.findall(r'<option value="(\d+)"', m.group(1)) if m else []

    # ── Source guard ────────────────────────────────────────────────────────

    def test_source_differs_from_baseline_only_by_the_two_projections(self):
        current = (REPO / STUDENTS_SRC).read_text(encoding='utf-8')
        diff = [d for d in difflib.ndiff(self.base_src.splitlines(), current.splitlines())
                if d[:2] in ('+ ', '- ')]
        self.assertEqual([d for d in diff if d.startswith('- ')], [])
        added = [d[2:].strip() for d in diff]
        allowed = {
            'from sqlalchemy.orm import load_only',
            '.options(load_only(User.id, User.username, User.full_name,',
            'User.school_id, User.phone))',
            '# Only what the parent picker renders (id, full_name, username, phone)',
            '# plus school_id, which the scope guard reads if a linked parent is',
            '# later notified in this request.',
            '# Same projection as create(): the picker renders id, full_name,',
            '# username and phone; school_id is kept for the scope guard.',
        }
        self.assertEqual(set(added) - allowed, set())
        self.assertEqual(sum(a.startswith('.options(load_only(') for a in added), 2)

    # ── A / B: GET pages ────────────────────────────────────────────────────

    def test_control_two_runs_of_one_version_are_byte_identical(self):
        a = self._request('optimised', 'admin_a', 'GET', '/students/create')
        b = self._request('optimised', 'admin_a', 'GET', '/students/create')
        self.assertEqual(a['body'], b['body'])

    def test_get_create_identical(self):
        base, new, lists, _ = self._parity('admin_a', 'GET', '/students/create')
        self.assertEqual(new['status'], 200)
        self._assert_projection(lists)
        self.assertEqual(len(lists[1]), 1)
        i = self.ids
        # Ordered by name; inactive and other-school parents absent.
        opts = self._options(new['body'])
        self.assertEqual(set(opts), {str(i[k]) for k in (
            'p_dup1', 'p_dup2', 'p_html', 'p_link', 'p_multi', 'p_free')})
        html = new['body'].decode('utf-8')
        self.assertIn(f"(@pp_p_dup1_{self.sfx}) — 07700000001", html)
        self.assertIn(f"أحمد علي (@pp_p_dup2_{self.sfx})\n", html)       # NULL phone
        self.assertIn('Zed &lt;b&gt;&amp;&#34;x&#39;', html)
        self.assertNotIn(f'pp_p_inactive_{self.sfx}', html)
        self.assertNotIn(f'pp_p_b_{self.sfx}', html)

    def test_get_edit_identical_with_linked_exclusion(self):
        base, new, lists, _ = self._parity('admin_a', 'GET',
                                           f"/students/{self.ids['s1']}/edit")
        self.assertEqual(new['status'], 200)
        self._assert_projection(lists)
        opts = self._options(new['body'])
        i = self.ids
        self.assertEqual(set(opts), {str(i[k]) for k in (
            'p_dup1', 'p_dup2', 'p_html', 'p_free')})          # p_link / p_multi linked

    def test_super_admin_active_school_identical(self):
        base, new, lists, _ = self._parity('super', 'GET', '/students/create',
                                           active_school=self.ids['school_a'])
        self.assertEqual(new['status'], 200)
        self._assert_projection(lists)
        self.assertIn(str(self.ids['p_free']), self._options(new['body']))

    # ── G: access ───────────────────────────────────────────────────────────

    def test_access_decisions_identical(self):
        cases = [(None, f"/students/create"),
                 ('p_free', '/students/create'),
                 ('admin_b', f"/students/{self.ids['s1']}/edit"),
                 ('p_free', f"/students/{self.ids['s1']}/edit")]
        for user_key, url in cases:
            with self.subTest(user=user_key, url=url):
                base, new, lists, _ = self._parity(user_key, 'GET', url)
                self.assertNotEqual(new['status'], 200)
                self.assertEqual(lists, ([], []))

    def test_school_b_sees_only_its_parent(self):
        base, new, lists, _ = self._parity('admin_b', 'GET', '/students/create')
        self.assertEqual(self._options(new['body']), [str(self.ids['p_b'])])
        (_, params, _), = [s for s in new['stmts'] if _is_list(s[0])]
        self.assertNotIn(self.ids['school_a'], params.values())

    # ── D: POST create ──────────────────────────────────────────────────────

    def _create_form(self, **extra):
        form = {'full_name': f'طالب جديد {self.sfx}',
                'section_id': str(self.ids['section_a']),
                'gender': 'male', 'date_of_birth': '2015-02-03',
                'guardian_name': 'ولي جديد', 'guardian_phone': '07711111111',
                'guardian_relation': 'father'}
        form.update(extra)
        return form

    def _fee(self, total='1000', pay='400'):
        return {'create_fee': '1', 'fee_type_id': str(self.ids['fee_type_a']),
                'total_amount': total, 'discount_type': 'fixed', 'discount_value': '0',
                'num_installments': '1', 'due_date_1': '2031-03-01',
                'pay_amount_1': pay, 'pay_date_1': '2031-02-01', 'pay_method_1': 'cash'}

    def _links_of_new_student(self, state):
        return sorted(r['user_id'] for r in state['parent_students']
                      if r['student_id'] == f'S:طالب جديد {self.sfx}')

    def test_post_create_link_existing_parent(self):
        base, new, lists, state = self._parity(
            'admin_a', 'POST', '/students/create',
            self._create_form(link_existing_parent_id=str(self.ids['p_free'])))
        self.assertEqual(new['status'], 302)
        self.assertEqual(self._links_of_new_student(state), [f'U:pp_p_free_{self.sfx}'])

    def test_post_create_rejects_other_schools_parent(self):
        base, new, lists, state = self._parity(
            'admin_a', 'POST', '/students/create',
            self._create_form(link_existing_parent_id=str(self.ids['p_b'])))
        self.assertEqual(new['status'], 302)
        self.assertEqual(self._links_of_new_student(state), [])

    def test_post_create_new_parent(self):
        base, new, lists, state = self._parity(
            'admin_a', 'POST', '/students/create',
            self._create_form(create_parent_account='1', parent_username=self.gen_user,
                              parent_password=self.gen_pass))
        self.assertEqual(new['status'], 302)
        self.assertEqual(self._links_of_new_student(state), [f'U:{self.gen_user}'])

    def test_post_create_with_fee_payment_and_parent_notifications(self):
        base, new, lists, state = self._parity(
            'admin_a', 'POST', '/students/create',
            self._create_form(link_existing_parent_id=str(self.ids['p_free']), **self._fee()))
        self.assertEqual(new['status'], 302)
        self.assertEqual(len(state['fee_records']), 1)
        self.assertTrue(state['revenues'])
        targets = {r['target_user_id'] for r in state['notifications']}
        self.assertIn(f'U:pp_p_free_{self.sfx}', targets)

    # ── F: error / rollback paths ───────────────────────────────────────────

    def test_fee_error_rollback_rerender_identical(self):
        base, new, lists, state = self._parity(
            'admin_a', 'POST', '/students/create',
            self._create_form(link_existing_parent_id=str(self.ids['p_free']),
                              **self._fee(total='1000', pay='5000')))
        self.assertEqual(new['status'], 200)
        self.assertEqual(self._links_of_new_student(state), [])
        self.assertEqual(state['fee_records'], [])
        self.assertTrue(self._options(new['body']))
        refresh = [s for s, _, _ in new['stmts']
                   if 'WHERE users.id = ' in _flat(s) and ' FROM users ' in _flat(s)]
        base_refresh = [s for s, _, _ in base['stmts']
                        if 'WHERE users.id = ' in _flat(s) and ' FROM users ' in _flat(s)]
        self.assertEqual(len(refresh), len(base_refresh))

    def test_missing_date_of_birth_identical(self):
        # Whatever the current outcome is, both versions must produce it.
        form = self._create_form()
        form.pop('date_of_birth')
        self._parity('admin_a', 'POST', '/students/create', form)

    def test_invalid_residential_area_rerender_identical(self):
        base, new, lists, state = self._parity(
            'admin_a', 'POST', '/students/create',
            self._create_form(residential_area_id='999999999',
                              link_existing_parent_id=str(self.ids['p_free'])))
        self.assertEqual(new['status'], 200)
        self.assertEqual(self._links_of_new_student(state), [])
        self.assertTrue(self._options(new['body']))

    def test_invalid_fee_type_rerender_identical(self):
        fee = self._fee()
        fee['fee_type_id'] = str(self.ids['fee_type_b'])            # other school
        base, new, lists, state = self._parity(
            'admin_a', 'POST', '/students/create', self._create_form(**fee))
        self.assertEqual(new['status'], 200)
        self.assertEqual(state['fee_records'], [])

    # ── E: POST edit ────────────────────────────────────────────────────────

    def _edit_form(self, **extra):
        form = {'full_name': 'طالب s1', 'section_id': str(self.ids['section_a']),
                'status': 'active', 'gender': 'male', 'date_of_birth': '2015-01-01',
                'guardian_name': 'ولي'}
        form.update(extra)
        return form

    def _links_of(self, state, student_key):
        return sorted(r['user_id'] for r in state['parent_students']
                      if r['student_id'] == f'S:طالب {student_key}')

    def test_post_edit_link_existing_parent_keeps_others(self):
        base, new, lists, state = self._parity(
            'admin_a', 'POST', f"/students/{self.ids['s1']}/edit",
            self._edit_form(link_existing_parent_id=str(self.ids['p_free'])))
        self.assertEqual(new['status'], 302)
        self.assertEqual(self._links_of(state, 's1'), sorted(
            f'U:pp_{k}_{self.sfx}' for k in ('p_free', 'p_link', 'p_multi')))
        self.assertEqual(self._links_of(state, 's2'), [f'U:pp_p_multi_{self.sfx}'])

    def test_post_edit_other_schools_parent_not_linked(self):
        base, new, lists, state = self._parity(
            'admin_a', 'POST', f"/students/{self.ids['s1']}/edit",
            self._edit_form(link_existing_parent_id=str(self.ids['p_b'])))
        self.assertEqual(self._links_of(state, 's1'), sorted(
            f'U:pp_{k}_{self.sfx}' for k in ('p_link', 'p_multi')))

    def test_post_edit_new_parent_account(self):
        uname = f'pp_new_{self.sfx}'
        base, new, lists, state = self._parity(
            'admin_a', 'POST', f"/students/{self.ids['s1']}/edit",
            self._edit_form(new_parent_username=uname, new_parent_password='Secret123',
                            new_parent_password_confirm='Secret123'))
        self.assertIn(f'U:{uname}', self._links_of(state, 's1'))

    def test_unlink_then_edit_page_identical(self):
        """The existing unlink route (unchanged code) removes a link; the edit
        page afterwards offers that parent again, identically in both."""
        self._reset()
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.ids['admin_a'])
            sess['_fresh'] = True
        resp = client.post(f"/students/{self.ids['s1']}/unlink-parent",
                           data={'parent_user_id': str(self.ids['p_link'])})
        self.assertEqual(resp.status_code, 302)
        fixture_links = list(self.links)
        # The fixture state for this test is "p_link no longer linked to s1".
        self.links = [l for l in fixture_links if l != (self.ids['p_link'], self.ids['s1'])]
        try:
            base, new, lists, state = self._parity('admin_a', 'GET',
                                                   f"/students/{self.ids['s1']}/edit")
            self._assert_projection(lists)
            self.assertEqual(self._links_of(state, 's1'), [f'U:pp_p_multi_{self.sfx}'])
            self.assertIn(str(self.ids['p_link']), self._options(new['body']))
        finally:
            self.links = fixture_links

    # ── Network measurement ─────────────────────────────────────────────────

    def test_protocol_bytes_with_200_parents(self):
        """GET /students/create with ~200 synthetic parents through the
        loopback relay (pool_pre_ping=True), baseline vs optimised interleaved;
        and the list query alone."""
        import config.settings as settings
        from flask_login import login_user
        from sqlalchemy.engine import make_url
        from sqlalchemy.orm import load_only
        from tests.test_employee_auto_absence_egress import _Relay

        runs = int(os.environ.get('PP_BYTE_RUNS', '10'))
        with self.app.app_context():
            db.session.execute(text(
                "INSERT INTO users (username, email, password_hash, full_name, role_id, "
                "school_id, is_active, phone, last_login, device_token, locale, "
                "created_at, updated_at) "
                "SELECT 'pp_syn_' || :sfx || '_' || g, NULL, "
                "'$2b$12$' || repeat('x', 53), 'ولي أمر تجريبي ' || lpad(g::text, 3, '0'), "
                ":role, :school, true, '0770' || lpad(g::text, 7, '0'), "
                "CASE WHEN g % 2 = 0 THEN now() END, NULL, 'ar', now(), now() "
                "FROM generate_series(1, 200) AS g"),
                {'sfx': self.sfx, 'role': self.roles['parent'],
                 'school': self.ids['school_a']})
            db.session.commit()
            n_parents = db.session.execute(text(
                'SELECT count(*) FROM users WHERE school_id = :s AND role_id = :r '
                'AND is_active'), {'s': self.ids['school_a'],
                                    'r': self.roles['parent']}).scalar()
        url = make_url(os.environ['TEST_DATABASE_URL'])
        relay = _Relay(url.port)
        orig_uri = settings.TestingConfig.SQLALCHEMY_DATABASE_URI
        had_opts = 'SQLALCHEMY_ENGINE_OPTIONS' in vars(settings.TestingConfig)
        settings.TestingConfig.SQLALCHEMY_DATABASE_URI = url.set(
            host='127.0.0.1', port=relay.port).render_as_string(hide_password=False)
        settings.TestingConfig.SQLALCHEMY_ENGINE_OPTIONS = {'pool_pre_ping': True}
        try:
            app_r = create_app('testing')
        finally:
            settings.TestingConfig.SQLALCHEMY_DATABASE_URI = orig_uri
            if not had_opts:
                del settings.TestingConfig.SQLALCHEMY_ENGINE_OPTIONS
        main_app = self.app
        page = {'baseline': [], 'optimised': []}
        query = {'baseline': [], 'optimised': []}
        rows = {}
        try:
            self.app = app_r
            self._request('optimised', 'admin_a', 'GET', '/students/create')   # warm
            for _ in range(runs):
                for impl in ('baseline', 'optimised'):
                    relay.reset()
                    r = self._request(impl, 'admin_a', 'GET', '/students/create')
                    self.assertEqual(r['status'], 200)
                    page[impl].append((relay.down, relay.up))
                    (_, _, rows[impl]), = [s for s in r['stmts'] if _is_list(s[0])]
            parent_role = self.roles['parent']
            for _ in range(runs):
                for impl in ('baseline', 'optimised'):
                    with app_r.test_request_context('/students/create'):
                        login_user(db.session.get(User, self.ids['admin_a'],
                                                  execution_options=OPTS))
                        app_r.preprocess_request()
                        q = User.query
                        if impl == 'optimised':
                            q = q.options(load_only(User.id, User.username, User.full_name,
                                                    User.school_id, User.phone))
                        q = (q.filter_by(school_id=self.ids['school_a'],
                                         role_id=parent_role, is_active=True)
                             .order_by(User.full_name))
                        db.session.execute(text('SELECT 1'))      # connection checked out
                        relay.reset()
                        result = q.all()
                        query[impl].append((relay.down, relay.up))
                        self.assertEqual(len(result), n_parents)
                        db.session.remove()
        finally:
            self.app = main_app
            with app_r.app_context():
                db.engine.dispose()
            relay.close()

        def fmt(samples):
            d = [x for x, _ in samples]
            u = [y for _, y in samples]
            return (f'down min/median/max={min(d)}/{statistics.median(d)}/{max(d)}B '
                    f'up min/median/max={min(u)}/{statistics.median(u)}/{max(u)}B')
        print(f'\n[parent-picker-bytes] parents={n_parents} rows={rows} runs={runs} '
              f'pool_pre_ping=True'
              f'\n  page  baseline:  {fmt(page["baseline"])}'
              f'\n  page  optimised: {fmt(page["optimised"])}'
              f'\n  query baseline:  {fmt(query["baseline"])}'
              f'\n  query optimised: {fmt(query["optimised"])}')
        self.assertEqual(rows['baseline'], rows['optimised'])
        self.assertLess(statistics.median(d for d, _ in query['optimised']),
                        statistics.median(d for d, _ in query['baseline']))


if __name__ == '__main__':                                    # pragma: no cover
    unittest.main()
