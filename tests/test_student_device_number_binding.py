"""Student attendance-device binding: one shared number across devices.

Covers
  * automatic default device selection on the student create form,
  * one number allocated once and reused on every target device,
  * reuse of a student's existing number for a missing binding,
  * the Student Linking "change number" action: collision rejection with no
    partial change, update of all the student's bindings, school isolation.
"""
import unittest
from datetime import date
from uuid import uuid4

from sqlalchemy import text

from app import create_app
from app.models import (db, AcademicYear, AttendanceDevice, DeviceEmployeeMapping,
                        DeviceStudentMapping, Employee, Grade, Role, School,
                        Section, Student, User)
from app.utils.device_numbering import ensure_student_device_mappings

_OPTS = {'bypass_tenant_scope': True}


class StudentDeviceNumberBindingTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.ids = {}
        with self.app.app_context():
            admin_role = Role.query.filter_by(name='school_admin').first()
            self.assertIsNotNone(admin_role, 'seed roles before running')
            for tag in ('a', 'b'):
                school = School(school_name=f'Dev {tag} {self.suffix}',
                                code=f'DV{tag.upper()}{self.suffix[:6]}',
                                capacity=0, is_active=True)
                db.session.add(school)
                db.session.flush()
                year = AcademicYear(school_id=school.id, name=f'Y {tag} {self.suffix}',
                                    start_date=date(2025, 8, 1),
                                    end_date=date(2026, 6, 30), is_current=True)
                db.session.add(year)
                db.session.flush()
                grade = Grade(school_id=school.id, academic_year_id=year.id,
                              name=f'G{tag}{self.suffix[:4]}')
                db.session.add(grade)
                db.session.flush()
                section = Section(school_id=school.id, academic_year_id=year.id,
                                  grade_id=grade.id, name=f'S{tag}', capacity=30)
                db.session.add(section)
                db.session.flush()
                self.ids.update({f'school_{tag}': school.id, f'year_{tag}': year.id})
                for n in (1, 2, 3):
                    st = Student(student_id=f'DV-{tag.upper()}{n}-{self.suffix}',
                                 full_name=f'Student {tag}{n}',
                                 date_of_birth=date(2015, 1, 1), gender='male',
                                 school_id=school.id, academic_year_id=year.id,
                                 section_id=section.id, status='active')
                    db.session.add(st)
                    db.session.flush()
                    self.ids[f'student_{tag}{n}'] = st.id
                admin = User(username=f'dv_ad_{tag}_{self.suffix}',
                             email=f'dv_ad_{tag}_{self.suffix}@example.test',
                             full_name=f'Admin {tag}', role_id=admin_role.id,
                             school_id=school.id, is_active=True)
                admin.set_password('Password123')
                db.session.add(admin)
                db.session.flush()
                self.ids[f'admin_{tag}'] = admin.id
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            sids = [self.ids['school_a'], self.ids['school_b']]
            for table in ('device_student_mappings', 'device_employee_mappings',
                          'attendance_devices', 'employees', 'audit_logs'):
                db.session.execute(text(f'DELETE FROM {table} WHERE school_id = ANY(:s)'),
                                   {'s': sids})
            for model in (Student, User, Section, Grade, AcademicYear):
                (db.session.query(model).execution_options(**_OPTS)
                 .filter(model.school_id.in_(sids)).delete(synchronize_session=False))
            db.session.query(School).filter(School.id.in_(sids)).delete(
                synchronize_session=False)
            db.session.commit()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _device(self, tag, name, scope='students'):
        with self.app.app_context():
            dev = AttendanceDevice(school_id=self.ids[f'school_{tag}'], name=name,
                                   device_scope=scope, ip_address='127.0.0.1',
                                   password='x', device_sn=f'SN-{name}-{self.suffix}')
            db.session.add(dev)
            db.session.commit()
            return dev.id

    def _map(self, tag, device_id, student_key, number):
        with self.app.app_context():
            m = DeviceStudentMapping(school_id=self.ids[f'school_{tag}'],
                                     device_id=device_id, employee_no_string=number,
                                     student_id=self.ids[student_key], is_active=True)
            db.session.add(m)
            db.session.commit()
            return m.id

    def _numbers(self, student_key):
        with self.app.app_context():
            rows = (db.session.query(DeviceStudentMapping.device_id,
                                     DeviceStudentMapping.employee_no_string)
                    .execution_options(**_OPTS)
                    .filter_by(student_id=self.ids[student_key]).all())
            return dict(rows)

    def _ensure(self, tag, device_ids, student_key):
        with self.app.test_request_context():
            devs = [db.session.get(AttendanceDevice, d, execution_options=_OPTS)
                    for d in device_ids]
            ensure_student_device_mappings(devs, self.ids[student_key],
                                           self.ids[f'school_{tag}'])
            db.session.commit()

    def _client(self, tag):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.ids[f'admin_{tag}'])
            sess['_fresh'] = True
        return client

    # ── 1. Automatic device selection on the create form ────────────────────

    def test_create_form_preselects_single_device(self):
        dev = self._device('a', 'only')
        html = self._client('a').get('/students/create').get_data(as_text=True)
        self.assertIn(f'<option value="{dev}" selected>', html)
        self.assertNotIn('allDevicesCb', html)

    def test_create_form_defaults_to_all_devices_when_several(self):
        self._device('a', 'd1')
        self._device('a', 'd2')
        html = self._client('a').get('/students/create').get_data(as_text=True)
        self.assertRegex(html, r'id="allDevicesCb"\s+checked')
        self.assertRegex(html, r'id="deviceSelect"\s+disabled')

    def test_create_form_never_lists_other_school_device(self):
        own = self._device('a', 'own')
        other = self._device('b', 'other')
        html = self._client('a').get('/students/create').get_data(as_text=True)
        self.assertIn(f'<option value="{own}" selected>', html)
        self.assertNotIn(f'<option value="{other}"', html)

    def test_edit_form_defaults_only_for_student_without_bindings(self):
        dev = self._device('a', 'only')
        self._map('a', dev, 'student_a2', '1')
        client = self._client('a')
        unbound = client.get(f"/students/{self.ids['student_a1']}/edit").get_data(as_text=True)
        bound = client.get(f"/students/{self.ids['student_a2']}/edit").get_data(as_text=True)
        self.assertIn(f'<option value="{dev}" selected>', unbound)
        self.assertIn(f'<option value="{dev}" >', bound)

    # ── 2. One shared number across devices ─────────────────────────────────

    def test_sequential_shared_numbers_on_empty_devices(self):
        d1, d2 = self._device('a', 'd1'), self._device('a', 'd2')
        self._ensure('a', [d1, d2], 'student_a1')
        self._ensure('a', [d1, d2], 'student_a2')
        self.assertEqual(self._numbers('student_a1'), {d1: '1', d2: '1'})
        self.assertEqual(self._numbers('student_a2'), {d1: '2', d2: '2'})

    def test_common_number_skips_numbers_used_on_any_device_including_staff(self):
        d1 = self._device('a', 'd1')
        d2 = self._device('a', 'd2', scope='mixed')
        self._map('a', d1, 'student_a3', '3')
        with self.app.app_context():
            emp = Employee(employee_id=f'E-{self.suffix}', full_name='Staff',
                           school_id=self.ids['school_a'])
            db.session.add(emp)
            db.session.flush()
            db.session.add(DeviceEmployeeMapping(
                school_id=self.ids['school_a'], device_id=d2,
                employee_id=emp.id, enrollment_no='7', is_active=True))
            db.session.commit()
        self._ensure('a', [d1, d2], 'student_a1')
        self.assertEqual(self._numbers('student_a1'), {d1: '8', d2: '8'})

    def test_existing_number_reused_and_resave_is_idempotent(self):
        d1, d2 = self._device('a', 'd1'), self._device('a', 'd2')
        self._map('a', d1, 'student_a1', '5')
        self._ensure('a', [d1, d2], 'student_a1')
        self._ensure('a', [d1, d2], 'student_a1')
        self.assertEqual(self._numbers('student_a1'), {d1: '5', d2: '5'})

    def test_existing_number_occupied_on_new_device_rejects_without_partial_change(self):
        d1, d2, d3 = (self._device('a', 'd1'), self._device('a', 'd2'),
                      self._device('a', 'd3'))
        self._map('a', d1, 'student_a1', '5')
        self._map('a', d2, 'student_a2', '5')
        resp = self._client('a').post(f'/attendance-devices/{d2}/mappings/add',
                                      data={'student_id': self.ids['student_a1']},
                                      follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('رقم الطالب 5 مستخدم لشخص آخر على الجهاز', resp.get_data(as_text=True))
        # Multi-device target: the free device must not be bound either.
        with self.app.test_request_context():
            from app.utils.device_numbering import DeviceNumberConflictError
            devs = [db.session.get(AttendanceDevice, d, execution_options=_OPTS)
                    for d in (d3, d2)]
            with self.assertRaises(DeviceNumberConflictError):
                ensure_student_device_mappings(devs, self.ids['student_a1'],
                                               self.ids['school_a'])
            db.session.rollback()
        self.assertEqual(self._numbers('student_a1'), {d1: '5'})
        self.assertEqual(self._numbers('student_a2'), {d2: '5'})

    # ── 3. Change number from Student Linking ───────────────────────────────

    def test_change_number_updates_all_bindings(self):
        d1, d2 = self._device('a', 'd1'), self._device('a', 'd2')
        m1 = self._map('a', d1, 'student_a1', '1')
        self._map('a', d2, 'student_a1', '1')
        # Same number in another school must not count as a collision.
        db_b = self._device('b', 'b1')
        self._map('b', db_b, 'student_b1', '9')
        resp = self._client('a').post(f'/attendance-devices/mappings/{m1}/change-number',
                                      data={'employee_no_string': '9'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._numbers('student_a1'), {d1: '9', d2: '9'})
        self.assertEqual(self._numbers('student_b1'), {db_b: '9'})

    def test_change_number_collision_on_one_device_changes_nothing(self):
        d1, d2 = self._device('a', 'd1'), self._device('a', 'd2')
        m1 = self._map('a', d1, 'student_a1', '1')
        self._map('a', d2, 'student_a1', '1')
        self._map('a', d2, 'student_a2', '4')
        client = self._client('a')
        resp = client.post(f'/attendance-devices/mappings/{m1}/change-number',
                           data={'employee_no_string': '4'}, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('الرقم 4 مستخدم لشخص آخر على الجهاز', resp.get_data(as_text=True))
        self.assertEqual(self._numbers('student_a1'), {d1: '1', d2: '1'})
        self.assertEqual(self._numbers('student_a2'), {d2: '4'})
        for bad in ('0', '-3', 'abc', ''):
            resp = client.post(f'/attendance-devices/mappings/{m1}/change-number',
                               data={'employee_no_string': bad})
            self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._numbers('student_a1'), {d1: '1', d2: '1'})

    def test_change_number_other_school_mapping_is_404(self):
        db_b = self._device('b', 'b1')
        mb = self._map('b', db_b, 'student_b1', '1')
        resp = self._client('a').post(f'/attendance-devices/mappings/{mb}/change-number',
                                      data={'employee_no_string': '2'})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self._numbers('student_b1'), {db_b: '1'})


if __name__ == '__main__':
    unittest.main()
