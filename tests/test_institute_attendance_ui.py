"""Focused checks for the institute-mode attendance UI follow-up.

Three claims, nothing wider:

  1. RENDER — a school still shows every automatic-absence control; an institute
     shows none of them, while manual attendance, shift management and the other
     attendance settings still render.
  2. SAVE — submitting the attendance-settings form WITHOUT the hidden
     automatic-absence fields preserves their stored values (the handler used to
     wipe any omitted field), and the shift cutoff is likewise untouched.
  3. LATENESS — student lateness is optional for institutes only: with no
     school-level late threshold an institute's student check-in is 'present'
     even when a shift carries a NOT NULL late_after_time; with one configured
     the existing calculation runs unchanged. Schools and EMPLOYEE lateness
     (which never passes a shift) are unaffected.

No production access, no device contact, no new framework.
"""
import unittest
from datetime import date, time, timedelta
from uuid import uuid4

from sqlalchemy import text

from app import create_app
from app.models import (
    db, AcademicYear, AttendanceShift, Grade, Role, School, Section, Student,
    User,
)
from app.utils.attendance_helpers import determine_check_in_status


TODAY = date.today()

# Arabic labels of the controls that must disappear for an institute.
SHIFT_CUTOFF_HEADING = 'وقت الغياب التلقائي للشفتات'
SHIFT_CUTOFF_FIELD = 'name="shift_absent_after_time"'
UNIFIED_CUTOFF_FIELD = 'name="att_absence_threshold"'
TRIGGER_ACTION = '/attendance/mark-absent-today'
# Controls that must SURVIVE for an institute.
LATE_FIELD = 'name="att_late_threshold"'
START_FIELD = 'name="att_start_time"'
DEPARTURE_FIELD = 'name="att_departure_time"'
SHIFT_MGMT_HEADING = 'إدارة الشفتات'


