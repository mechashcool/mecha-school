"""
Legacy display-photo backfill tool (app/services/display_photo_backfill.py).

Isolated test database + fake Storage only: the Supabase helpers are patched,
and storage.objects metadata is an in-memory stand-in. Nothing here can reach
a real bucket.
"""
import io
import json
import os
import pathlib
import re
import tempfile
import unittest
from datetime import date
from unittest import mock
from uuid import uuid4

from PIL import Image
from sqlalchemy import event, inspect as sa_inspect, text

from app import create_app
from app.models import db, AcademicYear, Employee, School, Student
from app.services import display_photo_backfill as bf
from app.utils import employee_display_photo, student_display_photo

OPTS = {'bypass_tenant_scope': True, 'include_all_years': True}
HOST = 'proj.supabase.co'
BASE = f'https://{HOST}/storage/v1/object/public/'
ROOT = pathlib.Path(__file__).resolve().parents[1]


def _jpeg(w=1600, h=1200, color=(200, 120, 60)):
    buf = io.BytesIO()
    Image.new('RGB', (w, h), color).save(buf, format='JPEG', quality=85)
    return buf.getvalue()


def _png(w=500, h=700):
    buf = io.BytesIO()
    Image.new('RGBA', (w, h), (10, 20, 200, 255)).save(buf, format='PNG')
    return buf.getvalue()


class FakeMetadata:
    """In-memory stand-in for storage.objects (bucket, key) -> size."""

    def __init__(self):
        self.objects = {}

    def available(self):
        return True

    def sizes(self, pairs):
        return {p: self.objects[p] for p in pairs if p in self.objects}


class BackfillTestBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config.update(SUPABASE_URL=f'https://{HOST}', SUPABASE_SERVICE_KEY='test-key',
                              SUPABASE_BUCKET='uploads',
                              SUPABASE_STORAGE_BUCKET_MEDIA='school-media')

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.tmp = tempfile.mkdtemp(prefix='bfmanifest-')
        self.meta = FakeMetadata()
        self.blobs = {}                                   # (bucket, key) -> bytes
        self.fetch = mock.patch('app.utils.helpers._supabase_fetch',
                                side_effect=self._fake_fetch).start()
        self.upload = mock.patch('app.utils.helpers._supabase_upload',
                                 side_effect=self._fake_upload).start()
        self.delete = mock.patch('app.utils.helpers._supabase_delete',
                                 side_effect=self._fake_delete).start()
        self.save_uploaded = mock.patch('app.utils.helpers.save_uploaded_file').start()
        self.addCleanup(mock.patch.stopall)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self._teardown_db)
        self.school_a = self._school('A')
        self.school_b = self._school('B')
        db.session.commit()

    # ── fake storage ──────────────────────────────────────────────────────────

    def _fake_fetch(self, key, bucket=None):
        data = self.blobs.get((bucket, key))
        return (data, 'image/jpeg') if data else (None, None)

    def _fake_upload(self, data, key, content_type, bucket=None):
        self.meta.objects[(bucket, key)] = len(data)
        self.blobs[(bucket, key)] = data
        return f'{BASE}{bucket}/{key}'

    def _fake_delete(self, key, bucket=None):
        self.meta.objects.pop((bucket, key), None)
        self.blobs.pop((bucket, key), None)
        return True

    # ── fixtures ──────────────────────────────────────────────────────────────

    def _school(self, tag):
        school = School(school_name=f'BF {tag} {self.sfx}', code=f'BF{tag}{self.sfx}'[:20],
                        capacity=0, is_active=True)
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'Y{tag}{self.sfx}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 6, 30))
        db.session.add(year)
        db.session.flush()
        school._year_id = year.id
        return school

    def _original(self, bucket, folder, raw, ext='jpg'):
        key = f'{folder}/{uuid4().hex}.{ext}'
        self.blobs[(bucket, key)] = raw
        self.meta.objects[(bucket, key)] = len(raw)
        return f'{BASE}{bucket}/{key}', key

    def _student(self, school, photo=None, display=None):
        s = Student(student_id=f'S{uuid4().hex[:10]}', full_name=f'Secret Student {uuid4().hex[:4]}',
                    school_id=school.id, academic_year_id=school._year_id, status='active',
                    photo=photo, photo_display=display)
        db.session.add(s)
        db.session.commit()
        return s.id

    def _employee(self, school, photo=None, display=None):
        e = Employee(school_id=school.id, employee_id=f'E{uuid4().hex[:10]}',
                     full_name=f'Secret Employee {uuid4().hex[:4]}', base_salary=0,
                     status='active', photo=photo, photo_display=display)
        db.session.add(e)
        db.session.commit()
        return e.id

    def _teardown_db(self):
        db.session.rollback()
        for school in (self.school_a, self.school_b):
            for model in (Student, Employee, AcademicYear):
                model.query.execution_options(**OPTS).filter_by(
                    school_id=school.id).delete(synchronize_session=False)
            School.query.filter_by(id=school.id).delete(synchronize_session=False)
        db.session.commit()
        db.session.remove()
        self.ctx.pop()

    def _row(self, model, rid):
        db.session.rollback()
        obj = db.session.get(model, rid, execution_options=OPTS, populate_existing=True)
        return {c.key: getattr(obj, c.key) for c in sa_inspect(model).mapper.column_attrs}

    def _run(self, **kw):
        kw.setdefault('entities', ('students', 'employees'))
        kw.setdefault('school_id', self.school_a.id)
        kw.setdefault('manifest_dir', self.tmp)
        kw.setdefault('metadata', self.meta)
        kw.setdefault('run_id', uuid4().hex[:12])
        if kw.get('apply'):
            kw.setdefault('confirm_storage_host', HOST)
        runner = bf.BackfillRunner(**kw)
        summary = runner.run()
        with open(summary['manifest'], encoding='utf-8') as fh:
            records = [json.loads(line) for line in fh]
        return runner, summary, records

    def _capture_sql(self):
        statements = []

        def _hook(conn, cursor, statement, params, context, executemany):
            statements.append(statement)
        event.listen(db.engine, 'before_cursor_execute', _hook)
        self.addCleanup(event.remove, db.engine, 'before_cursor_execute', _hook)
        return statements


# ─────────────────────────────────────────────────────────────────────────────
#  Dry run (default)
# ─────────────────────────────────────────────────────────────────────────────

