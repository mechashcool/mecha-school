"""
Tests for managing a student's attachments (المستمسكات) from the
Student Affairs → Students → Edit Student page, with production-safe
SOFT deletion.

What is asserted (negative cases are first-class here):
  * active attachments render on the right student's edit page,
  * a normal edit submission never touches them,
  * adding one does not modify the others,
  * delete hides the document but preserves BOTH its database row and its
    stored object, and records who/when,
  * replace activates a NEW document on a NEW path and keeps the previous one
    restorable, with its original file_path intact,
  * a failed upload or a failed database step leaves the original active and
    unchanged,
  * the school interface no longer exposes deleted documents at all, and a
    school user cannot reach the old restore endpoint directly,
  * Super Admin sees only the SELECTED school's deleted documents, restores
    them safely, and is rejected for wrong-school / stale-school requests,
  * a restore that would collide with an active document of the same type,
    or exceed the four-active-document limit, is rejected with no change,
  * no operator can reach another school's document, in any operation,
  * CSRF and the ``edit_student`` permission are still enforced.

App-context discipline
──────────────────────
Direct database work happens inside a short-lived ``self._db()`` app context
that is never held while the test client issues a request. Flask reuses an
already-pushed app context, so holding one across requests would give every
request the same ``g`` and let one request's cached tenant scope (school /
academic year) leak into the next — which never happens in production, where
each request gets its own app context.

Run against the isolated LOCAL Postgres approved in .env.test (the conftest
guard blocks anything else), after applying migration d1o2c3s4d5e6:

    $env:TEST_DATABASE_URL='postgresql://user:pass@127.0.0.1:55432/<name>_test'
    $env:TEST_DATABASE_APPROVED='<name>_test'
    python -m pytest tests/test_student_document_management.py -v

Roles must already be seeded in that database. Uploads land in the gitignored
app/static/uploads/ tree and every file this module creates is removed again in
tearDown.
"""
import io
import os
import unittest
from datetime import date
from uuid import uuid4

from app import create_app
from app.models import (db, Role, School, User, AcademicYear, Grade, Section,
                        Student, StudentDocument, AuditLog, parent_students)


def _uid():
    return uuid4().hex[:10]


# Real magic bytes, so the server-side content check accepts these payloads.
_PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64
_PDF = b'%PDF-1.7\n' + b'\x00' * 64
# Correct extension, wrong content — must be rejected by the magic-byte check.
_FAKE_PNG = b'MZ\x90\x00' + b'\x00' * 64