class InstituteAttendanceUITest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.ids = {}
        with self.app.app_context():
            admin_role = Role.query.filter_by(name='school_admin').first()
            self.assertIsNotNone(admin_role, 'seed roles before running')

            for tag, inst_type in (('sch', None), ('ins', 'institute')):
                school = School(
                    school_name=f'UI {tag} {self.suffix}',
                    code=f'UI{tag.upper()}{self.suffix[:5]}',
                    capacity=0, is_active=True,
                    institution_type=inst_type,
                    enable_attendance_shifts=True,
                    att_start_time=time(7, 0),
                    att_late_threshold=time(8, 0),
                    att_absence_threshold=time(10, 0),
                    att_departure_time=time(13, 0),
                    shift_absent_after_time=time(11, 0),
                )
                db.session.add(school)
                db.session.flush()

                year = AcademicYear(school_id=school.id,
                                    name=f'Y {tag} {self.suffix}',
                                    start_date=date(2025, 8, 1),
                                    end_date=date(2026, 6, 30),
                                    is_current=True)
                db.session.add(year)
                db.session.flush()

                grade = Grade(school_id=school.id, academic_year_id=year.id,
                              name=f'G{tag}{self.suffix[:4]}')
                db.session.add(grade)
                db.session.flush()

                shift = AttendanceShift(school_id=school.id, name='صباحي',
                                        start_time=time(7, 0),
                                        late_after_time=time(8, 0),
                                        absent_after_time=time(11, 0),
                                        is_active=True)
                db.session.add(shift)
                db.session.flush()

                section = Section(school_id=school.id, academic_year_id=year.id,
                                  grade_id=grade.id, shift_id=shift.id,
                                  name=f'S{tag}{self.suffix[:4]}', capacity=30)
                db.session.add(section)
                db.session.flush()

                student = Student(student_id=f'UI-{tag.upper()}-{self.suffix}',
                                  full_name=f'Student {tag}',
                                  date_of_birth=date(2015, 1, 1), gender='male',
                                  school_id=school.id, academic_year_id=year.id,
                                  section_id=section.id, status='active')
                db.session.add(student)

                admin = User(username=f'ui_ad_{tag}_{self.suffix}',
                             email=f'ui_ad_{tag}_{self.suffix}@example.test',
                             full_name=f'Admin {tag}', role_id=admin_role.id,
                             school_id=school.id, is_active=True)
                admin.set_password('Password123')
                db.session.add(admin)
                db.session.flush()

                self.ids.update({
                    f'school_{tag}': school.id, f'year_{tag}': year.id,
                    f'grade_{tag}': grade.id, f'section_{tag}': section.id,
                    f'shift_{tag}': shift.id, f'student_{tag}': student.id,
                    f'admin_{tag}': admin.id,
                })
            db.session.commit()

        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            opts = {'bypass_tenant_scope': True}
            sids = [self.ids['school_sch'], self.ids['school_ins']]
            for stmt in (
                'DELETE FROM student_attendance WHERE school_id = ANY(:s)',
                'DELETE FROM notifications WHERE school_id = ANY(:s)',
                'DELETE FROM push_notifications WHERE school_id = ANY(:s)',
                'DELETE FROM audit_logs WHERE school_id = ANY(:s)',
                'UPDATE sections SET shift_id = NULL WHERE school_id = ANY(:s)',
                'UPDATE grades SET shift_id = NULL WHERE school_id = ANY(:s)',
                'DELETE FROM attendance_shifts WHERE school_id = ANY(:s)',
            ):
                db.session.execute(text(stmt), {'s': sids})
            for model, keys in ((Student, ['student_sch', 'student_ins']),
                                (User, ['admin_sch', 'admin_ins']),
                                (Section, ['section_sch', 'section_ins']),
                                (Grade, ['grade_sch', 'grade_ins']),
                                (AcademicYear, ['year_sch', 'year_ins']),
                                (School, ['school_sch', 'school_ins'])):
                for key in keys:
                    row = db.session.get(model, self.ids[key],
                                         execution_options=opts)
                    if row is not None:
                        db.session.delete(row)
                db.session.flush()
            db.session.commit()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _login(self, tag):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.ids[f'admin_{tag}'])
            sess['_fresh'] = True

    def _settings_html(self, tag):
        self._login(tag)
        resp = self.client.get('/admin/attendance-settings')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def _school(self, tag):
        return db.session.get(School, self.ids[f'school_{tag}'],
                              execution_options={'bypass_tenant_scope': True})

    # ── 1. RENDER ────────────────────────────────────────────────────────────

    def test_school_still_shows_all_automatic_absence_controls(self):
        html = self._settings_html('sch')
        for marker in (SHIFT_CUTOFF_HEADING, SHIFT_CUTOFF_FIELD,
                       UNIFIED_CUTOFF_FIELD, TRIGGER_ACTION):
            self.assertIn(marker, html, f'school lost control: {marker}')

    def test_institute_hides_every_automatic_absence_control(self):
        html = self._settings_html('ins')
        for marker in (SHIFT_CUTOFF_HEADING, SHIFT_CUTOFF_FIELD,
                       UNIFIED_CUTOFF_FIELD, TRIGGER_ACTION):
            self.assertNotIn(marker, html, f'institute still shows: {marker}')

    def test_institute_keeps_unrelated_attendance_controls(self):
        html = self._settings_html('ins')
        for marker in (START_FIELD, LATE_FIELD, DEPARTURE_FIELD,
                       SHIFT_MGMT_HEADING):
            self.assertIn(marker, html, f'institute wrongly lost: {marker}')

    def test_institute_attendance_index_hides_trigger_button(self):
        self._login('ins')
        resp = self.client.get('/attendance/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(TRIGGER_ACTION, resp.get_data(as_text=True))

        self._login('sch')
        resp = self.client.get('/attendance/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(TRIGGER_ACTION, resp.get_data(as_text=True),
                      'school must keep its automatic-absence button')

    # ── 2. SAVE — omitted fields preserve stored values ──────────────────────

    def test_omitted_absence_fields_preserve_stored_values(self):
        """The institute form posts no att_absence_threshold — it must survive."""
        self._login('ins')
        resp = self.client.post('/admin/attendance-settings', data={
            'att_start_time': '07:30',
            'att_late_threshold': '08:15',
            'att_departure_time': '13:00',
            'enable_attendance_shifts': 'on',
        }, follow_redirects=False)
        self.assertIn(resp.status_code, (200, 302))

        with self.app.app_context():
            school = self._school('ins')
            self.assertEqual(school.att_absence_threshold, time(10, 0),
                             'omitted automatic-absence cutoff was wiped')
            self.assertEqual(school.shift_absent_after_time, time(11, 0),
                             'shift cutoff was wiped by the settings form')
            # The posted fields were still applied normally.
            self.assertEqual(school.att_start_time, time(7, 30))
            self.assertEqual(school.att_late_threshold, time(8, 15))

    def test_school_can_still_clear_a_posted_field(self):
        """A PRESENT but empty field still clears — school behaviour unchanged."""
        self._login('sch')
        resp = self.client.post('/admin/attendance-settings', data={
            'att_start_time': '07:00',
            'att_late_threshold': '08:00',
            'att_absence_threshold': '',
            'att_departure_time': '13:00',
            'enable_attendance_shifts': 'on',
        }, follow_redirects=False)
        self.assertIn(resp.status_code, (200, 302))

        with self.app.app_context():
            self.assertIsNone(self._school('sch').att_absence_threshold)

    def test_institute_can_explicitly_clear_lateness(self):
        """Operator empties the late field → stored NULL, absence cutoff kept."""
        self._login('ins')
        self.client.post('/admin/attendance-settings', data={
            'att_start_time': '07:00',
            'att_late_threshold': '',
            'att_departure_time': '13:00',
            'enable_attendance_shifts': 'on',
        }, follow_redirects=False)

        with self.app.app_context():
            school = self._school('ins')
            self.assertIsNone(school.att_late_threshold)
            self.assertEqual(school.att_absence_threshold, time(10, 0))

    # ── 3. LATENESS — optional for institutes only ───────────────────────────

    def test_institute_without_late_threshold_is_never_late(self):
        with self.app.app_context():
            school = self._school('ins')
            school.att_late_threshold = None
            db.session.commit()

            school = self._school('ins')
            shift = db.session.get(
                AttendanceShift, self.ids['shift_ins'],
                execution_options={'bypass_tenant_scope': True})
            # Well past the shift's NOT NULL late_after_time (08:00).
            self.assertEqual(
                determine_check_in_status(time(9, 30), school, shift=shift),
                'present')

    def test_school_without_late_threshold_still_uses_shift_time(self):
        """Existing school behaviour must NOT change."""
        with self.app.app_context():
            school = self._school('sch')
            school.att_late_threshold = None
            db.session.commit()

            school = self._school('sch')
            shift = db.session.get(
                AttendanceShift, self.ids['shift_sch'],
                execution_options={'bypass_tenant_scope': True})
            self.assertEqual(
                determine_check_in_status(time(9, 30), school, shift=shift),
                'late')

    def test_institute_with_late_threshold_reuses_existing_logic(self):
        with self.app.app_context():
            school = self._school('ins')      # att_late_threshold = 08:00
            shift = db.session.get(
                AttendanceShift, self.ids['shift_ins'],
                execution_options={'bypass_tenant_scope': True})
            # Shift time still takes priority, exactly as before.
            self.assertEqual(
                determine_check_in_status(time(9, 30), school, shift=shift),
                'late')
            self.assertEqual(
                determine_check_in_status(time(7, 30), school, shift=shift),
                'present')
            # Shiftless student falls back to the school threshold, as before.
            self.assertEqual(
                determine_check_in_status(time(9, 30), school), 'late')

    def test_employee_lateness_unaffected_for_institutes(self):
        """Employee attendance passes no shift — staff must stay unchanged."""
        with self.app.app_context():
            school = self._school('ins')
            school.att_late_threshold = None
            db.session.commit()
            school = self._school('ins')
            # No shift argument → the institute opt-out must not engage; with a
            # NULL threshold the pre-existing rule already returns 'present'.
            self.assertEqual(determine_check_in_status(time(9, 30), school),
                             'present')

            school.att_late_threshold = time(8, 0)
            db.session.commit()
            school = self._school('ins')
            self.assertEqual(determine_check_in_status(time(9, 30), school),
                             'late', 'staff lateness must still work')

    def test_switching_back_to_school_restores_controls_and_values(self):
        with self.app.app_context():
            school = self._school('ins')
            school.institution_type = School.INSTITUTION_SCHOOL
            db.session.commit()

        html = self._settings_html('ins')
        for marker in (SHIFT_CUTOFF_HEADING, SHIFT_CUTOFF_FIELD,
                       UNIFIED_CUTOFF_FIELD, TRIGGER_ACTION):
            self.assertIn(marker, html, f'restored school missing: {marker}')
        # The originally stored values are shown again.
        self.assertIn('value="10:00"', html)
        self.assertIn('value="11:00"', html)


if __name__ == '__main__':
    unittest.main()