class DryRunTest(BackfillTestBase):

    def test_01_02_03_dry_run_no_db_writes_and_no_storage_io(self):
        url, _ = self._original('uploads', 'students', _jpeg())
        sid = self._student(self.school_a, photo=url)
        eurl, _ = self._original('uploads', 'employees', _jpeg())
        eid = self._employee(self.school_a, photo=eurl)
        before = (self._row(Student, sid), self._row(Employee, eid))
        sql = self._capture_sql()

        runner, summary, records = self._run()                       # apply defaults False

        writes = [s for s in sql if re.match(r'\s*(INSERT|UPDATE|DELETE)\b', s, re.I)]
        self.assertEqual(writes, [])
        self.assertTrue(any('SET TRANSACTION READ ONLY' in s for s in sql))
        self.assertEqual((self._row(Student, sid), self._row(Employee, eid)), before)
        for m in (self.fetch, self.upload, self.delete, self.save_uploaded):
            m.assert_not_called()
        self.assertIsInstance(runner.storage, bf.NoStorage)
        self.assertEqual((summary['db_writes'], summary['storage_fetches'],
                          summary['storage_uploads'], summary['storage_deletes']), (0, 0, 0, 0))
        self.assertEqual(sorted(r['outcome'] for r in records), ['would_process'] * 2)
        ent = summary['entities']['students']
        self.assertEqual(ent['estimated_supabase_read_bytes'], len(_jpeg()))
        self.assertEqual(ent['expected_new_objects'], 1)

    def test_dry_run_read_only_transaction_is_enforced_by_postgres(self):
        runner = bf.BackfillRunner(entities=('students',), school_id=self.school_a.id,
                                   manifest_dir=self.tmp, metadata=self.meta, run_id='ro1')
        runner._enforce_read_only()
        try:
            with self.assertRaises(Exception) as cm:
                db.session.execute(text('update students set photo_display = null where id = -1'))
            self.assertIn('read-only', str(cm.exception).lower())
        finally:
            runner._release_read_only()

    def test_dry_run_reports_missing_metadata_and_already_has_display(self):
        url, key = self._original('uploads', 'students', _jpeg())
        self.meta.objects.pop(('uploads', key))
        self._student(self.school_a, photo=url)
        done, _ = self._original('uploads', 'students', _jpeg())
        self._student(self.school_a, photo=done, display=BASE + 'uploads/students/display/x.webp')
        _, summary, records = self._run(entities=('students',))
        self.assertEqual([r['outcome'] for r in records], ['original_missing'])
        self.assertEqual(summary['entities']['students']['already_has_display'], 1)

    def test_dry_run_on_db_without_storage_schema_reports_metadata_unavailable(self):
        url, _ = self._original('uploads', 'students', _jpeg())
        self._student(self.school_a, photo=url)
        runner = bf.BackfillRunner(entities=('students',), school_id=self.school_a.id,
                                   manifest_dir=self.tmp, run_id='nometa')
        summary = runner.run()                       # real StorageObjectsMetadata
        self.assertFalse(summary['identity']['storage_metadata_available'])
        self.assertEqual(summary['entities']['students']['outcomes'],
                         {'metadata_unavailable': 1})


# ─────────────────────────────────────────────────────────────────────────────
#  Candidate selection and scoping
# ─────────────────────────────────────────────────────────────────────────────

