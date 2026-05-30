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
