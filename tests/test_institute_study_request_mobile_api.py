"""Institute study requests ("أرسل درسي") — P2 parent mobile API.

POST/GET/GET-detail/DELETE /api/mobile/v1/parent/children/<id>/study-requests.
Storage is ALWAYS mocked: save_uploaded_file / delete_uploaded_file are
replaced in the route module, so nothing ever reaches Supabase or the disk.
"""
import io
import unittest
from datetime import date, datetime
from unittest import mock
from uuid import uuid4

from PIL import Image
from sqlalchemy import event

from app import create_app
from app.blueprints.mobile_api import parent_study_requests as mod
from app.blueprints.mobile_api.utils import encode_token
from app.models import (
    db, AcademicYear, InstituteStudyRequest, InstituteStudyRequestImage, Role,
    School, Student, User, parent_students,
)
from app.utils.school_cleanup import cleanup_school_cascade

OPTS = {'bypass_tenant_scope': True, 'include_all_years': True}
API = '/api/mobile/v1/parent/children'
IMG_TABLE = 'institute_study_request_images'


def _png(size=(24, 24), color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, format='PNG')
    return buf.getvalue()


def _jpeg(size=(24, 24)):
    buf = io.BytesIO()
    Image.new('RGB', size, (10, 120, 200)).save(buf, format='JPEG', quality=80)
    return buf.getvalue()


def _apng():
    a, b = Image.new('RGB', (16, 16), 'red'), Image.new('RGB', (16, 16), 'blue')
    buf = io.BytesIO()
    a.save(buf, format='PNG', save_all=True, append_images=[b])
    return buf.getvalue()


class FakeStorage:
    """Records uploads/deletes; can fail the Nth upload."""

    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.uploaded, self.deleted, self.calls = [], [], 0
        self.rows_at_delete = []          # DB image rows still present per delete

    def save(self, file, subfolder, **kw):
        self.calls += 1
        assert kw.get('bucket') == 'uploads' and kw.get('local_fallback') is False
        if self.fail_on == self.calls:
            return None
        ext = file.filename.rsplit('.', 1)[1]
        path = (f'https://isr.invalid/storage/v1/object/public/uploads/'
                f'{subfolder}/{uuid4().hex}.{ext}')
        self.uploaded.append(path)
        return path

    def delete(self, path, bucket=None):
        self.rows_at_delete.append(
            InstituteStudyRequestImage.query.execution_options(**OPTS)
            .filter_by(file_path=path).count())
        self.deleted.append(path)
        return True


class StudyRequestMobileApiTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config.update(RATELIMIT_ENABLED=False, WTF_CSRF_ENABLED=False,
                              PRIVATE_UPLOADS_ENABLED=False)
        with cls.app.app_context():
            cls.roles = {r.name: r.id for r in Role.query.all()}

    # ── fixture ───────────────────────────────────────────────────────────────

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        with self.app.app_context():
            self._school('a', institute=True)
            self._school('b', institute=True)
            self._school('c', institute=False)
            db.session.commit()
        self.storage = FakeStorage()
        self._patches = [
            mock.patch.object(mod, 'save_uploaded_file', side_effect=self.storage.save),
            mock.patch.object(mod, 'delete_uploaded_file', side_effect=self.storage.delete),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        with self.app.app_context():
            db.session.rollback()
            for key in ('a', 'b', 'c'):
                cleanup_school_cascade(self.ids[f'school_{key}'])
            db.session.commit()
            db.session.remove()

    def _school(self, key, *, institute):
        s = self.sfx
        school = School(school_name=f'ISM {key} {s}', code=f'ISM{key}{s}'[:20],
                        capacity=0, is_active=True,
                        institution_type='institute' if institute else 'school')
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'Y{key}{s}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 7, 31))
        old = AcademicYear(school_id=school.id, name=f'O{key}{s}', is_current=False,
                           start_date=date(2025, 8, 1), end_date=date(2026, 7, 31))
        db.session.add_all([year, old])
        db.session.flush()
        self.ids.update({f'school_{key}': school.id, f'year_{key}': year.id,
                         f'old_{key}': old.id})
        for tag in ('p1', 'p2', 'p3'):
            u = User(username=f'ism{tag}{key}_{s}', email=f'ism{tag}{key}_{s}@t.test',
                     full_name=f'{tag} {key}', role_id=self.roles['parent'],
                     school_id=school.id, is_active=True)
            u.set_password('Password123!')
            db.session.add(u)
            db.session.flush()
            self.ids[f'{tag}_{key}'] = u.id
        # child: linked to p1 AND p2.  other: linked to p3 only.  inactive: p1.
        for tag, status, parents in (('child', 'active', ('p1', 'p2')),
                                     ('other', 'active', ('p3',)),
                                     ('inactive', 'withdrawn', ('p1',))):
            st = Student(student_id=f'IM{uuid4().hex[:10]}', full_name=f'{tag} {s}',
                         school_id=school.id, academic_year_id=year.id, status=status)
            db.session.add(st)
            db.session.flush()
            for p in parents:
                db.session.execute(parent_students.insert().values(
                    user_id=self.ids[f'{p}_{key}'], student_id=st.id))
            self.ids[f'{tag}_{key}'] = st.id

    # ── helpers ───────────────────────────────────────────────────────────────

    def _call(self, method, parent_key, path, **kw):
        with self.app.app_context():
            user = db.session.get(User, self.ids[parent_key], execution_options=OPTS)
            token = encode_token(user)
        client = self.app.test_client()
        return getattr(client, method)(path, headers={'Authorization': f'Bearer {token}'},
                                       **kw)

    def _url(self, child='child_a', rid=None):
        base = f'{API}/{self.ids[child]}/study-requests'
        return f'{base}/{rid}' if rid else base

    def _post(self, parent='p1_a', child='child_a', images=None, note=None, field='images[]'):
        images = [('a.png', _png())] if images is None else images
        data = {field: [(io.BytesIO(raw), name) for name, raw in images]}
        if note is not None:
            data['note'] = note
        return self._call('post', parent, self._url(child), data=data,
                          content_type='multipart/form-data')

    def _rows(self, **filters):
        with self.app.app_context():
            return (InstituteStudyRequest.query.execution_options(**OPTS)
                    .filter_by(**filters).all())

    def _img_count(self, **filters):
        with self.app.app_context():
            return (InstituteStudyRequestImage.query.execution_options(**OPTS)
                    .filter_by(**filters).count())

    def _insert(self, child='child_a', key='a', parent='p1_a', n=0, year=None,
                status='pending', when=None):
        with self.app.app_context():
            req = InstituteStudyRequest(
                school_id=self.ids[f'school_{key}'],
                academic_year_id=self.ids[year or f'year_{key}'],
                student_id=self.ids[child], parent_id=self.ids[parent],
                status=status, image_count=n, created_at=when or datetime.utcnow())
            db.session.add(req)
            db.session.flush()
            for i in range(n):
                db.session.add(InstituteStudyRequestImage(
                    request_id=req.id, school_id=req.school_id, mime='image/webp',
                    size_bytes=10 + i, sort_order=n - 1 - i,
                    file_path=f'https://isr.invalid/storage/v1/object/public/uploads/'
                              f'x/{uuid4().hex}.webp'))
            db.session.commit()
            return req.id

    def _capture_sql(self):
        statements = []
        with self.app.app_context():
            engine = db.engine

        def _cap(conn, cursor, statement, *a):
            statements.append(statement)
        event.listen(engine, 'before_cursor_execute', _cap)
        self.addCleanup(event.remove, engine, 'before_cursor_execute', _cap)
        return statements

    # ── 1-3: authorization ───────────────────────────────────────────────────

    def test_01_normal_school_parent_rejected(self):
        for method in ('get', 'post'):
            with self.subTest(method=method):
                resp = (self._post('p1_c', 'child_c') if method == 'post'
                        else self._call('get', 'p1_c', self._url('child_c')))
                self.assertEqual(resp.status_code, 404)
                self.assertEqual(resp.get_json()['error'], 'institute_not_available')
        self.assertEqual(self.storage.calls, 0)

    def test_02_unlinked_or_other_school_child_rejected(self):
        for parent, child in (('p1_a', 'other_a'), ('p1_a', 'child_b')):
            with self.subTest(child=child):
                for resp in (self._post(parent, child),
                             self._call('get', parent, self._url(child))):
                    self.assertEqual(resp.status_code, 404)
                    self.assertTrue(resp.is_json)
                    self.assertEqual(resp.get_json()['error'], 'student_not_found')
        self.assertEqual(self.storage.calls, 0)

    def test_03_inactive_child_rejected(self):
        resp = self._post('p1_a', 'inactive_a')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()['error'], 'student_not_active')
        self.assertEqual(self.storage.calls, 0)

    # ── 4-5, 11: successful create ───────────────────────────────────────────

    def test_04_create_one_image(self):
        resp = self._post(note='  درس الكيمياء  ')
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True)[:300])
        body = resp.get_json()['request']
        self.assertEqual(body['status'], 'pending')
        self.assertEqual(body['image_count'], 1)
        self.assertEqual(body['note'], 'درس الكيمياء')
        self.assertTrue(body['can_delete'])
        self.assertNotIn('parent_id', body)
        (row,) = self._rows(student_id=self.ids['child_a'])
        self.assertEqual(row.academic_year_id, self.ids['year_a'])   # server-side year
        self.assertEqual(row.parent_id, self.ids['p1_a'])
        with self.app.app_context():
            img = (InstituteStudyRequestImage.query.execution_options(**OPTS)
                   .filter_by(request_id=row.id).one())
            self.assertEqual(img.mime, 'image/webp')
            self.assertTrue(img.file_path.endswith('.webp'))
            self.assertIn(f"schools/{self.ids['school_a']}/institute-study-requests/"
                          f"{self.ids['child_a']}/", img.file_path)

    def test_05_11_create_five_images_count_without_count_query(self):
        images = [(f'{i}.jpg' if i % 2 else f'{i}.png', _jpeg() if i % 2 else _png())
                  for i in range(5)]
        statements = self._capture_sql()
        resp = self._post(images=images, note='')
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True)[:300])
        self.assertEqual(resp.get_json()['request']['image_count'], 5)
        self.assertIsNone(resp.get_json()['request']['note'])        # '' → NULL
        img_statements = [st for st in statements if IMG_TABLE in st]
        self.assertEqual(len(img_statements), 1)                     # one INSERT batch
        self.assertTrue(img_statements[0].lstrip().upper().startswith('INSERT'))
        self.assertFalse(any('count(' in st.lower() and IMG_TABLE in st for st in statements))
        (row,) = self._rows(student_id=self.ids['child_a'])
        self.assertEqual(row.image_count, 5)
        self.assertEqual(self._img_count(request_id=row.id), 5)
        self.assertEqual(len(self.storage.uploaded), 5)

    # ── 6-10: validation ─────────────────────────────────────────────────────

    def _assert_rejected(self, resp, code, status=400):
        self.assertEqual(resp.status_code, status, resp.get_data(as_text=True)[:300])
        self.assertTrue(resp.is_json)
        self.assertEqual(resp.get_json()['error'], code)
        self.assertEqual(self._rows(student_id=self.ids['child_a']), [])
        self.assertEqual(self.storage.uploaded, [])

    def test_06_zero_images_rejected(self):
        self._assert_rejected(self._post(images=[]), 'images_required')

    def test_07_six_images_rejected(self):
        self._assert_rejected(self._post(images=[(f'{i}.png', _png()) for i in range(6)]),
                              'too_many_images')

    def test_08_fake_corrupt_animated_rejected(self):
        cases = {
            'renamed text': ('evil.png', b'MZ this is not an image at all'),
            'jpeg named png': ('x.png', _jpeg()),
            'bad extension': ('x.gif', _png()),
            'corrupt png': ('x.png', _png()[:40]),
            'animated png': ('x.png', _apng()),
        }
        for label, (name, raw) in cases.items():
            with self.subTest(case=label):
                resp = self._post(images=[('ok.png', _png()), (name, raw)])
                self._assert_rejected(resp, 'invalid_image')
                self.assertEqual(resp.get_json()['index'], 1)

    def test_09_original_over_5mb_rejected(self):
        big = b'\x89PNG\r\n\x1a\n' + b'\0' * (5 * 1024 * 1024)
        resp = self._post(images=[('big.png', big)])
        self._assert_rejected(resp, 'image_too_large')

    def test_10_note_over_1000_rejected(self):
        self._assert_rejected(self._post(note='ن' * 1001), 'note_too_long')
        self.assertEqual(self._post(note='ن' * 1000).status_code, 201)

    def test_10b_json_body_rejected(self):
        resp = self._call('post', 'p1_a', self._url(), json={'images': ['https://evil/x.png']})
        self._assert_rejected(resp, 'multipart_required', 415)

    # ── 12-13: all-or-nothing ────────────────────────────────────────────────

    def test_12_upload_failure_cleans_up(self):
        self.storage.fail_on = 3
        resp = self._post(images=[(f'{i}.png', _png()) for i in range(4)])
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json(), {'ok': False, 'error': 'image_upload_failed',
                                           'index': 2})
        self.assertEqual(len(self.storage.uploaded), 2)
        self.assertEqual(sorted(self.storage.deleted), sorted(self.storage.uploaded))
        self.assertEqual(self._rows(student_id=self.ids['child_a']), [])
        self.assertEqual(self._img_count(school_id=self.ids['school_a']), 0)

    def test_13_db_failure_cleans_up(self):
        with mock.patch('sqlalchemy.orm.session.Session.commit',
                        side_effect=RuntimeError('forced')):
            resp = self._post(images=[(f'{i}.png', _png()) for i in range(3)])
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json()['error'], 'server_error')
        self.assertEqual(len(self.storage.uploaded), 3)
        self.assertEqual(sorted(self.storage.deleted), sorted(self.storage.uploaded))
        self.assertEqual(self._rows(student_id=self.ids['child_a']), [])
        self.assertEqual(self._img_count(school_id=self.ids['school_a']), 0)

    # ── 14-18: list ──────────────────────────────────────────────────────────

    def test_14_15_list_only_that_child(self):
        mine = self._insert(n=2)
        self._insert(child='other_a', parent='p3_a')
        self._insert(child='child_b', key='b', parent='p1_b')
        body = self._call('get', 'p1_a', self._url()).get_json()
        self.assertEqual([r['id'] for r in body['requests']], [mine])
        item = body['requests'][0]
        self.assertEqual(item['image_count'], 2)
        self.assertEqual(set(item), {'id', 'student_id', 'note', 'status', 'status_label',
                                     'image_count', 'admin_note', 'viewed_at',
                                     'created_at', 'can_delete'})

    def test_16_17_list_reads_only_request_table_no_urls_no_n_plus_1(self):
        for _ in range(3):
            self._insert(n=3)
        # Warm-up: the first request of a worker fills the per-school active
        # academic-year cache (one extra SELECT that is unrelated to N).
        self._call('get', 'p1_a', self._url())
        with mock.patch.object(mod, 'photo_url') as url_fn:
            statements = self._capture_sql()
            body = self._call('get', 'p1_a', self._url()).get_json()
            few = list(statements)
            for _ in range(10):
                self._insert(n=3)
            statements.clear()
            body2 = self._call('get', 'p1_a', self._url()).get_json()
            many = list(statements)
        url_fn.assert_not_called()
        self.assertEqual(body['count'], 3)
        self.assertEqual(body2['count'], 13)
        for sql in (few, many):
            self.assertFalse(any(IMG_TABLE in st for st in sql))
            self.assertEqual(sum('FROM institute_study_requests' in st for st in sql), 1)
        self.assertEqual(len(few), len(many))                       # constant, not N+1
        self.assertTrue(all('url' not in r and 'images' not in r for r in body2['requests']))

    def test_18_pagination(self):
        base = datetime(2026, 9, 1, 8, 0)
        ids = [self._insert(when=base.replace(minute=i)) for i in range(5)]
        newest_first = list(reversed(ids))
        p1 = self._call('get', 'p1_a', self._url(), query_string={'limit': 2}).get_json()
        self.assertEqual([r['id'] for r in p1['requests']], newest_first[:2])
        self.assertTrue(p1['has_more'])
        self.assertEqual(p1['next_offset'], 2)
        p3 = self._call('get', 'p1_a', self._url(),
                        query_string={'limit': 2, 'offset': 4}).get_json()
        self.assertEqual([r['id'] for r in p3['requests']], newest_first[4:])
        self.assertFalse(p3['has_more'])
        self.assertIsNone(p3['next_offset'])
        capped = self._call('get', 'p1_a', self._url(), query_string={'limit': 500}).get_json()
        self.assertEqual(capped['limit'], 50)

    # ── 19-22: detail + viewing rights ───────────────────────────────────────

    def test_19_20_detail_ordered_protected_urls(self):
        rid = self._insert(n=3)
        body = self._call('get', 'p1_a', self._url(rid=rid)).get_json()['request']
        self.assertEqual([i['sort_order'] for i in body['images']], [0, 1, 2])
        self.assertEqual(set(body['images'][0]), {'id', 'url', 'mime', 'size_bytes',
                                                  'sort_order'})
        # Private uploads ON → every URL is the signed /media-proxy route, never
        # the raw storage URL.
        self.app.config['PRIVATE_UPLOADS_ENABLED'] = True
        try:
            body = self._call('get', 'p1_a', self._url(rid=rid)).get_json()['request']
        finally:
            self.app.config['PRIVATE_UPLOADS_ENABLED'] = False
        for img in body['images']:
            self.assertIn('/media-proxy/', img['url'])
            self.assertIn('sig=', img['url'])
            self.assertNotIn('isr.invalid', img['url'])
        # Another child's / school's request id → 404.
        other = self._insert(child='other_a', parent='p3_a', n=1)
        self.assertEqual(self._call('get', 'p1_a', self._url(rid=other)).status_code, 404)

    def test_21_22_second_parent_views_but_cannot_delete(self):
        rid = self._insert(n=1)
        body = self._call('get', 'p2_a', self._url(rid=rid)).get_json()['request']
        self.assertEqual(body['id'], rid)
        self.assertFalse(body['can_delete'])
        listed = self._call('get', 'p2_a', self._url()).get_json()['requests']
        self.assertEqual([r['id'] for r in listed], [rid])
        resp = self._call('delete', 'p2_a', self._url(rid=rid))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()['error'], 'only_sender_can_delete')
        self.assertEqual(len(self._rows(id=rid)), 1)
        self.assertEqual(self.storage.deleted, [])

    # ── 23-26: delete ────────────────────────────────────────────────────────

    def test_23_25_26_sender_deletes_pending(self):
        rid = self._insert(n=3)
        with self.app.app_context():
            paths = sorted(p for (p,) in db.session.query(InstituteStudyRequestImage.file_path)
                           .filter_by(request_id=rid).all())
        resp = self._call('delete', 'p1_a', self._url(rid=rid))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300])
        self.assertEqual(self._rows(id=rid), [])
        self.assertEqual(self._img_count(request_id=rid), 0)
        self.assertEqual(sorted(self.storage.deleted), paths)
        # Every storage delete ran after the DB commit (rows already gone).
        self.assertEqual(self.storage.rows_at_delete, [0, 0, 0])

    def test_24_viewed_request_cannot_be_deleted(self):
        rid = self._insert(n=1, status='viewed')
        resp = self._call('delete', 'p1_a', self._url(rid=rid))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()['error'], 'cannot_delete_viewed_request')
        self.assertEqual(len(self._rows(id=rid)), 1)
        self.assertEqual(self._img_count(request_id=rid), 1)
        self.assertEqual(self.storage.deleted, [])

    def test_26b_storage_failure_keeps_db_delete(self):
        rid = self._insert(n=2)
        with mock.patch.object(mod, 'delete_uploaded_file', return_value=False):
            resp = self._call('delete', 'p1_a', self._url(rid=rid))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._rows(id=rid), [])

    def test_26c_delete_other_school_or_child_404(self):
        other = self._insert(child='child_b', key='b', parent='p1_b', n=1)
        resp = self._call('delete', 'p1_a', self._url(rid=other))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(len(self._rows(id=other)), 1)

    # ── 27-28: pending limit, academic-year history ──────────────────────────

    def test_27_pending_limit(self):
        for _ in range(9):
            self._insert()
        self._insert(status='viewed')                      # viewed rows don't count
        self.assertEqual(self._post().status_code, 201)    # 10th pending
        resp = self._post()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()['error'], 'pending_limit_reached')
        self.assertEqual(len(self.storage.uploaded), 1)    # no work done for #11

    def test_28_history_spans_academic_years(self):
        old = self._insert(year='old_a', n=1, when=datetime(2026, 1, 10))
        new = self._insert(n=1)
        self._insert(child='child_b', key='b', parent='p1_b', year='old_b')
        body = self._call('get', 'p1_a', self._url()).get_json()
        self.assertEqual([r['id'] for r in body['requests']], [new, old])
        detail = self._call('get', 'p1_a', self._url(rid=old))
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.get_json()['request']['images']), 1)


if __name__ == '__main__':
    unittest.main()