class CandidateTest(BackfillTestBase):

    def test_04_05_existing_display_and_missing_photo_are_not_candidates(self):
        url, _ = self._original('uploads', 'students', _jpeg())
        has_display = self._student(self.school_a, photo=url,
                                    display=BASE + 'uploads/students/display/keep.webp')
        no_photo = self._student(self.school_a)
        blank = self._student(self.school_a, photo='   ')
        eurl, _ = self._original('uploads', 'employees', _jpeg())
        e_has = self._employee(self.school_a, photo=eurl,
                               display=BASE + 'uploads/employees/display/keep.webp')
        for apply in (False, True):
            _, summary, records = self._run(apply=apply)
            self.assertEqual(records, [])
            self.assertEqual(summary['processed'], 0)
        self.upload.assert_not_called()
        self.fetch.assert_not_called()
        self.assertEqual(self._row(Student, has_display)['photo_display'],
                         BASE + 'uploads/students/display/keep.webp')
        self.assertIsNone(self._row(Student, no_photo)['photo_display'])
        self.assertIsNone(self._row(Student, blank)['photo_display'])
        self.assertEqual(self._row(Employee, e_has)['photo_display'],
                         BASE + 'uploads/employees/display/keep.webp')

    def test_19_school_id_restricts_candidates(self):
        ua, _ = self._original('uploads', 'students', _jpeg())
        ub, _ = self._original('uploads', 'students', _jpeg())
        a = self._student(self.school_a, photo=ua)
        b = self._student(self.school_b, photo=ub)
        _, _, records = self._run(apply=True, school_id=self.school_a.id)
        self.assertEqual([(r['id'], r['school_id']) for r in records], [(a, self.school_a.id)])
        self.assertIsNone(self._row(Student, b)['photo_display'])
        _, _, records = self._run(school_id=None, exclude_school_ids=(self.school_b.id,),
                                  entities=('students',), limit=5)
        self.assertNotIn(b, [r['id'] for r in records])

    def test_20_resume_after_id(self):
        ids = [self._student(self.school_a, photo=self._original('uploads', 'students', _jpeg())[0])
               for _ in range(3)]
        _, _, records = self._run(entities=('students',), resume_after_id=ids[0])
        self.assertEqual([r['id'] for r in records], ids[1:])
        with self.assertRaises(bf.ConfigError):
            self._run(entities=('students', 'employees'), resume_after_id=ids[0])

    def test_22_relative_originals_skipped_by_default(self):
        sid = self._student(self.school_a, photo='uploads/students/legacy.jpg')
        eid = self._employee(self.school_a, photo='uploads/employees/legacy.png')
        for apply in (False, True):
            _, _, records = self._run(apply=apply)
            self.assertEqual({r['outcome'] for r in records}, {'local_original_unverified'})
            self.assertEqual({r['source'] for r in records}, {'local'})
        self.fetch.assert_not_called()
        self.upload.assert_not_called()
        self.assertIsNone(self._row(Student, sid)['photo_display'])
        self.assertIsNone(self._row(Employee, eid)['photo_display'])
        # --include-local without the confirmed application root is refused.
        with self.assertRaises(bf.ConfigError):
            self._run(include_local=True)
        with self.assertRaises(bf.ConfigError):
            self._run(include_local=True, expected_app_root='/somewhere/else')

    def test_apply_refuses_without_confirmed_storage_host_or_metadata(self):
        with self.assertRaises(bf.ConfigError):
            self._run(apply=True, confirm_storage_host='other.supabase.co')
        with self.assertRaises(bf.ConfigError):
            self._run(apply=True, confirm_storage_host='')
        no_meta = mock.Mock(available=mock.Mock(return_value=False))
        with self.assertRaises(bf.ConfigError):
            self._run(apply=True, metadata=no_meta)
        self.upload.assert_not_called()

    def test_foreign_storage_host_is_not_fetched(self):
        sid = self._student(self.school_a,
                            photo='https://old.supabase.co/storage/v1/object/public/uploads/students/a.jpg')
        _, _, records = self._run(apply=True, entities=('students',))
        self.assertEqual(records[0]['outcome'], 'storage_host_mismatch')
        self.fetch.assert_not_called()
        self.assertIsNone(self._row(Student, sid)['photo_display'])


# ─────────────────────────────────────────────────────────────────────────────
#  Apply mode
# ─────────────────────────────────────────────────────────────────────────────

