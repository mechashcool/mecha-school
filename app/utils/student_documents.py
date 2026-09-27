"""
Student DOCUMENT image processing — legibility-first, applied once per upload.

Only NEW student-document IMAGE uploads (Add Student, Edit → add, Replace) go
through here, after the existing extension / size / magic-byte checks of
validate_student_document_file(). PDFs never reach this module: they are stored
byte-for-byte. Existing documents are never read or rewritten.

Pipeline for an image document (jpg/jpeg/png):
  1. decode with Pillow restricted to JPEG/PNG — the bytes decide, not the name;
  2. refuse more than STUDENT_DOC_MAX_PIXELS before decoding pixel data (a tiny
     file can declare enormous dimensions) and refuse animated images (APNG);
  3. apply EXIF orientation; EXIF/XMP/GPS are dropped (an RGB ICC profile is
     kept so colours stay right);
  4. shrink so the longest side is <= 1600 px (never upscale), LANCZOS;
  5. encode WebP quality 88 (method 4) — conservative so fine print stays
     readable. If that comes out larger than the upload (flat graphics / simple
     scans), a lossless WebP is tried, and for PNG sources a metadata-free PNG;
     the smallest valid encoding is kept.

Only the processed bytes are stored; the original upload never is.
Uses Pillow APIs available in the pinned Pillow 10.3.0.
"""
from __future__ import annotations

import io
from typing import NamedTuple

STUDENT_DOC_IMAGE_MAX_SIDE = 1600        # longest stored side, px
STUDENT_DOC_WEBP_QUALITY = 88
# 40 MP: an A4 page scanned at 600 dpi is ~34.8 MP and 12-24 MP phone photos
# fit easily; a PNG at this size decodes to <=160 MB (RGBA), which keeps one
# upload from exhausting the web worker. Same ceiling as the other image paths.
STUDENT_DOC_MAX_PIXELS = 40_000_000
_ALLOWED_FORMATS = ('JPEG', 'PNG')

MSG_INVALID = 'تعذّر قراءة صورة المستند. يرجى رفع صورة صالحة بصيغة jpg أو png، أو ملف PDF.'
MSG_TOO_LARGE = 'أبعاد صورة المستند كبيرة جداً. يرجى رفع صورة بأبعاد أصغر.'
MSG_ANIMATED = 'الصور المتحركة غير مدعومة للمستندات. يرجى رفع صورة ثابتة.'


class StudentDocumentImageError(ValueError):
    """Document image refused; ``str(exc)`` is a user-facing Arabic message."""


class ProcessedDocumentImage(NamedTuple):
    data: bytes
    ext: str            # 'webp' normally; 'png' only when that is smaller
    width: int
    height: int


def _has_alpha(img) -> bool:
    return (img.mode in ('RGBA', 'LA', 'PA')
            or (img.mode in ('P', 'L', 'RGB') and 'transparency' in img.info))


def optimize_document_image(raw: bytes) -> ProcessedDocumentImage:
    """Validate and optimise one document image. Raises StudentDocumentImageError."""
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(raw), formats=list(_ALLOWED_FORMATS))
    except Image.DecompressionBombError:
        raise StudentDocumentImageError(MSG_TOO_LARGE) from None
    except Exception:
        raise StudentDocumentImageError(MSG_INVALID) from None

    with img:
        source_format = img.format
        width, height = img.size
        if width < 1 or height < 1:
            raise StudentDocumentImageError(MSG_INVALID)
        if width * height > STUDENT_DOC_MAX_PIXELS:
            raise StudentDocumentImageError(MSG_TOO_LARGE)
        if getattr(img, 'n_frames', 1) > 1:
            raise StudentDocumentImageError(MSG_ANIMATED)
        icc_profile = (img.info.get('icc_profile')
                       if img.mode in ('RGB', 'RGBA', 'P', 'PA') else None)
        try:
            if source_format == 'JPEG':
                # libjpeg decodes at a reduced scale never below the target
                # size: same result, far less memory for large photos.
                img.draft(img.mode, (STUDENT_DOC_IMAGE_MAX_SIDE, STUDENT_DOC_IMAGE_MAX_SIDE))
            img.load()                                    # full decode: corrupt -> error
            out = img
            if img.getexif().get(0x0112, 1) != 1:
                out = ImageOps.exif_transpose(img)        # copy only when rotating
            target = 'RGBA' if _has_alpha(out) else 'RGB'
            if out.mode != target:
                out = out.convert(target)
            elif out is img:
                out = img.copy()                          # detach from the file
        except StudentDocumentImageError:
            raise
        except Exception:
            raise StudentDocumentImageError(MSG_INVALID) from None

    if max(out.size) > STUDENT_DOC_IMAGE_MAX_SIDE:
        out.thumbnail((STUDENT_DOC_IMAGE_MAX_SIDE, STUDENT_DOC_IMAGE_MAX_SIDE),
                      Image.Resampling.LANCZOS)          # keeps aspect, never upscales

    extra = {'icc_profile': icc_profile} if icc_profile else {}

    def _webp(**kw):
        buf = io.BytesIO()
        out.save(buf, format='WEBP', method=4, **extra, **kw)   # no exif/xmp -> stripped
        return buf.getvalue()

    best_data, best_ext = _webp(quality=STUDENT_DOC_WEBP_QUALITY), 'webp'
    if len(best_data) > len(raw):
        # Flat graphics / simple scans can grow under lossy WebP.
        alt = _webp(lossless=True, quality=STUDENT_DOC_WEBP_QUALITY)
        if len(alt) < len(best_data):
            best_data = alt
        if source_format == 'PNG' and len(best_data) > len(raw):
            buf = io.BytesIO()
            out.save(buf, format='PNG', optimize=True, **extra)  # lossless, no EXIF
            if len(buf.getvalue()) < len(best_data):
                best_data, best_ext = buf.getvalue(), 'png'
    return ProcessedDocumentImage(best_data, best_ext, out.width, out.height)
