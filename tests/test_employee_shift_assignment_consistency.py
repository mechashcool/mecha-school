# -*- coding: utf-8 -*-
"""Employee ↔ shift assignment — ONE canonical "valid shift" rule everywhere.

Canonical rule (app.utils.attendance_helpers.is_valid_employee_shift):
    same school AND active AND start_time AND absent_after_time
    AND absent_after_time > COALESCE(late_after_time, start_time)

Pinned:
  * the pure rule and its reason codes;
  * employee create / edit / restore enforce it server-side in shift mode,
    reject crafted foreign / inactive / invalid ids, never auto-select or
    silently replace a shift; with shift mode OFF a valid shift may be saved
    as optional preparation (runtime keeps the general employee times) so all
    employees can be assigned BEFORE the mode is enabled;
  * the attendance-settings OFF→ON activation is refused while any ACTIVE
    employee is invalid, and lists the employees + reasons (current school
    only; archived / on_leave / terminated never block);
  * the three side doors (teacher user create, teacher role change, transport
    "new driver") cannot create an ACTIVE NULL-shift Employee in shift mode;
  * the runtime resolvers (AI Face / manual sheet / auto-absence / payroll)
    use the same rule, stay fail-closed, and keep their query counts.

DB-backed parts run only against the isolated loopback *_test database
(see the TEST_DATABASE_URL guard); every row is created and removed here.
"""
import re
import unittest
from datetime import date, time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import event

from app import create_app
from app.models import (db, AcademicYear, AuditLog, Employee,
                        EmployeeAttendanceShift, Role, School, TransportRoute,
                        User, teacher_subjects)
from app.utils.attendance_helpers import (
    EMPLOYEE_SHIFT_INVALID_REASONS,
    employee_shift_invalid_reason,
    get_effective_attendance_settings,
    get_employee_shift,
    get_employee_shift_map,
    is_valid_employee_shift,
    list_invalid_shift_employees,
)

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}
MSG_REQUIRED = 'يجب تعيين شفت صالح للموظف لأن نظام شفتات الموظفين مفعّل.'
MSG_INVALID = 'الشفت المحدد غير صالح لهذه المدرسة.'
MSG_NO_VALID_SHIFTS = 'لا توجد شفتات صالحة ونشطة. أنشئ أو فعّل شفتاً صالحاً قبل حفظ موظف نشط.'


def _stub(**kw):
    values = dict(id=1, school_id=1, name='S', is_active=True,
                  start_time=time(7, 0), late_after_time=time(7, 30),
                  absent_after_time=time(9, 0), dismissal_time=time(13, 0))
    values.update(kw)
    return SimpleNamespace(**values)


# ─────────────────────────────────────────────────────────────────────────────
#  1. The pure canonical rule
# ─────────────────────────────────────────────────────────────────────────────

class CanonicalRuleTest(unittest.TestCase):

    def test_valid_shift(self):
        self.assertTrue(is_valid_employee_shift(_stub(), 1))
        self.assertIsNone(employee_shift_invalid_reason(_stub(), 1))

    def test_each_failing_condition_has_its_reason(self):
        cases = [
            (None, 'missing'),
            (_stub(school_id=2), 'foreign'),
            (_stub(is_active=False), 'inactive'),
            (_stub(start_time=None), 'no_start_time'),
            (_stub(absent_after_time=None), 'no_absence_time'),
            (_stub(absent_after_time=time(7, 30)), 'bad_absence_order'),   # equal
            (_stub(absent_after_time=time(7, 15)), 'bad_absence_order'),   # before
        ]
        for shift, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(employee_shift_invalid_reason(shift, 1), reason)
                self.assertFalse(is_valid_employee_shift(shift, 1))
                self.assertIn(reason, EMPLOYEE_SHIFT_INVALID_REASONS)

    def test_boundary_falls_back_to_start_when_lateness_is_off(self):
        no_late = _stub(late_after_time=None, absent_after_time=time(7, 1))
        self.assertTrue(is_valid_employee_shift(no_late, 1))
        self.assertEqual(employee_shift_invalid_reason(
            _stub(late_after_time=None, absent_after_time=time(7, 0)), 1),
            'bad_absence_order')

    def test_dismissal_time_is_not_part_of_the_rule(self):
        self.assertTrue(is_valid_employee_shift(_stub(dismissal_time=None), 1))

    def test_missing_school_is_never_valid(self):
        self.assertEqual(employee_shift_invalid_reason(_stub(), None), 'foreign')

    def test_fallback_semantics_unchanged(self):
        """late/dismissal still fall back per field; the employee absence cutoff
        in shift mode never falls back to a school value."""
        school = SimpleNamespace(
            id=1, emp_enable_attendance_shifts=True,
            emp_att_late_threshold=time(8, 15), emp_att_departure_time=time(15, 45),
            emp_att_absence_threshold=time(10, 30), emp_att_start_time=time(7, 0),
            emp_shift_absent_after_time=time(10, 0))
        eff = get_effective_attendance_settings(
            school, 'employees', shift=_stub(late_after_time=None, dismissal_time=None,
                                             absent_after_time=time(9, 0)))
        self.assertEqual(eff.late_threshold, time(8, 15))
        self.assertEqual(eff.departure_time, time(15, 45))
        self.assertEqual(eff.absence_cutoff, time(9, 0))
        self.assertIsNone(get_effective_attendance_settings(
            school, 'employees', shift=None).absence_cutoff)


