"""
Stored profile-photo form (app/utils/face_photo.py).

normalize_face_photo() must produce exactly what the AI Face pipeline
(app.services.aiface_sync.prepare_photo_for_device) sends to the device:
EXIF orientation, RGB, longest side <= 640 px (aspect kept, never upscaled),
JPEG quality 85, optimize=True. The Face ID code itself is not changed.
"""
import io
import pathlib
import shutil
import unittest
from uuid import uuid4

from PIL import Image, ImageDraw, ImageOps

from app import create_app
from app.services.aiface_sync import prepare_photo_for_device
from app.utils.face_photo import (FACE_PHOTO_JPEG_QUALITY, FACE_PHOTO_MAX_SIDE,
                                  normalize_face_photo, normalized_face_upload)


def _enc(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _picture(w, h):
    img = Image.new('RGB', (w, h), (205, 190, 170))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w // 4, h // 4), fill=(220, 30, 30))             # top-left marker
    d.ellipse((w // 4, h // 6, 3 * w // 4, h // 2), fill=(225, 185, 150))
    return img


def _exif_jpeg(w, h, orientation):
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = 'SecretCam'
    return _enc(_picture(w, h), 'JPEG', quality=92, exif=exif.tobytes())


class FacePhotoFormTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def test_policy_constants(self):
        self.assertEqual((FACE_PHOTO_MAX_SIDE, FACE_PHOTO_JPEG_QUALITY), (640, 85))

    def test_identical_to_the_device_pipeline(self):
        folder = f'test-face-{uuid4().hex}'
        root = pathlib.Path(self.app.root_path, 'static', 'uploads', folder)
        root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        rgba = Image.new('RGBA', (900, 700), (20, 90, 160, 255))
        cases = {'jpeg-exif6.jpg': _exif_jpeg(4032, 3024, 6),
                 'portrait.jpg': _enc(_picture(3024, 4032), 'JPEG', quality=90),
                 'rgba.png': _enc(rgba, 'PNG'),
                 'small.png': _enc(_picture(300, 400), 'PNG'),
                 'palette.gif': _enc(_picture(500, 700).convert('P'), 'GIF')}
        with self.app.app_context():
            for name, raw in cases.items():
                with self.subTest(name):
                    (root / name).write_bytes(raw)
                    device, info = prepare_photo_for_device(f'uploads/{folder}/{name}')
                    self.assertIsNotNone(device, info)
                    self.assertEqual(normalize_face_photo(raw), device)

    def test_640_jpeg_q85_aspect_kept_never_upscaled(self):
        for (w, h), size in (((3024, 4032), (480, 640)), ((4032, 3024), (640, 480)),
                             ((2000, 900), (640, 288)), ((300, 400), (300, 400))):
            with self.subTest((w, h)):
                raw = _enc(_picture(w, h), 'JPEG', quality=92)
                out = normalize_face_photo(raw)
                img = Image.open(io.BytesIO(out))
                self.assertEqual((img.format, img.mode, img.size), ('JPEG', 'RGB', size))
                ref = Image.open(io.BytesIO(raw)).convert('RGB')
                ref.thumbnail((640, 640), Image.LANCZOS)
                self.assertEqual(out, _enc(ref, 'JPEG', quality=85, optimize=True))
                self.assertNotEqual(out, _enc(ref, 'JPEG', quality=95, optimize=True))

    def test_exif_orientation_applied_and_metadata_dropped(self):
        out = normalize_face_photo(_exif_jpeg(4032, 3024, 6))
        img = Image.open(io.BytesIO(out))
        self.assertEqual(img.size, (480, 640))                         # landscape → portrait
        self.assertNotIn(b'SecretCam', out)
        self.assertEqual(img.getexif().get(0x0112, 1), 1)
        ref = ImageOps.exif_transpose(Image.open(io.BytesIO(_exif_jpeg(4032, 3024, 6))))
        self.assertEqual(ref.size, (3024, 4032))

    def test_upload_wrapper_keeps_original_stream(self):
        class _FS:
            def __init__(self, raw):
                self.stream = io.BytesIO(raw)
        raw = _enc(_picture(1200, 1600), 'PNG')
        up = _FS(raw)
        up.stream.seek(0, 2)
        end = up.stream.tell()
        with self.app.app_context():
            face = normalized_face_upload(up)
            self.assertEqual((face.filename, face.content_type), ('photo.jpg', 'image/jpeg'))
            self.assertEqual(face.stream.read(), normalize_face_photo(raw))
            self.assertEqual(up.stream.tell(), end)                    # original untouched
            self.assertEqual(up.stream.getvalue(), raw)
            self.assertIsNone(normalized_face_upload(_FS(b'not an image')))


if __name__ == '__main__':
    unittest.main()
