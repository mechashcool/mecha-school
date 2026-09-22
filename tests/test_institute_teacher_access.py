"""Institute instructor access — focused guarantees only.

Covers exactly the ten Phase-3 acceptance points:
  1.  Instructor A sees only their assigned groups.
  2.  Instructor A cannot open Instructor B's group by direct URL.
  3.  A group roster contains ACTIVE enrollments only.
  4.  An ended enrollment is excluded from the roster but remains stored.
  5.  A student in two of the instructor's groups is not duplicated.
  6.  An instructor cannot open an unrelated institute student by direct URL.
  7.  Cross-school groups and students never leak.
  8.  An institute administrator still sees and manages every group.
  9.  School-teacher section-based visibility is unchanged.
  10. A teacher account with no linked Employee gets an EMPTY scope.

Out of scope here: homework, exams, notifications, schedules, attendance,
mobile API.
"""
import unittest
from datetime import date, datetime
from uuid import uuid4

from flask_login import login_user, logout_user
from werkzeug.exceptions import NotFound

from app import create_app
from app.models import (db, AcademicYear, AuditLog, Employee, Grade,
                        InstituteGroupEnrollment, InstituteStudyGroup, Role,
                        School, Section, Student, Subject, User,
                        teacher_subjects)


class InstituteTeacherAccessTest(unittest.TestCase):
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
                     full_name=f'{label} {self.suffix}', base_salary=0,
                     status='active', user_id=user_id)
        db.session.add(e)
        db.session.flush()
        return e

    def _student(self, org, name):
        s = Student(student_id=f'S-{uuid4().hex[:10]}', full_name=name,
                    school_id=org['school_id'], academic_year_id=org['year_id'],
                    section_id=None, status='active')
        db.session.add(s)
        db.session.flush()
        return s

    def _group(self, org, name, instructor_id, *, active=True):
        g = InstituteStudyGroup(school_id=org['school_id'],
                                academic_year_id=org['year_id'],
                                subject_id=org['subject_id'],
                                instructor_id=instructor_id,
                                name=name, is_active=active)
        db.session.add(g)
        db.session.flush()
        return g

    def _enroll(self, org, group_id, student_id, *, ended=False):
        row = InstituteGroupEnrollment(
            school_id=org['school_id'], group_id=group_id, student_id=student_id,
            status=(InstituteGroupEnrollment.STATUS_ENDED if ended
                    else InstituteGroupEnrollment.STATUS_ACTIVE),
            ended_at=(datetime.utcnow() if ended else None))
        db.session.add(row)
        db.session.flush()
        return row

    def _make_institute(self, label):
        school = School(school_name=f'{label} {self.suffix}',
                        code=f'{label.upper()}{self.suffix}'[:20],
                        capacity=0, is_active=True,
                        institution_type=School.INSTITUTION_INSTITUTE)
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'{label} Y {self.suffix}',
                            start_date=date(2025, 8, 1), end_date=date(2026, 6, 30),
                            is_current=True)
        db.session.add(year)
        db.session.flush()
        subject = Subject(school_id=school.id, academic_year_id=year.id,
                          name='الرياضيات', code=f'M{label[:2]}{self.suffix[:6]}')
        db.session.add(subject)
        db.session.flush()
        return {'school_id': school.id, 'year_id': year.id,
                'subject_id': subject.id}

    def _make_school(self, label):
        """An ordinary school with grade + section, to prove nothing changed."""
        school = School(school_name=f'{label} {self.suffix}',
                        code=f'{label.upper()}{self.suffix}'[:20],
                        capacity=0, is_active=True, institution_type=None)
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'{label} Y {self.suffix}',
                            start_date=date(2025, 8, 1), end_date=date(2026, 6, 30),
                            is_current=True)
        db.session.add(year)
        db.session.flush()
        grade = Grade(school_id=school.id, academic_year_id=year.id,
                      name=f'{label} Grade')
        db.session.add(grade)
        db.session.flush()
        mine = Section(school_id=school.id, academic_year_id=year.id,
                       grade_id=grade.id, name=f'A{self.suffix[:4]}', capacity=30)
        other = Section(school_id=school.id, academic_year_id=year.id,
                        grade_id=grade.id, name=f'B{self.suffix[:4]}', capacity=30)
        db.session.add_all([mine, other])
        db.session.flush()
        return {'school_id': school.id, 'year_id': year.id,
                'grade_id': grade.id,
                'my_section_id': mine.id, 'other_section_id': other.id}

    def setUp(self):
        self.suffix = uuid4().hex[:8]
        with self.app.app_context():
            self.teacher_role_id = Role.query.filter_by(name='teacher').first().id
            self.admin_role_id = Role.query.filter_by(name='school_admin').first().id

            # ── Institute A ──────────────────────────────────────────────────
            self.inst = self._make_institute('InstA')
            ua = self._user(self.inst['school_id'], 'insta', self.teacher_role_id)
            ub = self._user(self.inst['school_id'], 'instb', self.teacher_role_id)
            # A teacher account with NO linked Employee row.
            uorphan = self._user(self.inst['school_id'], 'orphan', self.teacher_role_id)
            uadmin = self._user(self.inst['school_id'], 'iadmin', self.admin_role_id)
            ea = self._employee(self.inst['school_id'], 'A', ua.id)
            eb = self._employee(self.inst['school_id'], 'B', ub.id)

            ga1 = self._group(self.inst, 'A-One', ea.id)
            ga2 = self._group(self.inst, 'A-Two', ea.id)
            gb1 = self._group(self.inst, 'B-One', eb.id)
            ga_off = self._group(self.inst, 'A-Inactive', ea.id, active=False)

            shared = self._student(self.inst, 'Shared Student')
            only1 = self._student(self.inst, 'Only In One')
            ended = self._student(self.inst, 'Ended Student')
            bstud = self._student(self.inst, 'B Student')
            loner = self._student(self.inst, 'Unrelated Student')

            self._enroll(self.inst, ga1.id, shared.id)
            self._enroll(self.inst, ga2.id, shared.id)      # same student, 2 groups
            self._enroll(self.inst, ga1.id, only1.id)
            ended_row = self._enroll(self.inst, ga1.id, ended.id, ended=True)
            self._enroll(self.inst, gb1.id, bstud.id)

            # ── Institute B (cross-tenant) ──────────────────────────────────
            self.other = self._make_institute('InstO')
            uo = self._user(self.other['school_id'], 'othert', self.teacher_role_id)
            eo = self._employee(self.other['school_id'], 'O', uo.id)
            go = self._group(self.other, 'O-One', eo.id)
            ostud = self._student(self.other, 'Other Student')
            self._enroll(self.other, go.id, ostud.id)

            # ── Ordinary school (regression baseline) ───────────────────────
            self.school = self._make_school('PlainSch')
            us = self._user(self.school['school_id'], 'schteach', self.teacher_role_id)
            es = self._employee(self.school['school_id'], 'S', us.id)
            db.session.execute(Section.__table__.update()
                               .where(Section.id == self.school['my_section_id'])
                               .values(teacher_id=es.id))
            smine = Student(student_id=f'SM-{uuid4().hex[:9]}', full_name='School Mine',
                            school_id=self.school['school_id'],
                            academic_year_id=self.school['year_id'],
                            section_id=self.school['my_section_id'], status='active')
            sother = Student(student_id=f'SO-{uuid4().hex[:9]}', full_name='School Other',
                             school_id=self.school['school_id'],
                             academic_year_id=self.school['year_id'],
                             section_id=self.school['other_section_id'], status='active')
            db.session.add_all([smine, sother])
            db.session.flush()

            db.session.commit()

            self.ids = {
                'ua': ua.id, 'ub': ub.id, 'uorphan': uorphan.id, 'uadmin': uadmin.id,
                'uo': uo.id, 'us': us.id,
                'ea': ea.id, 'eb': eb.id,
                'ga1': ga1.id, 'ga2': ga2.id, 'gb1': gb1.id, 'ga_off': ga_off.id,
                'go': go.id,
                'shared': shared.id, 'only1': only1.id, 'ended': ended.id,
                'bstud': bstud.id, 'loner': loner.id, 'ostud': ostud.id,
                'ended_row': ended_row.id,
                'smine': smine.id, 'sother': sother.id,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            opts = {'bypass_tenant_scope': True}
            school_ids = [self.inst['school_id'], self.other['school_id'],
                          self.school['school_id']]
            for sid in school_ids:
                for model in (AuditLog, InstituteGroupEnrollment,
                              InstituteStudyGroup, Student):
                    for row in (model.query.execution_options(**opts)
                                .filter_by(school_id=sid).all()):
                        db.session.delete(row)
                    db.session.flush()
                db.session.execute(
                    teacher_subjects.delete().where(
                        teacher_subjects.c.employee_id.in_(
                            db.session.query(Employee.id)
                            .filter(Employee.school_id == sid))))
                db.session.execute(Section.__table__.update()
                                   .where(Section.school_id == sid)
                                   .values(teacher_id=None))
                db.session.flush()
                for model in (Section, Subject, Grade, Employee, User):
                    for row in (model.query.execution_options(**opts)
                                .filter_by(school_id=sid).all()):
                        db.session.delete(row)
                    db.session.flush()
                for row in (AcademicYear.query.execution_options(**opts)
                            .filter_by(school_id=sid).all()):
                    db.session.delete(row)
                db.session.flush()
                sch = db.session.get(School, sid, execution_options=opts)
                if sch is not None:
                    db.session.delete(sch)
                db.session.flush()
            db.session.commit()
            db.session.remove()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _run_before_request(self):
        for fn in self.app.before_request_funcs.get(None, []):
            if fn() is not None:
                break

    def _login(self, user_key):
        user = db.session.get(User, self.ids[user_key],
                              execution_options={'bypass_tenant_scope': True})
        login_user(user)
        self._run_before_request()

    # ── 1. Instructor A sees only their assigned groups ──────────────────────

    def test_instructor_sees_only_their_own_active_groups(self):
        from app.blueprints.institute_groups import index
        with self.app.test_request_context('/institute-groups/'):
            self._login('ua')
            html = index()
            logout_user()
        self.assertIn('A-One', html)
        self.assertIn('A-Two', html)
        self.assertNotIn('B-One', html, "another instructor's group must not appear")
        self.assertNotIn('A-Inactive', html, 'inactive groups are not in the teacher list')
        self.assertIn('مجموعاتي الدراسية', html)

    # ── 2. Direct URL to another instructor's group ──────────────────────────

    def test_instructor_cannot_open_another_instructors_group(self):
        from app.blueprints.institute_groups import detail
        for key in ('gb1', 'ga_off', 'go'):
            with self.app.test_request_context(f'/institute-groups/{self.ids[key]}'):
                self._login('ua')
                with self.assertRaises(NotFound, msg=f'{key} must 404'):
                    detail(self.ids[key])
                logout_user()

    # ── 3 & 4. Roster is active-only; ended row still stored ─────────────────

    def test_roster_shows_active_only_and_keeps_ended_rows(self):
        from app.blueprints.institute_groups import detail
        with self.app.test_request_context(f'/institute-groups/{self.ids["ga1"]}'):
            self._login('ua')
            html = detail(self.ids['ga1'])
            logout_user()
        self.assertIn('Shared Student', html)
        self.assertIn('Only In One', html)
        self.assertNotIn('Ended Student', html,
                         'an ended enrollment must not appear in the roster')
        # The row itself is untouched and still stored.
        with self.app.app_context():
            row = db.session.get(InstituteGroupEnrollment, self.ids['ended_row'],
                                 execution_options={'bypass_tenant_scope': True})
            self.assertIsNotNone(row, 'the ended enrollment must remain stored')
            self.assertEqual(row.status, 'ended')
            self.assertIsNotNone(row.ended_at)

    # ── 5. No duplicate in the general student list ──────────────────────────

    def test_student_in_two_groups_is_not_duplicated(self):
        from app.utils.institute_groups import instructor_student_ids
        with self.app.test_request_context('/students/'):
            self._login('ua')
            school = db.session.get(School, self.inst['school_id'],
                                    execution_options={'bypass_tenant_scope': True})
            year = db.session.get(AcademicYear, self.inst['year_id'],
                                  execution_options={'bypass_tenant_scope': True})
            from flask_login import current_user
            ids = instructor_student_ids(school, current_user, year)
            logout_user()
        # shared is in TWO of instructor A's groups; it must appear once.
        self.assertEqual(ids, {self.ids['shared'], self.ids['only1']})

    # ── 6 & 7. Direct student URL / cross-tenant ─────────────────────────────

    def test_instructor_cannot_open_unrelated_or_cross_school_student(self):
        """Two refusal shapes, both non-disclosing and both already the
        project's existing convention:

          * ANOTHER SCHOOL's student -> 404 from the ORM tenant scope, before
            the route ever sees the row. Identical to a non-existent id, so it
            never reveals that the student exists elsewhere.
          * THIS institute, but outside the instructor's groups -> flash +
            redirect to the student list, exactly as a school teacher gets for
            a student outside their section.
        """
        from app.blueprints.students import view
        visible   = {'shared', 'only1'}
        redirected = {'ended', 'bstud', 'loner'}   # same institute, out of scope
        not_found  = {'ostud'}                      # another institute entirely

        for key in visible | redirected | not_found:
            with self.app.test_request_context(f'/students/{self.ids[key]}'):
                self._login('ua')
                if key in not_found:
                    with self.assertRaises(NotFound,
                                           msg=f'{key} must 404 (cross-school)'):
                        view(self.ids[key])
                    logout_user()
                    continue
                resp = view(self.ids[key])
                logout_user()
            if key in visible:
                self.assertNotEqual(getattr(resp, 'status_code', None), 302,
                                    f'{key} should be visible')
            else:
                self.assertEqual(getattr(resp, 'status_code', None), 302,
                                 f'{key} must be refused')

    # ── 8. Administrator keeps full management ───────────────────────────────

    def test_institute_admin_still_sees_and_manages_all_groups(self):
        from app.blueprints.institute_groups import index, detail
        with self.app.test_request_context('/institute-groups/'):
            self._login('uadmin')
            html = index()
            logout_user()
        for name in ('A-One', 'A-Two', 'B-One', 'A-Inactive'):
            self.assertIn(name, html, f'admin must still see {name}')
        self.assertIn('المجموعات الدراسية', html)
        self.assertIn('إضافة مجموعة', html, 'admin keeps the create control')

        with self.app.test_request_context(f'/institute-groups/{self.ids["gb1"]}'):
            self._login('uadmin')
            html = detail(self.ids['gb1'])
            logout_user()
        self.assertIn('إضافة طلاب إلى المجموعة', html,
                      'admin keeps the bulk-enroll control on any group')

    # ── 9. School teacher behaviour unchanged ────────────────────────────────

    def test_school_teacher_section_scope_is_unchanged(self):
        from app.blueprints.students import view, _teacher_scope_student_query
        with self.app.test_request_context('/students/'):
            self._login('us')
            school = db.session.get(School, self.school['school_id'],
                                    execution_options={'bypass_tenant_scope': True})
            q = _teacher_scope_student_query(
                Student.query.filter_by(school_id=school.id), school)
            visible = {s.id for s in q.all()}
            logout_user()
        self.assertEqual(visible, {self.ids['smine']},
                         'a school teacher still sees exactly their section')

        with self.app.test_request_context(f'/students/{self.ids["sother"]}'):
            self._login('us')
            resp = view(self.ids['sother'])
            logout_user()
        self.assertEqual(getattr(resp, 'status_code', None), 302,
                         'a school teacher is still blocked outside their section')

    # ── 10. Teacher account with no Employee row ─────────────────────────────

    def test_teacher_without_employee_record_gets_empty_scope(self):
        from app.blueprints.institute_groups import index
        from app.utils.institute_groups import instructor_student_ids
        with self.app.test_request_context('/institute-groups/'):
            self._login('uorphan')
            html = index()
            school = db.session.get(School, self.inst['school_id'],
                                    execution_options={'bypass_tenant_scope': True})
            year = db.session.get(AcademicYear, self.inst['year_id'],
                                  execution_options={'bypass_tenant_scope': True})
            from flask_login import current_user
            ids = instructor_student_ids(school, current_user, year)
            logout_user()
        self.assertEqual(ids, set(), 'an unlinked account must get an EMPTY scope')
        for name in ('A-One', 'A-Two', 'B-One'):
            self.assertNotIn(name, html)
        self.assertIn('لا توجد مجموعات دراسية مسندة إليك', html)


if __name__ == '__main__':
    unittest.main()