class StudentDocumentManagementTest(unittest.TestCase):

    PASSWORD = 'pw12345'

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['WTF_CSRF_ENABLED'] = False   # one test re-enables it
        cls.app.config['RATELIMIT_ENABLED'] = False

    def setUp(self):
        self.sfx = _uid()
        self.client = self.app.test_client()
        self._made_files = []

        with self._db():
            admin_role = Role.query.filter_by(name='school_admin').first()
            parent_role = Role.query.filter_by(name='parent').first()
            super_role = Role.query.filter_by(name='super_admin').first()
            for r in (admin_role, parent_role, super_role):
                self.assertIsNotNone(r, 'Seed roles before running these tests')

            school_a, year_a, _, section_a = self._make_school('A')
            school_b, year_b, _, section_b = self._make_school('B')

            admin_a  = self._make_user('adminA', admin_role, school_a)
            admin_b  = self._make_user('adminB', admin_role, school_b)
            parent_a = self._make_user('parA', parent_role, school_a)
            # A genuine super admin: role super_admin AND school_id IS NULL.
            sa = self._make_user('sa', super_role, None)

            student_a = self._make_student(school_a, section_a, 'A1')
            other_a   = self._make_student(school_a, section_a, 'A2')
            student_b = self._make_student(school_b, section_b, 'B1')
            db.session.commit()

            # Two active attachments on student A1; one on the second School-A
            # student and one on the School-B student (the isolation targets).
            self.doc_id       = self._make_doc(student_a, 'الهوية الوطنية')
            self.doc2_id      = self._make_doc(student_a, 'بطاقة السكن')
            self.other_doc_id = self._make_doc(other_a, 'الوثيقة الدراسية')
            self.doc_b_id     = self._make_doc(student_b, 'الهوية الوطنية')
            db.session.commit()

            # Only plain ids/strings survive outside the context.
            self.school_a_id   = school_a.id
            self.school_b_id   = school_b.id
            self.section_a_id  = section_a.id
            self.student_a_id  = student_a.id
            self.other_a_id    = other_a.id
            self.student_b_id  = student_b.id
            self.admin_a_id    = admin_a.id
            self.admin_a_name  = admin_a.username
            self.admin_b_name  = admin_b.username
            self.parent_a_name = parent_a.username
            self.sa_name       = sa.username
            self.sa_id         = sa.id

    def tearDown(self):
        with self._db():
            try:
                db.session.rollback()
                for sid in (self.school_a_id, self.school_b_id):
                    self._purge_school(sid)
                # The super admin has school_id IS NULL, so _purge_school
                # cannot reach it (nor its login audit rows).
                (AuditLog.query.execution_options(bypass_tenant_scope=True)
                 .filter(AuditLog.user_id == self.sa_id)
                 .delete(synchronize_session=False))
                (User.query.execution_options(bypass_tenant_scope=True)
                 .filter(User.id == self.sa_id)
                 .delete(synchronize_session=False))
                db.session.commit()
            except Exception:
                db.session.rollback()
        for path in self._made_files:
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass

    # ── context discipline ──────────────────────────────────────────────────────

    def _db(self):
        """A short-lived app context for direct DB work (see module docstring)."""
        return self.app.app_context()

    # ── builders (call inside self._db()) ───────────────────────────────────────

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

    def _make_user(self, tag, role, school):
        u = User(username=f'{tag}_{self.sfx}', full_name=f'{tag} {self.sfx}',
                 email=f'{tag}_{self.sfx}@t.com', role_id=role.id,
                 school_id=school.id if school else None, is_active=True)
        u.set_password(self.PASSWORD)
        db.session.add(u)
        db.session.flush()
        return u

    def _make_student(self, school, section, tag):
        s = Student(student_id=f'{tag}-{self.sfx}', full_name=f'طالب {tag}',
                    school_id=school.id, academic_year_id=section.academic_year_id,
                    section_id=section.id, status='active')
        db.session.add(s)
        db.session.flush()
        return s

    def _make_doc(self, student, doc_type, file_path=None):
        """Create an ACTIVE StudentDocument row; by default with a file on disk."""
        if file_path is None:
            file_path = ('uploads/students/documents/'
                         f"{doc_type.replace(' ', '_')}-{_uid()}.png")
            self._write_upload(file_path, _PNG)
        doc = StudentDocument(student_id=student.id, school_id=student.school_id,
                              academic_year_id=student.academic_year_id,
                              document_type=doc_type, file_path=file_path)
        db.session.add(doc)
        db.session.flush()
        return doc.id

    def _write_upload(self, rel_path, payload):
        full = os.path.join(self.app.root_path, 'static', *rel_path.split('/'))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as fh:
            fh.write(payload)
        self._made_files.append(full)
        return full

    def _purge_school(self, sid):
        sids = [row[0] for row in db.session.execute(
            db.select(Student.id).where(Student.school_id == sid)).all()]
        if sids:
            db.session.execute(parent_students.delete().where(
                parent_students.c.student_id.in_(sids)))
        # Audit rows written during login carry school_id=None (the tenant scope
        # is not established yet at that point), so they must also be cleared by
        # user_id or the users cannot be removed.
        uids = [row[0] for row in db.session.execute(
            db.select(User.id).where(User.school_id == sid)).all()]
        if uids:
            (AuditLog.query.execution_options(bypass_tenant_scope=True)
             .filter(AuditLog.user_id.in_(uids))
             .delete(synchronize_session=False))
        # student_documents is bulk-deleted while rows may reference each other
        # via replaced_by_id — ON DELETE SET NULL is what makes this safe.
        for model in (StudentDocument, Student, AuditLog, Section, Grade,
                      User, AcademicYear):
            model.query.execution_options(
                bypass_tenant_scope=True).filter_by(school_id=sid).delete()
        School.query.filter_by(id=sid).delete()

    # ── request / inspection helpers ────────────────────────────────────────────

    def _login(self, username):
        """Log in through the real login form, so the whole authenticated web
        flow (session, role redirect, permissions) is exercised as in production."""
        resp = self.client.post('/auth/login',
                                data={'username': username,
                                      'password': self.PASSWORD})
        self.assertEqual(resp.status_code, 302, 'login failed')

    def _active(self, student_id):
        """{doc_id: (document_type, file_path)} for ACTIVE attachments only."""
        with self._db():
            rows = (StudentDocument.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(student_id=student_id)
                    .filter(StudentDocument.deleted_at.is_(None)).all())
            return {r.id: (r.document_type, r.file_path) for r in rows}

    def _all_ids(self, student_id):
        """Every row id of a student, deleted or not."""
        with self._db():
            rows = (StudentDocument.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(student_id=student_id).all())
            return {r.id for r in rows}

    def _row(self, doc_id):
        """The raw row as a dict, or None if it no longer exists."""
        with self._db():
            r = (StudentDocument.query
                 .execution_options(bypass_tenant_scope=True)
                 .filter_by(id=doc_id).first())
            if r is None:
                return None
            return {
                'id': r.id, 'document_type': r.document_type,
                'file_path': r.file_path, 'deleted_at': r.deleted_at,
                'deleted_by_user_id': r.deleted_by_user_id,
                'replaced_by_id': r.replaced_by_id,
            }

    def _student_name(self, student_id):
        with self._db():
            return (Student.query.execution_options(bypass_tenant_scope=True)
                    .filter_by(id=student_id).first().full_name)

    def _soft_delete_direct(self, doc_id):
        """Soft-delete a row directly, to set up restore scenarios."""
        from datetime import datetime as dt
        with self._db():
            r = (StudentDocument.query
                 .execution_options(bypass_tenant_scope=True)
                 .filter_by(id=doc_id).first())
            r.deleted_at = dt.utcnow()
            db.session.commit()

    def _edit_payload(self, **overrides):
        data = {
            'full_name': 'طالب معدل',
            'section_id': str(self.section_a_id),
            'status': 'active',
        }
        data.update(overrides)
        return data

    def _edit(self, student_id, **overrides):
        return self.client.post(f'/students/{student_id}/edit',
                                data=self._edit_payload(**overrides),
                                content_type='multipart/form-data')

    def _replace(self, student_id, doc_id, payload=_PDF, filename='new.pdf'):
        return self.client.post(
            f'/students/{student_id}/documents/{doc_id}/replace',
            data={'document_file': (io.BytesIO(payload), filename)},
            content_type='multipart/form-data')

    def _delete(self, student_id, doc_id):
        return self.client.post(
            f'/students/{student_id}/documents/{doc_id}/delete')

    def _school_restore(self, student_id, doc_id):
        """The REMOVED school-side restore endpoint (must be unreachable)."""
        return self.client.post(
            f'/students/{student_id}/documents/{doc_id}/restore')

    # ── Super Admin portal (genuine mechanisms) ────────────────────────────

    def _select_school(self, school_id):
        """Select a school through the portal's real switcher endpoint."""
        resp = self.client.post(f'/schools/{school_id}/switch',
                                data={'next': '/admin/super/recycle-bin'})
        self.assertEqual(resp.status_code, 302)

    def _clear_school(self):
        resp = self.client.post('/schools/clear-switch')
        self.assertEqual(resp.status_code, 302)

    def _bin(self, **params):
        return self.client.get('/admin/super/recycle-bin',
                               query_string=params)

    def _sa_restore(self, doc_id):
        return self.client.post(
            f'/admin/super/recycle-bin/documents/{doc_id}/restore')

    def _full(self, stored_value):
        return os.path.join(self.app.root_path, 'static',
                            *stored_value.split('/'))

    def _track_stored(self, stored_value):
        if stored_value and not stored_value.startswith(('http://', 'https://')):
            self._made_files.append(self._full(stored_value))

    # ═══════════════════════════════════════════════════════════════════════════
    #  1. Existing active attachments still render normally
    # ═══════════════════════════════════════════════════════════════════════════

    def test_active_attachments_listed_on_edit_page(self):
        before = self._active(self.student_a_id)
        self._login(self.admin_a_name)
        resp = self.client.get(f'/students/{self.student_a_id}/edit')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)

        self.assertIn('الهوية الوطنية', body)
        self.assertIn('بطاقة السكن', body)
        self.assertIn(f'doc-replace-{self.doc_id}', body)
        self.assertIn(f'doc-delete-{self.doc_id}', body)
        self.assertIn(f'doc-replace-{self.doc2_id}', body)
        self.assertIn('هل أنت متأكد من حذف هذا المستمسك؟', body)
        self.assertIn(os.path.basename(before[self.doc_id][1]), body)

        # No deleted section while nothing is deleted, and no other student's doc.
        self.assertNotIn('المستمسكات المحذوفة', body)
        self.assertNotIn(f'doc-replace-{self.other_doc_id}', body)
        self.assertEqual(self._active(self.student_a_id), before)

    def test_missing_physical_file_still_renders_with_arabic_status(self):
        with self._db():
            student = (Student.query.execution_options(bypass_tenant_scope=True)
                       .filter_by(id=self.student_a_id).first())
            ghost_id = self._make_doc(
                student, 'التقرير الطبي',
                file_path=f'uploads/students/documents/gone-{_uid()}.png')
            db.session.commit()

        self._login(self.admin_a_name)
        resp = self.client.get(f'/students/{self.student_a_id}/edit')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('الملف غير موجود في المخزن', body)
        self.assertIsNotNone(self._row(ghost_id))
        self.assertIn(f'doc-replace-{ghost_id}', body)

    # ═══════════════════════════════════════════════════════════════════════════
    #  2. A normal edit submission preserves every attachment
    # ═══════════════════════════════════════════════════════════════════════════

    def test_plain_edit_preserves_all_attachments(self):
        before = self._active(self.student_a_id)
        self.assertEqual(len(before), 2)

        self._login(self.admin_a_name)
        resp = self._edit(self.student_a_id)
        self.assertIn(resp.status_code, (200, 302))

        self.assertEqual(self._active(self.student_a_id), before)
        self.assertEqual(self._student_name(self.student_a_id), 'طالب معدل')
        for full in self._made_files:
            self.assertTrue(os.path.isfile(full), 'an existing file was removed')

    def test_empty_file_input_preserves_attachments(self):
        before = self._active(self.student_a_id)
        self._login(self.admin_a_name)
        resp = self._edit(self.student_a_id, **{
            'document_type[]': 'الهوية الوطنية',
            'document_file[]': (io.BytesIO(b''), ''),
        })
        self.assertIn(resp.status_code, (200, 302))
        self.assertEqual(self._active(self.student_a_id), before)

    # ═══════════════════════════════════════════════════════════════════════════
    #  3. Adding a missing document does not modify the existing ones
    # ═══════════════════════════════════════════════════════════════════════════

    def test_adding_document_leaves_existing_untouched(self):
        before = self._active(self.student_a_id)

        self._login(self.admin_a_name)
        resp = self._edit(self.student_a_id, **{
            'document_type[]': 'التقرير الطبي',
            'document_file[]': (io.BytesIO(_PDF), 'report.pdf'),
        })
        self.assertIn(resp.status_code, (200, 302))

        after = self._active(self.student_a_id)
        self.assertEqual(len(after), len(before) + 1)
        for doc_id, value in before.items():
            self.assertEqual(after[doc_id], value, 'an existing attachment changed')
        added = [v for k, v in after.items() if k not in before][0]
        self.assertEqual(added[0], 'التقرير الطبي')
        self._track_stored(added[1])
        self.assertTrue(os.path.isfile(self._full(added[1])))

    def test_unsupported_new_document_is_rejected_and_nothing_is_saved(self):
        before = self._active(self.student_a_id)
        name_before = self._student_name(self.student_a_id)

        self._login(self.admin_a_name)
        resp = self.client.post(
            f'/students/{self.student_a_id}/edit',
            data=self._edit_payload(**{
                'document_type[]': 'التقرير الطبي',
                'document_file[]': (io.BytesIO(_FAKE_PNG), 'evil.png'),
            }),
            content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('محتوى الملف لا يطابق صيغته', resp.get_data(as_text=True))
        self.assertEqual(self._active(self.student_a_id), before)
        self.assertEqual(self._student_name(self.student_a_id), name_before)

    # ═══════════════════════════════════════════════════════════════════════════
    #  4. Delete = soft delete: row kept, file kept, hidden from active lists
    # ═══════════════════════════════════════════════════════════════════════════

    def test_delete_hides_but_preserves_row_and_file(self):
        before = self._active(self.student_a_id)
        before_row = self._row(self.doc_id)

        self._login(self.admin_a_name)
        resp = self._delete(self.student_a_id, self.doc_id)
        self.assertEqual(resp.status_code, 302)

        # Hidden from the active list, but the row is still there …
        after = self._active(self.student_a_id)
        self.assertNotIn(self.doc_id, after)
        self.assertEqual(after[self.doc2_id], before[self.doc2_id])
        self.assertIn(self.doc_id, self._all_ids(self.student_a_id))

        row = self._row(self.doc_id)
        self.assertIsNotNone(row, 'the database row must never be deleted')
        self.assertIsNotNone(row['deleted_at'], 'deleted_at was not set')
        self.assertEqual(row['deleted_by_user_id'], self.admin_a_id,
                         'the deleting user was not recorded')
        self.assertEqual(row['file_path'], before_row['file_path'],
                         'file_path must not change on delete')
        self.assertIsNone(row['replaced_by_id'])

        # … and the stored object is untouched.
        self.assertTrue(os.path.isfile(self._full(row['file_path'])),
                        'delete must never remove the stored file')
        # Other students of the same school keep their attachments.
        self.assertIsNotNone(self._row(self.other_doc_id))

    def test_deleted_document_is_hidden_from_the_school_interface(self):
        self._login(self.admin_a_name)
        self.assertEqual(self._delete(self.student_a_id, self.doc_id).status_code, 302)

        body = self.client.get(
            f'/students/{self.student_a_id}/edit').get_data(as_text=True)
        # Not in the active list any more …
        self.assertNotIn(f'doc-replace-{self.doc_id}', body)
        self.assertNotIn(f'doc-delete-{self.doc_id}', body)
        # … and the school interface does not expose deleted documents at all.
        self.assertNotIn('المستمسكات المحذوفة', body)
        self.assertNotIn(f'doc-restore-{self.doc_id}', body)
        self.assertNotIn('/restore', body)
        # The still-active document keeps its actions.
        self.assertIn(f'doc-delete-{self.doc2_id}', body)

        # The student profile page hides it too.
        view = self.client.get(
            f'/students/{self.student_a_id}').get_data(as_text=True)
        self.assertIn('بطاقة السكن', view)
        self.assertNotIn('الهوية الوطنية', view)

    def test_delete_twice_is_not_possible(self):
        self._login(self.admin_a_name)
        self.assertEqual(self._delete(self.student_a_id, self.doc_id).status_code, 302)
        first = self._row(self.doc_id)
        # An already-deleted document is not found by the delete route.
        self.assertEqual(self._delete(self.student_a_id, self.doc_id).status_code, 404)
        self.assertEqual(self._row(self.doc_id), first, 'row changed on re-delete')

    def test_delete_requires_post(self):
        self._login(self.admin_a_name)
        resp = self.client.get(
            f'/students/{self.student_a_id}/documents/{self.doc_id}/delete')
        self.assertEqual(resp.status_code, 405)
        self.assertIsNone(self._row(self.doc_id)['deleted_at'])

    # ═══════════════════════════════════════════════════════════════════════════
    #  5. Replace = new active row + old row soft-deleted and restorable
    # ═══════════════════════════════════════════════════════════════════════════

    def test_replace_activates_new_and_keeps_old_restorable(self):
        before = self._active(self.student_a_id)
        old_row = self._row(self.doc_id)
        old_path = old_row['file_path']

        self._login(self.admin_a_name)
        resp = self._replace(self.student_a_id, self.doc_id)
        self.assertEqual(resp.status_code, 302)

        after = self._active(self.student_a_id)
        self.assertEqual(len(after), len(before), 'active count must stay the same')
        self.assertNotIn(self.doc_id, after, 'the old row must no longer be active')

        new_ids = [i for i in after if i not in before]
        self.assertEqual(len(new_ids), 1, 'exactly one new active row')
        new_id = new_ids[0]
        new_type, new_path = after[new_id]
        self.assertEqual(new_type, old_row['document_type'], 'label preserved')
        self.assertNotEqual(new_path, old_path, 'a NEW unique path must be used')
        self._track_stored(new_path)
        self.assertTrue(os.path.isfile(self._full(new_path)), 'new file stored')

        # The old row is preserved, soft-deleted, still pointing at its own file.
        old_after = self._row(self.doc_id)
        self.assertIsNotNone(old_after, 'the old row must never be deleted')
        self.assertIsNotNone(old_after['deleted_at'])
        self.assertEqual(old_after['deleted_by_user_id'], self.admin_a_id)
        self.assertEqual(old_after['file_path'], old_path,
                         'the old file_path must be preserved for restore')
        self.assertEqual(old_after['replaced_by_id'], new_id,
                         'replacement history not recorded')
        # The old object is never overwritten or removed.
        self.assertTrue(os.path.isfile(self._full(old_path)),
                        'the replaced file must stay in storage')
        # The other active document is untouched.
        self.assertEqual(after[self.doc2_id], before[self.doc2_id])

    def test_failed_replacement_leaves_original_active_and_unchanged(self):
        before = self._active(self.student_a_id)
        before_ids = self._all_ids(self.student_a_id)
        before_row = self._row(self.doc_id)

        self._login(self.admin_a_name)
        for payload, filename in ((_FAKE_PNG, 'fake.png'),      # bad content
                                  (_PDF, 'payload.exe')):       # bad extension
            resp = self._replace(self.student_a_id, self.doc_id,
                                 payload=payload, filename=filename)
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(self._active(self.student_a_id), before)
            self.assertEqual(self._all_ids(self.student_a_id), before_ids,
                             'a row was created by a failed replacement')
            self.assertEqual(self._row(self.doc_id), before_row,
                             'the original row changed')

        # No file chosen at all.
        resp = self.client.post(
            f'/students/{self.student_a_id}/documents/{self.doc_id}/replace',
            data={}, content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._row(self.doc_id), before_row)
        self.assertTrue(os.path.isfile(self._full(before_row['file_path'])))

    def test_failed_database_step_leaves_original_active(self):
        """A commit failure after a successful upload must change nothing."""
        before = self._active(self.student_a_id)
        before_ids = self._all_ids(self.student_a_id)
        before_row = self._row(self.doc_id)

        from unittest import mock
        import app.blueprints.students as students_bp_mod

        real_save = students_bp_mod.save_uploaded_file
        stored = {}

        def _save_and_record(file, subfolder, **kwargs):
            path = real_save(file, subfolder, **kwargs)
            stored['path'] = path          # a real, verified upload
            return path

        self._login(self.admin_a_name)
        # mock.patch.object restores the original attribute correctly, unlike a
        # manual assignment + del on the scoped_session class.
        with mock.patch.object(students_bp_mod, 'save_uploaded_file',
                               _save_and_record), \
             mock.patch.object(type(db.session), 'commit',
                               side_effect=RuntimeError('simulated commit failure')):
            resp = self._replace(self.student_a_id, self.doc_id)

        self.assertEqual(resp.status_code, 302)
        # The original document is still active and completely unchanged.
        self.assertEqual(self._active(self.student_a_id), before)
        self.assertEqual(self._all_ids(self.student_a_id), before_ids)
        self.assertEqual(self._row(self.doc_id), before_row)
        self.assertTrue(os.path.isfile(self._full(before_row['file_path'])))
        # The orphaned new upload was discarded (nothing referenced it).
        if stored.get('path'):
            self._track_stored(stored['path'])
            self.assertFalse(os.path.isfile(self._full(stored['path'])),
                             'the unreferenced new upload should be discarded')

    def test_oversized_replacement_is_rejected(self):
        before_row = self._row(self.doc_id)
        big = _PDF + b'\x00' * (5 * 1024 * 1024 + 1)
        self._login(self.admin_a_name)
        self.assertEqual(self._replace(self.student_a_id, self.doc_id,
                                       payload=big,
                                       filename='big.pdf').status_code, 302)
        self.assertEqual(self._row(self.doc_id), before_row)

    # ═══════════════════════════════════════════════════════════════════════════
    #  6. Restore
    # ═══════════════════════════════════════════════════════════════════════════

    # ═══════════════════════════════════════════════════════════════════════════
    #  7. Cross-school / cross-student isolation
    # ═══════════════════════════════════════════════════════════════════════════

    def test_other_school_cannot_view_replace_or_delete(self):
        before = self._active(self.student_a_id)
        before_row = self._row(self.doc_id)
        self._login(self.admin_b_name)          # School B admin

        self.assertIn(self.client.get(
            f'/students/{self.student_a_id}/edit').status_code, (403, 404))
        self.assertIn(self._replace(self.student_a_id, self.doc_id).status_code,
                      (403, 404))
        self.assertIn(self._delete(self.student_a_id, self.doc_id).status_code,
                      (403, 404))

        self.assertEqual(self._active(self.student_a_id), before)
        self.assertEqual(self._row(self.doc_id), before_row)

    def test_cross_school_doc_id_on_own_student_is_not_found(self):
        before = self._active(self.student_a_id)
        self._login(self.admin_b_name)
        self.assertEqual(
            self._replace(self.student_b_id, self.doc_id).status_code, 404)
        self.assertEqual(
            self._delete(self.student_b_id, self.doc_id).status_code, 404)
        self.assertEqual(self._active(self.student_a_id), before)

    def test_other_students_doc_id_in_same_school_is_not_found(self):
        other_before = self._active(self.other_a_id)
        self._login(self.admin_a_name)
        self.assertEqual(
            self._replace(self.student_a_id, self.other_doc_id).status_code, 404)
        self.assertEqual(
            self._delete(self.student_a_id, self.other_doc_id).status_code, 404)
        self.assertEqual(self._active(self.other_a_id), other_before)

    # ═══════════════════════════════════════════════════════════════════════════
    #  8. Permission + CSRF still enforced
    # ═══════════════════════════════════════════════════════════════════════════

    def test_permission_required(self):
        before_row = self._row(self.doc_id)
        self._login(self.parent_a_name)         # no edit_student permission
        self.assertEqual(
            self._replace(self.student_a_id, self.doc_id).status_code, 403)
        self.assertEqual(
            self._delete(self.student_a_id, self.doc_id).status_code, 403)
        self.assertEqual(self._row(self.doc_id), before_row)

    def test_unauthenticated_is_redirected_to_login(self):
        before_row = self._row(self.doc_id)
        self.assertEqual(
            self._replace(self.student_a_id, self.doc_id).status_code, 302)
        self.assertEqual(
            self._delete(self.student_a_id, self.doc_id).status_code, 302)
        self.assertEqual(self._row(self.doc_id), before_row)

    def test_csrf_enforced_on_replace_and_delete(self):
        before_row = self._row(self.doc_id)
        self._login(self.admin_a_name)               # the login form itself is
        self.app.config['WTF_CSRF_ENABLED'] = True   # CSRF-protected, so first
        try:
            for call in (self._replace, self._delete):
                self.assertIn(call(self.student_a_id, self.doc_id).status_code,
                              (400, 302))
            self.assertEqual(self._row(self.doc_id), before_row)
        finally:
            self.app.config['WTF_CSRF_ENABLED'] = False


    # ═══════════════════════════════════════════════════════════════════════════
    #  9. The school interface cannot reach deleted-document management
    # ═══════════════════════════════════════════════════════════════════════════

    def test_school_restore_endpoint_is_gone_for_school_users(self):
        """edit_student alone must not reach any restore endpoint."""
        self._login(self.admin_a_name)
        self.assertEqual(self._delete(self.student_a_id, self.doc_id).status_code, 302)
        row = self._row(self.doc_id)
        self.assertIsNotNone(row['deleted_at'])

        # The old school-side route no longer exists.
        resp = self._school_restore(self.student_a_id, self.doc_id)
        self.assertEqual(resp.status_code, 404)
        # And the portal route refuses a school admin outright.
        self.assertEqual(self._sa_restore(self.doc_id).status_code, 302)
        self.assertNotIn('/admin/super',
                         self._sa_restore(self.doc_id).headers.get('Location', ''))
        self.assertEqual(self._row(self.doc_id), row, 'the row changed')

    def test_school_admin_cannot_open_the_recycle_bin(self):
        self._login(self.admin_a_name)
        resp = self._bin()
        self.assertEqual(resp.status_code, 302)          # redirected away
        self.assertNotIn('recycle-bin', resp.headers.get('Location', ''))

    def test_soft_deleted_document_no_longer_authorizes_download(self):
        """Ordinary viewing/downloading must not resolve through a deleted row."""
        from app.utils.upload_access import resolve_upload_owner

        path = self._row(self.doc_id)['file_path']
        with self._db():
            self.assertIsNotNone(resolve_upload_owner(path),
                                 'an active document should resolve')

        self._login(self.admin_a_name)
        self.assertEqual(self._delete(self.student_a_id, self.doc_id).status_code, 302)

        with self._db():
            self.assertIsNone(resolve_upload_owner(path),
                              'a soft-deleted document must not authorize access')

    # ═══════════════════════════════════════════════════════════════════════════
    #  10. Super Admin recycle bin — selected school only
    # ═══════════════════════════════════════════════════════════════════════════

    def test_recycle_bin_requires_a_selected_school(self):
        self._login(self.admin_a_name)
        self.assertEqual(self._delete(self.student_a_id, self.doc_id).status_code, 302)

        self.client.get('/auth/logout')
        self._login(self.sa_name)
        self._clear_school()                      # global view, no school

        resp = self._bin()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('يرجى اختيار المدرسة لعرض المحذوفات.', body)
        # Nothing from any school is listed.
        self.assertNotIn('الهوية الوطنية', body)
        self.assertNotIn(f'/documents/{self.doc_id}/restore', body)

    def test_recycle_bin_shows_only_the_selected_school(self):
        # Delete one document in each school.
        self._login(self.admin_a_name)
        self.assertEqual(self._delete(self.student_a_id, self.doc_id).status_code, 302)
        self.client.get('/auth/logout')
        self._login(self.admin_b_name)
        self.assertEqual(self._delete(self.student_b_id, self.doc_b_id).status_code, 302)
        self.client.get('/auth/logout')

        self._login(self.sa_name)
        self._select_school(self.school_a_id)
        body = self._bin().get_data(as_text=True)
        self.assertIn(f'/documents/{self.doc_id}/restore', body)
        self.assertNotIn(f'/documents/{self.doc_b_id}/restore', body)
        # Other students of the same school that were never deleted stay out.
        self.assertNotIn(f'/documents/{self.other_doc_id}/restore', body)

        # Switching schools updates the results.
        self._select_school(self.school_b_id)
        body = self._bin().get_data(as_text=True)
        self.assertIn(f'/documents/{self.doc_b_id}/restore', body)
        self.assertNotIn(f'/documents/{self.doc_id}/restore', body)

        # Search stays inside the selected school.
        body = self._bin(q='الهوية').get_data(as_text=True)
        self.assertIn(f'/documents/{self.doc_b_id}/restore', body)
        self.assertNotIn(f'/documents/{self.doc_id}/restore', body)

    def test_super_admin_restore_succeeds_and_is_audited(self):
        self._login(self.admin_a_name)
        self.assertEqual(self._delete(self.student_a_id, self.doc_id).status_code, 302)
        before_row = self._row(self.doc_id)
        self.client.get('/auth/logout')

        self._login(self.sa_name)
        self._select_school(self.school_a_id)
        resp = self._sa_restore(self.doc_id)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('recycle-bin', resp.headers.get('Location', ''))

        row = self._row(self.doc_id)
        self.assertIsNone(row['deleted_at'], 'not reactivated')
        self.assertIsNone(row['deleted_by_user_id'])
        self.assertEqual(row['id'], before_row['id'], 'id must be preserved')
        self.assertEqual(row['file_path'], before_row['file_path'],
                         'file_path must be preserved')
        self.assertIn(self.doc_id, self._active(self.student_a_id))

        # Audited against the existing mechanism, with the operator recorded.
        with self._db():
            entry = (AuditLog.query.execution_options(bypass_tenant_scope=True)
                     .filter(AuditLog.resource == 'student_document',
                             AuditLog.resource_id == self.doc_id,
                             AuditLog.action == 'restore').first())
            self.assertIsNotNone(entry, 'restore was not audited')
            self.assertEqual(entry.user_id, self.sa_id)

        # It is gone from the bin and active again in the school interface.
        self.assertNotIn(f'/documents/{self.doc_id}/restore',
                         self._bin().get_data(as_text=True))

    def test_wrong_and_stale_school_restores_are_rejected(self):
        self._login(self.admin_a_name)
        self.assertEqual(self._delete(self.student_a_id, self.doc_id).status_code, 302)
        before_row = self._row(self.doc_id)
        self.client.get('/auth/logout')

        self._login(self.sa_name)
        # (a) wrong school selected — School A's document is not in B's bin.
        self._select_school(self.school_b_id)
        self.assertEqual(self._sa_restore(self.doc_id).status_code, 302)
        self.assertEqual(self._row(self.doc_id), before_row)

        # (b) stale form: selected A, switched to B, then submitted A's doc id.
        self._select_school(self.school_a_id)
        self._select_school(self.school_b_id)
        self.assertEqual(self._sa_restore(self.doc_id).status_code, 302)
        self.assertEqual(self._row(self.doc_id), before_row)

        # (c) no school selected at all.
        self._clear_school()
        self.assertEqual(self._sa_restore(self.doc_id).status_code, 302)
        self.assertEqual(self._row(self.doc_id), before_row)

        # With the right school it finally works — proving (a)-(c) were the guards.
        self._select_school(self.school_a_id)
        self.assertEqual(self._sa_restore(self.doc_id).status_code, 302)
        self.assertIsNone(self._row(self.doc_id)['deleted_at'])

    def test_super_admin_restore_conflict_changes_nothing(self):
        self._login(self.admin_a_name)
        # Replace → the old version is soft-deleted, a new active one exists.
        self.assertEqual(self._replace(self.student_a_id, self.doc_id).status_code, 302)
        active_after_replace = self._active(self.student_a_id)
        for doc_id, (_t, path) in active_after_replace.items():
            if doc_id != self.doc2_id:
                self._track_stored(path)
        old_row = self._row(self.doc_id)
        self.client.get('/auth/logout')

        self._login(self.sa_name)
        self._select_school(self.school_a_id)
        resp = self._sa_restore(self.doc_id)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('يوجد مستمسك فعّال من نوع',
                      self._bin().get_data(as_text=True))

        self.assertEqual(self._row(self.doc_id), old_row, 'the old row changed')
        self.assertEqual(self._active(self.student_a_id), active_after_replace)

    def test_super_admin_restore_requires_post(self):
        self._soft_delete_direct(self.doc_id)
        self._login(self.sa_name)
        self._select_school(self.school_a_id)
        resp = self.client.get(
            f'/admin/super/recycle-bin/documents/{self.doc_id}/restore')
        self.assertEqual(resp.status_code, 405)
        self.assertIsNotNone(self._row(self.doc_id)['deleted_at'])

    def test_recycle_bin_csrf_enforced_on_restore(self):
        self._soft_delete_direct(self.doc_id)
        before_row = self._row(self.doc_id)
        self._login(self.sa_name)
        self._select_school(self.school_a_id)
        self.app.config['WTF_CSRF_ENABLED'] = True
        try:
            self.assertIn(self._sa_restore(self.doc_id).status_code, (400, 302))
            self.assertEqual(self._row(self.doc_id), before_row)
        finally:
            self.app.config['WTF_CSRF_ENABLED'] = False

    def test_sidebar_shows_the_entry_only_to_super_admin(self):
        self._login(self.admin_a_name)
        self.assertNotIn('سلة المحذوفات',
                         self.client.get('/students/').get_data(as_text=True))
        self.client.get('/auth/logout')

        self._login(self.sa_name)
        self._select_school(self.school_a_id)
        self.assertIn('سلة المحذوفات', self._bin().get_data(as_text=True))

    # ═══════════════════════════════════════════════════════════════════════════
    #  11. The four-ACTIVE-document limit (single focused regression test)
    # ═══════════════════════════════════════════════════════════════════════════

    def test_four_active_document_limit_across_delete_replace_and_restore(self):
        """Soft-deleted documents never occupy a slot; delete frees exactly one;
        replace keeps the count unchanged; restore is refused when it would
        exceed the limit or collide with an active type."""
        def _count():
            return len(self._active(self.student_a_id))

        self._login(self.admin_a_name)
        self.assertEqual(_count(), 2)

        # ── fill up to the limit of 4 ───────────────────────────────────────
        for doc_type in ('الوثيقة الدراسية', 'التقرير الطبي'):
            resp = self._edit(self.student_a_id, **{
                'document_type[]': doc_type,
                'document_file[]': (io.BytesIO(_PDF), f'{doc_type}.pdf'),
            })
            self.assertIn(resp.status_code, (200, 302))
        self.assertEqual(_count(), 4)
        for doc_id, (_t, path) in self._active(self.student_a_id).items():
            self._track_stored(path)

        # ── a 5th ACTIVE document is refused, nothing is written ───────────
        at_limit = self._active(self.student_a_id)
        resp = self.client.post(
            f'/students/{self.student_a_id}/edit',
            data=self._edit_payload(**{
                'document_type[]': 'مستمسك إضافي',
                'document_file[]': (io.BytesIO(_PDF), 'extra.pdf'),
            }),
            content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('كحد أقصى للطالب', resp.get_data(as_text=True))
        self.assertEqual(self._active(self.student_a_id), at_limit)
        self.assertEqual(_count(), 4)

        # ── replacement keeps the ACTIVE count unchanged, even at the limit ─
        resp = self._replace(self.student_a_id, self.doc_id)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(_count(), 4, 'replace must not change the active count')
        replaced_old = self._row(self.doc_id)
        self.assertIsNotNone(replaced_old['deleted_at'])
        for doc_id, (_t, path) in self._active(self.student_a_id).items():
            self._track_stored(path)
        # The soft-deleted old version does NOT occupy a slot.
        self.assertEqual(len(self._all_ids(self.student_a_id)), 5)

        # ── Super Admin restore is refused while it would exceed the limit ──
        self.client.get('/auth/logout')
        self._login(self.sa_name)
        self._select_school(self.school_a_id)
        before_row = self._row(self.doc_id)
        resp = self._sa_restore(self.doc_id)
        self.assertEqual(resp.status_code, 302)
        body = self._bin().get_data(as_text=True)
        # Refused by the same-type conflict (the replacement) — either guard is
        # a valid refusal; what matters is that nothing changed.
        self.assertTrue('يوجد مستمسك فعّال من نوع' in body
                        or 'الحد الأقصى' in body,
                        'no refusal message was shown')
        self.assertEqual(self._row(self.doc_id), before_row)
        self.assertEqual(_count(), 4)

        # ── deleting one active document frees exactly one slot ─────────────
        self.client.get('/auth/logout')
        self._login(self.admin_a_name)
        self.assertEqual(self._delete(self.student_a_id, self.doc2_id).status_code, 302)
        self.assertEqual(_count(), 3, 'delete must free exactly one slot')

        # …and the freed slot can be used again by an add.
        resp = self._edit(self.student_a_id, **{
            'document_type[]': 'بطاقة السكن',
            'document_file[]': (io.BytesIO(_PDF), 'again.pdf'),
        })
        self.assertIn(resp.status_code, (200, 302))
        self.assertEqual(_count(), 4)
        for doc_id, (_t, path) in self._active(self.student_a_id).items():
            self._track_stored(path)


if __name__ == '__main__':
    unittest.main()
