"""Institute "الجداول الدراسية" (all-groups schedules page) — focused guarantees.

  1.  An institute manager sees its study-group schedules.
  2.  "كل المجموعات" renders one separate card per authorized group.
  3.  The group filter narrows the page to the selected group only.
  4.  A foreign / nonexistent group id is a 404.
  5.  Another institute's groups and slots never appear.
  6.  The page reaches the EXISTING add / edit / delete routes, which return to
      the page only on an allow-listed flag and keep their default otherwise.
  7.  Teacher restrictions are unchanged (no schedule access by default; a
      teacher holding the permission still sees only their own groups).
  8.  Empty states: a group without slots, an institute without groups.
  9.  A school-type institution cannot open the page, still gets the original
      school /schedules/ page, and its sidebar still links to /schedules/.
  10. Query count stays constant as the number of groups grows (no N+1).
  11. Viewing / filtering writes nothing.
"""
import unittest
from datetime import date, time
from urllib.parse import urlencode
from uuid import uuid4

from flask_login import login_user, logout_user
from sqlalchemy import event
from werkzeug.exceptions import Forbidden, NotFound

from app import create_app
from app.models import (db, AcademicYear, AuditLog, Employee,
                        InstituteAttendanceSession, InstituteGroupSchedule,
                        InstituteStudyGroup, Permission, Role, School, Subject,
                        User)

OPTS = {'bypass_tenant_scope': True}


class InstituteSchedulesOverviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    # ── Fixtures ─────────────────────────────────────────────────────────────

    def _user(self, school_id, label, role_id):
        u = User(username=f'{label}_{self.suffix}',
                 email=f'{label}_{self.suffix}@example.test',
                 full_name=f'{label} {self.suffix}',
                 role_id=role_id, school_id=school_id, is_active=True)
        u.set_password('Password123')
        db.session.add(u)
        db.session.flush()
        return u

    def _employee(self, school_id, label, user_id=None):
        e = Employee(school_id=school_id, employee_id=f'E{label}{self.suffix}'[:38],
                     full_name=f'Instr{label} {self.suffix}', base_salary=0,
                     status='active', user_id=user_id)
        db.session.add(e)
        db.session.flush()
        return e

    def _group(self, school_id, year_id, subject_id, name, instructor_id):
        g = InstituteStudyGroup(school_id=school_id, academic_year_id=year_id,
                                subject_id=subject_id, instructor_id=instructor_id,
                                name=name, is_active=True)
        db.session.add(g)
        db.session.flush()
        return g

    def _slot(self, school_id, year_id, group_id, dow, start, end):
        s = InstituteGroupSchedule(school_id=school_id, academic_year_id=year_id,
                                   group_id=group_id, day_of_week=dow,
                                   start_time=start, end_time=end, is_active=True)
        db.session.add(s)
        db.session.flush()
        return s

    def _institution(self, label, *, institute):
        school = School(
            school_name=f'{label} {self.suffix}',
            code=f'{label.upper()}{self.suffix}'[:20], capacity=0, is_active=True,
            institution_type=(School.INSTITUTION_INSTITUTE if institute else None))
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'{label} Y {self.suffix}',
                            start_date=date(2025, 8, 1), end_date=date(2026, 6, 30),
                            is_current=True)
        db.session.add(year)
        db.session.flush()
        subject = Subject(school_id=school.id, academic_year_id=year.id,
                          name=f'مادة {label}', code=f'S{label[:2]}{self.suffix[:6]}')
        db.session.add(subject)
        db.session.flush()
        return school, year, subject

    def setUp(self):
        self.suffix = uuid4().hex[:8]
        with self.app.app_context():
            teacher = Role.query.filter_by(name='teacher').first().id
            admin = Role.query.filter_by(name='school_admin').first().id

            # ── Institute A ──────────────────────────────────────────────────
            inst, iyear, isubj = self._institution('SvA', institute=True)
            uadmin = self._user(inst.id, 'svadm', admin)
            ua = self._user(inst.id, 'svta', teacher)
            ub = self._user(inst.id, 'svtb', teacher)
            ea = self._employee(inst.id, 'A', ua.id)
            eb = self._employee(inst.id, 'B', ub.id)
            ga = self._group(inst.id, iyear.id, isubj.id, 'A-Group', ea.id)
            gb = self._group(inst.id, iyear.id, isubj.id, 'B-Group', eb.id)
            gempty = self._group(inst.id, iyear.id, isubj.id, 'Empty-Group', eb.id)
            a_sun = self._slot(inst.id, iyear.id, ga.id, 0, time(16, 0), time(18, 0))
            a_mon = self._slot(inst.id, iyear.id, ga.id, 1, time(17, 5), time(19, 5))
            self._slot(inst.id, iyear.id, gb.id, 2, time(10, 15), time(11, 45))

            # ── Institute O (cross-tenant, distinctive slot time) ────────────
            oinst, oyear, osubj = self._institution('SvO', institute=True)
            uoadmin = self._user(oinst.id, 'svoadm', admin)
            eo = self._employee(oinst.id, 'O')
            go = self._group(oinst.id, oyear.id, osubj.id, 'O-Group', eo.id)
            o_slot = self._slot(oinst.id, oyear.id, go.id, 3,
                                time(12, 34), time(13, 47))

            # ── Institute N (no groups at all) ───────────────────────────────
            ninst, _ny, _ns = self._institution('SvN', institute=True)
            unadmin = self._user(ninst.id, 'svnadm', admin)

            # ── Ordinary school ──────────────────────────────────────────────
            sch, _sy, _ss = self._institution('SvS', institute=False)
            usadmin = self._user(sch.id, 'svsadm', admin)
            db.session.commit()

            self.ids = {
                'inst': inst.id, 'iyear': iyear.id, 'isubj': isubj.id,
                'uadmin': uadmin.id, 'ua': ua.id, 'ub': ub.id,
                'ga': ga.id, 'gb': gb.id, 'gempty': gempty.id,
                'a_sun': a_sun.id, 'a_mon': a_mon.id,
                'oinst': oinst.id, 'uoadmin': uoadmin.id, 'go': go.id,
                'o_slot': o_slot.id,
                'ninst': ninst.id, 'unadmin': unadmin.id,
                'sch': sch.id, 'usadmin': usadmin.id,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for key in ('inst', 'oinst', 'ninst', 'sch'):
                sid = self.ids[key]
                for model in (AuditLog, InstituteAttendanceSession,
                              InstituteGroupSchedule, InstituteStudyGroup):
                    for row in (model.query.execution_options(**OPTS)
                                .filter_by(school_id=sid).all()):
                        db.session.delete(row)
                    db.session.flush()
                for model in (Subject, Employee, User):
                    for row in (model.query.execution_options(**OPTS)
                                .filter_by(school_id=sid).all()):
                        db.session.delete(row)
                    db.session.flush()
                for row in (AcademicYear.query.execution_options(**OPTS)
                            .filter_by(school_id=sid).all()):
                    db.session.delete(row)
                db.session.flush()
                school = db.session.get(School, sid, execution_options=OPTS)
                if school is not None:
                    db.session.delete(school)
                db.session.flush()
            db.session.commit()
            db.session.remove()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _obj(self, model, key):
        return db.session.get(model, self.ids[key], execution_options=OPTS)

    def _run_before_request(self):
        for fn in self.app.before_request_funcs.get(None, []):
            if fn() is not None:
                break

    def _page(self, user_key, **args):
        """Render the real overview route as `user_key`. HTML or raises."""
        from app.blueprints.institute_groups import schedules_overview
        qs = ('?' + urlencode(args)) if args else ''
        with self.app.test_request_context('/institute-groups/schedules' + qs):
            login_user(self._obj(User, user_key))
            self._run_before_request()
            try:
                return schedules_overview()
            finally:
                logout_user()

    def _post(self, view_name, path, user_key, data, *view_args):
        from app.blueprints import institute_groups as bp
        with self.app.test_request_context(path, method='POST', data=data):
            login_user(self._obj(User, user_key))
            self._run_before_request()
            try:
                return getattr(bp, view_name)(*view_args)
            finally:
                logout_user()

    def _counts(self):
        with self.app.app_context():
            return tuple(
                m.query.execution_options(**OPTS).count()
                for m in (InstituteGroupSchedule, InstituteAttendanceSession,
                          InstituteStudyGroup, AuditLog))

    def _card(self, key):
        return f'id="group-{self.ids[key]}"'

    # ── 1. Manager sees institute group schedules ───────────────────────────

    def test_manager_sees_group_schedules(self):
        html = self._page('uadmin')
        self.assertIn('الجداول الدراسية', html)
        self.assertIn('جداول المجموعات الدراسية', html)
        self.assertIn('A-Group', html)
        self.assertIn('16:00', html)
        self.assertIn('17:05', html)
        self.assertIn('مادة SvA', html)
        self.assertIn(f'InstrA {self.suffix}', html)
        self.assertIn('كل المجموعات', html)
        # No school stage / grade / section controls.
        self.assertNotIn('schedStage', html)
        self.assertNotIn('name="grade_id"', html)
        self.assertNotIn('name="section_id"', html)
        self.assertNotIn('schedules/create', html)

    # ── 2. "كل المجموعات" shows every authorized group, separately ─────────

    def test_all_groups_render_as_separate_cards(self):
        html = self._page('uadmin')
        for key in ('ga', 'gb', 'gempty'):
            self.assertIn(self._card(key), html)
        self.assertEqual(html.count('class="card mb-4" id="group-'), 3)
        # B-Group's slot sits inside B's card, not A's.
        a_card = html.split(self._card('ga'))[1].split('id="group-')[0]
        self.assertNotIn('10:15', a_card)
        self.assertIn('16:00', a_card)

    # ── 3. Group filter ──────────────────────────────────────────────────────

    def test_group_filter_shows_only_selected_group(self):
        html = self._page('uadmin', group_id=self.ids['gb'])
        self.assertIn(self._card('gb'), html)
        self.assertNotIn(self._card('ga'), html)
        self.assertNotIn(self._card('gempty'), html)
        self.assertIn('10:15', html)
        self.assertNotIn('16:00', html)
        self.assertNotIn('17:05', html)

    # ── 4 & 5. Tenant isolation ──────────────────────────────────────────────

    def test_foreign_or_unknown_group_is_404(self):
        with self.assertRaises(NotFound):
            self._page('uadmin', group_id=self.ids['go'])
        with self.assertRaises(NotFound):
            self._page('uadmin', group_id=999999999)
        with self.assertRaises(NotFound):
            self._page('uoadmin', group_id=self.ids['ga'])

    def test_foreign_schedule_never_appears(self):
        for args in ({}, {'group_id': self.ids['ga']}):
            html = self._page('uadmin', **args)
            self.assertNotIn('O-Group', html)
            self.assertNotIn('12:34', html)
            self.assertNotIn('مادة SvO', html)
            self.assertNotIn(f'id="group-{self.ids["go"]}"', html)
        html = self._page('uoadmin')
        self.assertIn('O-Group', html)
        self.assertIn('12:34', html)
        self.assertNotIn('A-Group', html)
        self.assertNotIn('B-Group', html)
        self.assertNotIn('16:00', html)

    def test_bulk_slot_loader_is_school_and_year_bounded(self):
        from app.services import institute_attendance as att
        with self.app.app_context():
            got = att.slots_by_group(self.ids['inst'], self.ids['iyear'],
                                     [self.ids['ga'], self.ids['go']])
            self.assertEqual(set(got), {self.ids['ga']},
                             "a foreign group id yields nothing")
            self.assertEqual([s.id for s in got[self.ids['ga']]],
                             [self.ids['a_sun'], self.ids['a_mon']])
            self.assertEqual(att.slots_by_group(self.ids['inst'], None,
                                                [self.ids['ga']]), {})

    # ── 6. Existing add / edit / delete are reachable and reused ────────────

    def test_page_links_existing_write_routes(self):
        html = self._page('uadmin')
        ga, a_sun = self.ids['ga'], self.ids['a_sun']
        self.assertIn(f'/institute-groups/{ga}/schedule/add', html)
        self.assertIn(f'/institute-groups/{ga}/schedule/{a_sun}/edit', html)
        self.assertIn(f'/institute-groups/{ga}/schedule/{a_sun}/delete', html)
        self.assertIn(f'/institute-groups/{ga}/schedule"', html)
        self.assertIn('name="return_to" value="overview"', html)
        self.assertIn('إضافة حصة', html)
        # Edit form carries is_active so saving never silently disables a slot.
        self.assertIn(f'id="ovact{a_sun}" checked', html)

    def test_add_edit_delete_return_to_overview(self):
        ga = self.ids['ga']
        resp = self._post('schedule_add', f'/institute-groups/{ga}/schedule/add',
                          'uadmin', {'day_of_week': '5', 'start_time': '09:00',
                                     'end_time': '10:00', 'return_to': 'overview'},
                          ga)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/institute-groups/schedules', resp.location)
        self.assertIn(f'#group-{ga}', resp.location)
        with self.app.app_context():
            new = (InstituteGroupSchedule.query.execution_options(**OPTS)
                   .filter_by(group_id=ga, day_of_week=5).one())
            new_id = new.id
            self.assertEqual(new.academic_year_id, self.ids['iyear'])

        resp = self._post('schedule_edit',
                          f'/institute-groups/{ga}/schedule/{new_id}/edit',
                          'uadmin', {'day_of_week': '6', 'start_time': '09:30',
                                     'end_time': '10:30', 'is_active': '1',
                                     'return_to': 'overview',
                                     'return_group_id': str(ga)},
                          ga, new_id)
        self.assertIn(f'group_id={ga}', resp.location)
        with self.app.app_context():
            row = db.session.get(InstituteGroupSchedule, new_id,
                                 execution_options=OPTS)
            self.assertEqual((row.day_of_week, row.start_time, row.is_active),
                             (6, time(9, 30), True))

        resp = self._post('schedule_delete',
                          f'/institute-groups/{ga}/schedule/{new_id}/delete',
                          'uadmin', {'return_to': 'overview'}, ga, new_id)
        self.assertIn('/institute-groups/schedules', resp.location)
        with self.app.app_context():
            self.assertIsNone(db.session.get(InstituteGroupSchedule, new_id,
                                             execution_options=OPTS))

    def test_write_routes_keep_default_redirect_and_validation(self):
        ga = self.ids['ga']
        # No flag (or an unknown one) -> the per-group editor, as before.
        for extra in ({}, {'return_to': 'https://evil.example/'}):
            data = {'day_of_week': '5', 'start_time': '08:00',
                    'end_time': '08:30', **extra}
            resp = self._post('schedule_add',
                              f'/institute-groups/{ga}/schedule/add',
                              'uadmin', data, ga)
            self.assertTrue(resp.location.endswith(
                f'/institute-groups/{ga}/schedule'), resp.location)
            self.assertNotIn('evil', resp.location)
        # Existing validation still applies on the overview path (bad times).
        before = self._counts()
        self._post('schedule_add', f'/institute-groups/{ga}/schedule/add',
                   'uadmin', {'day_of_week': '4', 'start_time': '12:00',
                              'end_time': '11:00', 'return_to': 'overview'}, ga)
        self.assertEqual(self._counts()[0], before[0])

    def test_foreign_slot_write_still_404(self):
        go, o_slot = self.ids['go'], self.ids['o_slot']
        with self.assertRaises(NotFound):
            self._post('schedule_edit', f'/institute-groups/{go}/schedule/{o_slot}/edit',
                       'uadmin', {'day_of_week': '0', 'start_time': '08:00',
                                  'end_time': '09:00', 'return_to': 'overview'},
                       go, o_slot)
        with self.assertRaises(NotFound):
            # Own group id, foreign slot id.
            self._post('schedule_delete',
                       f'/institute-groups/{self.ids["ga"]}/schedule/{o_slot}/delete',
                       'uadmin', {'return_to': 'overview'}, self.ids['ga'], o_slot)
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(InstituteGroupSchedule, o_slot,
                                                execution_options=OPTS))

    # ── 7. Teacher restrictions unchanged ────────────────────────────────────

    def test_teacher_has_no_schedule_access_by_default(self):
        with self.assertRaises(Forbidden):
            self._page('ua')
        # Same as the existing per-group editor, which is also manager-only.
        from app.blueprints.institute_groups import schedule
        with self.app.test_request_context(f'/institute-groups/{self.ids["ga"]}/schedule'):
            login_user(self._obj(User, 'ua'))
            self._run_before_request()
            try:
                with self.assertRaises(Forbidden):
                    schedule(self.ids['ga'])
            finally:
                logout_user()

    def test_teacher_with_permission_still_sees_only_own_groups(self):
        with self.app.app_context():
            perm = Permission.query.filter_by(name='manage_institute_groups').first()
            if perm is None:
                self.skipTest('manage_institute_groups not seeded in this DB')
            user = self._obj(User, 'ua')
            user.extra_permissions.append(perm)
            db.session.commit()
        html = self._page('ua')
        self.assertIn(self._card('ga'), html)
        self.assertNotIn(self._card('gb'), html)
        self.assertNotIn('B-Group', html)
        self.assertNotIn('إضافة حصة', html, 'teacher view stays read-only')
        with self.assertRaises(NotFound):
            self._page('ua', group_id=self.ids['gb'])

    # ── 8. Empty states ──────────────────────────────────────────────────────

    def test_empty_group_message(self):
        html = self._page('uadmin', group_id=self.ids['gempty'])
        self.assertIn(self._card('gempty'), html)
        self.assertIn('لا توجد حصص مضافة لهذه المجموعة.', html)
        html = self._page('uadmin', group_id=self.ids['ga'])
        self.assertNotIn('لا توجد حصص مضافة لهذه المجموعة.', html)

    def test_no_groups_message(self):
        html = self._page('unadmin')
        self.assertIn('لا توجد مجموعات دراسية متاحة.', html)
        self.assertNotIn('id="group-', html)

    # ── 9. School-type institutions are unchanged ────────────────────────────

    def test_school_cannot_open_institute_page(self):
        with self.assertRaises(Forbidden):
            self._page('usadmin')

    def test_school_schedules_page_unchanged(self):
        from app.blueprints.schedules import index
        with self.app.test_request_context('/schedules/'):
            login_user(self._obj(User, 'usadmin'))
            self._run_before_request()
            try:
                html = index()
            finally:
                logout_user()
        self.assertIn('بناء وإدارة الجدول الأسبوعي لكل صف أو شعبة', html)
        self.assertIn('href="/schedules/"', html, 'school sidebar link unchanged')
        self.assertNotIn('/institute-groups/schedules', html)
        self.assertNotIn('جداول المجموعات الدراسية', html)

    def test_institute_sidebar_points_to_group_schedules(self):
        html = self._page('uadmin')
        self.assertIn('href="/institute-groups/schedules"', html)
        self.assertNotIn('href="/schedules/"', html)

    # ── 10. No N+1 ───────────────────────────────────────────────────────────

    def _statement_count(self, user_key):
        engine = None
        with self.app.app_context():
            engine = db.engine
        seen = []

        def _count(*_a, **_k):
            seen.append(1)
        event.listen(engine, 'before_cursor_execute', _count)
        try:
            self._page(user_key)
        finally:
            event.remove(engine, 'before_cursor_execute', _count)
        return len(seen)

    def test_query_count_constant_in_group_count(self):
        self._page('uadmin')                      # warm any first-use caches
        baseline = self._statement_count('uadmin')
        with self.app.app_context():
            for i in range(6):
                emp = self._employee(self.ids['inst'], f'X{i}')
                g = self._group(self.ids['inst'], self.ids['iyear'],
                                self.ids['isubj'], f'Extra-{i}', emp.id)
                self._slot(self.ids['inst'], self.ids['iyear'], g.id, 0,
                           time(8, i), time(9, i))
                self._slot(self.ids['inst'], self.ids['iyear'], g.id, 3,
                           time(8, i), time(9, i))
            db.session.commit()
        grown = self._statement_count('uadmin')
        self.assertIn('Extra-5', self._page('uadmin'))
        self.assertEqual(grown, baseline,
                         f'{baseline} statements with 3 groups, {grown} with 9')

    # ── 11. Read-only ────────────────────────────────────────────────────────

    def test_viewing_and_filtering_write_nothing(self):
        before = self._counts()
        self._page('uadmin')
        self._page('uadmin', group_id=self.ids['ga'])
        self._page('uadmin', group_id=self.ids['gempty'])
        self._page('unadmin')
        self.assertEqual(self._counts(), before)


if __name__ == '__main__':
    unittest.main()
