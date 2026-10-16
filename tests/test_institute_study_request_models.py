"""Institute study requests ("أرسل درسي") — P1 foundation only.

Models, constraints, tenant scoping, upload-access ownership and cleanup
integration of InstituteStudyRequest / InstituteStudyRequestImage. No API, no
upload: every file_path here is a metadata string — nothing is ever written to
Supabase Storage or to disk.
"""
import unittest
from datetime import date, datetime
from uuid import uuid4

from flask import g
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.models import (
    db, AcademicYear, FeeInstallment, FeeRecord, FeeType, InstituteStudyRequest,
    InstituteStudyRequestImage, Permission, Role, School, Student, User,
    parent_students,
)
from app.utils.school_cleanup import cleanup_school_cascade, linked_school_counts
from app.utils.student_removal import (OUTCOME_FINANCIAL_TOMBSTONE,
                                       OUTCOME_PHYSICAL_DELETE,
                                       remove_student_permanently)
from app.utils.upload_access import (STUDY_REQUEST_IMAGE_KIND, can_access_upload,
                                     resolve_upload_owner)

OPTS = {'bypass_tenant_scope': True, 'include_all_years': True}
PASSWORD = 'Password123!'
IMG_TABLE = 'institute_study_request_images'


class InstituteStudyRequestModelTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config.update(RATELIMIT_ENABLED=False, WTF_CSRF_ENABLED=False)
        with cls.app.app_context():
            cls.roles = {r.name: r.id for r in Role.query.all()}
            perm = Permission.query.filter_by(name='manage_leave_requests').first()
            assert perm is not None, 'manage_leave_requests must exist in the test DB'
            cls.leave_perm_id = perm.id

    # ── fixture ───────────────────────────────────────────────────────────────

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        with self.app.app_context():
            # Two custom staff roles: one WITH manage_leave_requests, one without.
            perm = db.session.get(Permission, self.leave_perm_id)
            with_perm = Role(name=f'isr_leave_{self.sfx}', label='isr leave')
            with_perm.permissions.append(perm)
            no_perm = Role(name=f'isr_noperm_{self.sfx}', label='isr noperm')
            db.session.add_all([with_perm, no_perm])
            db.session.flush()
            self.ids.update(role_leave=with_perm.id, role_noperm=no_perm.id)
            self._school('a', institute=True)
            self._school('b', institute=True)
            self._school('c', institute=False)
            sup = self._user('sup', 'super_admin', None)
            self.ids['super'] = sup.id
            db.session.commit()

    def _user(self, tag, role, school_id, role_id=None):
        u = User(username=f'isr{tag}_{self.sfx}', email=f'isr{tag}_{self.sfx}@t.test',
                 full_name=f'isr {tag}', role_id=role_id or self.roles[role],
                 school_id=school_id, is_active=True)
        u.set_password(PASSWORD)
        db.session.add(u)
        db.session.flush()
        return u

    def _school(self, key, *, institute):
        s = self.sfx
        school = School(school_name=f'ISR {key} {s}', code=f'ISR{key}{s}'[:20],
                        capacity=0, is_active=True,
                        institution_type='institute' if institute else 'school')
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'Y{key}{s}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 7, 31))
        db.session.add(year)
        db.session.flush()
        ft = FeeType(name=f'Tuition {s}', school_id=school.id, academic_year_id=year.id)
        db.session.add(ft)
        sid = school.id
        users = {
            'admin':   self._user(f'adm{key}', 'school_admin', sid),
            'parent':  self._user(f'par{key}', 'parent', sid),
            'parent2': self._user(f'pr2{key}', 'parent', sid),
            'teacher': self._user(f'tch{key}', 'teacher', sid),
            'acct':    self._user(f'acc{key}', 'accountant', sid),
            'investor': self._user(f'inv{key}', 'investor_viewer', sid),
            'staff_leave':  self._user(f'stl{key}', None, sid, self.ids['role_leave']),
            'staff_noperm': self._user(f'stn{key}', None, sid, self.ids['role_noperm']),
        }
        db.session.flush()
        self.ids.update({f'school_{key}': sid, f'year_{key}': year.id, f'ft_{key}': ft.id})
        self.ids.update({f'{name}_{key}': u.id for name, u in users.items()})

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for key in ('a', 'b', 'c'):
                cleanup_school_cascade(self.ids[f'school_{key}'])
            User.query.execution_options(**OPTS).filter_by(
                id=self.ids['super']).delete(synchronize_session=False)
            for rid in (self.ids['role_leave'], self.ids['role_noperm']):
                role = db.session.get(Role, rid)
                if role is not None:
                    db.session.delete(role)
            db.session.commit()
            db.session.remove()

    # ── data helpers (call inside an app context) ────────────────────────────

    def _student(self, key='a', parent_key='parent'):
        st = Student(student_id=f'IS{uuid4().hex[:10]}', full_name=f'Stu {self.sfx}',
                     school_id=self.ids[f'school_{key}'],
                     academic_year_id=self.ids[f'year_{key}'], status='active')
        db.session.add(st)
        db.session.flush()
        if parent_key:
            db.session.execute(parent_students.insert().values(
                user_id=self.ids[f'{parent_key}_{key}'], student_id=st.id))
        return st

    def _request(self, student, key='a', **kw):
        req = InstituteStudyRequest(
            school_id=self.ids[f'school_{key}'], academic_year_id=self.ids[f'year_{key}'],
            student_id=student.id, parent_id=self.ids[f'parent_{key}'], **kw)
        db.session.add(req)
        db.session.flush()
        return req

    def _image(self, req, school_key='a', sort_order=0):
        path = (f'https://isr.invalid/storage/v1/object/public/uploads/schools/'
                f'{req.school_id}/institute-study-requests/{uuid4().hex}.webp')
        img = InstituteStudyRequestImage(request_id=req.id,
                                         school_id=self.ids[f'school_{school_key}'],
                                         file_path=path, mime='image/webp',
                                         size_bytes=1234, sort_order=sort_order)
        db.session.add(img)
        db.session.flush()
        return img

    def _count(self, model, **filters):
        return model.query.execution_options(**OPTS).filter_by(**filters).count()

    # ── 1-6: creation, defaults, status constraint ───────────────────────────

    def test_01_03_create_with_defaults(self):
        with self.app.app_context():
            req = self._request(self._student())
            db.session.commit()
            row = (InstituteStudyRequest.query.execution_options(**OPTS)
                   .filter_by(id=req.id).one())
            self.assertEqual(row.status, 'pending')
            self.assertEqual(row.image_count, 0)
            self.assertIsNone(row.note)
            self.assertIsNone(row.admin_note)
            self.assertIsNone(row.viewed_by)
            self.assertIsNotNone(row.created_at)
            self.assertIsNotNone(row.updated_at)

    def test_04_05_pending_and_viewed_allowed(self):
        with self.app.app_context():
            req = self._request(self._student(), status='pending', note='درس الرياضيات')
            db.session.commit()
            req.status = 'viewed'
            req.viewed_by = self.ids['admin_a']
            req.viewed_at = datetime.utcnow()
            req.admin_note = 'سيتم شرح هذا الموضوع في المحاضرة القادمة'
            db.session.commit()
            row = db.session.get(InstituteStudyRequest, req.id, execution_options=OPTS)
            self.assertEqual(row.status, 'viewed')
            self.assertEqual(row.admin_note, 'سيتم شرح هذا الموضوع في المحاضرة القادمة')

    def test_06_invalid_status_rejected_by_db(self):
        for bad in ('approved', 'rejected', 'cancelled', ''):
            with self.subTest(status=bad), self.app.app_context():
                stu = self._student()
                with self.assertRaises(IntegrityError) as ctx:
                    self._request(stu, status=bad)
                self.assertIn('ck_inst_study_request_status', str(ctx.exception))
                db.session.rollback()
        with self.app.app_context():
            stu = self._student()
            with self.assertRaises(IntegrityError) as ctx:
                self._request(stu, image_count=-1)
            self.assertIn('ck_inst_study_request_image_count', str(ctx.exception))
            db.session.rollback()

    # ── 7-9: same-school integrity (ORM guard AND PostgreSQL) ────────────────

    def test_07_image_same_school_ok(self):
        with self.app.app_context():
            req = self._request(self._student())
            img = self._image(req, sort_order=1)
            req.image_count = 1
            db.session.commit()
            self.assertEqual(self._count(InstituteStudyRequestImage, request_id=req.id), 1)
            self.assertEqual(img.request.id, req.id)

    def test_08_cross_school_image_rejected(self):
        with self.app.app_context():
            req = self._request(self._student('a'), 'a')
            db.session.commit()
            req_id = req.id
            # Central write guard.
            with self.assertRaisesRegex(ValueError, 'same school as its request'):
                self._image(req, school_key='b')
            db.session.rollback()
            # PostgreSQL composite FK, even with the ORM guard switched off.
            db.session.info['skip_tenant_validation'] = True
            try:
                db.session.add(InstituteStudyRequestImage(
                    request_id=req_id, school_id=self.ids['school_b'],
                    file_path=f'x/{uuid4().hex}.webp'))
                with self.assertRaises(IntegrityError) as ctx:
                    db.session.flush()
                self.assertIn('fk_inst_study_request_image_request_school', str(ctx.exception))
            finally:
                db.session.rollback()
                db.session.info.pop('skip_tenant_validation', None)

    def test_09_cross_school_request_links_rejected(self):
        with self.app.app_context():
            stu_a = self._student('a')
            stu_b = self._student('b')
            db.session.commit()
            cases = {
                'student school': dict(student_id=stu_b.id, parent_id=self.ids['parent_a'],
                                       academic_year_id=self.ids['year_a']),
                'parent school': dict(student_id=stu_a.id, parent_id=self.ids['parent_b'],
                                      academic_year_id=self.ids['year_a']),
                'academic_year_id does not belong': dict(
                    student_id=stu_a.id, parent_id=self.ids['parent_a'],
                    academic_year_id=self.ids['year_b']),
            }
            for msg, kw in cases.items():
                with self.subTest(case=msg):
                    db.session.add(InstituteStudyRequest(school_id=self.ids['school_a'], **kw))
                    with self.assertRaisesRegex(ValueError, msg):
                        db.session.flush()
                    db.session.rollback()
            # Viewer from another school.
            req = self._request(stu_a)
            req.viewed_by = self.ids['admin_b']
            with self.assertRaisesRegex(ValueError, 'viewer must belong'):
                db.session.flush()
            db.session.rollback()
            # PostgreSQL composite FK on the student, guard switched off.
            db.session.info['skip_tenant_validation'] = True
            try:
                db.session.add(InstituteStudyRequest(
                    school_id=self.ids['school_a'], academic_year_id=self.ids['year_a'],
                    student_id=stu_b.id, parent_id=self.ids['parent_a']))
                with self.assertRaises(IntegrityError) as ctx:
                    db.session.flush()
                self.assertIn('fk_inst_study_request_student_school', str(ctx.exception))
            finally:
                db.session.rollback()
                db.session.info.pop('skip_tenant_validation', None)

    def test_09b_non_institute_school_rejected_on_insert(self):
        with self.app.app_context():
            with self.assertRaisesRegex(ValueError, 'institutes only'):
                self._request(self._student('c'), 'c')
            db.session.rollback()

    def test_09c_tenant_scope_hides_other_school_rows(self):
        with self.app.app_context():
            req_a = self._request(self._student('a'), 'a')
            img_a = self._image(req_a, 'a')
            req_b = self._request(self._student('b'), 'b')
            img_b = self._image(req_b, 'b')
            db.session.commit()
            ids = (req_a.id, req_b.id, img_a.id, img_b.id)
        with self.app.test_request_context('/'):
            g.tenant_scope_school_id = self.ids['school_a']
            g.tenant_scope_academic_year_id = self.ids['year_a']
            g.tenant_scope_view_year_id = self.ids['year_a']
            req_ids = {r.id for r in InstituteStudyRequest.query
                       .filter(InstituteStudyRequest.id.in_(ids[:2])).all()}
            img_ids = {i.id for i in InstituteStudyRequestImage.query
                       .filter(InstituteStudyRequestImage.id.in_(ids[2:])).all()}
            self.assertEqual(req_ids, {ids[0]})
            self.assertEqual(img_ids, {ids[2]})
            self.assertIsNone(db.session.get(InstituteStudyRequest, ids[1]))
            db.session.remove()

    # ── 10: list queries never touch the image table ─────────────────────────

    def test_10_request_query_does_not_load_images(self):
        with self.app.app_context():
            stu = self._student()
            for _ in range(3):
                req = self._request(stu)
                self._image(req)
                self._image(req, sort_order=1)
                req.image_count = 2
            db.session.commit()
            stu_id = stu.id
            db.session.expunge_all()
            statements = []
            engine = db.engine

            def _capture(conn, cursor, statement, *args):
                statements.append(statement)

            event.listen(engine, 'before_cursor_execute', _capture)
            try:
                rows = (InstituteStudyRequest.query.execution_options(**OPTS)
                        .filter_by(student_id=stu_id)
                        .order_by(InstituteStudyRequest.created_at.desc()).all())
                counts = [r.image_count for r in rows]
            finally:
                event.remove(engine, 'before_cursor_execute', _capture)
            self.assertEqual(counts, [2, 2, 2])
            self.assertEqual(len(statements), 1)
            self.assertNotIn(IMG_TABLE, statements[0])
            self.assertTrue(all('images' not in r.__dict__ for r in rows))
            # Detail path: images load only on explicit access, in sort order.
            self.assertEqual([i.sort_order for i in rows[0].images], [0, 1])

    # ── 11-12: cleanup integration ───────────────────────────────────────────

    def test_11_physical_student_delete_removes_rows(self):
        with self.app.app_context():
            stu = self._student()
            req = self._request(stu)
            self._image(req)
            self._image(req, sort_order=1)
            other = self._request(self._student())     # unrelated student kept
            self._image(other)
            db.session.commit()
            sid, req_id, other_id = stu.id, req.id, other.id
            student = db.session.get(Student, sid, execution_options=OPTS)
            self.assertEqual(remove_student_permanently(student), OUTCOME_PHYSICAL_DELETE)
            db.session.commit()
            self.assertIsNone(db.session.get(Student, sid, execution_options=OPTS))
            self.assertEqual(self._count(InstituteStudyRequest, student_id=sid), 0)
            self.assertEqual(self._count(InstituteStudyRequestImage, request_id=req_id), 0)
            self.assertEqual(self._count(InstituteStudyRequestImage, request_id=other_id), 1)

    def test_11b_financial_tombstone_keeps_requests(self):
        with self.app.app_context():
            stu = self._student()
            rec = FeeRecord(student_id=stu.id, fee_type_id=self.ids['ft_a'],
                            academic_year_id=self.ids['year_a'],
                            school_id=self.ids['school_a'], total_amount=100, discount=0)
            db.session.add(rec)
            db.session.flush()
            db.session.add(FeeInstallment(
                fee_record_id=rec.id, school_id=rec.school_id,
                academic_year_id=rec.academic_year_id, installment_no=1, amount=100,
                received_amount=100, due_date=date(2026, 9, 1)))
            req = self._request(stu)
            self._image(req)
            db.session.commit()
            sid, req_id = stu.id, req.id
            student = db.session.get(Student, sid, execution_options=OPTS)
            self.assertEqual(remove_student_permanently(student), OUTCOME_FINANCIAL_TOMBSTONE)
            db.session.commit()
            self.assertEqual(self._count(InstituteStudyRequest, id=req_id), 1)
            self.assertEqual(self._count(InstituteStudyRequestImage, request_id=req_id), 1)

    def test_12_school_cleanup_removes_rows(self):
        with self.app.app_context():
            req = self._request(self._student('b'), 'b')
            self._image(req, 'b')
            self._image(req, 'b', sort_order=1)
            keep = self._request(self._student('a'), 'a')
            self._image(keep, 'a')
            db.session.commit()
            sid_b = self.ids['school_b']
            counts = linked_school_counts(sid_b)
            self.assertEqual(counts.get('طلبات أرسل درسي'), 1)
            self.assertEqual(counts.get('صور طلبات أرسل درسي'), 2)
            deleted = cleanup_school_cascade(sid_b)
            db.session.commit()
            self.assertEqual(deleted.get('صور طلبات أرسل درسي'), 2)
            self.assertEqual(deleted.get('طلبات أرسل درسي'), 1)
            self.assertEqual(self._count(InstituteStudyRequest, school_id=sid_b), 0)
            self.assertEqual(self._count(InstituteStudyRequestImage, school_id=sid_b), 0)
            self.assertEqual(self._count(InstituteStudyRequestImage,
                                         school_id=self.ids['school_a']), 1)

    # ── 13-15: upload-access ownership and rules ─────────────────────────────

    def _owned_image(self):
        stu = self._student('a', parent_key='parent')
        req = self._request(stu)
        img = self._image(req)
        db.session.commit()
        return img.file_path, stu.id, req.parent_id

    def _can(self, key, path):
        user = db.session.get(User, self.ids[key], execution_options=OPTS)
        return can_access_upload(user, path)

    def test_13_owner_resolution(self):
        with self.app.app_context():
            path, stu_id, parent_id = self._owned_image()
            owner = resolve_upload_owner(path)
            self.assertEqual(owner['kind'], STUDY_REQUEST_IMAGE_KIND)
            self.assertEqual(owner['school_id'], self.ids['school_a'])
            self.assertEqual(owner['student_id'], stu_id)
            self.assertEqual(owner['parent_id'], parent_id)
            self.assertIsNone(resolve_upload_owner(path + '.missing'))

    def test_14_teacher_always_denied(self):
        with self.app.app_context():
            path, _, _ = self._owned_image()
            self.assertFalse(self._can('teacher_a', path))
            # Even when the teacher account is explicitly granted the permission.
            teacher = db.session.get(User, self.ids['teacher_a'], execution_options=OPTS)
            teacher.extra_permissions.append(db.session.get(Permission, self.leave_perm_id))
            db.session.commit()
            self.assertTrue(teacher.has_permission('manage_leave_requests'))
            self.assertFalse(can_access_upload(teacher, path))

    def test_15_staff_without_permission_and_other_school_denied(self):
        with self.app.app_context():
            path, _, _ = self._owned_image()
            for key in ('staff_noperm_a', 'acct_a', 'investor_a',
                        'admin_b', 'staff_leave_b', 'parent_b'):
                with self.subTest(user=key):
                    self.assertFalse(self._can(key, path))

    def test_15b_family_and_administration_allowed(self):
        with self.app.app_context():
            path, stu_id, _ = self._owned_image()
            self.assertTrue(self._can('parent_a', path))        # linked parent
            self.assertFalse(self._can('parent2_a', path))      # same school, not linked
            self.assertTrue(self._can('admin_a', path))         # school manager
            self.assertTrue(self._can('staff_leave_a', path))   # manage_leave_requests
            self.assertTrue(self._can('super', path))           # platform owner
            anon = type('Anon', (), {'is_authenticated': False})()
            self.assertFalse(can_access_upload(anon, path))
            # A second linked parent of the same child is the child's family.
            db.session.execute(parent_students.insert().values(
                user_id=self.ids['parent2_a'], student_id=stu_id))
            db.session.commit()
            self.assertTrue(self._can('parent2_a', path))


if __name__ == '__main__':
    unittest.main()
