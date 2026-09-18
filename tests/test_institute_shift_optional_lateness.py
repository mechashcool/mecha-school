"""Focused checks: optional shift lateness for institutes only.

  1. CREATE — an institute creates a shift with only name + start time, leaving
     lateness and departure blank; a school still gets the required-field error.
  2. EDIT — an institute clears an existing shift's lateness; a school cannot.
  3. FORM — the required star and the HTML `required` attribute are gone for an
     institute on BOTH the create form and the edit modal, and present for a
     school.
  4. PROCESSING — a NULL cutoff yields 'present' with no None comparison, for
     students (shift) and employees (no shift); configured cutoffs still work.
  5. SWITCH-BACK — turning an institute with a blank shift cutoff into a school
     is refused with a clear message and changes nothing, never a runtime error.

No production access, no device contact, no full suite.
"""
import unittest
from datetime import date, time
from uuid import uuid4

from sqlalchemy import text

from app import create_app
from app.models import (
    db, AcademicYear, AttendanceShift, Grade, Role, School, Section, User,
)
from app.utils.attendance_helpers import determine_check_in_status


class InstituteShiftOptionalLatenessTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.ids = {}
        with self.app.app_context():
            admin_role = Role.query.filter_by(name='school_admin').first()
            super_role = Role.query.filter_by(name='super_admin').first()
            self.assertIsNotNone(admin_role, 'seed roles before running')
            self.assertIsNotNone(super_role, 'seed roles before running')

            for tag, inst_type in (('sch', None), ('ins', 'institute')):
                school = School(
                    school_name=f'SL {tag} {self.suffix}',
                    code=f'SL{tag.upper()}{self.suffix[:5]}',
                    capacity=0, is_active=True,
                    institution_type=inst_type,
                    enable_attendance_shifts=True,
                    att_start_time=time(7, 0),
                    att_late_threshold=time(8, 0),
                    att_absence_threshold=time(10, 0),
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

                shift = AttendanceShift(school_id=school.id,
                                        name=f'صباحي {self.suffix[:4]}',
                                        start_time=time(7, 0),
                                        late_after_time=time(8, 0),
                                        absent_after_time=time(11, 0),
                                        is_active=True)
                db.session.add(shift)
                db.session.flush()

                admin = User(username=f'sl_ad_{tag}_{self.suffix}',
                             email=f'sl_ad_{tag}_{self.suffix}@example.test',
                             full_name=f'Admin {tag}', role_id=admin_role.id,
                             school_id=school.id, is_active=True)
                admin.set_password('Password123')
                db.session.add(admin)
                db.session.flush()

                self.ids.update({
                    f'school_{tag}': school.id, f'year_{tag}': year.id,
                    f'grade_{tag}': grade.id, f'shift_{tag}': shift.id,
                    f'admin_{tag}': admin.id,
                })

            superu = User(username=f'sl_su_{self.suffix}',
                          email=f'sl_su_{self.suffix}@example.test',
                          full_name='Super', role_id=super_role.id,
                          school_id=None, is_active=True)
            superu.set_password('Password123')
            db.session.add(superu)
            db.session.flush()
            self.ids['super'] = superu.id
            db.session.commit()

        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            opts = {'bypass_tenant_scope': True}
            sids = [self.ids['school_sch'], self.ids['school_ins']]
            for stmt in (
                'DELETE FROM audit_logs WHERE school_id = ANY(:s)',
                'UPDATE sections SET shift_id = NULL WHERE school_id = ANY(:s)',
                'UPDATE grades SET shift_id = NULL WHERE school_id = ANY(:s)',
                'DELETE FROM sections WHERE school_id = ANY(:s)',
                'DELETE FROM attendance_shifts WHERE school_id = ANY(:s)',
            ):
                db.session.execute(text(stmt), {'s': sids})
            for model, keys in ((User, ['admin_sch', 'admin_ins', 'super']),
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

    def _login(self, key):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.ids[key])
            sess['_fresh'] = True

    def _shifts(self, tag):
        return (AttendanceShift.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=self.ids[f'school_{tag}'])
                .all())

    def _shift(self, tag):
        return db.session.get(AttendanceShift, self.ids[f'shift_{tag}'],
                              execution_options={'bypass_tenant_scope': True})

    def _school(self, tag):
        return db.session.get(School, self.ids[f'school_{tag}'],
                              execution_options={'bypass_tenant_scope': True})

    # ── 1. CREATE ────────────────────────────────────────────────────────────

    def test_institute_creates_shift_without_lateness(self):
        self._login('admin_ins')
        new_name = f'مسائي {self.suffix[:4]}'
        resp = self.client.post('/attendance-shifts/create', data={
            'name': new_name,
            'start_time': '16:00',
            'late_after_time': '',
            'dismissal_time': '',
        }, follow_redirects=False)
        self.assertIn(resp.status_code, (200, 302))

        with self.app.app_context():
            created = [s for s in self._shifts('ins') if s.name == new_name]
            self.assertEqual(len(created), 1, 'institute shift was not created')
            shift = created[0]
            self.assertEqual(shift.start_time, time(16, 0))
            self.assertIsNone(shift.late_after_time)
            self.assertIsNone(shift.dismissal_time)
            # No cutoff may be fabricated, and start_time must not be reused.
            self.assertIsNone(shift.absent_after_time)

    def test_school_still_requires_lateness_on_create(self):
        self._login('admin_sch')
        new_name = f'مسائي {self.suffix[:4]}'
        resp = self.client.post('/attendance-shifts/create', data={
            'name': new_name,
            'start_time': '16:00',
            'late_after_time': '',
        }, follow_redirects=False)
        self.assertIn(resp.status_code, (200, 302))

        with self.app.app_context():
            self.assertEqual([s for s in self._shifts('sch') if s.name == new_name],
                             [], 'school shift must be rejected without lateness')

    def test_institute_create_still_rejects_lateness_before_start(self):
        """The existing ordering rule still applies when a time IS given."""
        self._login('admin_ins')
        new_name = f'خطأ {self.suffix[:4]}'
        self.client.post('/attendance-shifts/create', data={
            'name': new_name, 'start_time': '16:00', 'late_after_time': '15:00',
        }, follow_redirects=False)
        with self.app.app_context():
            self.assertEqual([s for s in self._shifts('ins') if s.name == new_name], [])

    # ── 2. EDIT ──────────────────────────────────────────────────────────────

    def test_institute_can_clear_existing_shift_lateness(self):
        self._login('admin_ins')
        shift_id = self.ids['shift_ins']
        resp = self.client.post(f'/attendance-shifts/{shift_id}/edit', data={
            'name': f'صباحي {self.suffix[:4]}',
            'start_time': '07:00',
            'late_after_time': '',
        }, follow_redirects=False)
        self.assertIn(resp.status_code, (200, 302))

        with self.app.app_context():
            shift = self._shift('ins')
            self.assertIsNone(shift.late_after_time)
            self.assertEqual(shift.start_time, time(7, 0))
            # The legacy column keeps its stored value — never rewritten.
            self.assertEqual(shift.absent_after_time, time(11, 0))

    def test_school_cannot_clear_shift_lateness(self):
        self._login('admin_sch')
        shift_id = self.ids['shift_sch']
        self.client.post(f'/attendance-shifts/{shift_id}/edit', data={
            'name': f'صباحي {self.suffix[:4]}',
            'start_time': '07:00',
            'late_after_time': '',
        }, follow_redirects=False)

        with self.app.app_context():
            self.assertEqual(self._shift('sch').late_after_time, time(8, 0),
                             'school shift lateness must be preserved')

    # ── 3. FORM markup ───────────────────────────────────────────────────────

    def _settings_html(self, tag):
        self._login(f'admin_{tag}')
        resp = self.client.get('/admin/attendance-settings')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_institute_form_has_no_required_lateness(self):
        html = self._settings_html('ins')
        self.assertIn('اختياري — اتركه فارغاً لتعطيل التأخر', html)
        # Neither the create input nor the edit-modal input carries `required`.
        self.assertNotIn('name="late_after_time" class="form-control"\n'
                         '                 style="border-color:#e8a020" required', html)
        self.assertNotIn('id="editShiftLate" class="form-control" required', html)
        for fragment in ('name="late_after_time"', 'id="editShiftLate"'):
            idx = html.find(fragment)
            self.assertNotEqual(idx, -1)
            self.assertNotIn('required', html[idx:idx + 200].split('>')[0],
                             f'{fragment} still marked required for institute')

    def test_school_form_keeps_required_lateness(self):
        html = self._settings_html('sch')
        self.assertNotIn('اختياري — اتركه فارغاً لتعطيل التأخر', html)
        for fragment in ('name="late_after_time"', 'id="editShiftLate"'):
            idx = html.find(fragment)
            self.assertNotEqual(idx, -1)
            self.assertIn('required', html[idx:idx + 200].split('>')[0],
                          f'{fragment} lost its required attribute for a school')

    # ── 4. PROCESSING — no None comparison, no automatic lateness ────────────

    def test_null_shift_cutoff_never_raises_and_is_present(self):
        with self.app.app_context():
            school = self._school('ins')
            shift = self._shift('ins')
            shift.late_after_time = None
            db.session.commit()

            school, shift = self._school('ins'), self._shift('ins')
            # School-level threshold IS set (08:00); the cleared shift cutoff
            # must not fall back to it.
            self.assertEqual(school.att_late_threshold, time(8, 0))
            self.assertEqual(
                determine_check_in_status(time(23, 59), school, shift=shift),
                'present')

    def test_institute_global_disable_still_applies(self):
        with self.app.app_context():
            school = self._school('ins')
            school.att_late_threshold = None
            db.session.commit()
            school, shift = self._school('ins'), self._shift('ins')
            # Shift cutoff configured (08:00), global switch off → present.
            self.assertEqual(
                determine_check_in_status(time(9, 30), school, shift=shift),
                'present')

    def test_configured_cutoffs_still_calculate_lateness(self):
        with self.app.app_context():
            school, shift = self._school('ins'), self._shift('ins')
            self.assertEqual(
                determine_check_in_status(time(9, 30), school, shift=shift), 'late')
            self.assertEqual(
                determine_check_in_status(time(7, 30), school, shift=shift), 'present')

    def test_school_with_null_shift_cutoff_does_not_raise(self):
        """Defensive: a school row that somehow has NULL falls back safely."""
        with self.app.app_context():
            school = self._school('sch')
            shift = self._shift('sch')
            shift.late_after_time = None
            db.session.commit()
            school, shift = self._school('sch'), self._shift('sch')
            # Falls back to the school threshold (08:00) exactly as before.
            self.assertEqual(
                determine_check_in_status(time(9, 30), school, shift=shift), 'late')
            school.att_late_threshold = None
            db.session.commit()
            school = self._school('sch')
            self.assertEqual(
                determine_check_in_status(time(9, 30), school, shift=self._shift('sch')),
                'present')

    def test_employee_lateness_option_applies_to_institutes(self):
        """Employees have no shifts — the school-level switch governs them."""
        with self.app.app_context():
            school = self._school('ins')
            self.assertEqual(determine_check_in_status(time(9, 30), school), 'late')
            school.att_late_threshold = None
            db.session.commit()
            school = self._school('ins')
            self.assertEqual(determine_check_in_status(time(9, 30), school),
                             'present')

    # ── 5. SWITCH BACK TO SCHOOL ─────────────────────────────────────────────

    def test_switch_back_to_school_is_refused_when_cutoff_missing(self):
        with self.app.app_context():
            shift = self._shift('ins')
            shift.late_after_time = None
            db.session.commit()

        self._login('super')
        school_id = self.ids['school_ins']
        resp = self.client.post(f'/schools/{school_id}/edit', data={
            'school_name': f'SL ins {self.suffix}',
            'capacity': '0',
            'institution_type': 'school',
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 200, 'must re-render with an error')
        self.assertIn('لا يمكن التحويل إلى «مدرسة»', resp.get_data(as_text=True))

        with self.app.app_context():
            school = self._school('ins')
            self.assertEqual(school.institution_type, 'institute',
                             'the switch must not have been applied')
            self.assertIsNone(self._shift('ins').late_after_time,
                              'no cutoff may be invented by the refusal')

    def test_switch_back_to_school_succeeds_when_cutoffs_present(self):
        self._login('super')
        school_id = self.ids['school_ins']
        resp = self.client.post(f'/schools/{school_id}/edit', data={
            'school_name': f'SL ins {self.suffix}',
            'capacity': '0',
            'institution_type': 'school',
        }, follow_redirects=False)
        self.assertIn(resp.status_code, (200, 302))

        with self.app.app_context():
            school = self._school('ins')
            self.assertEqual(school.institution_type, 'school')
            self.assertFalse(school.is_institute)
            self.assertEqual(self._shift('ins').late_after_time, time(8, 0))


if __name__ == '__main__':
    unittest.main()
