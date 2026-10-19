"""
Admin "Add Student Leave" — multi-student (group) leave across sections/grades.

POST /admin/leave-requests/create accepts ``student_ids`` (one value per
selected student) and creates ONE ordinary LeaveRequest row per student with
identical leave data, in a single commit. Stage / grade / section are only
picker filters: the selection may span sections and grades, and every student
is validated on its own row (school, active, active-year section, buildings).
The legacy single ``student_id`` field keeps working.

Proves:
  * students from two sections / two grades are created together, one row each;
  * duplicates (also across filter selections) produce one row;
  * a foreign-school / unknown / malformed / inactive / other-year /
    out-of-building id rejects the whole submission and creates nothing;
  * a rejected submission restores only scoped-valid students (never a foreign
    student's name) from every section;
  * single-student behaviour, attachment rule and field validation unchanged;
  * picker endpoints apply the same eligibility rule;
  * JS (real rendered page, fake DOM in Node): filter changes keep the global
    selection, no confirmation, "select all" adds, dedup, chip ×, clear all;
  * approved group → attendance per student; parent notifications same-school;
  * statement count does not grow with the number of selected students.
"""
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import date
from unittest import mock
from uuid import uuid4

from sqlalchemy import event

from app import create_app
from app.models import (db, AcademicYear, AuditLog, Grade, LeaveRequest, Notification,
                        Role, School, SchoolBuilding, Section, Student,
                        StudentAttendance, User, UserBuildingAccess, parent_students)

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True, 'include_all_years': True}
URL = '/admin/leave-requests/create'
SECTION_URL = '/admin/api/leave/students-for-section'
SEARCH_URL = '/admin/api/leave/students-search'


class AdminGroupStudentLeaveTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False
        with cls.app.app_context():
            cls.admin_role = Role.query.filter_by(name='school_admin').first().id
            cls.parent_role = Role.query.filter_by(name='parent').first().id

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        with self.app.app_context():
            self._school('a')
            self._school('b')
            # Parent of school A linked to a1 + a2; a parent account of school B
            # corruptly linked to a3 must never be notified by school A.
            self._link(self._parent('a'), 'a1', 'a2')
            self._link(self._parent('b'), 'a3')
            db.session.commit()

    def _school(self, key):
        s = self.sfx
        school = School(school_name=f'GLv {key} {s}', code=f'GL{key}{s}'[:20],
                        capacity=0, is_active=True)
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'Y{key}{s}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 6, 30))
        old = AcademicYear(school_id=school.id, name=f'O{key}{s}', is_current=False,
                           start_date=date(2025, 8, 1), end_date=date(2026, 6, 30))
        db.session.add_all([year, old])
        db.session.flush()
        secs = {}

        def grade(gkey, stage, yr, sections):
            g = Grade(name=f'{gkey}{key}{s}', stage=stage, school_id=school.id,
                      academic_year_id=yr.id)
            db.session.add(g)
            db.session.flush()
            self.ids[f'grade_{gkey}_{key}'] = g.id
            for name in sections:
                sec = Section(name=f'{name}-{key}', grade_id=g.id, school_id=school.id,
                              academic_year_id=yr.id)
                db.session.add(sec)
                db.session.flush()
                secs[name] = sec.id

        grade('g1', 'ابتدائي', year, ('s1', 's2', 'big'))
        grade('g2', 'متوسط', year, ('t1',))
        grade('gold', 'ابتدائي', old, ('old',))      # previous academic year

        admin = User(username=f'gladm{key}_{s}', email=f'gladm{key}_{s}@t.test',
                     full_name=f'adm {key}', role_id=self.admin_role,
                     school_id=school.id, is_active=True)
        admin.set_password(PASSWORD)
        db.session.add(admin)
        db.session.flush()

        def student(code, sec, status='active'):
            st = Student(student_id=f'GL{key}{s}-{code}', full_name=f'طالب {key} {code}',
                         school_id=school.id, academic_year_id=year.id,
                         section_id=secs[sec] if sec else None, status=status)
            db.session.add(st)
            db.session.flush()
            self.ids[f'{key}{code}' if key == 'a' else f'f{code}'] = st.id

        if key == 'a':
            for code in ('1', '2', '3', '4'):
                student(code, 's1')
            student('x', 's1', status='archived')      # inactive, same section
            student('o', 's2')                          # other section, same grade
            student('5', 't1')                          # other grade
            student('6', 't1')
            student('y', 'old')                         # placed in last year's section
            student('n', None)                          # no section (search-mode student)
            for i in range(15):                         # statement-count probe set
                student(f'p{i:02d}', 'big')
        else:
            student('1', 's1')                          # foreign-school student
        self.ids.update({f'school_{key}': school.id, f'year_{key}': year.id,
                         f'admin_{key}': admin.username, f'admin_id_{key}': admin.id})
        self.ids.update({f'{n}_{key}': sid for n, sid in secs.items()})

    def _parent(self, key):
        user = User(username=f'glpar{key}_{self.sfx}', email=f'glpar{key}_{self.sfx}@t.test',
                    full_name=f'par {key}', role_id=self.parent_role,
                    school_id=self.ids[f'school_{key}'], is_active=True)
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.flush()
        self.ids[f'parent_{key}'] = user.id
        return user.id

    def _link(self, parent_id, *codes):
        for code in codes:
            db.session.execute(parent_students.insert().values(
                user_id=parent_id, student_id=self.ids[code]))

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for key in ('a', 'b'):
                sid = self.ids[f'school_{key}']
                stu_ids = [st.id for st in Student.query.execution_options(**OPTS)
                           .filter_by(school_id=sid).all()]
                uids = [u.id for u in User.query.execution_options(**OPTS)
                        .filter_by(school_id=sid).all()]
                if stu_ids:
                    db.session.execute(parent_students.delete().where(
                        parent_students.c.student_id.in_(stu_ids)))
                if uids:
                    AuditLog.query.execution_options(**OPTS).filter(
                        AuditLog.user_id.in_(uids)).delete(synchronize_session=False)
                for model in (Notification, StudentAttendance, LeaveRequest,
                              UserBuildingAccess, Student, SchoolBuilding, Section,
                              Grade, User, AcademicYear):
                    model.query.execution_options(**OPTS).filter_by(
                        school_id=sid).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _web(self, key='a'):
        client = self.app.test_client()
        resp = client.post('/auth/login', data={'username': self.ids[f'admin_{key}'],
                                                'password': PASSWORD})
        self.assertEqual(resp.status_code, 302)
        return client

    def _form(self, student_ids=None, section='s1', **over):
        """The filters reflect whatever section is currently displayed — they
        never authorise the selected students."""
        data = {
            'stage': 'ابتدائي',
            'grade_id': str(self.ids['grade_g1_a']),
            'section_id': str(self.ids[f'{section}_a']) if section else '',
            'leave_type': 'sick',
            'status': 'pending',
            'from_date': '2026-10-20',
            'to_date': '2026-10-22',
            'notes': 'رحلة مدرسية',
        }
        if student_ids is not None:
            data['student_ids'] = [str(i) for i in student_ids]
        data.update(over)
        return data

    def _rows(self):
        with self.app.app_context():
            return (LeaveRequest.query.execution_options(**OPTS)
                    .filter(LeaveRequest.school_id.in_(
                        [self.ids['school_a'], self.ids['school_b']]))
                    .order_by(LeaveRequest.id).all())

    def _notifications(self):
        with self.app.app_context():
            return (Notification.query.execution_options(**OPTS)
                    .filter(Notification.school_id.in_(
                        [self.ids['school_a'], self.ids['school_b']])).all())

    def _assert_rejected(self, resp, *snippets):
        self.assertEqual(resp.status_code, 422)
        html = resp.get_data(as_text=True)
        for snippet in snippets:
            self.assertIn(snippet, html)
        self.assertEqual(self._rows(), [], 'rejected submission created rows')
        self.assertEqual(self._notifications(), [], 'rejected submission notified')
        return html

    def _code(self, key):
        return f'GLa{self.sfx}-{key}'

    # ── 1-3: cross-section / cross-grade groups ───────────────────────────────

    def test_students_from_two_sections_and_two_grades_one_row_each(self):
        client = self._web()
        keys = ('a1', 'a2', 'a3', 'ao', 'a5', 'a6', 'an')   # s1 ×3, s2, t1 ×2, no section
        ids = [self.ids[k] for k in keys]
        # Displayed filter is grade 2 / t1 — irrelevant to the s1/s2 students.
        resp = client.post(URL, data=self._form(ids, section='t1', stage='متوسط',
                                                grade_id=str(self.ids['grade_g2_a'])),
                           follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('تمت إضافة الإجازة بنجاح لـ 7 طلاب.', resp.get_data(as_text=True))
        rows = self._rows()
        self.assertEqual(sorted(r.student_id for r in rows), sorted(ids))
        shared = {(r.school_id, r.academic_year_id, r.leave_type, r.from_date, r.to_date,
                   r.notes, r.status, r.source, r.parent_id, r.created_by_user_id,
                   r.attachment_path, r.reviewed_by) for r in rows}
        self.assertEqual(shared, {(self.ids['school_a'], self.ids['year_a'], 'sick',
                                   date(2026, 10, 20), date(2026, 10, 22), 'رحلة مدرسية',
                                   'pending', 'admin', None, self.ids['admin_id_a'],
                                   None, None)})

    def test_group_without_any_section_filter(self):
        client = self._web()
        ids = [self.ids['a1'], self.ids['a5']]
        resp = client.post(URL, data=self._form(ids, section=None, grade_id='', stage=''))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(sorted(r.student_id for r in self._rows()), sorted(ids))

    # ── 6: duplicates ─────────────────────────────────────────────────────────

    def test_duplicates_across_filter_selections_give_one_row(self):
        client = self._web()
        a1, a5 = self.ids['a1'], self.ids['a5']
        resp = client.post(URL, data=self._form([a1, a5, a1, a5, a1]),
                           follow_redirects=True)
        self.assertIn('لـ 2 طلاب', resp.get_data(as_text=True))
        self.assertEqual(sorted(r.student_id for r in self._rows()), sorted([a1, a5]))

    # ── 7-9: security rejections create nothing ───────────────────────────────

    def test_foreign_school_student_rejects_whole_group_without_leaking(self):
        client = self._web()
        html = self._assert_rejected(
            client.post(URL, data=self._form([self.ids['a1'], self.ids['f1'],
                                              self.ids['a5']])),
            'بعض الطلاب المحددين غير موجودين')
        # The valid ones are restored; the foreign student is never rendered.
        self.assertIn(self._code('1'), html)
        self.assertIn(self._code('5'), html)
        self.assertNotIn(f'GLb{self.sfx}-1', html)
        self.assertNotIn('طالب b 1', html)
        # Alone, too (single-student message preserved).
        self._assert_rejected(
            client.post(URL, data=self._form(section=None, grade_id='', stage='',
                                             student_id=str(self.ids['f1']))),
            'الطالب غير موجود أو لا ينتمي لهذه المدرسة.')

    def test_unknown_malformed_inactive_and_other_year_ids_create_nothing(self):
        client = self._web()
        a1 = self.ids['a1']
        for bad in (['999999999'], ['abc'], ['-5'], ['0'], ['²'], ['99999999999'],
                    [str(self.ids['ax'])], [str(self.ids['ay'])]):
            with self.subTest(bad=bad):
                resp = client.post(URL, data=self._form([str(a1), *bad]))
                self.assertEqual(resp.status_code, 422)
                self.assertEqual(self._rows(), [])
                self.assertEqual(self._notifications(), [])

    def test_building_restricted_inaccessible_student_rejects_whole_group(self):
        with self.app.app_context():
            sid = self.ids['school_a']
            School.query.filter_by(id=sid).update({'enable_buildings': True})
            b1 = SchoolBuilding(school_id=sid, name=f'B1 {self.sfx}')
            b2 = SchoolBuilding(school_id=sid, name=f'B2 {self.sfx}')
            db.session.add_all([b1, b2])
            db.session.flush()
            for keys, bid in ((('a1', 'a2', 'a5'), b1.id), (('a3', 'a4', 'a6'), b2.id)):
                Student.query.execution_options(**OPTS).filter(
                    Student.id.in_([self.ids[k] for k in keys])).update(
                    {'building_id': bid}, synchronize_session=False)
            db.session.add(UserBuildingAccess(school_id=sid, building_id=b1.id,
                                              user_id=self.ids['admin_id_a']))
            db.session.commit()
        client = self._web()
        data = client.get(SECTION_URL, query_string={'section_id': self.ids['s1_a']}).get_json()
        self.assertEqual(sorted(s['id'] for s in data),
                         sorted([self.ids['a1'], self.ids['a2']]))
        # a6 (other grade, building 2) inside an otherwise valid cross-grade group.
        self._assert_rejected(
            client.post(URL, data=self._form([self.ids['a1'], self.ids['a5'],
                                              self.ids['a6']])),
            'بعض الطلاب المحددين غير موجودين')
        resp = client.post(URL, data=self._form([self.ids['a1'], self.ids['a5']]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(self._rows()), 2)

    # ── 10: single student unchanged ──────────────────────────────────────────

    def test_single_student_legacy_field_creates_one_row(self):
        client = self._web()
        resp = client.post(URL, data=self._form(student_id=str(self.ids['a1'])),
                           follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('تم إضافة طلب الإجازة بنجاح.', resp.get_data(as_text=True))
        self.assertEqual([r.student_id for r in self._rows()], [self.ids['a1']])

    def test_single_student_via_student_ids_and_search_mode(self):
        client = self._web()
        client.post(URL, data=self._form([self.ids['a1']]))
        client.post(URL, data=self._form([self.ids['ao']], section=None,
                                         grade_id='', stage=''))
        self.assertEqual(sorted(r.student_id for r in self._rows()),
                         sorted([self.ids['a1'], self.ids['ao']]))

    def test_single_student_keeps_attachment(self):
        client = self._web()
        with mock.patch('app.utils.helpers.save_uploaded_file',
                        return_value='uploads/leave_requests/glv.pdf') as save:
            resp = client.post(URL, content_type='multipart/form-data',
                               data={**self._form([self.ids['a1']]),
                                     'attachment': (io.BytesIO(b'%PDF-1.4'), 'note.pdf')})
        self.assertEqual(resp.status_code, 302)
        save.assert_called_once()
        self.assertEqual([r.attachment_path for r in self._rows()],
                         ['uploads/leave_requests/glv.pdf'])

    # ── 11: group attachment still rejected ───────────────────────────────────

    def test_group_refuses_attachment_before_upload(self):
        client = self._web()
        with mock.patch('app.utils.helpers.save_uploaded_file') as save:
            resp = client.post(URL, content_type='multipart/form-data',
                               data={**self._form([self.ids['a1'], self.ids['a5']]),
                                     'attachment': (io.BytesIO(b'%PDF-1.4'), 'n.pdf')})
        self._assert_rejected(resp, 'لا يمكن إرفاق ملف مع إجازة لأكثر من طالب.')
        save.assert_not_called()

    # ── 12: field validation unchanged; selection restored across sections ───

    def test_existing_leave_validation_applies_and_restores_selection(self):
        client = self._web()
        ids = [self.ids['a1'], self.ids['ao'], self.ids['a5']]
        for over, msg in (({'to_date': '2026-10-19'}, 'تاريخ النهاية يجب أن يكون بعد تاريخ البداية.'),
                          ({'leave_type': 'holiday'}, 'نوع الإجازة غير صالح.'),
                          ({'status': 'done'}, 'حالة الطلب غير صالحة.'),
                          ({'from_date': ''}, 'تاريخ البداية مطلوب.')):
            with self.subTest(over=over):
                html = self._assert_rejected(client.post(URL, data=self._form(ids, **over)),
                                             msg)
                for code in ('1', 'o', '5'):        # three sections, two grades
                    self.assertIn(self._code(code), html)
        self._assert_rejected(client.post(URL, data=self._form([])), 'يرجى اختيار الطالب.')

    # ── picker endpoints use the same eligibility rule ────────────────────────

    def test_picker_endpoints_scoped(self):
        client = self._web()
        data = client.get(SECTION_URL, query_string={'section_id': self.ids['s1_a']}).get_json()
        self.assertEqual(sorted(s['id'] for s in data),
                         sorted(self.ids[k] for k in ('a1', 'a2', 'a3', 'a4')))
        for sec in (self.ids['s1_b'], self.ids['old_a']):   # foreign / last year
            self.assertEqual(client.get(SECTION_URL, query_string={
                'section_id': sec}).get_json(), [])
        def search(code):
            return [s['code'] for s in client.get(SEARCH_URL, query_string={
                'q': code}).get_json()]
        self.assertEqual(search(self._code('n')), [self._code('n')])   # no section
        self.assertEqual(search(self._code('5')), [self._code('5')])   # other grade
        self.assertEqual(search(self._code('y')), [])                  # last year
        self.assertEqual(search(self._code('x')), [])                  # inactive
        self.assertEqual(search(f'GLb{self.sfx}-1'), [])               # other school

    # ── approved group → attendance + notifications ───────────────────────────

    def test_approved_group_syncs_attendance_and_notifies_own_school_parents(self):
        client = self._web()
        ids = [self.ids['a1'], self.ids['a2'], self.ids['a3'], self.ids['a5']]
        resp = client.post(URL, data=self._form(ids, status='approved'))
        self.assertEqual(resp.status_code, 302)
        rows = self._rows()
        self.assertTrue(all(r.reviewed_by == self.ids['admin_id_a'] and r.reviewed_at
                            for r in rows))
        with self.app.app_context():
            att = (StudentAttendance.query.execution_options(**OPTS)
                   .filter(StudentAttendance.school_id == self.ids['school_a'],
                           StudentAttendance.status == 'on_leave').all())
            self.assertEqual(sorted((a.student_id, a.date.day) for a in att),
                             sorted((s, d) for s in ids for d in (20, 21, 22)))
        notes = self._notifications()
        # parent_a: one per linked child (a1, a2); parent_b (school B) none.
        self.assertEqual(sorted(n.body for n in notes),
                         sorted(f'أضافت الإدارة طلب إجازة للطالب طالب a {c}.'
                                for c in ('1', '2')))
        self.assertTrue(all(n.target_user_id == self.ids['parent_a']
                            and n.school_id == self.ids['school_a']
                            and n.ntype == 'parent_request' for n in notes))

    # ── performance: statements do not scale with the group size ──────────────

    def test_statement_count_independent_of_group_size(self):
        client = self._web()
        big = [self.ids[f'ap{i:02d}'] for i in range(15)]
        with self.app.app_context():
            engine = db.engine

        def count(ids):
            n = [0]

            def on_exec(*_a, **_k):
                n[0] += 1
            event.listen(engine, 'before_cursor_execute', on_exec)
            try:
                resp = client.post(URL, data=self._form(ids, section='big'))
            finally:
                event.remove(engine, 'before_cursor_execute', on_exec)
            self.assertEqual(resp.status_code, 302)
            return n[0]

        count(big[:3])                       # warm-up
        small, large = count(big[3:6]), count(big[6:15])
        self.assertEqual(small, large, f'3 students={small} stmts, 9 students={large}')
        self.assertEqual(len(self._rows()), 15)

    # ── 4-6: JS behaviour of the real rendered page ───────────────────────────

    @unittest.skipUnless(shutil.which('node'), 'node not available')
    def test_js_filters_keep_global_selection(self):
        client = self._web()
        html = client.get(URL).get_data(as_text=True)
        scripts = [s for s in re.findall(r'<script>(.*?)</script>', html, re.S)
                   if 'selectedChipsEl' in s]
        self.assertEqual(len(scripts), 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'leave_js_test.js')
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(_JS_HARNESS_HEAD + scripts[0] + _JS_HARNESS_TAIL)
            proc = subprocess.run(['node', path], capture_output=True, text=True,
                                  encoding='utf-8', timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        r = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(r['afterPick2'], ['101', '102'])
        self.assertEqual(r['afterSectionChange'], ['101', '102'])     # not cleared
        self.assertEqual(r['afterSelectAllB'], ['101', '102', '103', '104', '105'])
        self.assertEqual(r['afterSelectAllAgain'], r['afterSelectAllB'])   # dedup
        self.assertEqual(r['afterRepickDuplicate'], r['afterSelectAllB'])
        self.assertEqual(r['afterBackToAAll'], ['101', '102', '103', '104', '105', '106'])
        self.assertEqual(r['afterGradeAndStageChange'], r['afterBackToAAll'])
        self.assertEqual(r['countText'], '6')
        self.assertEqual(r['chips'], 6)
        self.assertEqual(r['hiddenInputs'], ['101', '102', '103', '104', '105', '106'])
        self.assertTrue(r['attachmentDisabledForGroup'])
        self.assertEqual(r['afterChipRemove'], ['102', '103', '104', '105', '106'])
        self.assertEqual(r['afterClearAll'], [])
        self.assertFalse(r['attachmentDisabledAfterClear'])
        self.assertEqual(r['confirmCalls'], 0)


# Minimal DOM for the page script: only what it touches. fetch is mocked with
# two sections (11 → 101,102,106 ; 12 → 103,104,105).
_JS_HARNESS_HEAD = r"""
'use strict';
function El(tag, id) {
  this.tagName = String(tag).toUpperCase(); this.id = id || '';
  this.children = []; this.style = {}; this.dataset = {}; this._l = {};
  this.disabled = false; this.textContent = ''; this._value = ''; this._sel = -1;
  this.className = ''; this.type = ''; this.name = ''; this.attrs = {};
  this.classList = { add: function () {}, remove: function () {} };
}
El.prototype.appendChild = function (c) {
  this.children.push(c);
  if (this.tagName === 'SELECT' && this._sel === -1) this._sel = 0;
  return c;
};
El.prototype.setAttribute = function (k, v) { this.attrs[k] = v; };
El.prototype.focus = function () {};
El.prototype.addEventListener = function (t, fn) { (this._l[t] = this._l[t] || []).push(fn); };
El.prototype.fire = function (t) {
  var ev = { defaultPrevented: false, preventDefault: function () { this.defaultPrevented = true; } };
  (this._l[t] || []).forEach(function (fn) { fn.call(this, ev); }, this);
  return ev;
};
Object.defineProperty(El.prototype, 'innerHTML', {
  get: function () { return ''; },
  set: function (html) {
    this.children = []; this._sel = -1;
    var re = /<option value="([^"]*)">([^<]*)<\/option>/g, m;
    while ((m = re.exec(html))) {
      var o = new El('option'); o._value = m[1]; o.textContent = m[2]; this.appendChild(o);
    }
  }
});
Object.defineProperty(El.prototype, 'options', { get: function () { return this.children; } });
Object.defineProperty(El.prototype, 'value', {
  get: function () {
    if (this.tagName !== 'SELECT') return this._value;
    var o = this.children[this._sel]; return o ? o._value : '';
  },
  set: function (v) {
    v = String(v);
    if (this.tagName !== 'SELECT') { this._value = v; return; }
    this._sel = -1;
    for (var i = 0; i < this.children.length; i++) {
      if (this.children[i]._value === v) { this._sel = i; break; }
    }
  }
});
var IDS = ['stageSelect', 'gradeSelect', 'sectionSelect', 'searchWrapper',
  'studentSearchInput', 'searchDropdown', 'searchSelected', 'dropdownWrapper',
  'sectionStudentSelect', 'selectAllBtn', 'clearAllBtn', 'selectedCount',
  'selectedChips', 'selectedEmpty', 'selectedInputs', 'selectionError',
  'attachmentInput', 'attachmentGroupNote', 'adminLeaveForm'];
var REG = {};
IDS.forEach(function (id) {
  var tag = /Select$/.test(id) ? 'select' : 'div';
  REG[id] = new El(tag, id);
});
global.document = {
  getElementById: function (id) { return REG[id] || null; },
  createElement: function (tag) { return new El(tag); }
};
var confirmCalls = 0;
global.window = { confirm: function () { confirmCalls++; return true; } };
var STUDENTS = {
  '11': [{id: 101, name: 'A1', code: 'c101'}, {id: 102, name: 'A2', code: 'c102'},
         {id: 106, name: 'A3', code: 'c106'}],
  '12': [{id: 103, name: 'B1', code: 'c103'}, {id: 104, name: 'B2', code: 'c104'},
         {id: 105, name: 'B3', code: 'c105'}]
};
global.fetch = function (url) {
  var data = [];
  if (url.indexOf('sections-for-grade') !== -1) data = [{id: 11, name: 'A'}, {id: 12, name: 'B'}];
  var m = /section_id=(\d+)/.exec(url);
  if (m && url.indexOf('students-for-section') !== -1) data = STUDENTS[m[1]] || [];
  return Promise.resolve({ json: function () { return Promise.resolve(data); } });
};
REG.stageSelect.innerHTML = '<option value="">-</option><option value="ابتدائي">ابتدائي</option><option value="متوسط">متوسط</option>';
"""

_JS_HARNESS_TAIL = r"""
;(async function () {
  function flush() { return new Promise(function (r) { setImmediate(r); }); }
  async function settle() { for (var i = 0; i < 5; i++) await flush(); }
  function sel() { return REG.selectedInputs.children.map(function (c) { return c.value; }); }
  function setFire(id, v) { REG[id].value = v; REG[id].fire('change'); }
  var out = {};

  setFire('stageSelect', 'ابتدائي');
  setFire('gradeSelect', REG.gradeSelect.options[1].value);
  await settle();
  setFire('sectionSelect', '11'); await settle();
  setFire('sectionStudentSelect', '101');
  setFire('sectionStudentSelect', '102');
  out.afterPick2 = sel();

  setFire('sectionSelect', '12'); await settle();
  out.afterSectionChange = sel();
  REG.selectAllBtn.fire('click');
  out.afterSelectAllB = sel();
  REG.selectAllBtn.fire('click');
  out.afterSelectAllAgain = sel();
  setFire('sectionStudentSelect', '103');
  out.afterRepickDuplicate = sel();

  setFire('sectionSelect', '11'); await settle();
  REG.selectAllBtn.fire('click');
  out.afterBackToAAll = sel();

  setFire('gradeSelect', ''); await settle();
  setFire('stageSelect', 'متوسط'); await settle();
  setFire('stageSelect', ''); await settle();
  out.afterGradeAndStageChange = sel();
  out.countText = String(REG.selectedCount.textContent);
  out.chips = REG.selectedChips.children.length;
  out.hiddenInputs = sel();
  out.attachmentDisabledForGroup = REG.attachmentInput.disabled;

  REG.selectedChips.children[0].children[1].fire('click');
  out.afterChipRemove = sel();
  REG.clearAllBtn.fire('click');
  out.afterClearAll = sel();
  out.attachmentDisabledAfterClear = REG.attachmentInput.disabled;
  out.confirmCalls = confirmCalls;
  console.log(JSON.stringify(out));
})().catch(function (e) { console.error(e); process.exit(1); });
"""


if __name__ == '__main__':
    unittest.main()
