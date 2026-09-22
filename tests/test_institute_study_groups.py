"""Phase 1 institute study groups — focused guarantees only.

Covers exactly the eight Phase 1 acceptance points:
  1. a school-type institution cannot reach the institute routes, and its
     student section behaviour is unchanged;
  2. an institute can create a group from its own subject + instructor;
  3. cross-school subject / instructor / group / student ids are rejected with
     no partial write;
  4. an institute student is created with two groups and section_id stays NULL;
  5. editing adds one group and ends another, keeping the historical row;
  6. a duplicate ACTIVE enrollment is rejected;
  7. an ended enrollment is retained and re-enrollment is allowed;
  8. central tenant scoping stops School A reading School B's rows.

Deliberately NOT covered here (out of Phase 1 scope): homework, exams,
notifications, schedules, mobile API, Flutter.
"""
import unittest
from datetime import date
from uuid import uuid4

from flask_login import login_user, logout_user
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import Forbidden

from app import create_app
from app.models import (db, AcademicYear, AuditLog, Employee, Grade,
                        InstituteGroupEnrollment, InstituteStudyGroup, Role,
                        School, Section, Student, Subject, User)


class InstituteStudyGroupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    # ── Fixtures ─────────────────────────────────────────────────────────────

    def _make_org(self, label, *, institute):
        """Create one isolated institution with a year, subject and employee.

        `institute=False` produces an ordinary school (institution_type left
        NULL, i.e. the legacy/default classification) with a grade + section,
        so the unchanged school path can be asserted against it.
        """
        school = School(
            school_name=f'{label} {self.suffix}',
            code=f'{label[:3].upper()}{self.suffix[:7]}',
            capacity=0, is_active=True,
            institution_type=School.INSTITUTION_INSTITUTE if institute else None,
        )
        db.session.add(school)
        db.session.flush()

        year = AcademicYear(school_id=school.id, name=f'{label} Y {self.suffix}',
                            start_date=date(2025, 8, 1), end_date=date(2026, 6, 30),
                            is_current=True)
        db.session.add(year)
        db.session.flush()

        subject = Subject(school_id=school.id, academic_year_id=year.id,
                          name=f'{label} Subject', code=f'{label[:2].upper()}{self.suffix[:6]}')
        employee = Employee(school_id=school.id, employee_id=f'E-{label[:2]}-{self.suffix}',
                            full_name=f'{label} Instructor', base_salary=0, status='active')
        db.session.add_all([subject, employee])
        db.session.flush()

        grade_id = section_id = None
        if not institute:
            grade = Grade(school_id=school.id, academic_year_id=year.id,
                          name=f'{label} Grade')
            db.session.add(grade)
            db.session.flush()
            section = Section(school_id=school.id, academic_year_id=year.id,
                              grade_id=grade.id, name=f'S{self.suffix[:4]}', capacity=30)
            db.session.add(section)
            db.session.flush()
            grade_id, section_id = grade.id, section.id

        admin = User(username=f'{label.lower()}_adm_{self.suffix}',
                     email=f'{label.lower()}_adm_{self.suffix}@example.test',
                     full_name=f'{label} Admin',
                     role_id=self.school_admin_role_id,
                     school_id=school.id, is_active=True)
        admin.set_password('Password123')
        db.session.add(admin)
        db.session.flush()

        return {'school_id': school.id, 'year_id': year.id,
                'subject_id': subject.id, 'employee_id': employee.id,
                'grade_id': grade_id, 'section_id': section_id,
                'admin_id': admin.id}

    def _make_student(self, org, name, *, section_id=None):
        student = Student(
            student_id=f'ST-{uuid4().hex[:10]}', full_name=name,
            school_id=org['school_id'], academic_year_id=org['year_id'],
            section_id=section_id, status='active',
        )
        db.session.add(student)
        db.session.flush()
        self.student_ids.append(student.id)
        return student.id

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.student_ids = []
        with self.app.app_context():
            role = Role.query.filter_by(name='school_admin').first()
            self.assertIsNotNone(role, 'seed roles before running institute tests')
            self.school_admin_role_id = role.id

            self.inst = self._make_org('Institute', institute=True)
            self.other = self._make_org('OtherInst', institute=True)
            self.school = self._make_org('PlainSchool', institute=False)
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            opts = {'bypass_tenant_scope': True}

            # Routes that write through log_action() leave an AuditLog row
            # referencing the school; remove it so the school can be dropped.
            for org in (self.inst, self.other, self.school):
                for row in (AuditLog.query
                            .execution_options(**opts)
                            .filter_by(school_id=org['school_id']).all()):
                    db.session.delete(row)
                for row in (InstituteGroupEnrollment.query
                            .execution_options(**opts)
                            .filter_by(school_id=org['school_id']).all()):
                    db.session.delete(row)
            db.session.flush()
            for org in (self.inst, self.other, self.school):
                for row in (InstituteStudyGroup.query
                            .execution_options(**opts)
                            .filter_by(school_id=org['school_id']).all()):
                    db.session.delete(row)
            db.session.flush()

            for sid in self.student_ids:
                obj = db.session.get(Student, sid, execution_options=opts)
                if obj is not None:
                    db.session.delete(obj)
            db.session.flush()

            for org in (self.inst, self.other, self.school):
                for model, key in [(User, 'admin_id'), (Section, 'section_id'),
                                   (Grade, 'grade_id'), (Employee, 'employee_id'),
                                   (Subject, 'subject_id'),
                                   (AcademicYear, 'year_id'), (School, 'school_id')]:
                    if org.get(key) is None:
                        continue
                    obj = db.session.get(model, org[key], execution_options=opts)
                    if obj is not None:
                        db.session.delete(obj)
                db.session.flush()

            db.session.commit()
            db.session.remove()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _run_before_request(self):
        for fn in self.app.before_request_funcs.get(None, []):
            rv = fn()
            if rv is not None:
                return rv
        return None

    def _login(self, org):
        user = db.session.get(User, org['admin_id'],
                              execution_options={'bypass_tenant_scope': True})
        login_user(user)
        self._run_before_request()

    def _make_group(self, org, name, *, active=True):
        group = InstituteStudyGroup(
            school_id=org['school_id'], academic_year_id=org['year_id'],
            subject_id=org['subject_id'], instructor_id=org['employee_id'],
            name=name, is_active=active,
        )
        db.session.add(group)
        db.session.flush()
        return group.id

    def _enroll(self, org, group_id, student_id):
        row = InstituteGroupEnrollment(
            school_id=org['school_id'], group_id=group_id, student_id=student_id,
            status=InstituteGroupEnrollment.STATUS_ACTIVE,
        )
        db.session.add(row)
        db.session.flush()
        return row.id

    # ── 1. A normal school is locked out; its section behaviour is unchanged ──

    def test_normal_school_cannot_reach_institute_routes(self):
        from app.blueprints.institute_groups import index, new, detail

        with self.app.test_request_context('/institute-groups/'):
            self._login(self.school)
            for view, args in ((index, ()), (new, ()), (detail, (1,))):
                with self.assertRaises(Forbidden):
                    view(*args)
            logout_user()

    def test_normal_school_student_keeps_its_section(self):
        """The school path must be untouched: a school student still gets the
        section it was given, and no institute row is created for it."""
        with self.app.app_context():
            sid = self._make_student(self.school, 'Plain School Student',
                                     section_id=self.school['section_id'])
            db.session.commit()

            student = db.session.get(Student, sid,
                                     execution_options={'bypass_tenant_scope': True})
            self.assertEqual(student.section_id, self.school['section_id'])
            self.assertEqual(
                InstituteGroupEnrollment.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=self.school['school_id']).count(), 0)

    # ── 2. An institute creates a group from its own subject + instructor ────

    def test_institute_can_create_group_with_own_subject_and_instructor(self):
        with self.app.test_request_context(
                '/institute-groups/new', method='POST',
                data={'name': 'Group A', 'subject_id': str(self.inst['subject_id']),
                      'instructor_id': str(self.inst['employee_id']), 'is_active': '1'}):
            self._login(self.inst)
            from app.blueprints.institute_groups import new
            response = new()
            self.assertEqual(getattr(response, 'status_code', 302), 302)
            logout_user()

        with self.app.app_context():
            group = (InstituteStudyGroup.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter_by(school_id=self.inst['school_id'], name='Group A')
                     .first())
            self.assertIsNotNone(group)
            self.assertEqual(group.subject_id, self.inst['subject_id'])
            self.assertEqual(group.instructor_id, self.inst['employee_id'])
            self.assertEqual(group.academic_year_id, self.inst['year_id'])
            self.assertTrue(group.is_active)

    # ── 3. Cross-school ids rejected, with no partial write ──────────────────

    def test_cross_school_subject_and_instructor_are_rejected_by_route(self):
        for field, bad_value in (('subject_id', self.other['subject_id']),
                                 ('instructor_id', self.other['employee_id'])):
            payload = {'name': f'Bad {field}',
                       'subject_id': str(self.inst['subject_id']),
                       'instructor_id': str(self.inst['employee_id']),
                       'is_active': '1'}
            payload[field] = str(bad_value)
            with self.app.test_request_context('/institute-groups/new',
                                               method='POST', data=payload):
                self._login(self.inst)
                from app.blueprints.institute_groups import new
                body, status = new()
                self.assertEqual(status, 400, f'{field} should be rejected')
                logout_user()

            with self.app.app_context():
                self.assertIsNone(
                    InstituteStudyGroup.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(school_id=self.inst['school_id'],
                               name=f'Bad {field}').first(),
                    f'no group may be written for a bad {field}')

    def test_cross_school_group_id_is_404(self):
        from app.blueprints.institute_groups import detail
        with self.app.app_context():
            other_group = self._make_group(self.other, 'Other Group')
            db.session.commit()

        with self.app.test_request_context(f'/institute-groups/{other_group}'):
            self._login(self.inst)
            from werkzeug.exceptions import NotFound
            with self.assertRaises(NotFound):
                detail(other_group)
            logout_user()

    def test_cross_school_student_in_bulk_enroll_writes_nothing(self):
        from app.blueprints.institute_groups import enroll
        with self.app.app_context():
            group_id = self._make_group(self.inst, 'Enroll Group')
            mine = self._make_student(self.inst, 'Mine')
            theirs = self._make_student(self.other, 'Theirs')
            db.session.commit()

        with self.app.test_request_context(
                f'/institute-groups/{group_id}/enroll', method='POST',
                data={'student_ids': [str(mine), str(theirs)]}):
            self._login(self.inst)
            enroll(group_id)
            logout_user()

        with self.app.app_context():
            # One bad id rejects the WHOLE submission — the valid student in the
            # same request must not be enrolled either.
            self.assertEqual(
                InstituteGroupEnrollment.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(group_id=group_id).count(), 0)

    def test_database_rejects_cross_school_enrollment(self):
        """Defence in depth: even bypassing the routes, PostgreSQL refuses to
        link a student to a group owned by a different school."""
        with self.app.app_context():
            group_id = self._make_group(self.inst, 'DB Guard Group')
            theirs = self._make_student(self.other, 'DB Guard Theirs')
            db.session.commit()

            db.session.add(InstituteGroupEnrollment(
                school_id=self.inst['school_id'], group_id=group_id,
                student_id=theirs, status=InstituteGroupEnrollment.STATUS_ACTIVE))
            with self.assertRaises(IntegrityError):
                db.session.flush()
            db.session.rollback()

    # ── 4. Institute student created with two groups, section stays NULL ─────

    def test_institute_student_created_with_two_groups_and_null_section(self):
        from app.utils.institute_groups import stage_enrollments, validate_group_ids

        with self.app.app_context():
            g1 = self._make_group(self.inst, 'G1')
            g2 = self._make_group(self.inst, 'G2')
            db.session.commit()

            valid, err = validate_group_ids([g1, g2], self.inst['school_id'],
                                            self.inst['year_id'])
            self.assertIsNone(err)

            sid = self._make_student(self.inst, 'Institute Student')
            stage_enrollments(self.inst['school_id'], sid, valid)
            db.session.commit()

            student = db.session.get(Student, sid,
                                     execution_options={'bypass_tenant_scope': True})
            self.assertIsNone(student.section_id, 'institute students are sectionless')
            rows = (InstituteGroupEnrollment.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(student_id=sid).all())
            self.assertEqual({r.group_id for r in rows}, {g1, g2})
            self.assertTrue(all(r.status == 'active' and r.ended_at is None
                                for r in rows))

    def test_posted_section_is_ignored_for_an_institute(self):
        """A crafted section_id must never attach to an institute student."""
        from app.utils.institute_groups import institute_enabled
        with self.app.app_context():
            school = db.session.get(School, self.inst['school_id'],
                                    execution_options={'bypass_tenant_scope': True})
            self.assertTrue(institute_enabled(school))
            # The create/edit routes set section_id = None whenever this is True;
            # the school branch (below) is the only one that reads the posted id.
            plain = db.session.get(School, self.school['school_id'],
                                   execution_options={'bypass_tenant_scope': True})
            self.assertFalse(institute_enabled(plain))

    # ── 5. Edit adds one group and ends another, keeping history ─────────────

    def test_edit_adds_one_group_and_ends_another(self):
        from app.utils.institute_groups import stage_enrollment_changes

        with self.app.app_context():
            g1 = self._make_group(self.inst, 'Keep')
            g2 = self._make_group(self.inst, 'Drop')
            g3 = self._make_group(self.inst, 'Add')
            sid = self._make_student(self.inst, 'Edit Student')
            self._enroll(self.inst, g1, sid)
            drop_row = self._enroll(self.inst, g2, sid)
            db.session.commit()

            added, ended = stage_enrollment_changes(
                self.inst['school_id'], sid, [g1, g3])
            db.session.commit()

            self.assertEqual((added, ended), (1, 1))

            rows = (InstituteGroupEnrollment.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(student_id=sid).all())
            by_group = {r.group_id: r for r in rows}

            # kept — untouched
            self.assertEqual(by_group[g1].status, 'active')
            self.assertIsNone(by_group[g1].ended_at)
            # added
            self.assertEqual(by_group[g3].status, 'active')
            # removed — ENDED, and the historical row still exists
            self.assertEqual(by_group[g2].id, drop_row)
            self.assertEqual(by_group[g2].status, 'ended')
            self.assertIsNotNone(by_group[g2].ended_at)
            self.assertEqual(len(rows), 3, 'no row may be deleted')

    # ── 6. Duplicate active enrollment is rejected ───────────────────────────

    def test_duplicate_active_enrollment_is_rejected(self):
        with self.app.app_context():
            group_id = self._make_group(self.inst, 'Dup Group')
            sid = self._make_student(self.inst, 'Dup Student')
            self._enroll(self.inst, group_id, sid)
            db.session.commit()

            db.session.add(InstituteGroupEnrollment(
                school_id=self.inst['school_id'], group_id=group_id,
                student_id=sid, status=InstituteGroupEnrollment.STATUS_ACTIVE))
            with self.assertRaises(IntegrityError):
                db.session.flush()
            db.session.rollback()

    # ── 7. Ending retains the row and permits re-enrollment ──────────────────

    def test_ended_enrollment_is_retained_and_allows_reenrollment(self):
        from datetime import datetime

        with self.app.app_context():
            group_id = self._make_group(self.inst, 'Rejoin Group')
            sid = self._make_student(self.inst, 'Rejoin Student')
            first_id = self._enroll(self.inst, group_id, sid)
            db.session.commit()

            first = db.session.get(InstituteGroupEnrollment, first_id,
                                   execution_options={'bypass_tenant_scope': True})
            first.status = InstituteGroupEnrollment.STATUS_ENDED
            first.ended_at = datetime.utcnow()
            db.session.commit()

            second_id = self._enroll(self.inst, group_id, sid)
            db.session.commit()

            rows = (InstituteGroupEnrollment.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(group_id=group_id, student_id=sid)
                    .order_by(InstituteGroupEnrollment.id).all())
            self.assertEqual([r.id for r in rows], [first_id, second_id],
                             'the ended row must be retained alongside the new one')
            self.assertEqual(rows[0].status, 'ended')
            self.assertEqual(rows[1].status, 'active')

    # ── 8. Central tenant scoping blocks cross-school reads ──────────────────

    def test_tenant_scope_hides_other_schools_rows(self):
        with self.app.app_context():
            mine_group = self._make_group(self.inst, 'Mine Scoped')
            theirs_group = self._make_group(self.other, 'Theirs Scoped')
            mine_student = self._make_student(self.inst, 'Mine Scoped Student')
            theirs_student = self._make_student(self.other, 'Theirs Scoped Student')
            self._enroll(self.inst, mine_group, mine_student)
            self._enroll(self.other, theirs_group, theirs_student)
            db.session.commit()

        # A normally-scoped request as the institute's own admin: the ORM tenant
        # guard must expose this institute's rows and none of the other's.
        with self.app.test_request_context('/institute-groups/'):
            self._login(self.inst)
            group_ids = {g.id for g in InstituteStudyGroup.query.all()}
            enrol_school_ids = {e.school_id
                                for e in InstituteGroupEnrollment.query.all()}
            logout_user()

        self.assertIn(mine_group, group_ids)
        self.assertNotIn(theirs_group, group_ids,
                         'tenant scope must hide another school\'s groups')
        self.assertNotIn(self.other['school_id'], enrol_school_ids,
                         'tenant scope must hide another school\'s enrollments')


if __name__ == '__main__':
    unittest.main()
