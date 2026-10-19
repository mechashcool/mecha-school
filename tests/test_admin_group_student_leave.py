"""
Admin "Add Student Leave" — multi-student (group) leave within one section.

POST /admin/leave-requests/create now accepts ``student_ids`` (one value per
selected student) and creates ONE ordinary LeaveRequest row per student with
identical leave data, in a single commit. The legacy single ``student_id``
field (direct-search mode) keeps working unchanged.

Proves:
  * one student → one row, legacy field + single-student message preserved;
  * three students → exactly three rows with identical leave data;
  * duplicate ids are de-duplicated (no duplicate rows);
  * a foreign-school / unknown / malformed / inactive / other-section id rejects
    the whole submission and creates nothing (no partial creation);
  * a group requires a selected section (no cross-section group);
  * existing leave validation (dates, type, status) still applies to groups;
  * a group refuses an attachment BEFORE any upload; single keeps attachments;
  * the section student list ("select all" source) is active + same-section +
    same-school only; building-restricted users are narrowed on list and POST;
  * approved group leave → per-student on_leave attendance (existing service);
  * parent notifications: one per (linked parent, student), never to a parent
    of another school;
  * statement count does not grow with the number of selected students.
"""
import io
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
        db.session.add(year)
        db.session.flush()
        grade = Grade(name=f'G{key}{s}', stage='ابتدائي', school_id=school.id,
                      academic_year_id=year.id)
        db.session.add(grade)
        db.session.flush()
        secs = {}
        for name in ('s1', 's2', 'big'):
            sec = Section(name=f'{name}-{key}', grade_id=grade.id, school_id=school.id,
                          academic_year_id=year.id)
            db.session.add(sec)
            db.session.flush()
            secs[name] = sec.id
        admin = User(username=f'gladm{key}_{s}', email=f'gladm{key}_{s}@t.test',
                     full_name=f'adm {key}', role_id=self.admin_role,
                     school_id=school.id, is_active=True)
        admin.set_password(PASSWORD)
        db.session.add(admin)
        db.session.flush()

        def student(code, sec, status='active'):
            st = Student(student_id=f'GL{key}{s}-{code}', full_name=f'طالب {key} {code}',
                         school_id=school.id, academic_year_id=year.id,
                         section_id=secs[sec], status=status)
            db.session.add(st)
            db.session.flush()
            self.ids[f'{key}{code}' if key == 'a' else f'f{code}'] = st.id

        if key == 'a':
            for code in ('1', '2', '3', '4'):
                student(code, 's1')
            student('x', 's1', status='archived')      # inactive, same section
            student('o', 's2')                          # other section
            for i in range(15):                         # statement-count probe set
                student(f'p{i:02d}', 'big')
        else:
            student('1', 's1')                          # foreign-school student
        self.ids.update({f'school_{key}': school.id, f'year_{key}': year.id,
                         f'grade_{key}': grade.id, f'admin_{key}': admin.username,
                         f'admin_id_{key}': admin.id})
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
        data = {
            'stage': 'ابتدائي',
            'grade_id': str(self.ids['grade_a']),
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

    def _assert_rejected(self, resp, *snippets):
        self.assertEqual(resp.status_code, 422)
        html = resp.get_data(as_text=True)
        for snippet in snippets:
            self.assertIn(snippet, html)
        self.assertEqual(self._rows(), [], 'rejected submission created rows')

    # ── 1: single student unchanged ───────────────────────────────────────────

    def test_single_student_legacy_field_creates_one_row(self):
        client = self._web()
        resp = client.post(URL, data=self._form(student_id=str(self.ids['a1'])),
                           follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('تم إضافة طلب الإجازة بنجاح.', resp.get_data(as_text=True))
        rows = self._rows()
        self.assertEqual([r.student_id for r in rows], [self.ids['a1']])

    def test_single_student_via_student_ids_and_search_mode(self):
        client = self._web()
        client.post(URL, data=self._form([self.ids['a1']]))
        # Direct-search mode: no section, single legacy field.
        client.post(URL, data=self._form(section=None, grade_id='', stage='',
                                         student_id=str(self.ids['ao'])))
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

    # ── 2-3: group creates one identical row per student ──────────────────────

    def test_three_students_create_three_identical_rows(self):
        client = self._web()
        ids = [self.ids['a1'], self.ids['a2'], self.ids['a3']]
        resp = client.post(URL, data=self._form(ids), follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('تمت إضافة الإجازة بنجاح لـ 3 طلاب.', resp.get_data(as_text=True))
        rows = self._rows()
        self.assertEqual(sorted(r.student_id for r in rows), sorted(ids))
        shared = {(r.school_id, r.academic_year_id, r.leave_type, r.from_date, r.to_date,
                   r.notes, r.status, r.source, r.parent_id, r.created_by_user_id,
                   r.attachment_path, r.reviewed_by) for r in rows}
        self.assertEqual(shared, {(self.ids['school_a'], self.ids['year_a'], 'sick',
                                   date(2026, 10, 20), date(2026, 10, 22), 'رحلة مدرسية',
                                   'pending', 'admin', None, self.ids['admin_id_a'],
                                   None, None)})

    # ── 4: duplicates ─────────────────────────────────────────────────────────

    def test_duplicate_ids_are_deduplicated(self):
        client = self._web()
        a1, a2 = self.ids['a1'], self.ids['a2']
        resp = client.post(URL, data=self._form([a1, a2, a1, a2, a1]),
                           follow_redirects=True)
        self.assertIn('لـ 2 طلاب', resp.get_data(as_text=True))
        self.assertEqual(sorted(r.student_id for r in self._rows()), sorted([a1, a2]))

    # ── 5-6: security rejections create nothing ───────────────────────────────

    def test_foreign_school_student_rejects_whole_group(self):
        client = self._web()
        resp = client.post(URL, data=self._form([self.ids['a1'], self.ids['f1'],
                                                 self.ids['a2']]))
        self._assert_rejected(resp, 'بعض الطلاب المحددين غير موجودين')
        # Alone, too (single-student message preserved).
        resp = client.post(URL, data=self._form(section=None, grade_id='', stage='',
                                                student_id=str(self.ids['f1'])))
        self._assert_rejected(resp, 'الطالب غير موجود أو لا ينتمي لهذه المدرسة.')

    def test_unknown_malformed_and_inactive_ids_create_nothing(self):
        client = self._web()
        a1 = self.ids['a1']
        for bad in (['999999999'], ['abc'], ['-5'], ['0'], ['²'], ['99999999999'],
                    [str(self.ids['ax'])]):
            with self.subTest(bad=bad):
                resp = client.post(URL, data=self._form([str(a1), *bad]))
                self.assertEqual(resp.status_code, 422)
                self.assertEqual(self._rows(), [])

    def test_group_cannot_cross_sections(self):
        client = self._web()
        # A student of another section of the same school.
        resp = client.post(URL, data=self._form([self.ids['a1'], self.ids['ao']]))
        self._assert_rejected(resp, 'بعض الطلاب المحددين لا ينتمون للشعبة المحددة.')
        # A group without a selected section.
        resp = client.post(URL, data=self._form([self.ids['a1'], self.ids['ao']],
                                                section=None, grade_id='', stage=''))
        self._assert_rejected(resp, 'الإجازة لأكثر من طالب تتطلب اختيار الشعبة.')
        # Another school's section id.
        resp = client.post(URL, data=self._form([self.ids['a1'], self.ids['a2']],
                                                section_id=str(self.ids['s1_b'])))
        self._assert_rejected(resp, 'الشعبة لا تنتمي للصف المحدد.')

    def test_group_refuses_attachment_before_upload(self):
        client = self._web()
        with mock.patch('app.utils.helpers.save_uploaded_file') as save:
            resp = client.post(URL, content_type='multipart/form-data',
                               data={**self._form([self.ids['a1'], self.ids['a2']]),
                                     'attachment': (io.BytesIO(b'%PDF-1.4'), 'n.pdf')})
        self._assert_rejected(resp, 'لا يمكن إرفاق ملف مع إجازة لأكثر من طالب.')
        save.assert_not_called()

    # ── 7: "select all" source list is section-scoped ─────────────────────────

    def test_section_student_list_is_active_same_section_same_school(self):
        client = self._web()
        data = client.get(SECTION_URL, query_string={'section_id': self.ids['s1_a']}).get_json()
        self.assertEqual(sorted(s['id'] for s in data),
                         sorted(self.ids[k] for k in ('a1', 'a2', 'a3', 'a4')))
        self.assertEqual(client.get(SECTION_URL, query_string={
            'section_id': self.ids['s1_b']}).get_json(), [])
        # Posting the whole list ("select all") succeeds for exactly those.
        client.post(URL, data=self._form([s['id'] for s in data]))
        self.assertEqual(len(self._rows()), 4)

    def test_building_restricted_user_is_narrowed(self):
        with self.app.app_context():
            sid = self.ids['school_a']
            School.query.filter_by(id=sid).update({'enable_buildings': True})
            b1 = SchoolBuilding(school_id=sid, name=f'B1 {self.sfx}')
            b2 = SchoolBuilding(school_id=sid, name=f'B2 {self.sfx}')
            db.session.add_all([b1, b2])
            db.session.flush()
            Student.query.execution_options(**OPTS).filter(
                Student.id.in_([self.ids['a1'], self.ids['a2']])).update(
                {'building_id': b1.id}, synchronize_session=False)
            Student.query.execution_options(**OPTS).filter(
                Student.id.in_([self.ids['a3'], self.ids['a4']])).update(
                {'building_id': b2.id}, synchronize_session=False)
            db.session.add(UserBuildingAccess(school_id=sid, building_id=b1.id,
                                              user_id=self.ids['admin_id_a']))
            db.session.commit()
        client = self._web()
        data = client.get(SECTION_URL, query_string={'section_id': self.ids['s1_a']}).get_json()
        self.assertEqual(sorted(s['id'] for s in data),
                         sorted([self.ids['a1'], self.ids['a2']]))
        resp = client.post(URL, data=self._form([self.ids['a1'], self.ids['a3']]))
        self._assert_rejected(resp, 'بعض الطلاب المحددين غير موجودين')

    # ── 8: existing validation still applies ──────────────────────────────────

    def test_existing_leave_validation_applies_to_group(self):
        client = self._web()
        ids = [self.ids['a1'], self.ids['a2']]
        for over, msg in (({'to_date': '2026-10-19'}, 'تاريخ النهاية يجب أن يكون بعد تاريخ البداية.'),
                          ({'leave_type': 'holiday'}, 'نوع الإجازة غير صالح.'),
                          ({'status': 'done'}, 'حالة الطلب غير صالحة.'),
                          ({'from_date': ''}, 'تاريخ البداية مطلوب.')):
            with self.subTest(over=over):
                self._assert_rejected(client.post(URL, data=self._form(ids, **over)), msg)
        self._assert_rejected(client.post(URL, data=self._form([])), 'يرجى اختيار الطالب.')

    # ── approved group → attendance + notifications ───────────────────────────

    def test_approved_group_syncs_attendance_and_notifies_own_school_parents(self):
        client = self._web()
        ids = [self.ids['a1'], self.ids['a2'], self.ids['a3']]
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
            notes = (Notification.query.execution_options(**OPTS)
                     .filter(Notification.target_user_id.in_(
                         [self.ids['parent_a'], self.ids['parent_b']])).all())
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


if __name__ == '__main__':
    unittest.main()
