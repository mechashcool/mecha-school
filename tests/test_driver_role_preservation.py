"""
A linked `driver` account's role must never change on an ordinary save.

  Web  /employees/<id>/edit       Employee Edit (linked-account panel)
       /admin/users/<id>/edit     School User Management edit

Every save is submitted the way a browser would: the edit page is rendered,
its form's default values are collected (selected option, or the first option
when none is selected), one ordinary field is changed and the form is posted.
A forged role_id is then added on top to prove the server side ignores it.

Fixture (per test, unique suffix), one school:
  AA  school_admin
  DA  driver account  ← e_drv  (job 'سائق')
  TA  teacher account ← e_tch
  PA  parent account

Requires an isolated, approved test database (tests/conftest.py guard).
"""
import unittest
from datetime import date
from html.parser import HTMLParser
from uuid import uuid4

from werkzeug.datastructures import MultiDict

from app import create_app
from app.models import db, AcademicYear, AuditLog, Employee, Role, School, User

OPTS = {'bypass_tenant_scope': True}
PASSWORD = 'Test1234!'


class _FormFields(HTMLParser):
    """The fields a browser submits for the form posting to `action`."""

    def __init__(self, action):
        super().__init__()
        self.action, self.inside, self.fields = action, False, []
        self._select = None          # [name, first_value, selected_value]
        self._textarea = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'form':
            self.inside = a.get('action') == self.action
        if not self.inside or 'disabled' in a:
            return
        if tag == 'input' and a.get('name'):
            kind = (a.get('type') or 'text').lower()
            if kind in ('checkbox', 'radio'):
                if 'checked' in a:
                    self.fields.append((a['name'], a.get('value', 'on')))
            elif kind not in ('file', 'submit', 'button', 'reset'):
                self.fields.append((a['name'], a.get('value', '')))
        elif tag == 'select' and a.get('name'):
            self._select = [a['name'], None, None]
        elif tag == 'option' and self._select is not None:
            value = a.get('value', '')
            if self._select[1] is None:
                self._select[1] = value
            if 'selected' in a:
                self._select[2] = value
        elif tag == 'textarea' and a.get('name'):
            self._textarea = [a['name'], '']

    def handle_data(self, data):
        if self._textarea is not None:
            self._textarea[1] += data

    def handle_endtag(self, tag):
        if tag == 'form':
            self.inside = False
        elif tag == 'select' and self._select is not None:
            name, first, chosen = self._select
            if first is not None:
                self.fields.append((name, chosen if chosen is not None else first))
            self._select = None
        elif tag == 'textarea' and self._textarea is not None:
            self.fields.append(tuple(self._textarea))
            self._textarea = None


class DriverRolePreservationTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False
        with cls.app.app_context():
            cls.role_ids = {n: Role.query.filter_by(name=n).first().id
                            for n in ('school_admin', 'teacher', 'parent', 'driver')}

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        with self.app.app_context():
            school = School(school_name=f'DRP {self.sfx}', code=f'DRP{self.sfx}',
                            capacity=0, is_active=True)
            db.session.add(school)
            db.session.flush()
            db.session.add(AcademicYear(school_id=school.id, name=f'Y{self.sfx}',
                                        is_current=True, start_date=date(2026, 8, 1),
                                        end_date=date(2027, 6, 30)))
            self.ids['school'] = school.id
            self._user(school, 'aa', 'school_admin')
            self._emp(school, 'e_drv', self._user(school, 'da', 'driver'), 'سائق')
            self._emp(school, 'e_tch', self._user(school, 'ta', 'teacher'), 'معلم')
            self._user(school, 'pa', 'parent')
            db.session.commit()

    def _user(self, school, label, role):
        u = User(username=f'drp{label}_{self.sfx}', full_name=f'{label} {self.sfx}',
                 role_id=self.role_ids[role], school_id=school.id, is_active=True)
        u.set_password(PASSWORD)
        db.session.add(u)
        db.session.flush()
        self.ids[label] = u.id
        return u

    def _emp(self, school, label, user, job):
        e = Employee(school_id=school.id, employee_id=f'{label}-{self.sfx}',
                     full_name=f'Emp {label} {self.sfx}', job_title=job,
                     phone='07700000001', base_salary=100, status='active',
                     user_id=user.id)
        db.session.add(e)
        db.session.flush()
        self.ids[label] = e.id

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            sid = self.ids['school']
            uids = [u.id for u in User.query.execution_options(**OPTS)
                    .filter(User.school_id == sid).all()]
            AuditLog.query.execution_options(**OPTS).filter(
                AuditLog.user_id.in_(uids)).delete(synchronize_session=False)
            for model in (AuditLog, Employee, User, AcademicYear):
                model.query.execution_options(**OPTS).filter(
                    model.school_id == sid).delete(synchronize_session=False)
            School.query.filter(School.id == sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _web(self):
        client = self.app.test_client()
        resp = client.post('/auth/login', data={'username': f'drpaa_{self.sfx}',
                                                'password': PASSWORD})
        self.assertEqual(resp.status_code, 302)
        return client

    def _browser_save(self, client, url, changes, forged_role=None):
        """Render `url`, submit its default form values with `changes` applied
        (plus a forged role_id when given). Returns the rendered page."""
        page = client.get(url)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        parser = _FormFields(url)
        parser.feed(html)
        self.assertTrue(parser.fields, 'edit form not found on the page')
        data = [(k, v) for k, v in parser.fields if k not in changes]
        data += list(changes.items())
        if forged_role:
            data = [(k, v) for k, v in data if k != 'role_id']
            data.append(('role_id', str(self.role_ids[forged_role])))
        resp = client.post(url, data=MultiDict(data))
        self.assertEqual(resp.status_code, 302)
        return html

    def _user_row(self, label):
        with self.app.app_context():
            u = db.session.get(User, self.ids[label], execution_options=OPTS)
            return u.role.name, u.phone, u.is_active, u.check_password('NewPass#2026')

    def _emp_row(self, label):
        with self.app.app_context():
            e = db.session.get(Employee, self.ids[label], execution_options=OPTS)
            return e.phone, int(e.base_salary), e.job_title, e.user_id

    # ── A. Employee Edit ──────────────────────────────────────────────────────

    def test_a_employee_edit_keeps_driver_role(self):
        client = self._web()
        url = f'/employees/{self.ids["e_drv"]}/edit'
        html = self._browser_save(client, url, {'phone': '07711112222',
                                                'base_salary': '250'})
        self.assertIn('الدور: سائق', html)
        self.assertNotIn('name="role_id"', html)
        self.assertEqual(self._emp_row('e_drv'),
                         ('07711112222', 250, 'سائق', self.ids['da']))
        self.assertEqual(self._user_row('da')[0], 'driver')

        for forged in ('teacher', 'parent'):
            self._browser_save(client, url, {'phone': '07733334444'}, forged)
            self.assertEqual(self._user_row('da')[0], 'driver')
        self.assertEqual(self._emp_row('e_drv')[0], '07733334444')

    # ── B. School User Management ─────────────────────────────────────────────

    def test_b_user_management_keeps_driver_role(self):
        client = self._web()
        url = f'/admin/users/{self.ids["da"]}/edit'
        html = self._browser_save(client, url, {'phone': '07755556666',
                                                'new_password': 'NewPass#2026'})
        self.assertIn('الدور: سائق', html)
        self.assertNotIn('name="role_id"', html)
        self.assertEqual(self._user_row('da'), ('driver', '07755556666', True, True))

        for forged in ('parent', 'teacher'):
            self._browser_save(client, url, {'phone': '07777778888'}, forged)
            self.assertEqual(self._user_row('da')[:3], ('driver', '07777778888', True))
        # The linked Employee is not touched by an account edit.
        self.assertEqual(self._emp_row('e_drv')[2:], ('سائق', self.ids['da']))

    # ── C. teacher/parent role changes still work ─────────────────────────────

    def test_c_teacher_parent_role_edit_unchanged(self):
        client = self._web()
        # Employee Edit: a teacher-linked account keeps the role dropdown, an
        # ordinary save keeps 'teacher', and an explicit choice still applies.
        url = f'/employees/{self.ids["e_tch"]}/edit'
        html = self._browser_save(client, url, {'phone': '07799990000'})
        self.assertIn('name="role_id"', html)
        self.assertEqual(self._user_row('ta')[0], 'teacher')
        self._browser_save(client, url, {}, 'parent')
        self.assertEqual(self._user_row('ta')[0], 'parent')

        # User Management: a parent account can still be switched to teacher
        # by the school manager (its role dropdown is still rendered).
        url = f'/admin/users/{self.ids["pa"]}/edit'
        html = self._browser_save(client, url, {}, 'teacher')
        self.assertIn('name="role_id"', html)
        self.assertEqual(self._user_row('pa')[0], 'teacher')


if __name__ == '__main__':
    unittest.main()