class ApplyTest(BackfillTestBase):

    def test_06_08_09_student_uses_make_display_photo_and_only_display_changes(self):
        raw = _jpeg(1600, 1200)
        url, key = self._original('uploads', 'students', raw)
        sid = self._student(self.school_a, photo=url)
        before = self._row(Student, sid)
        with mock.patch.object(student_display_photo, 'make_display_photo',
                               wraps=student_display_photo.make_display_photo) as sm, \
             mock.patch.object(employee_display_photo, 'make_employee_display_photo',
                               wraps=employee_display_photo.make_employee_display_photo) as em:
            runner, summary, records = self._run(apply=True, entities=('students',), run_id='r1')
        sm.assert_called_once_with(raw)
        em.assert_not_called()
        after = self._row(Student, sid)
        rec = records[0]
        self.assertEqual(rec['outcome'], 'ok')
        self.assertRegex(rec['new_object_key'], r'^students/display/bfr1-[0-9a-f]{32}\.webp$')
        self.assertEqual(after['photo_display'], BASE + 'uploads/' + rec['new_object_key'])
        self.assertEqual(after['photo'], url)                        # original untouched
        changed = {k for k in before if before[k] != after[k]}
        self.assertLessEqual(changed, {'photo_display', 'updated_at'})
        self.assertIn('photo_display', changed)
        self.assertEqual(self.blobs[('uploads', key)], raw)          # original bytes intact
        disp = Image.open(io.BytesIO(self.blobs[('uploads', rec['new_object_key'])]))
        self.assertEqual(disp.format, 'WEBP')
        self.assertEqual(max(disp.size), 1024)
        self.assertEqual(summary['db_writes'], 1)
        self.save_uploaded.assert_not_called()

    def test_07_employee_uses_make_employee_display_photo(self):
        raw = _png()
        url, _ = self._original('uploads', 'employees', raw, ext='png')
        eid = self._employee(self.school_a, photo=url)
        before = self._row(Employee, eid)
        with mock.patch.object(employee_display_photo, 'make_employee_display_photo',
                               wraps=employee_display_photo.make_employee_display_photo) as em, \
             mock.patch.object(student_display_photo, 'make_display_photo',
                               wraps=student_display_photo.make_display_photo) as sm:
            _, _, records = self._run(apply=True, entities=('employees',), run_id='r2')
        em.assert_called_once_with(raw)
        sm.assert_not_called()
        after = self._row(Employee, eid)
        self.assertRegex(records[0]['new_object_key'], r'^employees/display/bfr2-[0-9a-f]{32}\.webp$')
        self.assertEqual(after['photo'], url)
        self.assertLessEqual({k for k in before if before[k] != after[k]},
                             {'photo_display', 'updated_at'})

    def test_school_media_registration_original_is_supported(self):
        url, _ = self._original('school-media', 'registration/5/photos', _jpeg())
        sid = self._student(self.school_a, photo=url)
        _, _, records = self._run(apply=True, entities=('students',))
        self.assertEqual(records[0]['outcome'], 'ok')
        self.assertTrue(self._row(Student, sid)['photo_display'].startswith(
            BASE + 'uploads/students/display/'))

    def test_10_confirmed_supabase_upload_required_before_db_update(self):
        url, _ = self._original('uploads', 'students', _jpeg())
        sid = self._student(self.school_a, photo=url)
        seen = []

        def upload(data, key, content_type, bucket=None):
            with db.engine.connect() as conn:          # row state at upload time
                seen.append(conn.execute(text('select photo_display from students where id=:i'),
                                         {'i': sid}).scalar())
            return f'{BASE}{bucket}/{key}'             # response OK but object NOT registered
        self.upload.side_effect = upload
        _, _, records = self._run(apply=True, entities=('students',))
        self.assertEqual(seen, [None])
        self.assertEqual((records[0]['outcome'], records[0]['error_code']),
                         ('storage_upload_failed', 'upload_unverified'))
        self.assertEqual(records[0]['cleanup'], 'not_created')
        self.assertIsNone(self._row(Student, sid)['photo_display'])

    def test_11_local_storage_fallback_is_rejected(self):
        url, _ = self._original('uploads', 'students', _jpeg())
        sid = self._student(self.school_a, photo=url)
        display_dir = ROOT / 'app' / 'static' / 'uploads' / 'students' / 'display'
        before_files = set(display_dir.glob('bf*')) if display_dir.exists() else set()

        self.upload.side_effect = lambda data, key, ct, bucket=None: None      # Supabase failed
        _, _, rec_none = self._run(apply=True, entities=('students',))
        self.upload.side_effect = lambda data, key, ct, bucket=None: f'uploads/{key}'  # local-shaped
        _, _, rec_local = self._run(apply=True, entities=('students',))

        self.assertEqual((rec_none[0]['outcome'], rec_none[0]['error_code']),
                         ('storage_upload_failed', 'upload_failed'))
        self.assertEqual((rec_local[0]['outcome'], rec_local[0]['error_code']),
                         ('storage_upload_failed', 'unexpected_upload_result'))
        self.save_uploaded.assert_not_called()
        after_files = set(display_dir.glob('bf*')) if display_dir.exists() else set()
        self.assertEqual(after_files, before_files)
        self.assertIsNone(self._row(Student, sid)['photo_display'])

    def test_12_locked_recheck_uses_select_for_update_by_id_and_school(self):
        url, _ = self._original('uploads', 'students', _jpeg())
        self._student(self.school_a, photo=url)
        sql = self._capture_sql()
        self._run(apply=True, entities=('students',))
        locks = [s for s in sql if 'FOR UPDATE' in s.upper()]
        self.assertEqual(len(locks), 1)
        self.assertIn('students.id =', locks[0])
        self.assertIn('students.school_id =', locks[0])
        updates = [s for s in sql if s.lstrip().upper().startswith('UPDATE')]
        self.assertEqual(len(updates), 1)
        set_clause = updates[0].split(' SET ', 1)[1].split(' WHERE ', 1)[0]
        self.assertEqual(sorted(p.split('=')[0].strip() for p in set_clause.split(',')),
                         ['photo_display', 'updated_at'])

    def test_13_changed_original_between_read_and_commit_is_conflict(self):
        url, key = self._original('uploads', 'students', _jpeg())
        sid = self._student(self.school_a, photo=url)
        new_photo = f'{BASE}uploads/students/replaced.jpg'

        def fetch(k, bucket=None):
            with db.engine.begin() as conn:            # a user replaces the photo meanwhile
                conn.execute(text('update students set photo=:p where id=:i'),
                             {'p': new_photo, 'i': sid})
            return self.blobs[(bucket, k)], 'image/jpeg'
        self.fetch.side_effect = fetch
        _, _, records = self._run(apply=True, entities=('students',))
        rec = records[0]
        self.assertEqual((rec['outcome'], rec['error_code'], rec['cleanup']),
                         ('conflict_skipped', 'changed_under_lock', 'deleted'))
        row = self._row(Student, sid)
        self.assertEqual((row['photo'], row['photo_display']), (new_photo, None))
        self.assertNotIn(('uploads', rec['new_object_key']), self.meta.objects)
        self.delete.assert_called_once_with(rec['new_object_key'], bucket='uploads')

    def test_14_display_populated_by_another_process_is_not_overwritten(self):
        url, _ = self._original('uploads', 'employees', _jpeg())
        eid = self._employee(self.school_a, photo=url)
        other = BASE + 'uploads/employees/display/made-by-web.webp'

        def upload(data, key, content_type, bucket=None):
            with db.engine.begin() as conn:
                conn.execute(text('update employees set photo_display=:d where id=:i'),
                             {'d': other, 'i': eid})
            return self._fake_upload(data, key, content_type, bucket)
        self.upload.side_effect = upload
        _, _, records = self._run(apply=True, entities=('employees',))
        self.assertEqual(records[0]['outcome'], 'conflict_skipped')
        self.assertEqual(records[0]['cleanup'], 'deleted')
        self.assertEqual(self._row(Employee, eid)['photo_display'], other)

    def test_display_set_before_download_skips_without_fetch(self):
        url, _ = self._original('uploads', 'students', _jpeg())
        sid = self._student(self.school_a, photo=url)
        runner = bf.BackfillRunner(entities=('students',), apply=True, school_id=self.school_a.id,
                                   manifest_dir=self.tmp, metadata=self.meta,
                                   confirm_storage_host=HOST, run_id='pre')
        real_current = runner._current

        def current(spec, cid, sid_):
            with db.engine.begin() as conn:
                conn.execute(text('update students set photo_display=:d where id=:i'),
                             {'d': BASE + 'uploads/students/display/web.webp', 'i': sid})
            return real_current(spec, cid, sid_)
        runner._current = current
        summary = runner.run()
        self.assertEqual(summary['entities']['students']['outcomes'], {'conflict_skipped': 1})
        self.fetch.assert_not_called()
        self.upload.assert_not_called()

    def test_15_uploaded_object_deleted_on_db_failure(self):
        url, _ = self._original('uploads', 'students', _jpeg())
        sid = self._student(self.school_a, photo=url)
        with mock.patch.object(bf.BackfillRunner, '_commit', side_effect=RuntimeError('db down')):
            _, _, records = self._run(apply=True, entities=('students',))
        rec = records[0]
        self.assertEqual((rec['outcome'], rec['error_code'], rec['cleanup']),
                         ('db_update_failed', 'RuntimeError', 'deleted'))
        self.assertNotIn(('uploads', rec['new_object_key']), self.meta.objects)
        self.assertIsNone(self._row(Student, sid)['photo_display'])

    def test_16_failed_delete_becomes_orphan_candidate(self):
        url, _ = self._original('uploads', 'students', _jpeg())
        self._student(self.school_a, photo=url)
        self.delete.side_effect = lambda key, bucket=None: False
        with mock.patch.object(bf.BackfillRunner, '_commit', side_effect=RuntimeError('db down')):
            _, summary, records = self._run(apply=True, entities=('students',))
        rec = records[0]
        self.assertEqual((rec['outcome'], rec['error_code'], rec['cleanup']),
                         ('orphan_candidate', 'db_update_failed', 'delete_failed'))
        self.assertIn(('uploads', rec['new_object_key']), self.meta.objects)
        self.assertEqual(summary['errors'], 1)

    def test_17_original_is_never_deleted(self):
        url, key = self._original('uploads', 'students', _jpeg())
        self._student(self.school_a, photo=url)
        with mock.patch.object(bf.BackfillRunner, '_commit', side_effect=RuntimeError('x')):
            runner, _, _ = self._run(apply=True, entities=('students',), run_id='r17')
        for call in self.delete.call_args_list:
            self.assertRegex(call.args[0], r'^students/display/bfr17-[0-9a-f]{32}\.webp$')
        self.assertIn(('uploads', key), self.meta.objects)
        self.delete.reset_mock()
        for bad in (key, 'students/display/' + uuid4().hex + '.webp',
                    'students/display/bfOTHERRUN-' + uuid4().hex + '.webp',
                    '../students/display/bfr17-' + uuid4().hex + '.webp'):
            self.assertEqual(runner._cleanup(bad), 'refused')
        self.delete.assert_not_called()

    def test_18_second_run_is_idempotent(self):
        url, _ = self._original('uploads', 'students', _jpeg())
        sid = self._student(self.school_a, photo=url)
        eurl, _ = self._original('uploads', 'employees', _jpeg())
        self._employee(self.school_a, photo=eurl)
        _, first, _ = self._run(apply=True)
        display = self._row(Student, sid)['photo_display']
        uploads = self.upload.call_count
        _, second, records = self._run(apply=True)
        self.assertEqual(first['db_writes'], 2)
        self.assertEqual((second['processed'], second['db_writes'], records), (0, 0, []))
        self.assertEqual(self.upload.call_count, uploads)
        self.assertEqual(self._row(Student, sid)['photo_display'], display)

    def test_21_max_errors_stops_safely(self):
        ids = [self._student(self.school_a, photo=self._original('uploads', 'students', _jpeg())[0])
               for _ in range(3)]
        self.fetch.side_effect = lambda key, bucket=None: (None, None)
        _, summary, records = self._run(apply=True, entities=('students',), max_errors=2)
        self.assertEqual(summary['stopped_reason'], 'max_errors_reached')
        self.assertEqual([r['outcome'] for r in records], ['storage_fetch_failed'] * 2)
        self.assertEqual([r['id'] for r in records], ids[:2])
        self.upload.assert_not_called()
        self.assertTrue(all(self._row(Student, i)['photo_display'] is None for i in ids))

    def test_one_failure_does_not_undo_previous_success(self):
        good, _ = self._original('uploads', 'students', _jpeg())
        bad, bad_key = self._original('uploads', 'students', b'not an image at all')
        g = self._student(self.school_a, photo=good)
        b = self._student(self.school_a, photo=bad)
        _, summary, records = self._run(apply=True, entities=('students',))
        self.assertEqual({r['id']: r['outcome'] for r in records},
                         {g: 'ok', b: 'unsupported_image'})
        self.assertIsNotNone(self._row(Student, g)['photo_display'])
        self.assertIsNone(self._row(Student, b)['photo_display'])

    def test_23_cleanup_checks_both_display_columns(self):
        runner = bf.BackfillRunner(entities=('students',), apply=True, school_id=self.school_a.id,
                                   manifest_dir=self.tmp, metadata=self.meta,
                                   confirm_storage_host=HOST, run_id='r23')
        for model_maker, folder in ((self._student, 'students'), (self._employee, 'employees')):
            key = f'{folder}/display/bfr23-{uuid4().hex}.webp'
            self.meta.objects[('uploads', key)] = 10
            model_maker(self.school_b, photo=BASE + 'uploads/x.jpg',
                        display=BASE + 'uploads/' + key)
            sql = self._capture_sql()
            self.assertEqual(runner._cleanup(key), 'kept_referenced')
            joined = ' '.join(sql)
            self.assertIn('FROM students', joined)
            self.assertIn('FROM employees', joined)
            self.assertIn('students.photo_display', joined)
            self.assertIn('employees.photo_display', joined)
        self.delete.assert_not_called()
        self.assertNotIn('resolve_upload_owner', (ROOT / 'app' / 'services' /
                                                  'display_photo_backfill.py').read_text('utf-8')
                         .split('"""', 2)[2])

    def test_manifest_has_no_personal_data(self):
        url, _ = self._original('uploads', 'students', _jpeg())
        sid = self._student(self.school_a, photo=url)
        _, summary, records = self._run(apply=True, entities=('students',))
        text_ = pathlib.Path(summary['manifest']).read_text('utf-8')
        self.assertNotIn('Secret Student', text_)
        self.assertEqual(set(records[0]), {
            'run_id', 'mode', 'entity', 'id', 'school_id', 'original_value', 'source',
            'new_display_value', 'new_object_key', 'outcome', 'error_code', 'cleanup',
            'original_size_bytes', 'display_size_bytes', 'timestamp'})
        self.assertEqual(records[0]['id'], sid)
        self.assertTrue(os.path.isfile(summary['manifest'].replace('.jsonl', '.summary.json')))