class SourceContractTest(unittest.TestCase):
    """Every consumer goes through the shared rule; no private copy remains."""

    def _src(self, rel):
        return (ROOT / rel).read_text(encoding='utf-8')

    def test_consumers_use_shared_resolvers_and_fail_closed(self):
        ai = self._src('app/services/ai_face_ws.py')
        self.assertIn('emp_shift = get_employee_shift(employee, school)', ai)
        self.assertRegex(ai, r"emp_enable_attendance_shifts', False\)\s*\n\s*and emp_shift is None\):"
                             r"[\s\S]{0,400}?return 'skipped'")
        auto = self._src('app/services/auto_attendance.py')
        self.assertIn('shift_map = get_employee_shift_map(school, employees)', auto)
        self.assertIn('valid_shift_employee_ids = set(shift_map)', auto)
        manual = self._src('app/blueprints/employees/__init__.py')
        self.assertIn('_emp_shift_cache = get_employee_shift_map(school, employees)', manual)
        self.assertRegex(manual, r"and emp_shift is None\):\s*\n\s*invalid_shift_skipped \+= 1")

    def test_no_private_validity_copies_remain(self):
        admin = self._src('app/blueprints/admin/__init__.py')
        self.assertIn('list_invalid_shift_employees(settings_row)', admin)
        self.assertNotIn('EmployeeAttendanceShift.absent_after_time\n', admin)
        emp = self._src('app/blueprints/employees/__init__.py')
        self.assertNotIn('absent_after_time.isnot(None)', emp)
        self.assertIn('is_valid_employee_shift(shift, school.id)', emp)


# ─────────────────────────────────────────────────────────────────────────────
#  2. DB-backed: routes, side doors and runtime
# ─────────────────────────────────────────────────────────────────────────────

class EmployeeShiftAssignmentDbTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False
        with cls.app.app_context():
            cls.role_ids = {n: Role.query.filter_by(name=n).first().id
                            for n in ('school_admin', 'teacher', 'parent')}

    # ── fixture ───────────────────────────────────────────────────────────────

    def _add(self, obj):
        db.session.add(obj)
        db.session.flush()
        return obj

    def _shift(self, school, key, start=time(7, 0), late=time(7, 30),
               absent=time(9, 0), active=True):
        s = self._add(EmployeeAttendanceShift(
            school_id=school.id, name=f'{key} {self.sfx}', start_time=start,
            late_after_time=late, absent_after_time=absent,
            dismissal_time=time(13, 0), is_active=active))
        self.ids[key] = s.id
        return s

    def _emp(self, school, key, shift_id=None, status='active'):
        e = self._add(Employee(employee_id=f'{key}-{self.sfx}',
                               full_name=f'Emp {key} {self.sfx}', school_id=school.id,
                               base_salary=0, status=status, shift_id=shift_id))
        self.ids[key] = e.id
        return e

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        with self.app.app_context():
            for key in ('a', 'b'):
                s = self._add(School(school_name=f'ShiftCons {key} {self.sfx}',
                                     code=f'SC{key}{self.sfx}'[:20], capacity=0,
                                     is_active=True, emp_enable_attendance_shifts=False))
                self._add(AcademicYear(school_id=s.id, name=f'Y{key}{self.sfx}',
                                       is_current=True, start_date=date(2026, 8, 1),
                                       end_date=date(2027, 6, 30)))
                u = User(username=f'sc{key}_{self.sfx}', email=f'sc{key}_{self.sfx}@t.test',
                         full_name=f'admin {key}', role_id=self.role_ids['school_admin'],
                         school_id=s.id, is_active=True)
                u.set_password(PASSWORD)
                self._add(u)
                self.ids[f'school_{key}'] = s.id
            a = db.session.get(School, self.ids['school_a'])
            b = db.session.get(School, self.ids['school_b'])
            self._shift(a, 'sh_valid')
            self._shift(a, 'sh_valid2', start=time(13, 0), late=time(13, 30), absent=time(15, 0))
            self._shift(a, 'sh_inactive', active=False)
            self._shift(a, 'sh_noabs', absent=None)
            self._shift(a, 'sh_badorder', absent=time(7, 20))
            self._shift(b, 'sh_b')
            self._emp(a, 'e_valid', self.ids['sh_valid'])
            self._emp(a, 'e_none')
            self._emp(a, 'e_inactive', self.ids['sh_inactive'])
            self._emp(a, 'e_noabs', self.ids['sh_noabs'])
            self._emp(a, 'e_badorder', self.ids['sh_badorder'])
            self._emp(a, 'e_foreign', self.ids['sh_b'])
            self._emp(a, 'e_archived', status='archived')
            self._emp(a, 'e_leave', status='on_leave')
            self._emp(a, 'e_term', status='terminated')
            self._emp(b, 'e_b_none')
            parent = User(username=f'scp_{self.sfx}', email=f'scp_{self.sfx}@t.test',
                          full_name=f'parent {self.sfx}', role_id=self.role_ids['parent'],
                          school_id=a.id, is_active=True)
            parent.set_password(PASSWORD)
            self._add(parent)
            self.ids['parent_user'] = parent.id
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            sids = [self.ids['school_a'], self.ids['school_b']]
            uids = [u.id for u in User.query.execution_options(**OPTS)
                    .filter(User.school_id.in_(sids)).all()]
            eids = [e.id for e in Employee.query.execution_options(**OPTS)
                    .filter(Employee.school_id.in_(sids)).all()]
            TransportRoute.query.execution_options(**OPTS).filter(
                TransportRoute.school_id.in_(sids)).delete(synchronize_session=False)
            if eids:
                db.session.execute(teacher_subjects.delete().where(
                    teacher_subjects.c.employee_id.in_(eids)))
            if uids:
                AuditLog.query.execution_options(**OPTS).filter(
                    AuditLog.user_id.in_(uids)).delete(synchronize_session=False)
            for model in (AuditLog, Employee, EmployeeAttendanceShift, User, AcademicYear):
                model.query.execution_options(**OPTS).filter(
                    model.school_id.in_(sids)).delete(synchronize_session=False)
            School.query.filter(School.id.in_(sids)).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _mode(self, on, key='a'):
        with self.app.app_context():
            db.session.get(School, self.ids[f'school_{key}']).emp_enable_attendance_shifts = on
            db.session.commit()

    def _client(self, key='a'):
        client = self.app.test_client()
        resp = client.post('/auth/login', data={'username': f'sc{key}_{self.sfx}',
                                                'password': PASSWORD})
        self.assertEqual(resp.status_code, 302)
        return client

    def _emp_row(self, key):
        with self.app.app_context():
            e = db.session.get(Employee, self.ids[key], execution_options=OPTS)
            return e.status, e.shift_id

    def _emps_named(self, name):
        with self.app.app_context():
            return [(e.status, e.shift_id, e.school_id) for e in
                    Employee.query.execution_options(**OPTS).filter_by(full_name=name).all()]

    def _create(self, client, name, shift=None):
        data = {'full_name': name, 'gender': 'male'}
        if shift is not None:
            data['shift_id'] = str(shift)
        return client.post('/employees/create', data=data)

    def _edit(self, client, key, shift=None, status='active', name=None):
        data = {'full_name': name or f'Emp {key} {self.sfx}', 'gender': 'male',
                'status': status}
        if shift is not None:
            data['shift_id'] = str(shift)
        return client.post(f'/employees/{self.ids[key]}/edit', data=data)

    def _selected_options(self, html):
        block = re.search(r'<select name="shift_id"[\s\S]*?</select>', html)
        self.assertIsNotNone(block, 'shift selector not rendered')
        return re.findall(r'<option value="([^"]*)"[^>]*?\bselected\b[^>]*>', block.group(0))

    def _flashes(self, client):
        with client.session_transaction() as sess:
            return [m for _, m in sess.pop('_flashes', [])]

    # ── create ────────────────────────────────────────────────────────────────

    def test_01_mode_off_create_without_shift_allowed(self):
        client = self._client()
        resp = self._create(client, f'Off {self.sfx}')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._emps_named(f'Off {self.sfx}'),
                         [('active', None, self.ids['school_a'])])

    def test_02_mode_off_create_with_valid_shift_saves_it(self):
        """Preparation while OFF: a valid same-school shift IS stored."""
        client = self._client()
        resp = self._create(client, f'OffPrep {self.sfx}', shift=self.ids['sh_valid'])
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._emps_named(f'OffPrep {self.sfx}'),
                         [('active', self.ids['sh_valid'], self.ids['school_a'])])
        # Blank stays allowed while OFF.
        self.assertEqual(self._create(client, f'OffBlank {self.sfx}', shift='').status_code, 302)
        self.assertEqual(self._emps_named(f'OffBlank {self.sfx}'),
                         [('active', None, self.ids['school_a'])])

    def test_02b_mode_off_selector_available_and_optional(self):
        client = self._client()
        html = client.get('/employees/create').get_data(as_text=True)
        block = re.search(r'<select name="shift_id"[^>]*>', html).group(0)
        self.assertNotIn('required', block)
        self.assertEqual(self._selected_options(html), [''])
        self.assertIn('الشفت المحفوظ للتحضير فقط', html)
        html = client.get(f"/employees/{self.ids['e_valid']}/edit").get_data(as_text=True)
        self.assertEqual(self._selected_options(html), [str(self.ids['sh_valid'])])
        html = client.get(f"/employees/{self.ids['e_none']}/edit").get_data(as_text=True)
        self.assertEqual(self._selected_options(html), [''])

    def test_02c_mode_off_edit_assigns_and_persists_valid_shift(self):
        client = self._client()
        resp = self._edit(client, 'e_none', shift=self.ids['sh_valid2'])
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._emp_row('e_none'), ('active', self.ids['sh_valid2']))
        # Field absent -> kept; blank -> cleared (optional while OFF).
        self.assertEqual(self._edit(client, 'e_none').status_code, 302)
        self.assertEqual(self._emp_row('e_none'), ('active', self.ids['sh_valid2']))
        self.assertEqual(self._edit(client, 'e_none', shift='').status_code, 302)
        self.assertEqual(self._emp_row('e_none'), ('active', None))
        # An unchanged legacy (invalid) stored shift is kept, not replaced.
        self.assertEqual(self._edit(client, 'e_inactive',
                                    shift=self.ids['sh_inactive']).status_code, 302)
        self.assertEqual(self._emp_row('e_inactive'), ('active', self.ids['sh_inactive']))

    def test_02d_mode_off_invalid_or_foreign_shift_cannot_be_stored(self):
        client = self._client()
        for key in ('sh_b', 'sh_inactive', 'sh_noabs', 'sh_badorder'):
            with self.subTest(shift=key):
                name = f'OffBad {key} {self.sfx}'
                resp = self._create(client, name, shift=self.ids[key])
                self.assertEqual(resp.status_code, 200)
                self.assertIn(MSG_INVALID, resp.get_data(as_text=True))
                self.assertEqual(self._emps_named(name), [])
                resp = self._edit(client, 'e_valid', shift=self.ids[key])
                self.assertIn(MSG_INVALID, resp.get_data(as_text=True))
                self.assertEqual(self._emp_row('e_valid'), ('active', self.ids['sh_valid']))

    def test_02e_mode_off_saved_shift_does_not_affect_runtime(self):
        with self.app.app_context():
            school = db.session.get(School, self.ids['school_a'])
            school.emp_att_late_threshold = time(8, 15)
            school.emp_att_absence_threshold = time(10, 30)
            school.emp_att_departure_time = time(15, 45)
            db.session.commit()
        client = self._client()
        self.assertEqual(self._edit(client, 'e_none', shift=self.ids['sh_valid']).status_code, 302)
        with self.app.app_context():
            school = db.session.get(School, self.ids['school_a'])
            emp = db.session.get(Employee, self.ids['e_none'], execution_options=OPTS)
            self.assertEqual(emp.shift_id, self.ids['sh_valid'])
            self.assertIsNone(get_employee_shift(emp, school))
            self.assertEqual(get_employee_shift_map(school, [emp]), {})
            eff = get_effective_attendance_settings(
                school, 'employees', shift=get_employee_shift(emp, school))
            self.assertFalse(eff.shift_enabled)
            self.assertEqual((eff.late_threshold, eff.absence_cutoff, eff.departure_time),
                             (time(8, 15), time(10, 30), time(15, 45)))

    def test_02f_prepare_while_off_then_activation_succeeds(self):
        """The documented workflow: OFF -> assign one by one -> enable -> used."""
        client = self._client()
        self.assertIn('emp_shift_blockers=1', self._activate(client).headers['Location'])
        self.assertFalse(self._school_mode())
        for key, shift in (('e_none', 'sh_valid'), ('e_inactive', 'sh_valid2'),
                           ('e_noabs', 'sh_valid'), ('e_badorder', 'sh_valid2'),
                           ('e_foreign', 'sh_valid')):
            with self.subTest(employee=key):
                resp = self._edit(client, key, shift=self.ids[shift])
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(self._emp_row(key), ('active', self.ids[shift]))
                self.assertFalse(self._school_mode())          # still OFF
        resp = self._activate(client)
        self.assertNotIn('emp_shift_blockers', resp.headers['Location'])
        self.assertTrue(self._school_mode())
        with self.app.app_context():
            school = db.session.get(School, self.ids['school_a'])
            emp = db.session.get(Employee, self.ids['e_inactive'], execution_options=OPTS)
            self.assertEqual(get_employee_shift(emp, school).id, self.ids['sh_valid2'])

    def test_03_mode_on_create_without_shift_rejected(self):
        self._mode(True)
        client = self._client()
        resp = self._create(client, f'NoShift {self.sfx}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(MSG_REQUIRED, resp.get_data(as_text=True))
        self.assertEqual(self._emps_named(f'NoShift {self.sfx}'), [])
        resp = self._create(client, f'Empty {self.sfx}', shift='')
        self.assertIn(MSG_REQUIRED, resp.get_data(as_text=True))
        self.assertEqual(self._emps_named(f'Empty {self.sfx}'), [])

    def test_04_mode_on_valid_same_school_shift_persists(self):
        self._mode(True)
        client = self._client()
        resp = self._create(client, f'Valid {self.sfx}', shift=self.ids['sh_valid2'])
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._emps_named(f'Valid {self.sfx}'),
                         [('active', self.ids['sh_valid2'], self.ids['school_a'])])

    def test_05_mode_on_invalid_shift_ids_rejected(self):
        self._mode(True)
        client = self._client()
        for key in ('sh_b', 'sh_inactive', 'sh_noabs', 'sh_badorder'):
            with self.subTest(shift=key):
                name = f'Bad {key} {self.sfx}'
                resp = self._create(client, name, shift=self.ids[key])
                self.assertEqual(resp.status_code, 200)
                html = resp.get_data(as_text=True)
                self.assertIn(MSG_INVALID, html)
                self.assertEqual(self._emps_named(name), [])
                # Re-render never pre-selects a shift the server refused.
                self.assertEqual(self._selected_options(html), [''])
        for raw in ('abc', '99999999999999999999', '-1', '٣'):
            with self.subTest(raw=raw):
                name = f'Raw {self.sfx} {len(raw)}'
                resp = self._create(client, name, shift=raw)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(self._emps_named(name), [])

    def test_06_selector_offers_only_valid_same_school_shifts(self):
        self._mode(True)
        html = self._client().get('/employees/create').get_data(as_text=True)
        block = re.search(r'<select name="shift_id"[\s\S]*?</select>', html).group(0)
        offered = {int(v) for v in re.findall(r'<option value="(\d+)"', block)}
        self.assertEqual(offered, {self.ids['sh_valid'], self.ids['sh_valid2']})
        self.assertEqual(self._selected_options(html), [''])

    # ── edit ──────────────────────────────────────────────────────────────────

    def test_07_edit_no_shift_never_autoselects_first_shift(self):
        self._mode(True)
        html = self._client().get(f"/employees/{self.ids['e_none']}/edit").get_data(as_text=True)
        self.assertEqual(self._selected_options(html), [''])

    def test_08_edit_current_valid_shift_is_selected(self):
        self._mode(True)
        html = self._client().get(f"/employees/{self.ids['e_valid']}/edit").get_data(as_text=True)
        self.assertEqual(self._selected_options(html), [str(self.ids['sh_valid'])])

    def test_09_edit_invalid_current_shift_is_visible_not_replaced(self):
        self._mode(True)
        client = self._client()
        html = client.get(f"/employees/{self.ids['e_inactive']}/edit").get_data(as_text=True)
        self.assertEqual(self._selected_options(html), [str(self.ids['sh_inactive'])])
        self.assertIn('الشفت الحالي غير صالح: الشفت المعيَّن معطَّل', html)
        # Foreign stored shift: reason only — the other school's name never leaks.
        html = client.get(f"/employees/{self.ids['e_foreign']}/edit").get_data(as_text=True)
        self.assertIn('الشفت المعيَّن غير موجود', html)
        self.assertNotIn(f'sh_b {self.sfx}', html)
        # Active employee keeping the invalid shift → refused, nothing changes.
        resp = self._edit(client, 'e_inactive', shift=self.ids['sh_inactive'],
                          name=f'Renamed {self.sfx}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._emp_row('e_inactive'), ('active', self.ids['sh_inactive']))
        self.assertEqual(self._emps_named(f'Renamed {self.sfx}'), [])
        # Field absent → the stored (invalid) shift is re-validated, not kept.
        resp = self._edit(client, 'e_inactive')
        self.assertIn(MSG_REQUIRED, resp.get_data(as_text=True))
        self.assertEqual(self._emp_row('e_inactive'), ('active', self.ids['sh_inactive']))

    def test_10_unrelated_edit_does_not_change_shift(self):
        self._mode(True)
        client = self._client()
        resp = self._edit(client, 'e_valid', name=f'Field absent {self.sfx}')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._emp_row('e_valid'), ('active', self.ids['sh_valid']))
        resp = self._edit(client, 'e_valid', shift=self.ids['sh_valid'],
                          name=f'Same shift {self.sfx}')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._emp_row('e_valid'), ('active', self.ids['sh_valid']))
        # Mode OFF: field absent -> the stored shift is kept.
        self._mode(False)
        resp = self._edit(client, 'e_valid', name=f'Off absent {self.sfx}')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._emp_row('e_valid'), ('active', self.ids['sh_valid']))

    def test_11_valid_shift_change_persists_and_edit_rules(self):
        self._mode(True)
        client = self._client()
        resp = self._edit(client, 'e_valid', shift=self.ids['sh_valid2'])
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._emp_row('e_valid'), ('active', self.ids['sh_valid2']))
        # Clearing the shift of an active employee → refused.
        resp = self._edit(client, 'e_valid', shift='')
        self.assertIn(MSG_REQUIRED, resp.get_data(as_text=True))
        self.assertEqual(self._emp_row('e_valid'), ('active', self.ids['sh_valid2']))
        # Cross-school id on edit → refused, stored value unchanged.
        resp = self._edit(client, 'e_valid', shift=self.ids['sh_b'])
        self.assertIn(MSG_INVALID, resp.get_data(as_text=True))
        self.assertEqual(self._emp_row('e_valid'), ('active', self.ids['sh_valid2']))
        # Fixing an invalid employee with a valid shift works.
        self.assertEqual(self._edit(client, 'e_none', shift=self.ids['sh_valid']).status_code, 302)
        self.assertEqual(self._emp_row('e_none'), ('active', self.ids['sh_valid']))
        # A non-active employee: no shift required; unchanged invalid id kept.
        self.assertEqual(self._edit(client, 'e_leave', status='on_leave').status_code, 302)
        self.assertEqual(self._emp_row('e_leave'), ('on_leave', None))
        # ...but becoming active requires a valid shift.
        resp = self._edit(client, 'e_leave', status='active')
        self.assertIn(MSG_REQUIRED, resp.get_data(as_text=True))
        self.assertEqual(self._emp_row('e_leave'), ('on_leave', None))

    def test_12_zero_valid_shifts_still_renders_selector(self):
        self._mode(True)
        with self.app.app_context():
            for key in ('sh_valid', 'sh_valid2'):
                db.session.get(EmployeeAttendanceShift, self.ids[key],
                               execution_options=OPTS).is_active = False
            db.session.commit()
        client = self._client()
        html = client.get(f"/employees/{self.ids['e_none']}/edit").get_data(as_text=True)
        self.assertIn(MSG_NO_VALID_SHIFTS, html)
        self.assertEqual(self._selected_options(html), [''])
        resp = self._edit(client, 'e_none')
        self.assertIn(MSG_REQUIRED, resp.get_data(as_text=True))
        self.assertEqual(self._emp_row('e_none'), ('active', None))
        self.assertIn(MSG_NO_VALID_SHIFTS,
                      client.get('/employees/create').get_data(as_text=True))

    # ── restore ───────────────────────────────────────────────────────────────

    def test_13_restore_requires_canonical_valid_shift(self):
        self._mode(True)
        client = self._client()
        with self.app.app_context():
            db.session.get(Employee, self.ids['e_archived'],
                           execution_options=OPTS).shift_id = self.ids['sh_noabs']
            db.session.commit()
        client.post(f"/employees/{self.ids['e_archived']}/restore")
        self.assertEqual(self._emp_row('e_archived'), ('archived', self.ids['sh_noabs']))
        with self.app.app_context():
            db.session.get(Employee, self.ids['e_archived'],
                           execution_options=OPTS).shift_id = self.ids['sh_valid']
            db.session.commit()
        client.post(f"/employees/{self.ids['e_archived']}/restore")
        self.assertEqual(self._emp_row('e_archived'), ('active', self.ids['sh_valid']))

    # ── attendance-settings activation ───────────────────────────────────────

    def _activate(self, client):
        return client.post('/admin/attendance-settings',
                           data={'emp_enable_attendance_shifts': 'on'})

    def _school_mode(self, key='a'):
        with self.app.app_context():
            return db.session.get(School, self.ids[f'school_{key}']).emp_enable_attendance_shifts

    def test_14_activation_blocked_and_blockers_listed(self):
        client = self._client()
        resp = self._activate(client)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('emp_shift_blockers=1', resp.headers['Location'])
        self.assertFalse(self._school_mode())
        self.assertTrue(any('5 موظف' in m for m in self._flashes(client)))
        html = client.get(resp.headers['Location']).get_data(as_text=True)
        expected = {
            'e_none': 'لم يُعيَّن له شفت',
            'e_inactive': 'الشفت المعيَّن معطَّل',
            'e_noabs': 'الشفت المعيَّن بلا وقت غياب تلقائي',
            'e_badorder': 'وقت الغياب التلقائي للشفت ليس بعد وقت التأخر/البداية',
            'e_foreign': 'الشفت المعيَّن لا يتبع هذه المدرسة',
        }
        block = re.search(r'id="empShiftBlockers"[\s\S]*?</table>', html).group(0)
        for key, label in expected.items():
            with self.subTest(employee=key):
                row = re.search(rf'Emp {key} {self.sfx}</td>[\s\S]*?</tr>', block)
                self.assertIsNotNone(row, key)
                self.assertIn(label, row.group(0))
        for key in ('e_valid', 'e_archived', 'e_leave', 'e_term'):
            self.assertNotIn(f'Emp {key} {self.sfx}<', block)
        self.assertNotIn(f'Emp e_b_none {self.sfx}', html)       # other school
        self.assertNotIn(f'sh_b {self.sfx}', html)               # foreign shift name

    def test_15_non_active_employees_never_block_and_valid_school_activates(self):
        with self.app.app_context():
            for key in ('e_none', 'e_inactive', 'e_noabs', 'e_badorder', 'e_foreign'):
                db.session.get(Employee, self.ids[key],
                               execution_options=OPTS).shift_id = self.ids['sh_valid']
            db.session.commit()
            school = db.session.get(School, self.ids['school_a'])
            self.assertEqual(list_invalid_shift_employees(school), [])
        client = self._client()
        resp = self._activate(client)
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('emp_shift_blockers', resp.headers['Location'])
        self.assertTrue(self._school_mode())
        self.assertFalse(self._school_mode('b'))

    def test_16_blocker_query_is_one_statement(self):
        with self.app.app_context():
            school = db.session.get(School, self.ids['school_a'])
            with _count_selects() as counter:
                rows = list_invalid_shift_employees(school)
            self.assertEqual(counter['n'], 1)
            self.assertEqual(len(rows), 5)

    # ── side doors ────────────────────────────────────────────────────────────

    def test_17_teacher_user_create_cannot_create_null_shift_employee(self):
        self._mode(True)
        client = self._client()
        uname = f'tch_{self.sfx}'
        client.post('/admin/users/create', data={
            'username': uname, 'full_name': f'Teacher {self.sfx}', 'password': 'Passw0rd!x',
            'role_id': str(self.role_ids['teacher']),
            'school_id': str(self.ids['school_b'])})            # forged — ignored
        with self.app.app_context():
            self.assertIsNone(User.query.execution_options(**OPTS)
                              .filter_by(username=uname).first())
        self.assertEqual(self._emps_named(f'Teacher {self.sfx}'), [])
        self.assertTrue(any('نظام شفتات الموظفين مفعّل' in m for m in self._flashes(client)))
        # Mode OFF: unchanged behaviour (account + employee created).
        self._mode(False)
        client.post('/admin/users/create', data={
            'username': uname, 'full_name': f'Teacher {self.sfx}', 'password': 'Passw0rd!x',
            'role_id': str(self.role_ids['teacher'])})
        self.assertEqual(self._emps_named(f'Teacher {self.sfx}'),
                         [('active', None, self.ids['school_a'])])

    def test_18_teacher_role_change_cannot_create_null_shift_employee(self):
        self._mode(True)
        client = self._client()
        uid = self.ids['parent_user']
        client.post(f'/admin/users/{uid}/edit', data={
            'username': f'scp_{self.sfx}', 'full_name': f'Promoted {self.sfx}',
            'role_id': str(self.role_ids['teacher']), 'is_active': 'on'})
        with self.app.app_context():
            u = db.session.get(User, uid, execution_options=OPTS)
            self.assertEqual(u.role.name, 'parent')                  # rolled back
            self.assertEqual(u.full_name, f'parent {self.sfx}')
            self.assertIsNone(Employee.query.execution_options(**OPTS)
                              .filter_by(user_id=uid).first())
        self.assertTrue(any('نظام شفتات الموظفين مفعّل' in m for m in self._flashes(client)))

    def test_19_transport_new_driver_cannot_create_null_shift_employee(self):
        self._mode(True)
        client = self._client()
        resp = client.post('/transport/create', data={
            'name': f'Route {self.sfx}', 'route_number': '7', 'vehicle_type': 'Bus',
            'vehicle_number': 'P-1', 'capacity': '15', 'status': 'active',
            'driver_mode': 'new', 'new_driver_name': f'Driver {self.sfx}',
            'new_driver_phone': '07701234567'})
        self.assertNotEqual(resp.status_code, 500)
        self.assertEqual(self._emps_named(f'Driver {self.sfx}'), [])
        with self.app.app_context():
            self.assertIsNone(TransportRoute.query.execution_options(**OPTS)
                              .filter_by(name=f'Route {self.sfx}').first())
            self.assertIsNone(User.query.execution_options(**OPTS)
                              .filter_by(full_name=f'Driver {self.sfx}').first())
        body = resp.get_data(as_text=True) + ' '.join(self._flashes(client))
        self.assertIn('نظام شفتات الموظفين مفعّل', body)

    # ── runtime resolvers ─────────────────────────────────────────────────────

    def test_20_runtime_resolvers_use_canonical_rule_fail_closed(self):
        self._mode(True)
        with self.app.app_context():
            school = db.session.get(School, self.ids['school_a'])
            emps = (Employee.query.execution_options(**OPTS)
                    .filter_by(school_id=school.id, status='active').all())
            by_key = {k: db.session.get(Employee, self.ids[k], execution_options=OPTS)
                      for k in ('e_valid', 'e_none', 'e_inactive', 'e_noabs',
                                'e_badorder', 'e_foreign')}
            # Bulk (manual sheet / auto-absence / payroll) — ONE query.
            with _count_selects() as counter:
                shift_map = get_employee_shift_map(school, emps)
            self.assertEqual(counter['n'], 1)
            self.assertEqual(set(shift_map), {self.ids['e_valid']})
            # Single (AI Face) resolver — at most ONE primary-key lookup.
            with _count_selects() as counter:
                shift = get_employee_shift(by_key['e_valid'], school)
            self.assertEqual(shift.id, self.ids['sh_valid'])
            self.assertLessEqual(counter['n'], 1)
            for key in ('e_none', 'e_inactive', 'e_noabs', 'e_badorder', 'e_foreign'):
                with self.subTest(employee=key):
                    resolved = get_employee_shift(by_key[key], school)
                    self.assertIsNone(resolved)
                    # Fail closed: no general absence-cutoff fallback.
                    self.assertIsNone(get_effective_attendance_settings(
                        school, 'employees', shift=resolved).absence_cutoff)
            # Mode OFF → nothing resolves (general settings path unchanged).
            school.emp_enable_attendance_shifts = False
            self.assertIsNone(get_employee_shift(by_key['e_valid'], school))
            self.assertEqual(get_employee_shift_map(school, emps), {})
            db.session.rollback()


class _count_selects:
    """Count SELECT statements on the session engine inside the block."""

    def __enter__(self):
        self.counter = {'n': 0}
        self.engine = db.engine

        def _before(conn, cursor, statement, *args):
            if statement.lstrip().upper().startswith('SELECT'):
                self.counter['n'] += 1
        self._fn = _before
        event.listen(self.engine, 'before_cursor_execute', _before)
        return self.counter

    def __exit__(self, *exc):
        event.remove(self.engine, 'before_cursor_execute', self._fn)
        return False


if __name__ == '__main__':
    unittest.main()
