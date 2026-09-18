"""Focused coverage for the opt-in institute mode (School.institution_type).

Scope is deliberately narrow — exactly the three claims the change makes:

  1. SCHOOL behaviour is unchanged: an institution with institution_type NULL
     (every existing row, since nothing is backfilled) still generates automatic
     absence and still notifies parents.
  2. INSTITUTE automatic absence is skipped on every generation path: the
     unified path, the scheduler tick, the midnight catch-up, the shift-mode
     web trigger and the shiftless fallback — with no attendance row and no
     absence notification written, and historical rows left intact.
  3. MANUAL daily attendance still works for an institute, through the real
     /attendance/take route: present and absent both record normally, one row
     per student per day.

Plus the two form guarantees: an edit request that omits نوع المؤسسة preserves
the stored choice, and an unrecognised value never switches an institution into
institute mode.

Nothing here touches production, devices, payroll, staff attendance or any
unrelated feature.
"""
import unittest
from datetime import date, datetime, time, timedelta
from uuid import uuid4

from sqlalchemy import text

from app import create_app
from app.models import (
    db, AcademicYear, AttendanceShift, Grade, Notification, Role, School,
    Section, Student, StudentAttendance, User, parent_students,
)


TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)


class InstitutionTypeAutoAbsenceTest(unittest.TestCase):
    """One 'school' institution and one 'institute' institution, side by side."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.ids = {}
        with self.app.app_context():
            admin_role = Role.query.filter_by(name='school_admin').first()
            parent_role = Role.query.filter_by(name='parent').first()
            super_role = Role.query.filter_by(name='super_admin').first()
            for role in (admin_role, parent_role, super_role):
                self.assertIsNotNone(role, 'seed roles before running')

            # tag 'sch' → institution_type NULL (existing school behaviour)
            # tag 'ins' → institution_type 'institute' (explicit opt-in)
            for tag, inst_type in (('sch', None), ('ins', 'institute')):
                school = School(
                    school_name=f'Inst {tag} {self.suffix}',
                    code=f'IT{tag.upper()}{self.suffix[:5]}',
                    capacity=0, is_active=True,
                    institution_type=inst_type,
                    # Cutoff already passed for any wall-clock time in the day,
                    # so "school" genuinely generates absence during the test.
                    att_absence_threshold=time(0, 1),
                    weekly_off_days=None,
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

                section = Section(school_id=school.id, academic_year_id=year.id,
                                  grade_id=grade.id,
                                  name=f'S{tag}{self.suffix[:4]}', capacity=30)
                db.session.add(section)
                db.session.flush()

                student = Student(student_id=f'IT-{tag.upper()}-{self.suffix}',
                                  full_name=f'Student {tag} {self.suffix}',
                                  date_of_birth=date(2015, 1, 1), gender='male',
                                  school_id=school.id, academic_year_id=year.id,
                                  section_id=section.id, status='active')
                db.session.add(student)
                db.session.flush()

                parent = User(username=f'it_p_{tag}_{self.suffix}',
                              email=f'it_p_{tag}_{self.suffix}@example.test',
                              full_name=f'Parent {tag}', role_id=parent_role.id,
                              school_id=school.id, is_active=True)
                parent.set_password('Password123')
                admin = User(username=f'it_ad_{tag}_{self.suffix}',
                             email=f'it_ad_{tag}_{self.suffix}@example.test',
                             full_name=f'Admin {tag}', role_id=admin_role.id,
                             school_id=school.id, is_active=True)
                admin.set_password('Password123')
                db.session.add_all([parent, admin])
                db.session.flush()

                db.session.execute(parent_students.insert().values(
                    user_id=parent.id, student_id=student.id))

                self.ids.update({
                    f'school_{tag}': school.id, f'year_{tag}': year.id,
                    f'grade_{tag}': grade.id, f'section_{tag}': section.id,
                    f'student_{tag}': student.id, f'parent_{tag}': parent.id,
                    f'admin_{tag}': admin.id,
                })

            superu = User(username=f'it_su_{self.suffix}',
                          email=f'it_su_{self.suffix}@example.test',
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
            stids = [self.ids['student_sch'], self.ids['student_ins']]
            db.session.execute(text(
                'DELETE FROM student_attendance WHERE school_id = ANY(:s)'),
                {'s': sids})
            db.session.execute(text(
                'DELETE FROM notifications WHERE school_id = ANY(:s)'), {'s': sids})
            # push_notifications.user_id is NOT NULL, so these rows must go
            # before the users they reference.
            db.session.execute(text(
                'DELETE FROM push_notifications WHERE school_id = ANY(:s)'), {'s': sids})
            db.session.execute(text(
                'DELETE FROM parent_students WHERE student_id = ANY(:s)'), {'s': stids})
            db.session.execute(text(
                'DELETE FROM audit_logs WHERE school_id = ANY(:s)'), {'s': sids})
            # sections/grades reference attendance_shifts — release the FK first.
            db.session.execute(text(
                'UPDATE sections SET shift_id = NULL WHERE school_id = ANY(:s)'),
                {'s': sids})
            db.session.execute(text(
                'UPDATE grades SET shift_id = NULL WHERE school_id = ANY(:s)'),
                {'s': sids})
            db.session.execute(text(
                'DELETE FROM attendance_shifts WHERE school_id = ANY(:s)'), {'s': sids})
            for model, keys in ((Student, ['student_sch', 'student_ins']),
                                (User, ['parent_sch', 'parent_ins',
                                        'admin_sch', 'admin_ins', 'super']),
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

    def _login(self, user_id):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True

    def _school(self, tag):
        return db.session.get(School, self.ids[f'school_{tag}'],
                              execution_options={'bypass_tenant_scope': True})

    def _year(self, tag):
        return db.session.get(AcademicYear, self.ids[f'year_{tag}'],
                              execution_options={'bypass_tenant_scope': True})

    def _attendance_rows(self, tag, on_date=None):
        q = (StudentAttendance.query
             .execution_options(bypass_tenant_scope=True)
             .filter_by(student_id=self.ids[f'student_{tag}']))
        if on_date is not None:
            q = q.filter_by(date=on_date)
        return q.all()

    def _absence_notifications(self, tag):
        return (Notification.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=self.ids[f'school_{tag}'])
                .all())

    # ── 1. SCHOOL behaviour unchanged ────────────────────────────────────────

    def test_school_mode_still_generates_automatic_absence(self):
        """institution_type IS NULL → automatic absence runs exactly as before."""
        from app.blueprints.attendance import _run_auto_absent
        with self.app.app_context():
            school, year = self._school('sch'), self._year('sch')
            result = _run_auto_absent(school, year, school, target_date=TODAY)

            self.assertFalse(result.get('institute'),
                             'a NULL institution_type must not be treated as institute')
            self.assertEqual(result['count'], 1)

            rows = self._attendance_rows('sch', TODAY)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, 'absent')
            self.assertEqual(rows[0].source, 'automatic')
            self.assertEqual(rows[0].school_id, self.ids['school_sch'])
            self.assertEqual(rows[0].academic_year_id, self.ids['year_sch'])
            self.assertTrue(self._absence_notifications('sch'),
                            'school mode must still notify parents')

            # Isolation: the institute next door is untouched by this run.
            self.assertEqual(self._attendance_rows('ins'), [])

    # ── 2. INSTITUTE automatic absence skipped on every path ─────────────────

    def test_institute_unified_path_skipped(self):
        from app.blueprints.attendance import _run_auto_absent
        with self.app.app_context():
            school, year = self._school('ins'), self._year('ins')
            result = _run_auto_absent(school, year, school, target_date=TODAY)

            self.assertTrue(result.get('institute'))
            self.assertEqual(result['count'], 0)
            self.assertEqual(result['students'], [])
            self.assertFalse(result['holiday'])
            self.assertFalse(result['too_early'])
            self.assertEqual(self._attendance_rows('ins'), [],
                             'institute must get NO automatic attendance row')
            self.assertEqual(self._absence_notifications('ins'), [],
                             'institute must get NO automatic absence notification')

    def test_institute_scheduler_tick_and_catchup_skipped(self):
        """Background job + midnight catch-up both generate nothing."""
        from app.services.auto_attendance import _check_school, _catchup_previous_day
        with self.app.app_context():
            school = self._school('ins')
            _check_school(school)
            # hour < _CATCHUP_WINDOW_HOURS → the midnight catch-up really runs.
            _catchup_previous_day(school, school.school_name,
                                  datetime.combine(TODAY, time(0, 30)), TODAY)

            self.assertEqual(self._attendance_rows('ins'), [])
            self.assertEqual(self._absence_notifications('ins'), [])

    def test_institute_shift_mode_paths_skipped(self):
        """Shift web-trigger, per-shift pass and shiftless fallback all skip."""
        from app.services.auto_attendance import (
            run_school_shift_auto_absent_now, _run_auto_absent_for_shift,
            _run_auto_absent_shiftless,
        )
        with self.app.app_context():
            school, year = self._school('ins'), self._year('ins')
            school.enable_attendance_shifts = True
            school.shift_absent_after_time = time(0, 1)
            shift = AttendanceShift(school_id=school.id, name='صباحي',
                                    start_time=time(7, 30),
                                    late_after_time=time(8, 0),
                                    absent_after_time=time(9, 0),
                                    is_active=True)
            db.session.add(shift)
            db.session.commit()

            section = db.session.get(
                Section, self.ids['section_ins'],
                execution_options={'bypass_tenant_scope': True})
            section.shift_id = shift.id
            db.session.commit()

            summary = run_school_shift_auto_absent_now(school, year, school)
            self.assertTrue(summary.get('institute'))
            self.assertEqual(summary['count'], 0)

            # The inner writers are guarded independently of their callers.
            self.assertEqual(
                _run_auto_absent_for_shift(school, year, shift, TODAY)['count'], 0)
            self.assertEqual(
                _run_auto_absent_shiftless(school, year, school, TODAY,
                                           force=True)['count'], 0)

            self.assertEqual(self._attendance_rows('ins'), [])
            self.assertEqual(self._absence_notifications('ins'), [])

    def test_institute_keeps_historical_absence_and_settings(self):
        """Nothing is erased: past records and att_* cutoffs survive untouched."""
        from app.blueprints.attendance import _run_auto_absent
        with self.app.app_context():
            db.session.add(StudentAttendance(
                student_id=self.ids['student_ins'],
                school_id=self.ids['school_ins'],
                academic_year_id=self.ids['year_ins'],
                date=YESTERDAY, status='absent', source='automatic'))
            db.session.commit()

            school, year = self._school('ins'), self._year('ins')
            _run_auto_absent(school, year, school, target_date=TODAY)

            kept = self._attendance_rows('ins', YESTERDAY)
            self.assertEqual(len(kept), 1, 'historical absence must not be erased')
            self.assertEqual(kept[0].status, 'absent')
            self.assertEqual(self._school('ins').att_absence_threshold, time(0, 1),
                             'attendance cutoff settings must not be rewritten')

    def test_switching_back_to_school_restores_generation(self):
        from app.blueprints.attendance import _run_auto_absent
        with self.app.app_context():
            school = self._school('ins')
            school.institution_type = School.INSTITUTION_SCHOOL
            db.session.commit()

            school, year = self._school('ins'), self._year('ins')
            result = _run_auto_absent(school, year, school, target_date=TODAY)

            self.assertFalse(result.get('institute'))
            self.assertEqual(result['count'], 1)
            rows = self._attendance_rows('ins', TODAY)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, 'absent')
            self.assertEqual(rows[0].source, 'automatic')

    # ── 3. MANUAL attendance unchanged for an institute ──────────────────────

    def test_institute_manual_attendance_present_then_absent(self):
        """The real /attendance/take route still records one row per day."""
        self._login(self.ids['admin_ins'])
        section_id = self.ids['section_ins']
        student_id = self.ids['student_ins']

        resp = self.client.post(
            f'/attendance/take/{section_id}?date={TODAY.isoformat()}',
            data={f'status_{student_id}': 'present'}, follow_redirects=False)
        self.assertIn(resp.status_code, (200, 302), resp.status_code)

        with self.app.app_context():
            rows = self._attendance_rows('ins', TODAY)
            self.assertEqual(len(rows), 1, 'exactly one daily record per student')
            self.assertIn(rows[0].status, ('present', 'late'))
            self.assertIsNotNone(rows[0].check_in)

        # Manual absence on another day remains allowed.
        resp = self.client.post(
            f'/attendance/take/{section_id}?date={YESTERDAY.isoformat()}',
            data={f'status_{student_id}': 'absent'}, follow_redirects=False)
        self.assertIn(resp.status_code, (200, 302))

        with self.app.app_context():
            rows = self._attendance_rows('ins', YESTERDAY)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, 'absent')
            self.assertEqual(rows[0].recorded_by, self.ids['admin_ins'])

    def test_institute_students_are_not_auto_marked_present(self):
        """Skipping auto-absence must not silently create 'present' rows."""
        from app.services.auto_attendance import _check_school
        with self.app.app_context():
            _check_school(self._school('ins'))
            self.assertEqual(self._attendance_rows('ins'), [],
                             'no attendance row of ANY status may be generated')

    # ── 4. Form contract ─────────────────────────────────────────────────────

    def test_edit_omitting_institution_type_preserves_stored_choice(self):
        self._login(self.ids['super'])
        school_id = self.ids['school_ins']
        resp = self.client.post(
            f'/schools/{school_id}/edit',
            data={'school_name': f'Renamed {self.suffix}', 'capacity': '0'},
            follow_redirects=False)
        self.assertIn(resp.status_code, (200, 302))

        with self.app.app_context():
            school = self._school('ins')
            self.assertEqual(school.institution_type, 'institute',
                             'an edit omitting the field must preserve the choice')
            self.assertTrue(school.is_institute)

    def test_unrecognised_value_never_enables_institute_mode(self):
        from app.blueprints.schools import _parse_institution_type
        for raw in (None, '', '   ', 'INSTITUTE_X', 'معهد', 'true', 1):
            self.assertIsNone(_parse_institution_type(raw), repr(raw))
        self.assertEqual(_parse_institution_type(' Institute '), 'institute')
        self.assertEqual(_parse_institution_type('school'), 'school')

        with self.app.app_context():
            school = self._school('sch')
            school.institution_type = 'INSTITUTE_X'
            self.assertFalse(school.is_institute,
                             'only the exact opt-in value enables institute mode')
            db.session.rollback()


if __name__ == '__main__':
    unittest.main()
