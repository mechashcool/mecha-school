import unittest
from datetime import date, time
from uuid import uuid4

from flask import get_flashed_messages
from flask_login import login_user, logout_user

from app import create_app
from app.blueprints.schools import delete as delete_school
from app.models import (
    db, AcademicYear, Announcement, AnnouncementTarget, AuditLog, Device,
    Employee, EmployeeAttendance, EmployeeDocument, EmployeeEvaluation, Exam,
    ExamResult, Expense, ExpenseCategory, FeeInstallment, FeeRecord, FeeType,
    Grade, Notification, NotificationRead, PushNotification, Revenue,
    RevenueCategory, Role, SalaryRecord, Schedule, School, Section, Student,
    StudentAttendance, StudentDocument, StudentSuspension, Subject, User,
    parent_students, teacher_subjects,
)
from app.utils.school_cleanup import cleanup_school_cascade


class SchoolDemoCleanupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.created = {}

        with self.app.app_context():
            super_admin_role = Role.query.filter_by(name='super_admin').first()
            school_admin_role = Role.query.filter_by(name='school_admin').first()
            parent_role = Role.query.filter_by(name='parent').first()
            self.assertIsNotNone(super_admin_role)
            self.assertIsNotNone(school_admin_role)
            self.assertIsNotNone(parent_role)

            super_admin = User(
                username=f'demo_cleanup_sa_{self.suffix}',
                email=f'demo_cleanup_sa_{self.suffix}@example.test',
                full_name=f'Demo Cleanup Super Admin {self.suffix}',
                role_id=super_admin_role.id,
                school_id=None,
                is_active=True,
            )
            super_admin.set_password('Password123')
            db.session.add(super_admin)
            db.session.commit()

            self.created = {
                'super_admin_id': super_admin.id,
                'school_admin_role_id': school_admin_role.id,
                'parent_role_id': parent_role.id,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for key in ('demo_school_id', 'real_school_id'):
                school_id = self.created.get(key)
                if not school_id:
                    continue
                school = db.session.get(
                    School,
                    school_id,
                    execution_options={'bypass_tenant_scope': True},
                )
                if school is not None:
                    cleanup_school_cascade(school.id)

            admin = db.session.get(
                User,
                self.created.get('super_admin_id'),
                execution_options={'bypass_tenant_scope': True},
            )
            if admin is not None:
                db.session.delete(admin)

            db.session.commit()
            db.session.remove()

    def _run_before_request(self):
        for fn in self.app.before_request_funcs.get(None, []):
            rv = fn()
            if rv is not None:
                return rv
        return None

    def _login_super_admin(self):
        user = db.session.get(
            User,
            self.created['super_admin_id'],
            execution_options={'bypass_tenant_scope': True},
        )
        login_user(user)
        self._run_before_request()
        return user

    def _create_school_graph(self, school_name):
        admin_role_id = self.created['school_admin_role_id']
        parent_role_id = self.created['parent_role_id']

        school = School(
            school_name=school_name,
            code=f'DC{self.suffix[:8]}',
            capacity=0,
            is_active=True,
        )
        db.session.add(school)
        db.session.flush()

        year = AcademicYear(
            school_id=school.id,
            name=f'Cleanup Year {self.suffix}',
            start_date=date(2025, 8, 1),
            end_date=date(2026, 6, 30),
            is_current=True,
        )
        db.session.add(year)
        db.session.flush()

        grade = Grade(
            school_id=school.id,
            academic_year_id=year.id,
            name=f'Cleanup Grade {self.suffix}',
        )
        db.session.add(grade)
        db.session.flush()

        section = Section(
            school_id=school.id,
            academic_year_id=year.id,
            grade_id=grade.id,
            name=f'C{self.suffix[:4]}',
            capacity=30,
        )
        subject = Subject(
            school_id=school.id,
            academic_year_id=year.id,
            name=f'Cleanup Subject {self.suffix}',
            code=f'CS{self.suffix[:6]}',
        )
        db.session.add_all([section, subject])
        db.session.flush()

        manager = User(
            username=f'demo_cleanup_mgr_{self.suffix}',
            email=f'demo_cleanup_mgr_{self.suffix}@example.test',
            full_name=f'Demo Cleanup Manager {self.suffix}',
            role_id=admin_role_id,
            school_id=school.id,
            is_active=True,
        )
        parent = User(
            username=f'demo_cleanup_parent_{self.suffix}',
            email=f'demo_cleanup_parent_{self.suffix}@example.test',
            full_name=f'Demo Cleanup Parent {self.suffix}',
            role_id=parent_role_id,
            school_id=school.id,
            is_active=True,
        )
        teacher_user = User(
            username=f'demo_cleanup_teacher_{self.suffix}',
            email=f'demo_cleanup_teacher_{self.suffix}@example.test',
            full_name=f'Demo Cleanup Teacher {self.suffix}',
            role_id=admin_role_id,
            school_id=school.id,
            is_active=True,
        )
        for user in (manager, parent, teacher_user):
            user.set_password('Password123')
        db.session.add_all([manager, parent, teacher_user])
        db.session.flush()

        student = Student(
            student_id=f'DC-ST-{self.suffix}',
            full_name=f'Demo Cleanup Student {self.suffix}',
            date_of_birth=date(2015, 1, 1),
            gender='male',
            school_id=school.id,
            academic_year_id=year.id,
            section_id=section.id,
            status='active',
        )
        employee = Employee(
            employee_id=f'DC-EM-{self.suffix}',
            full_name=f'Demo Cleanup Employee {self.suffix}',
            job_title='Teacher',
            base_salary=1000,
            email=f'demo_cleanup_employee_{self.suffix}@example.test',
            user_id=teacher_user.id,
            school_id=school.id,
            status='active',
        )
        db.session.add_all([student, employee])
        db.session.flush()

        section.teacher_id = employee.id
        db.session.execute(parent_students.insert().values(
            user_id=parent.id,
            student_id=student.id,
            relation='guardian',
        ))
        db.session.execute(teacher_subjects.insert().values(
            employee_id=employee.id,
            subject_id=subject.id,
            section_id=section.id,
        ))

        device = Device(
            device_id=f'DC-DEV-{self.suffix}',
            api_key=f'DC-KEY-{self.suffix}',
            name=f'Demo Cleanup Device {self.suffix}',
            school_id=school.id,
            created_by=manager.id,
        )
        fee_type = FeeType(
            school_id=school.id,
            academic_year_id=year.id,
            name=f'Demo Fee {self.suffix}',
        )
        revenue_category = RevenueCategory(
            school_id=school.id,
            name=f'Demo Revenue Category {self.suffix}',
        )
        expense_category = ExpenseCategory(
            school_id=school.id,
            name=f'Demo Expense Category {self.suffix}',
        )
        db.session.add_all([device, fee_type, revenue_category, expense_category])
        db.session.flush()

        fee_record = FeeRecord(
            student_id=student.id,
            fee_type_id=fee_type.id,
            academic_year_id=year.id,
            school_id=school.id,
            total_amount=100,
        )
        revenue = Revenue(
            category_id=revenue_category.id,
            school_id=school.id,
            academic_year_id=year.id,
            amount=50,
            description='Demo revenue',
            recorded_by=manager.id,
        )
        expense = Expense(
            category_id=expense_category.id,
            school_id=school.id,
            academic_year_id=year.id,
            amount=25,
            description='Demo expense',
            approved_by=manager.id,
            created_by=manager.id,
        )
        db.session.add_all([fee_record, revenue, expense])
        db.session.flush()

        notification = Notification(
            school_id=school.id,
            title=f'Demo Notification {self.suffix}',
            body='Demo notification',
            ntype='announcement',
            target_user_id=parent.id,
            created_by=manager.id,
        )
        announcement = Announcement(
            school_id=school.id,
            title=f'Demo Announcement {self.suffix}',
            body='Demo announcement',
            created_by=manager.id,
            status='sent',
        )
        exam = Exam(
            school_id=school.id,
            academic_year_id=year.id,
            subject_id=subject.id,
            section_id=section.id,
            exam_date=date(2026, 1, 15),
            max_marks=100,
            pass_marks=50,
        )
        schedule = Schedule(
            school_id=school.id,
            academic_year_id=year.id,
            section_id=section.id,
            subject_id=subject.id,
            teacher_id=employee.id,
            day_of_week=1,
            start_time=time(8, 0),
            end_time=time(8, 45),
        )
        db.session.add_all([notification, announcement, exam, schedule])
        db.session.flush()

        db.session.add_all([
            NotificationRead(notification_id=notification.id, user_id=parent.id),
            AnnouncementTarget(announcement_id=announcement.id, user_id=parent.id),
            PushNotification(
                user_id=parent.id,
                school_id=school.id,
                title=f'Demo Push {self.suffix}',
                body='Demo push',
                ntype='announcement',
            ),
            StudentDocument(
                student_id=student.id,
                school_id=school.id,
                academic_year_id=year.id,
                document_type='ID',
                file_path=f'demo-student-{self.suffix}.pdf',
            ),
            StudentSuspension(
                student_id=student.id,
                school_id=school.id,
                academic_year_id=year.id,
                start_date=date(2026, 2, 1),
                end_date=date(2026, 2, 2),
                created_by=manager.id,
            ),
            StudentAttendance(
                student_id=student.id,
                school_id=school.id,
                academic_year_id=year.id,
                date=date(2026, 1, 10),
                status='present',
                device_id=device.id,
                recorded_by=manager.id,
            ),
            EmployeeAttendance(
                employee_id=employee.id,
                school_id=school.id,
                academic_year_id=year.id,
                date=date(2026, 1, 10),
                status='present',
                recorded_by=manager.id,
            ),
            EmployeeDocument(
                employee_id=employee.id,
                school_id=school.id,
                title='Contract',
                file_path=f'demo-employee-{self.suffix}.pdf',
            ),
            EmployeeEvaluation(
                employee_id=employee.id,
                evaluator_id=manager.id,
                school_id=school.id,
                academic_year_id=year.id,
                period='Q1',
                performance=90,
                discipline=90,
                attendance_score=90,
            ),
            SalaryRecord(
                employee_id=employee.id,
                school_id=school.id,
                academic_year_id=year.id,
                month=1,
                year=2026,
                base_salary=1000,
                net_salary=1000,
                expense_id=expense.id,
                created_by=manager.id,
            ),
            FeeInstallment(
                fee_record_id=fee_record.id,
                school_id=school.id,
                academic_year_id=year.id,
                installment_no=1,
                amount=100,
                received_amount=0,
                due_date=date(2026, 1, 31),
                collected_by=manager.id,
            ),
            ExamResult(
                exam_id=exam.id,
                student_id=student.id,
                school_id=school.id,
                academic_year_id=year.id,
                marks=95,
                entered_by=manager.id,
            ),
            AuditLog(
                school_id=school.id,
                user_id=manager.id,
                action='demo',
                resource='school',
                resource_id=school.id,
            ),
        ])
        db.session.commit()
        return school.id

    def test_real_school_delete_is_blocked_with_arabic_linked_details(self):
        with self.app.app_context():
            school_id = self._create_school_graph(
                f'Real Cleanup School {self.suffix}'
            )
            self.created['real_school_id'] = school_id

        with self.app.test_request_context(f'/schools/{school_id}/delete',
                                           method='POST'):
            self._login_super_admin()
            response = delete_school(school_id)
            self.assertEqual(response.status_code, 302)

            school = db.session.get(
                School,
                school_id,
                execution_options={'bypass_tenant_scope': True},
            )
            self.assertIsNotNone(school)
            messages = ' '.join(
                msg for _category, msg in get_flashed_messages(
                    with_categories=True
                )
            )
            self.assertIn('لا يمكن حذف المدرسة', messages)
            self.assertIn('الأعوام الدراسية', messages)
            self.assertIn('الطلاب', messages)
            logout_user()

    def test_demo_access_school_delete_cascades_related_records(self):
        with self.app.app_context():
            school_id = self._create_school_graph(
                f'Access School {self.suffix}'
            )
            self.created['demo_school_id'] = school_id

        with self.app.test_request_context(f'/schools/{school_id}/delete',
                                           method='POST'):
            self._login_super_admin()
            response = delete_school(school_id)
            self.assertEqual(response.status_code, 302)
            messages = ' '.join(
                msg for _category, msg in get_flashed_messages(
                    with_categories=True
                )
            )
            self.assertIn('تم تنظيف وحذف المدرسة التجريبية', messages)
            logout_user()

        with self.app.app_context():
            self.assertIsNone(db.session.get(
                School,
                school_id,
                execution_options={'bypass_tenant_scope': True},
            ))
            for model in (
                AcademicYear, Grade, Section, Subject, Student, Employee,
                FeeType, FeeRecord, RevenueCategory, ExpenseCategory, Device,
                Notification, Announcement, Schedule, AuditLog,
            ):
                count = (
                    model.query.execution_options(bypass_tenant_scope=True)
                    .filter(model.school_id == school_id)
                    .count()
                )
                self.assertEqual(count, 0, model.__name__)


if __name__ == '__main__':
    unittest.main()
