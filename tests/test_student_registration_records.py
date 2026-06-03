"""
Tests for the Student Registration Record (سجل قيد الطالب) feature.

Covers:
  - Main page loads for admin
  - Main page blocked for non-admin (parent)
  - Student search returns same-school students only
  - Student search excludes other-school students
  - Autofill endpoint returns correct data
  - Save new record (POST /new)
  - Duplicate student guard
  - Edit an existing record
  - PDF endpoint returns bytes
  - Cross-school access blocked (404)
"""
import json
import unittest
from datetime import date
from uuid import uuid4

from app import create_app
from app.models import (
    db, Role, School, User, AcademicYear, Grade, Section,
    Student, StudentRegistrationRecord,
)


def _uid():
    return uuid4().hex[:10]


class StudentRegistrationRecordTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')
        with cls.app.app_context():
            cls.admin_role  = Role.query.filter_by(name='school_admin').first()
            cls.parent_role = Role.query.filter_by(name='parent').first()
            assert cls.admin_role,  'school_admin role must exist'
            assert cls.parent_role, 'parent role must exist'

    def setUp(self):
        self.sfx    = _uid()
        self.client = self.app.test_client()

        with self.app.app_context():
            # Two schools
            school_a = School(
                school_name=f'RegRec School A {self.sfx}',
                school_name_ar=f'المدرسة أ {self.sfx}',
                code=f'RRA{self.sfx[:7]}',
                capacity=0, is_active=True,
            )
            school_b = School(
                school_name=f'RegRec School B {self.sfx}',
                code=f'RRB{self.sfx[:7]}',
                capacity=0, is_active=True,
            )
            db.session.add_all([school_a, school_b])
            db.session.flush()

            year_a = AcademicYear(
                school_id=school_a.id,
                name=f'2025-2026-{self.sfx}',
                start_date=date(2025, 9, 1),
                end_date=date(2026, 6, 30),
                is_current=True,
            )
            db.session.add(year_a)
            db.session.flush()

            grade_a = Grade(
                name='الأول',
                stage='ابتدائي',
                school_id=school_a.id,
                academic_year_id=year_a.id,
            )
            db.session.add(grade_a)
            db.session.flush()

            section_a = Section(
                name='أ',
                school_id=school_a.id,
                academic_year_id=year_a.id,
                grade_id=grade_a.id,
            )
            db.session.add(section_a)
            db.session.flush()

            # Admin user (school A)
            admin_a = User(
                username=f'rr_admin_a_{self.sfx}',
                email=f'rr_admin_a_{self.sfx}@test.test',
                full_name=f'RR Admin A {self.sfx}',
                role_id=self.admin_role.id,
                school_id=school_a.id,
                is_active=True,
            )
            admin_a.set_password('Test1234!')

            # Parent user (school A)
            parent_a = User(
                username=f'rr_parent_a_{self.sfx}',
                email=f'rr_parent_a_{self.sfx}@test.test',
                full_name=f'RR Parent A {self.sfx}',
                role_id=self.parent_role.id,
                school_id=school_a.id,
                is_active=True,
            )
            parent_a.set_password('Test1234!')

            # Admin user (school B)
            admin_b = User(
                username=f'rr_admin_b_{self.sfx}',
                email=f'rr_admin_b_{self.sfx}@test.test',
                full_name=f'RR Admin B {self.sfx}',
                role_id=self.admin_role.id,
                school_id=school_b.id,
                is_active=True,
            )
            admin_b.set_password('Test1234!')

            db.session.add_all([admin_a, parent_a, admin_b])
            db.session.flush()

            # Student in school A
            student_a = Student(
                student_id=f'STU-{self.sfx}',
                full_name=f'Ahmed Test {self.sfx}',
                school_id=school_a.id,
                academic_year_id=year_a.id,
                section_id=section_a.id,
                gender='male',
                guardian_name='Parent Name',
                guardian_phone='07701234567',
                guardian_relation='father',
                status='active',
                enrollment_date=date(2025, 9, 1),
            )
            # Student in school B
            student_b = Student(
                student_id=f'STU-B-{self.sfx}',
                full_name=f'School B Student {self.sfx}',
                school_id=school_b.id,
                academic_year_id=year_a.id,
            )
            db.session.add_all([student_a, student_b])
            db.session.flush()

            db.session.commit()

            # Store IDs for later use
            self.school_a_id  = school_a.id
            self.school_b_id  = school_b.id
            self.admin_a_id   = admin_a.id
            self.admin_b_id   = admin_b.id
            self.parent_a_id  = parent_a.id
            self.student_a_id = student_a.id
            self.student_b_id = student_b.id
            self.year_a_id    = year_a.id

    def tearDown(self):
        with self.app.app_context():
            StudentRegistrationRecord.query.filter(
                StudentRegistrationRecord.school_id.in_(
                    [self.school_a_id, self.school_b_id]
                )
            ).delete(synchronize_session=False)
            db.session.commit()

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _login(self, username, password='Test1234!'):
        return self.client.post('/auth/login', data={
            'username': username, 'password': password,
        }, follow_redirects=True)

    def _admin_a_username(self):
        with self.app.app_context():
            u = User.query.get(self.admin_a_id)
            return u.username

    def _parent_a_username(self):
        with self.app.app_context():
            u = User.query.get(self.parent_a_id)
            return u.username

    def _admin_b_username(self):
        with self.app.app_context():
            u = User.query.get(self.admin_b_id)
            return u.username

    def _create_record(self):
        """POST a new record for student_a as admin_a, return record id."""
        self._login(self._admin_a_username())
        resp = self.client.post(
            '/student-registration-records/new',
            data={
                'student_id':        str(self.student_a_id),
                'snap_full_name':    'Ahmed Test',
                'snap_student_number': f'STU-{self.sfx}',
                'snap_status':       'active',
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 302), f'Unexpected {resp.status_code}'
        with self.app.app_context():
            rec = StudentRegistrationRecord.query.filter_by(
                school_id=self.school_a_id,
                student_id=self.student_a_id,
            ).first()
            return rec.id if rec else None

    # ── Tests ──────────────────────────────────────────────────────────────────

    def test_index_loads_for_admin(self):
        self._login(self._admin_a_username())
        resp = self.client.get('/student-registration-records/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('سجل قيد الطالب', resp.data.decode('utf-8'))

    def test_index_blocked_for_parent(self):
        self._login(self._parent_a_username())
        resp = self.client.get('/student-registration-records/',
                               follow_redirects=False)
        # Parent has no admin permission — should redirect or 403
        self.assertIn(resp.status_code, [302, 403])

    def test_search_returns_same_school_student(self):
        self._login(self._admin_a_username())
        resp = self.client.get(
            f'/student-registration-records/search-students?q=Ahmed Test {self.sfx}'
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        ids = [s['id'] for s in data]
        self.assertIn(self.student_a_id, ids)

    def test_search_excludes_other_school_student(self):
        self._login(self._admin_a_username())
        resp = self.client.get(
            f'/student-registration-records/search-students?q=School B Student {self.sfx}'
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        ids = [s['id'] for s in data]
        self.assertNotIn(self.student_b_id, ids)

    def test_autofill_endpoint_returns_correct_data(self):
        self._login(self._admin_a_username())
        resp = self.client.get(
            f'/student-registration-records/student-data/{self.student_a_id}'
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data['snap_guardian_name'], 'Parent Name')
        self.assertEqual(data['snap_guardian_phone'], '07701234567')
        self.assertEqual(data['snap_grade_name'], 'الأول')
        self.assertEqual(data['snap_section_name'], 'أ')

    def test_autofill_cross_school_blocked(self):
        """Admin A cannot fetch autofill for school B's student."""
        self._login(self._admin_a_username())
        resp = self.client.get(
            f'/student-registration-records/student-data/{self.student_b_id}'
        )
        self.assertEqual(resp.status_code, 404)

    def test_save_new_record(self):
        record_id = self._create_record()
        self.assertIsNotNone(record_id)
        with self.app.app_context():
            rec = StudentRegistrationRecord.query.get(record_id)
            self.assertIsNotNone(rec)
            self.assertEqual(rec.snap_full_name, 'Ahmed Test')
            self.assertEqual(rec.school_id, self.school_a_id)
            self.assertEqual(rec.student_id, self.student_a_id)

    def test_duplicate_record_blocked(self):
        record_id = self._create_record()
        self.assertIsNotNone(record_id)

        # Attempt second create for same student
        resp = self.client.post(
            '/student-registration-records/new',
            data={
                'student_id':     str(self.student_a_id),
                'snap_full_name': 'Ahmed Test',
                'snap_status':    'active',
            },
            follow_redirects=False,
        )
        # Should redirect to view (302) not create another record
        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            count = StudentRegistrationRecord.query.filter_by(
                school_id=self.school_a_id,
                student_id=self.student_a_id,
            ).count()
            self.assertEqual(count, 1)

    def test_view_record(self):
        record_id = self._create_record()
        resp = self.client.get(
            f'/student-registration-records/{record_id}'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Ahmed Test', resp.data.decode('utf-8'))

    def test_edit_record(self):
        record_id = self._create_record()
        resp = self.client.post(
            f'/student-registration-records/{record_id}/edit',
            data={
                'snap_full_name':    'Ahmed Updated',
                'snap_student_number': f'STU-{self.sfx}',
                'snap_status':       'active',
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            rec = StudentRegistrationRecord.query.get(record_id)
            self.assertEqual(rec.snap_full_name, 'Ahmed Updated')

    def test_pdf_endpoint_returns_bytes(self):
        record_id = self._create_record()
        resp = self.client.get(
            f'/student-registration-records/{record_id}/pdf'
        )
        # Either 200 PDF or 302 redirect if reportlab not available
        self.assertIn(resp.status_code, [200, 302])
        if resp.status_code == 200:
            self.assertEqual(resp.content_type, 'application/pdf')
            self.assertGreater(len(resp.data), 100)

    def test_cross_school_view_blocked(self):
        """Admin A cannot view a record that belongs to school B."""
        # Insert school-B record directly — avoids HTTP session cross-contamination
        with self.app.app_context():
            rec_b = StudentRegistrationRecord(
                school_id=self.school_b_id,
                student_id=self.student_b_id,
                snap_full_name='School B Student',
                snap_status='active',
            )
            db.session.add(rec_b)
            db.session.commit()
            rec_b_id = rec_b.id

        # Admin A (school A) must not be able to view school B's record
        self._login(self._admin_a_username())
        resp = self.client.get(f'/student-registration-records/{rec_b_id}')
        self.assertEqual(resp.status_code, 404)

    def test_new_page_shows_existing_record_warning(self):
        """GET /new?student_id= shows warning if student already has a record."""
        record_id = self._create_record()
        resp = self.client.get(
            f'/student-registration-records/new?student_id={self.student_a_id}'
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode('utf-8')
        self.assertIn('يوجد سجل قيد لهذا الطالب مسبقاً', body)


if __name__ == '__main__':
    unittest.main()
