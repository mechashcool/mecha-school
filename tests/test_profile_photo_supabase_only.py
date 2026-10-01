"""
save_uploaded_file(local_fallback=False) — Supabase is the ONLY destination.

New original profile photos (Student create/edit, Employee create/edit, public
registration) call the shared upload helper with local_fallback=False: a
successful Supabase upload returns its URL exactly as before; a failed or
unconfigured upload returns None and writes NOTHING under app/static/uploads.
Every other caller keeps the default (local fallback unchanged).

Storage is mocked; no network, no production data. Route-level behaviour is
covered next to the existing photo tests (test_student_display_photo,
test_employee_display_photo, test_registration_media).
"""
import io
import pathlib
import shutil
import unittest
from unittest import mock
from uuid import uuid4

from werkzeug.datastructures import FileStorage

from app import create_app
from app.utils import helpers

URL = 'https://storage.test/storage/v1/object/public/uploads/'


def _upload(name='photo.jpg', data=b'\xff\xd8\xff\xe0 jpeg bytes'):
    return FileStorage(io.BytesIO(data), filename=name, content_type='image/jpeg')


class SupabaseOnlyUploadTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def setUp(self):
        self.sub = f'test-sbonly-{uuid4().hex}'
        self.local_dir = pathlib.Path(self.app.root_path, 'static', 'uploads', self.sub)
        self.addCleanup(shutil.rmtree, self.local_dir, ignore_errors=True)

    def test_success_returns_supabase_reference(self):
        with self.app.app_context(), \
                mock.patch('app.utils.helpers._supabase_upload',
                           side_effect=lambda d, p, c, bucket=None: URL + p) as up:
            stored = helpers.save_uploaded_file(_upload(), self.sub, local_fallback=False)
        self.assertEqual(stored, URL + up.call_args.args[1])
        self.assertRegex(stored, rf'{self.sub}/[0-9a-f]{{32}}\.jpg$')
        self.assertFalse(self.local_dir.exists())

    def test_failure_returns_none_and_writes_nothing_locally(self):
        with self.app.app_context(), \
                mock.patch.dict(self.app.config, {'SUPABASE_SERVICE_KEY': 'secret-key-xyz'}), \
                mock.patch('app.utils.helpers._supabase_upload', return_value=None) as up, \
                self.assertLogs(self.app.logger, 'WARNING') as logs:
            stored = helpers.save_uploaded_file(_upload(), self.sub, local_fallback=False)
        self.assertIsNone(stored)
        up.assert_called_once()                              # Supabase was attempted
        self.assertFalse(self.local_dir.exists())            # no local fallback
        self.assertNotIn('secret-key-xyz', '\n'.join(logs.output))

    def test_unconfigured_supabase_is_a_failure_too(self):
        with self.app.app_context(), \
                mock.patch.dict(self.app.config, {'SUPABASE_URL': '', 'SUPABASE_SERVICE_KEY': ''}):
            stored = helpers.save_uploaded_file(_upload(), self.sub, local_fallback=False)
        self.assertIsNone(stored)
        self.assertFalse(self.local_dir.exists())

    def test_default_callers_keep_local_fallback(self):
        with self.app.app_context(), \
                mock.patch('app.utils.helpers._supabase_upload', return_value=None):
            stored = helpers.save_uploaded_file(_upload(), self.sub)
        self.assertRegex(stored, rf'^uploads/{self.sub}/[0-9a-f]{{32}}\.jpg$')
        self.assertTrue(pathlib.Path(self.app.root_path, 'static', *stored.split('/')).is_file())


if __name__ == '__main__':
    unittest.main()