# ─────────────────────────────────────────────────────────────────────────────
#  Face ID / structural guards
# ─────────────────────────────────────────────────────────────────────────────

class StructureTest(unittest.TestCase):

    def test_24_face_id_still_reads_the_original(self):
        devices = (ROOT / 'app' / 'blueprints' / 'attendance_devices' / '__init__.py').read_text('utf-8')
        self.assertIn('photo=student.photo,', devices)
        self.assertIn('photo=employee.photo,', devices)
        self.assertIn("photo=student.photo if student else None", devices)
        self.assertIn("photo=employee.photo if employee else None", devices)
        self.assertNotIn('photo_display', devices)
        self.assertNotIn('photo_display',
                         (ROOT / 'app' / 'services' / 'aiface_sync.py').read_text('utf-8'))

    def test_tool_never_writes_original_or_is_wired_into_the_app(self):
        src = (ROOT / 'app' / 'services' / 'display_photo_backfill.py').read_text('utf-8')
        code = src.split('"""', 2)[2]
        self.assertIsNone(re.search(r'\.photo\s*=(?!=)', code))
        self.assertEqual(re.findall(r'\.photo_display\s*=(?!=)', code), ['.photo_display ='])
        self.assertNotIn('save_uploaded_file(', code)
        for path in (ROOT / 'app').rglob('*.py'):
            if path.name != 'display_photo_backfill.py':
                self.assertNotIn('display_photo_backfill', path.read_text('utf-8'), str(path))


if __name__ == '__main__':
    unittest.main()
