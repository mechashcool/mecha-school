"""
Tests for the external (public) student-registration feature.

Focus: token security, tenant isolation, field-exclusion (financial/account),
no-auto-fee, upload safety, idempotency, authorization, and CSRF.

Run against a LOCAL Postgres (the conftest guard blocks production URLs):
    $env:DATABASE_URL='postgresql://user:pass@localhost:5432/almuhandis_test'
    python -m pytest tests/test_external_registration.py -v
Roles must already be seeded in that database.
"""
import io
import unittest
from datetime import date
from uuid import uuid4

from app import create_app
from app.models import (db, Role, School, User, AcademicYear, Grade, Section,
                        Student, Notification, StudentRegistrationRequest,
                        StudentRegistrationRequestDocument, parent_students)
from app.utils.registration_tokens import (generate_token, hash_token,
                                           encrypt_token, decrypt_token)


def _uid():
    return uuid4().hex[:10]


# ── PNG magic-byte header so upload content-sniffing accepts the test image ────
_PNG = (b'\x89PNG\r\n\x1a\n' + b'\x00' * 64)
_FAKE_EXE = b'MZ\x90\x00' + b'\x00' * 64   # disguised as .png but not a PNG


class ExternalRegistrationTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        # Disable CSRF + rate limiting for the functional flow tests; a dedicated
        # test re-enables CSRF to verify it is enforced.
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.app.config['RATELIMIT_ENABLED'] = False

    def setUp(self):
        self.sfx = _uid()
        self.ctx = self.app.test_request_context()
        self.ctx.push()
        self.client = self.app.test_client()

        self.super_role   = Role.query.filter_by(name='super_admin').first()
        self.admin_role   = Role.query.filter_by(name='school_admin').first()
        self.teacher_role = Role.query.filter_by(name='teacher').first()
        self.parent_role  = Role.query.filter_by(name='parent').first()
        for r in (self.super_role, self.admin_role, self.teacher_role, self.parent_role):
            self.assertIsNotNone(r, 'Seed roles before running these tests')

        # School A (feature enabled) + School B (isolation target).
        self.school_a, self.year_a, self.grade_a, self.section_a = self._make_school('A')
        self.school_b, self.year_b, self.grade_b, self.section_b = self._make_school('B')

        self.token_a = self._enable_registration(self.school_a)
        self.token_b = self._enable_registration(self.school_b)

        self.admin_a   = self._make_user('adminA', self.admin_role, self.school_a)
        self.teacher_a = self._make_user('tchA', self.teacher_role, self.school_a)
        self.admin_b   = self._make_user('adminB', self.admin_role, self.school_b)
        db.session.commit()

        self.school_a_id = self.school_a.id
        self.school_b_id = self.school_b.id
        self.grade_a_id  = self.grade_a.id
        self.grade_b_id  = self.grade_b.id
        self.admin_a_id  = self.admin_a.id
        self.teacher_a_id = self.teacher_a.id
        self.admin_b_id  = self.admin_b.id

    def tearDown(self):
        try:
            db.session.rollback()
            for sid in (self.school_a_id, self.school_b_id):
                self._purge_school(sid)
            db.session.commit()
        except Exception:
            db.session.rollback()
        self.ctx.pop()

    # ── builders ──────────────────────────────────────────────────────────────

    def _make_school(self, tag):
        s = School(school_name=f'S{tag}{self.sfx}', code=f'{tag}{self.sfx[:6]}',
                   is_active=True)
        db.session.add(s)
        db.session.flush()
        y = AcademicYear(school_id=s.id, name=f'Y{tag}{self.sfx}',
                         start_date=date(2025, 8, 1), end_date=date(2026, 6, 30),
                         is_current=True)
        db.session.add(y)
        db.session.flush()
        g = Grade(name=f'G{tag}', school_id=s.id, academic_year_id=y.id)
        db.session.add(g)
        db.session.flush()
        sec = Section(name=f'{tag}1', grade_id=g.id, school_id=s.id,
                      academic_year_id=y.id)
        db.session.add(sec)
        db.session.flush()
        return s, y, g, sec

    def _make_user(self, tag, role, school, phone=None):
        u = User(username=f'{tag}_{self.sfx}', full_name=f'{tag} {self.sfx}',
                 email=f'{tag}_{self.sfx}@t.com', phone=phone,
                 role_id=role.id, school_id=school.id if school else None,
                 is_active=True)
        u.set_password('pw12345')
        db.session.add(u)
        db.session.flush()
        return u

    def _enable_registration(self, school):
        raw = generate_token()
        school.external_registration_enabled = True
        school.registration_token_hash = hash_token(raw)
        school.registration_token_encrypted = encrypt_token(raw)
        db.session.flush()
        return raw

    def _purge_school(self, sid):
        StudentRegistrationRequestDocument.query.execution_options(
            bypass_tenant_scope=True).filter_by(school_id=sid).delete()
        StudentRegistrationRequest.query.execution_options(
            bypass_tenant_scope=True).filter_by(school_id=sid).delete()
        # parent_students links for this school's students
        sids = [row[0] for row in db.session.execute(
            db.select(Student.id).where(Student.school_id == sid)).all()]
        if sids:
            db.session.execute(parent_students.delete().where(
                parent_students.c.student_id.in_(sids)))
        from app.models import StudentDocument, AuditLog
        StudentDocument.query.execution_options(
            bypass_tenant_scope=True).filter_by(school_id=sid).delete()
        Student.query.execution_options(
            bypass_tenant_scope=True).filter_by(school_id=sid).delete()
        Notification.query.execution_options(
            bypass_tenant_scope=True).filter_by(school_id=sid).delete()
        AuditLog.query.execution_options(
            bypass_tenant_scope=True).filter_by(school_id=sid).delete()
        Section.query.execution_options(
            bypass_tenant_scope=True).filter_by(school_id=sid).delete()
        Grade.query.execution_options(
            bypass_tenant_scope=True).filter_by(school_id=sid).delete()
        User.query.execution_options(
            bypass_tenant_scope=True).filter_by(school_id=sid).delete()
        AcademicYear.query.execution_options(
            bypass_tenant_scope=True).filter_by(school_id=sid).delete()
        School.query.filter_by(id=sid).delete()

    # ── helpers ─────────────────────────────────────────────────────────────────

    def _login(self, user_id):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True

    def _submit(self, token, **overrides):
        data = {
            'submission_nonce': _uid(),
            'desired_grade_id': str(self.grade_a_id),
            'full_name': 'الطالب التجريبي',
            'guardian_name': 'ولي الأمر',
            'guardian_phone': '07701234567',
        }
        data.update(overrides)
        return self.client.post(f'/register/{token}', data=data,
                                content_type='multipart/form-data')

    def _pending_for(self, school_id):
        return (StudentRegistrationRequest.query.execution_options(
                bypass_tenant_scope=True)
                .filter_by(school_id=school_id).all())

    # ══════════════════════════════════════════════════════════════════════════
    #  TOKEN SECURITY
    # ══════════════════════════════════════════════════════════════════════════

    def test_invalid_token_generic_page(self):
        resp = self.client.get('/register/not-a-real-token')
        self.assertEqual(resp.status_code, 404)

    def test_disabled_feature_fails_closed(self):
        self.school_a.external_registration_enabled = False
        db.session.commit()
        resp = self.client.get(f'/register/{self.token_a}')
        self.assertEqual(resp.status_code, 404)

    def test_inactive_school_fails_closed(self):
        self.school_a.is_active = False
        db.session.commit()
        self.assertEqual(self.client.get(f'/register/{self.token_a}').status_code, 404)

    def test_regenerate_invalidates_old_and_keeps_recoverable(self):
        old = self.token_a
        # Super-admin regenerate.
        self._login(self.super_role and self._make_user('sup', self.super_role, None).id)
        db.session.commit()
        resp = self.client.post(
            f'/admin/super/schools/{self.school_a_id}/registration/regenerate')
        self.assertIn(resp.status_code, (301, 302))
        db.session.expire_all()
        school = School.query.get(self.school_a_id)
        # Old link no longer resolves.
        self.assertEqual(self.client.get(f'/register/{old}').status_code, 404)
        # New link is recoverable (decryptable) and works.
        raw = decrypt_token(school.registration_token_encrypted)
        self.assertIsNotNone(raw)
        self.assertNotEqual(raw, old)
        self.assertEqual(self.client.get(f'/register/{raw}').status_code, 200)

    def test_token_only_stored_as_hash_and_ciphertext(self):
        school = School.query.get(self.school_a_id)
        # Raw token must NOT appear in stored fields.
        self.assertNotEqual(school.registration_token_hash, self.token_a)
        self.assertNotEqual(school.registration_token_encrypted, self.token_a)
        self.assertEqual(school.registration_token_hash, hash_token(self.token_a))
        self.assertEqual(decrypt_token(school.registration_token_encrypted), self.token_a)

    # ══════════════════════════════════════════════════════════════════════════
    #  PUBLIC SUBMISSION
    # ══════════════════════════════════════════════════════════════════════════

    def test_valid_submission_creates_request_only(self):
        resp = self._submit(self.token_a)
        self.assertIn(resp.status_code, (301, 302))
        self.assertTrue(resp.headers['Location'].split('?')[0]
                        .startswith('/register/track/'))
        reqs = self._pending_for(self.school_a_id)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].status, 'pending')
        self.assertEqual(reqs[0].school_id, self.school_a_id)
        # No Student is created at submission time.
        self.assertEqual(Student.query.execution_options(bypass_tenant_scope=True)
                         .filter_by(school_id=self.school_a_id).count(), 0)
        # Staff notification exists for this school only.
        self.assertGreaterEqual(Notification.query.execution_options(
            bypass_tenant_scope=True).filter_by(
            school_id=self.school_a_id, ntype='registration_request').count(), 1)

    def test_no_store_on_tracking(self):
        resp = self._submit(self.token_a)
        track = self.client.get(resp.headers['Location'])
        self.assertEqual(track.status_code, 200)
        self.assertIn('no-store', track.headers.get('Cache-Control', ''))

    def test_cross_school_grade_rejected(self):
        # Grade from School B submitted through School A's link → rejected.
        resp = self._submit(self.token_a, desired_grade_id=str(self.grade_b_id))
        self.assertEqual(resp.status_code, 200)  # re-render, not a redirect
        self.assertEqual(len(self._pending_for(self.school_a_id)), 0)

    def test_financial_field_rejected(self):
        resp = self._submit(self.token_a, fee_type_id='1', create_fee='1')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self._pending_for(self.school_a_id)), 0)

    def test_account_field_rejected(self):
        resp = self._submit(self.token_a, parent_username='HACKER',
                            parent_password='xx123456', create_parent_account='1')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self._pending_for(self.school_a_id)), 0)

    def test_section_and_rfid_fields_rejected(self):
        resp = self._submit(self.token_a, section_id='1', rfid_tag_id='ABC')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self._pending_for(self.school_a_id)), 0)

    def test_double_submit_same_nonce_is_idempotent(self):
        nonce = _uid()
        r1 = self._submit(self.token_a, submission_nonce=nonce)
        r2 = self._submit(self.token_a, submission_nonce=nonce)
        self.assertIn(r1.status_code, (301, 302))
        self.assertEqual(r2.status_code, 200)  # "already received" page
        self.assertEqual(len(self._pending_for(self.school_a_id)), 1)

    def test_disguised_executable_upload_rejected(self):
        resp = self._submit(
            self.token_a,
            **{'document_type[]': 'الهوية الوطنية',
               'document_file[]': (io.BytesIO(_FAKE_EXE), 'id.png')})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self._pending_for(self.school_a_id)), 0)

    def test_valid_png_document_accepted(self):
        resp = self._submit(
            self.token_a,
            **{'document_type[]': 'الهوية الوطنية',
               'document_file[]': (io.BytesIO(_PNG), 'id.png')})
        self.assertIn(resp.status_code, (301, 302))
        reqs = self._pending_for(self.school_a_id)
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].documents.count(), 1)

    # ══════════════════════════════════════════════════════════════════════════
    #  STAFF PROCESSING / ISOLATION
    # ══════════════════════════════════════════════════════════════════════════

    def _create_pending(self, school_id, grade_id, phone='07701234567'):
        req = StudentRegistrationRequest(
            school_id=school_id, academic_year_id=(
                AcademicYear.query.execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=school_id, is_current=True).first().id),
            desired_grade_id=grade_id, full_name='طالب', guardian_name='ولي',
            guardian_phone=phone, status='pending',
            tracking_token_hash=hash_token(generate_token()),
            submission_nonce=_uid())
        db.session.add(req)
        db.session.commit()
        return req.id

    def test_cross_school_request_detail_404(self):
        rid = self._create_pending(self.school_b_id, self.grade_b_id)
        self._login(self.admin_a_id)   # School A admin
        resp = self.client.get(f'/admissions/{rid}')
        self.assertEqual(resp.status_code, 404)

    def test_cross_school_approve_404(self):
        rid = self._create_pending(self.school_b_id, self.grade_b_id)
        self._login(self.admin_a_id)
        resp = self.client.post(f'/admissions/{rid}/approve')
        self.assertEqual(resp.status_code, 404)
        req = StudentRegistrationRequest.query.execution_options(
            bypass_tenant_scope=True).get(rid)
        self.assertEqual(req.status, 'pending')

    def test_unauthorized_role_cannot_approve(self):
        rid = self._create_pending(self.school_a_id, self.grade_a_id)
        self._login(self.teacher_a_id)  # teacher lacks add_student
        resp = self.client.post(f'/admissions/{rid}/approve')
        self.assertEqual(resp.status_code, 403)

    def test_approve_creates_student_and_new_parent_once(self):
        rid = self._create_pending(self.school_a_id, self.grade_a_id)
        self._login(self.admin_a_id)
        r1 = self.client.post(f'/admissions/{rid}/approve',
                              data={'parent_choice': 'new'})
        self.assertIn(r1.status_code, (301, 302))
        req = StudentRegistrationRequest.query.execution_options(
            bypass_tenant_scope=True).get(rid)
        self.assertEqual(req.status, 'approved')
        self.assertIsNotNone(req.approved_student_id)
        self.assertTrue(req.parent_account_created)
        # Second approve is idempotent — no duplicate student/parent.
        r2 = self.client.post(f'/admissions/{rid}/approve',
                              data={'parent_choice': 'new'})
        self.assertIn(r2.status_code, (301, 302))
        self.assertEqual(Student.query.execution_options(bypass_tenant_scope=True)
                         .filter_by(school_id=self.school_a_id).count(), 1)

    def test_approve_links_existing_parent_without_touching_password(self):
        parent = self._make_user('par', self.parent_role, self.school_a,
                                  phone='07709998888')
        db.session.commit()
        old_hash = parent.password_hash
        pid = parent.id
        rid = self._create_pending(self.school_a_id, self.grade_a_id,
                                   phone='07709998888')
        self._login(self.admin_a_id)
        resp = self.client.post(f'/admissions/{rid}/approve',
                               data={'parent_choice': 'link',
                                     'link_parent_id': str(pid)})
        self.assertIn(resp.status_code, (301, 302))
        db.session.expire_all()
        parent = User.query.execution_options(bypass_tenant_scope=True).get(pid)
        # Password never changed; account reused (linked), not duplicated.
        self.assertEqual(parent.password_hash, old_hash)
        req = StudentRegistrationRequest.query.execution_options(
            bypass_tenant_scope=True).get(rid)
        self.assertFalse(req.parent_account_created)
        self.assertEqual(req.linked_parent_id, pid)
        link = db.session.execute(db.select(parent_students.c.user_id).where(
            parent_students.c.user_id == pid,
            parent_students.c.student_id == req.approved_student_id)).first()
        self.assertIsNotNone(link)

    def test_approve_creates_no_fee(self):
        from app.models import FeeRecord
        rid = self._create_pending(self.school_a_id, self.grade_a_id)
        self._login(self.admin_a_id)
        self.client.post(f'/admissions/{rid}/approve', data={'parent_choice': 'new'})
        self.assertEqual(FeeRecord.query.execution_options(bypass_tenant_scope=True)
                         .filter_by(school_id=self.school_a_id).count(), 0)

    def test_reject_sets_reason_and_is_idempotent(self):
        rid = self._create_pending(self.school_a_id, self.grade_a_id)
        self._login(self.admin_a_id)
        self.client.post(f'/admissions/{rid}/reject',
                         data={'rejection_reason': 'مستندات ناقصة'})
        req = StudentRegistrationRequest.query.execution_options(
            bypass_tenant_scope=True).get(rid)
        self.assertEqual(req.status, 'rejected')
        self.assertEqual(req.rejection_reason, 'مستندات ناقصة')
        # Second reject is a no-op.
        self.client.post(f'/admissions/{rid}/reject', data={'rejection_reason': 'x'})
        req = StudentRegistrationRequest.query.execution_options(
            bypass_tenant_scope=True).get(rid)
        self.assertEqual(req.status, 'rejected')

    def test_approved_request_cannot_be_rejected(self):
        rid = self._create_pending(self.school_a_id, self.grade_a_id)
        self._login(self.admin_a_id)
        self.client.post(f'/admissions/{rid}/approve', data={'parent_choice': 'new'})
        self.client.post(f'/admissions/{rid}/reject', data={'rejection_reason': 'x'})
        req = StudentRegistrationRequest.query.execution_options(
            bypass_tenant_scope=True).get(rid)
        self.assertEqual(req.status, 'approved')  # unchanged

    # ══════════════════════════════════════════════════════════════════════════
    #  CSRF
    # ══════════════════════════════════════════════════════════════════════════

    def test_csrf_required_on_approve(self):
        rid = self._create_pending(self.school_a_id, self.grade_a_id)
        self.app.config['WTF_CSRF_ENABLED'] = True
        try:
            self._login(self.admin_a_id)
            resp = self.client.post(f'/admissions/{rid}/approve',
                                    data={'parent_choice': 'new'})
            self.assertIn(resp.status_code, (400, 302))
            req = StudentRegistrationRequest.query.execution_options(
                bypass_tenant_scope=True).get(rid)
            self.assertEqual(req.status, 'pending')  # not approved without CSRF
        finally:
            self.app.config['WTF_CSRF_ENABLED'] = False


if __name__ == '__main__':
    unittest.main()
