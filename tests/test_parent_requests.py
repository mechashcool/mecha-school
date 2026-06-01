import unittest
from datetime import date
from uuid import uuid4

from flask_login import login_user, logout_user
from werkzeug.exceptions import NotFound

from app import create_app
from app.models import (
    db, AcademicYear, Complaint, Grade, LeaveRequest, Notification, Role,
    School, Section, Student, User,
)


class ParentRequestsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.created = {}

        with self.app.app_context():
            parent_role = Role.query.filter_by(name='parent').first()
            manager_role = Role.query.filter_by(name='school_admin').first()
            self.assertIsNotNone(parent_role)
            self.assertIsNotNone(manager_role)

            school_a = School(
                school_name=f'Parent Request A {self.suffix}',
                code=f'PRA{self.suffix[:7]}',
                capacity=0,
                is_active=True,
            )
            school_b = School(
                school_name=f'Parent Request B {self.suffix}',
                code=f'PRB{self.suffix[:7]}',
                capacity=0,
                is_active=True,
            )
            db.session.add_all([school_a, school_b])
            db.session.flush()

            year_a = AcademicYear(
                school_id=school_a.id,
                name=f'Year A {self.suffix}',
                start_date=date(2025, 8, 1),
                end_date=date(2026, 6, 30),
                is_current=True,
            )
            year_b = AcademicYear(
                school_id=school_b.id,
                name=f'Year B {self.suffix}',
                start_date=date(2025, 8, 1),
                end_date=date(2026, 6, 30),
                is_current=True,
            )
            db.session.add_all([year_a, year_b])
            db.session.flush()

            grade_a = Grade(school_id=school_a.id, academic_year_id=year_a.id, name=f'GA {self.suffix}')
            grade_b = Grade(school_id=school_b.id, academic_year_id=year_b.id, name=f'GB {self.suffix}')
            db.session.add_all([grade_a, grade_b])
            db.session.flush()

            section_a = Section(
                school_id=school_a.id,
                academic_year_id=year_a.id,
                grade_id=grade_a.id,
                name=f'A{self.suffix[:4]}',
                capacity=30,
            )
            section_b = Section(
                school_id=school_b.id,
                academic_year_id=year_b.id,
                grade_id=grade_b.id,
                name=f'B{self.suffix[:4]}',
                capacity=30,
            )
            db.session.add_all([section_a, section_b])
            db.session.flush()

            student_a = Student(
                student_id=f'PRA-S-{self.suffix}',
                full_name=f'Own Student {self.suffix}',
                date_of_birth=date(2015, 1, 1),
                gender='male',
                school_id=school_a.id,
                academic_year_id=year_a.id,
                section_id=section_a.id,
                status='active',
            )
            student_b = Student(
                student_id=f'PRB-S-{self.suffix}',
                full_name=f'Other Student {self.suffix}',
                date_of_birth=date(2015, 2, 1),
                gender='female',
                school_id=school_b.id,
                academic_year_id=year_b.id,
                section_id=section_b.id,
                status='active',
            )
            db.session.add_all([student_a, student_b])
            db.session.flush()

            parent = User(
                username=f'parent_req_{self.suffix}',
                email=f'parent_req_{self.suffix}@example.test',
                full_name=f'Parent Request {self.suffix}',
                role_id=parent_role.id,
                school_id=school_a.id,
                is_active=True,
            )
            parent_b = User(
                username=f'parent_req_b_{self.suffix}',
                email=f'parent_req_b_{self.suffix}@example.test',
                full_name=f'Parent Request B {self.suffix}',
                role_id=parent_role.id,
                school_id=school_b.id,
                is_active=True,
            )
            manager_a = User(
                username=f'manager_req_a_{self.suffix}',
                email=f'manager_req_a_{self.suffix}@example.test',
                full_name=f'Manager A {self.suffix}',
                role_id=manager_role.id,
                school_id=school_a.id,
                is_active=True,
            )
            manager_b = User(
                username=f'manager_req_b_{self.suffix}',
                email=f'manager_req_b_{self.suffix}@example.test',
                full_name=f'Manager B {self.suffix}',
                role_id=manager_role.id,
                school_id=school_b.id,
                is_active=True,
            )
            for user in [parent, parent_b, manager_a, manager_b]:
                user.set_password('Password123')
            db.session.add_all([parent, parent_b, manager_a, manager_b])
            db.session.flush()
            parent.children = [student_a]
            parent_b.children = [student_b]

            db.session.commit()
            self.created = {
                'school_a_id': school_a.id,
                'school_b_id': school_b.id,
                'year_a_id': year_a.id,
                'year_b_id': year_b.id,
                'grade_a_id': grade_a.id,
                'grade_b_id': grade_b.id,
                'section_a_id': section_a.id,
                'section_b_id': section_b.id,
                'student_a_id': student_a.id,
                'student_b_id': student_b.id,
                'parent_id': parent.id,
                'parent_b_id': parent_b.id,
                'manager_a_id': manager_a.id,
                'manager_b_id': manager_b.id,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            ids = self.created
            parent = db.session.get(
                User, ids.get('parent_id'),
                execution_options={'bypass_tenant_scope': True},
            )
            if parent:
                parent.children = []
                db.session.flush()
            parent_b = db.session.get(
                User, ids.get('parent_b_id'),
                execution_options={'bypass_tenant_scope': True},
            )
            if parent_b:
                parent_b.children = []
                db.session.flush()

            for model in [Notification, Complaint, LeaveRequest]:
                (model.query
                 .execution_options(bypass_tenant_scope=True, include_all_years=True)
                 .filter(model.school_id.in_([ids.get('school_a_id'), ids.get('school_b_id')]))
                 .delete(synchronize_session=False))
            db.session.flush()

            for model, keys in [
                (User, ['parent_id', 'parent_b_id', 'manager_a_id', 'manager_b_id']),
                (Student, ['student_a_id', 'student_b_id']),
                (Section, ['section_a_id', 'section_b_id']),
                (Grade, ['grade_a_id', 'grade_b_id']),
                (AcademicYear, ['year_a_id', 'year_b_id']),
                (School, ['school_a_id', 'school_b_id']),
            ]:
                for key in keys:
                    obj = db.session.get(
                        model, ids.get(key),
                        execution_options={'bypass_tenant_scope': True},
                    )
                    if obj is not None:
                        db.session.delete(obj)
            db.session.commit()
            db.session.remove()

    def _run_before_request(self):
        for fn in self.app.before_request_funcs.get(None, []):
            rv = fn()
            if rv is not None:
                return rv
        return None

    def test_parent_creates_complaint_for_own_child_only(self):
        from app.blueprints.parent import create_complaint

        ids = self.created
        with self.app.test_request_context(
            '/parent/complaints/new',
            method='POST',
            data={
                'student_id': str(ids['student_a_id']),
                'title': 'طلب متابعة',
                'complaint_type': 'academic',
                'details': 'تفاصيل الطلب',
            },
        ):
            parent = db.session.get(User, ids['parent_id'], execution_options={'bypass_tenant_scope': True})
            login_user(parent)
            self._run_before_request()
            response = create_complaint()
            self.assertEqual(response.status_code, 302)
            logout_user()

        with self.app.app_context():
            complaint = (Complaint.query
                         .execution_options(bypass_tenant_scope=True, include_all_years=True)
                         .filter_by(parent_id=ids['parent_id'])
                         .first())
        self.assertIsNotNone(complaint)
        self.assertEqual(complaint.student_id, ids['student_a_id'])
        self.assertEqual(complaint.school_id, ids['school_a_id'])

        with self.app.test_request_context(
            '/parent/complaints/new',
            method='POST',
            data={
                'student_id': str(ids['student_b_id']),
                'title': 'طلب غير مسموح',
                'complaint_type': 'academic',
                'details': 'تفاصيل',
            },
        ):
            parent = db.session.get(User, ids['parent_id'], execution_options={'bypass_tenant_scope': True})
            login_user(parent)
            self._run_before_request()
            with self.assertRaises(NotFound):
                create_complaint()
            logout_user()

    def test_leave_request_is_parent_owned_and_school_scoped(self):
        from app.blueprints.parent import create_leave_request

        ids = self.created
        with self.app.test_request_context(
            '/parent/leave-requests/new',
            method='POST',
            data={
                'student_id': str(ids['student_a_id']),
                'leave_type': 'sick',
                'from_date': '2026-01-10',
                'to_date': '2026-01-12',
                'notes': 'ملاحظة',
            },
        ):
            parent = db.session.get(User, ids['parent_id'], execution_options={'bypass_tenant_scope': True})
            login_user(parent)
            self._run_before_request()
            response = create_leave_request()
            self.assertEqual(response.status_code, 302)
            logout_user()

        with self.app.app_context():
            leave_request = (LeaveRequest.query
                             .execution_options(bypass_tenant_scope=True, include_all_years=True)
                             .filter_by(parent_id=ids['parent_id'])
                             .first())
        self.assertIsNotNone(leave_request)
        self.assertEqual(leave_request.student_id, ids['student_a_id'])
        self.assertEqual(leave_request.school_id, ids['school_a_id'])

    # ── Mobile API helpers ────────────────────────────────────────────────────

    def _token_for(self, user_id):
        with self.app.app_context():
            from app.blueprints.mobile_api.utils import encode_token
            user = db.session.get(User, user_id, execution_options={'bypass_tenant_scope': True})
            return encode_token(user)

    def _api(self, method, path, token, **kwargs):
        client = self.app.test_client()
        fn = getattr(client, method)
        return fn(
            f'/api/mobile/v1{path}',
            headers={'Authorization': f'Bearer {token}'},
            content_type='application/json',
            **kwargs,
        )

    # ── Mobile Leave Request tests ────────────────────────────────────────────

    def test_mobile_parent_creates_leave_request(self):
        ids   = self.created
        token = self._token_for(ids['parent_id'])

        resp = self._api('post',
                         f'/parent/children/{ids["student_a_id"]}/leave-requests',
                         token,
                         json={'leave_type': 'sick', 'start_date': '2026-06-10',
                               'end_date': '2026-06-12', 'reason': 'مريض'})
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['message'], 'leave_request_created')
        req = data['request']
        self.assertEqual(req['leave_type'], 'sick')
        self.assertEqual(req['status'], 'pending')
        self.assertEqual(req['student_id'], ids['student_a_id'])

        # Verify persisted
        req_id = req['id']
        resp2 = self._api('get',
                          f'/parent/children/{ids["student_a_id"]}/leave-requests/{req_id}',
                          token)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.get_json()['request']['id'], req_id)

        # List includes the new record
        resp3 = self._api('get',
                          f'/parent/children/{ids["student_a_id"]}/leave-requests',
                          token)
        self.assertEqual(resp3.status_code, 200)
        ids_in_list = [r['id'] for r in resp3.get_json()['requests']]
        self.assertIn(req_id, ids_in_list)

    def test_mobile_parent_cannot_create_leave_request_for_other_child(self):
        ids   = self.created
        token = self._token_for(ids['parent_id'])
        resp  = self._api('post',
                          f'/parent/children/{ids["student_b_id"]}/leave-requests',
                          token,
                          json={'leave_type': 'sick', 'start_date': '2026-06-10',
                                'end_date': '2026-06-12', 'reason': 'test'})
        self.assertEqual(resp.status_code, 404)

    def test_mobile_invalid_dates_return_400(self):
        ids   = self.created
        token = self._token_for(ids['parent_id'])

        resp = self._api('post',
                         f'/parent/children/{ids["student_a_id"]}/leave-requests',
                         token,
                         json={'leave_type': 'sick', 'start_date': 'bad-date',
                               'end_date': '2026-06-12'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('invalid_date_format', resp.get_json()['error'])

        resp2 = self._api('post',
                          f'/parent/children/{ids["student_a_id"]}/leave-requests',
                          token,
                          json={'leave_type': 'sick', 'start_date': '2026-06-12',
                                'end_date': '2026-06-10'})
        self.assertEqual(resp2.status_code, 400)
        self.assertIn('end_date_before_start_date', resp2.get_json()['error'])

    def test_mobile_missing_leave_fields_return_400(self):
        ids   = self.created
        token = self._token_for(ids['parent_id'])

        resp = self._api('post',
                         f'/parent/children/{ids["student_a_id"]}/leave-requests',
                         token,
                         json={'start_date': '2026-06-10', 'end_date': '2026-06-12'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('leave_type', resp.get_json()['error'])

    def test_mobile_parent_cannot_cancel_approved_leave_request(self):
        ids = self.created
        with self.app.app_context():
            leave = LeaveRequest(
                parent_id=ids['parent_id'],
                student_id=ids['student_a_id'],
                school_id=ids['school_a_id'],
                academic_year_id=ids['year_a_id'],
                leave_type='sick',
                from_date=date(2026, 6, 1),
                to_date=date(2026, 6, 3),
                status='approved',
            )
            db.session.add(leave)
            db.session.commit()
            leave_id = leave.id

        token = self._token_for(ids['parent_id'])
        resp  = self._api('delete',
                          f'/parent/children/{ids["student_a_id"]}/leave-requests/{leave_id}',
                          token)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('cannot_cancel', resp.get_json()['error'])

    # ── Mobile Complaint tests ────────────────────────────────────────────────

    def test_mobile_parent_creates_complaint(self):
        ids   = self.created
        token = self._token_for(ids['parent_id'])

        resp = self._api('post',
                         f'/parent/children/{ids["student_a_id"]}/complaints',
                         token,
                         json={'category': 'academic',
                               'title':    'مشكلة في الواجبات',
                               'body':     'لم تظهر الواجبات بشكل صحيح.'})
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['message'], 'complaint_created')
        c = data['complaint']
        self.assertEqual(c['category'], 'academic')
        self.assertEqual(c['status'], 'new')
        self.assertEqual(c['student_id'], ids['student_a_id'])

        c_id = c['id']
        resp2 = self._api('get',
                          f'/parent/children/{ids["student_a_id"]}/complaints/{c_id}',
                          token)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.get_json()['complaint']['id'], c_id)

        resp3 = self._api('get',
                          f'/parent/children/{ids["student_a_id"]}/complaints',
                          token)
        self.assertEqual(resp3.status_code, 200)
        self.assertIn(c_id, [x['id'] for x in resp3.get_json()['complaints']])

    def test_mobile_parent_cannot_access_other_childs_complaint(self):
        ids   = self.created
        token = self._token_for(ids['parent_id'])
        resp  = self._api('post',
                          f'/parent/children/{ids["student_b_id"]}/complaints',
                          token,
                          json={'category': 'academic',
                                'title':    'test', 'body': 'test'})
        self.assertEqual(resp.status_code, 404)

    def test_mobile_missing_complaint_fields_return_400(self):
        ids   = self.created
        token = self._token_for(ids['parent_id'])

        resp = self._api('post',
                         f'/parent/children/{ids["student_a_id"]}/complaints',
                         token,
                         json={'category': 'academic', 'body': 'text only, no title'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('title', resp.get_json()['error'])

        resp2 = self._api('post',
                          f'/parent/children/{ids["student_a_id"]}/complaints',
                          token,
                          json={'category': 'bad_category', 'title': 'x', 'body': 'y'})
        self.assertEqual(resp2.status_code, 400)
        self.assertIn('invalid_category', resp2.get_json()['error'])

    def test_mobile_school_scoping_complaints(self):
        ids      = self.created
        token_a  = self._token_for(ids['parent_id'])
        token_b  = self._token_for(ids['parent_b_id'])

        # parent_b creates for their own child
        self._api('post',
                  f'/parent/children/{ids["student_b_id"]}/complaints',
                  token_b,
                  json={'category': 'financial', 'title': 'B complaint',
                        'body': 'school B details'})

        # parent_a cannot see parent_b's complaints on student_b
        resp = self._api('get',
                         f'/parent/children/{ids["student_b_id"]}/complaints',
                         token_a)
        self.assertEqual(resp.status_code, 404)

    def test_mobile_complaint_visible_in_admin_list_across_years(self):
        """A complaint filed under an older (non-current) academic year must still
        appear in the admin list when the school's current year is different.
        Regression guard for the year-scoped ORM filter on Complaint."""
        from app.blueprints.admin import complaints_list

        ids = self.created
        # Create an extra NON-current year and a complaint filed under it.
        # self.created only has year_a (current); old_year is extra.
        with self.app.app_context():
            old_year = AcademicYear(
                school_id=ids['school_a_id'],
                name=f'Old Complaint Year {self.suffix}',
                start_date=date(2024, 8, 1),
                end_date=date(2025, 6, 30),
                is_current=False,        # NOT the current year
            )
            db.session.add(old_year)
            db.session.flush()
            complaint = Complaint(
                parent_id=ids['parent_id'],
                student_id=ids['student_a_id'],
                school_id=ids['school_a_id'],
                academic_year_id=old_year.id,   # old year, not the session view year
                title=f'Cross-year Complaint {self.suffix}',
                complaint_type='academic',
                details='filed under old year',
                status='new',
            )
            db.session.add(complaint)
            db.session.commit()
            old_year_id = old_year.id
            complaint_id = complaint.id

        # Admin opens the complaints list.  _run_before_request sets the view year
        # to year_a (is_current=True).  Without include_all_years the ORM filter
        # would restrict to year_a and hide this complaint; with it, it must appear.
        with self.app.test_request_context('/admin/complaints'):
            manager = db.session.get(User, ids['manager_a_id'],
                                     execution_options={'bypass_tenant_scope': True})
            login_user(manager)
            self._run_before_request()   # g.tenant_scope_view_year_id = year_a (current)
            html = complaints_list()
            self.assertIn(f'Cross-year Complaint {self.suffix}', html,
                          'Complaint from old academic year must appear in admin list')
            logout_user()

        # Clean up the extra records this test created.
        with self.app.app_context():
            for cls, pk in [(Complaint, complaint_id), (AcademicYear, old_year_id)]:
                obj = db.session.get(cls, pk,
                                     execution_options={'bypass_tenant_scope': True})
                if obj is not None:
                    db.session.delete(obj)
            db.session.commit()

    def test_mobile_leave_request_visible_in_admin_list_across_years(self):
        """Same cross-year visibility test for leave requests."""
        from app.blueprints.admin import leave_requests_list

        ids = self.created
        with self.app.app_context():
            old_year = AcademicYear(
                school_id=ids['school_a_id'],
                name=f'Old LR Year {self.suffix}',
                start_date=date(2024, 8, 1),
                end_date=date(2025, 6, 30),
                is_current=False,
            )
            db.session.add(old_year)
            db.session.flush()
            leave = LeaveRequest(
                parent_id=ids['parent_id'],
                student_id=ids['student_a_id'],
                school_id=ids['school_a_id'],
                academic_year_id=old_year.id,
                leave_type='sick',
                from_date=date(2025, 1, 10),
                to_date=date(2025, 1, 12),
                notes=f'Cross-year leave note {self.suffix}',
                status='pending',
            )
            db.session.add(leave)
            db.session.commit()
            old_year_id = old_year.id
            leave_id = leave.id

        with self.app.test_request_context('/admin/leave-requests'):
            manager = db.session.get(User, ids['manager_a_id'],
                                     execution_options={'bypass_tenant_scope': True})
            login_user(manager)
            self._run_before_request()
            html = leave_requests_list()
            # student name is present in the rendered list row for the leave request
            self.assertIn(f'Own Student {self.suffix}', html,
                          'Leave request from old academic year must appear in admin list')
            logout_user()

        with self.app.app_context():
            for cls, pk in [(LeaveRequest, leave_id), (AcademicYear, old_year_id)]:
                obj = db.session.get(cls, pk,
                                     execution_options={'bypass_tenant_scope': True})
                if obj is not None:
                    db.session.delete(obj)
            db.session.commit()

    def test_mobile_no_auth_returns_401(self):
        ids  = self.created
        resp = self.app.test_client().get(
            f'/api/mobile/v1/parent/children/{ids["student_a_id"]}/complaints',
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 401)

    def test_mobile_complaint_admin_reply_visible_in_mobile(self):
        from app.blueprints.admin import complaint_detail
        from flask_login import login_user, logout_user

        ids = self.created
        with self.app.app_context():
            complaint = Complaint(
                parent_id=ids['parent_id'],
                student_id=ids['student_a_id'],
                school_id=ids['school_a_id'],
                academic_year_id=ids['year_a_id'],
                title='Mobile reply test',
                complaint_type='academic',
                details='details',
                status='new',
            )
            db.session.add(complaint)
            db.session.commit()
            complaint_id = complaint.id

        # Admin replies via web
        with self.app.test_request_context(
            f'/admin/complaints/{complaint_id}',
            method='POST',
            data={'status': 'replied', 'manager_reply': 'تمت المعالجة'},
        ):
            manager = db.session.get(User, ids['manager_a_id'],
                                     execution_options={'bypass_tenant_scope': True})
            login_user(manager)
            self._run_before_request()
            complaint_detail(complaint_id)
            logout_user()

        # Mobile API sees the updated status and reply
        token = self._token_for(ids['parent_id'])
        resp  = self._api('get',
                          f'/parent/children/{ids["student_a_id"]}/complaints/{complaint_id}',
                          token)
        self.assertEqual(resp.status_code, 200)
        c = resp.get_json()['complaint']
        self.assertEqual(c['status'], 'replied')
        self.assertEqual(c['admin_reply'], 'تمت المعالجة')

    def test_manager_sees_only_own_school_complaints_and_reply_notifies_parent(self):
        from app.blueprints.admin import complaint_detail, complaints_list

        ids = self.created
        with self.app.app_context():
            complaint_a = Complaint(
                parent_id=ids['parent_id'],
                student_id=ids['student_a_id'],
                school_id=ids['school_a_id'],
                academic_year_id=ids['year_a_id'],
                title=f'Visible Complaint {self.suffix}',
                complaint_type='academic',
                details='visible',
                status='new',
            )
            complaint_b = Complaint(
                parent_id=ids['parent_b_id'],
                student_id=ids['student_b_id'],
                school_id=ids['school_b_id'],
                academic_year_id=ids['year_b_id'],
                title=f'Hidden Complaint {self.suffix}',
                complaint_type='academic',
                details='hidden',
                status='new',
            )
            db.session.add_all([complaint_a, complaint_b])
            db.session.commit()
            complaint_a_id = complaint_a.id

        with self.app.test_request_context('/admin/complaints'):
            manager = db.session.get(User, ids['manager_a_id'], execution_options={'bypass_tenant_scope': True})
            login_user(manager)
            self._run_before_request()
            html = complaints_list()
            self.assertIn(f'Visible Complaint {self.suffix}', html)
            self.assertNotIn(f'Hidden Complaint {self.suffix}', html)
            logout_user()

        with self.app.test_request_context(
            f'/admin/complaints/{complaint_a_id}',
            method='POST',
            data={'status': 'replied', 'manager_reply': 'تمت المتابعة'},
        ):
            manager = db.session.get(User, ids['manager_a_id'], execution_options={'bypass_tenant_scope': True})
            login_user(manager)
            self._run_before_request()
            response = complaint_detail(complaint_a_id)
            self.assertEqual(response.status_code, 302)
            logout_user()

        with self.app.app_context():
            parent_notification = (Notification.query
                                   .execution_options(bypass_tenant_scope=True)
                                   .filter_by(target_user_id=ids['parent_id'],
                                              school_id=ids['school_a_id'])
                                   .first())
        self.assertIsNotNone(parent_notification)


if __name__ == '__main__':
    unittest.main()
