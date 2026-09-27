"""
School Board image optimisation — applied ONCE, at upload time.

Only School Board media uses this module; the generic save_uploaded_file()
helper and every other upload feature (students, employees, homework, leave,
AI Face) are untouched.

Pipeline for an uploaded board image (raw size already checked <= 5 MB):
  1. decode with Pillow, restricted to the formats the feature allows
     (JPEG, PNG, WEBP) — the actual bytes decide, not the filename/MIME;
  2. refuse oversized pixel counts BEFORE decoding pixel data (a tiny file
     can declare enormous dimensions), and refuse animated images;
  3. apply the EXIF orientation, then drop EXIF/XMP (an RGB ICC colour
     profile is kept so colours render correctly);
  4. shrink so the longest side is <= 1600 px (never upscale), LANCZOS;
  5. encode WebP quality 80 (method 4), keeping an alpha channel only when
     the source has transparency; if that comes out larger than the upload
     (flat graphics), one lossless WebP attempt and the smaller one wins.

Only the optimised bytes are stored; the original upload never is.
Uses Pillow APIs available in the pinned Pillow 10.3.0.
"""
from __future__ import annotations

import io
from typing import NamedTuple

BOARD_IMAGE_MAX_SIDE = 1600          # longest stored side, px
BOARD_IMAGE_WEBP_QUALITY = 80
BOARD_IMAGE_MAX_PIXELS = 40_000_000  # decode guard (e.g. 8000 x 5000)
_ALLOWED_FORMATS = ('JPEG', 'PNG', 'WEBP')

MSG_INVALID = 'تعذّر قراءة الصورة. يرجى رفع صورة صالحة بصيغة jpg أو png أو webp.'
MSG_TOO_LARGE = 'أبعاد الصورة كبيرة جداً. يرجى رفع صورة بأبعاد أصغر.'
MSG_ANIMATED = 'الصور المتحركة غير مدعومة. يرجى رفع صورة ثابتة.'


class BoardImageError(ValueError):
    """Upload refused; ``str(exc)`` is a user-facing Arabic message."""


class OptimizedImage(NamedTuple):
    data: bytes
    width: int
    height: int
    source_format: str
    lossless: bool


def _has_alpha(img) -> bool:
    return (img.mode in ('RGBA', 'LA', 'PA')
            or (img.mode in ('P', 'L', 'RGB') and 'transparency' in img.info))


def optimize_board_image(raw: bytes) -> OptimizedImage:
    """Validate and optimise one board image. Raises BoardImageError."""
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(raw), formats=list(_ALLOWED_FORMATS))
    except Exception:
        raise BoardImageError(MSG_INVALID) from None

    with img:
        source_format = img.format
        width, height = img.size
        if width < 1 or height < 1:
            raise BoardImageError(MSG_INVALID)
        if width * height > BOARD_IMAGE_MAX_PIXELS:
            raise BoardImageError(MSG_TOO_LARGE)
        if getattr(img, 'n_frames', 1) > 1:
            raise BoardImageError(MSG_ANIMATED)
        # Keep the colour profile only for RGB-space sources: a CMYK or grey
        # profile would be wrong on the RGB/RGBA output.
        icc_profile = (img.info.get('icc_profile')
                       if img.mode in ('RGB', 'RGBA', 'P', 'PA') else None)
        try:
            if source_format == 'JPEG':
                # Let libjpeg decode at a reduced scale (never below the
                # target size) — same result, far less memory for big photos.
                img.draft(img.mode, (BOARD_IMAGE_MAX_SIDE, BOARD_IMAGE_MAX_SIDE))
            img.load()                                   # full decode: corrupt -> error
            oriented = ImageOps.exif_transpose(img)      # returns a new image
            out = oriented.convert('RGBA' if _has_alpha(oriented) else 'RGB')
        except BoardImageError:
            raise
        except Exception:
            raise BoardImageError(MSG_INVALID) from None

    if max(out.size) > BOARD_IMAGE_MAX_SIDE:
        out.thumbnail((BOARD_IMAGE_MAX_SIDE, BOARD_IMAGE_MAX_SIDE),
                      Image.Resampling.LANCZOS)          # keeps aspect, never upscales

    extra = {'icc_profile': icc_profile} if icc_profile else {}

    def _encode(**kw):
        buf = io.BytesIO()
        out.save(buf, format='WEBP', method=4, **extra, **kw)   # no exif/xmp -> stripped
        return buf.getvalue()

    data = _encode(quality=BOARD_IMAGE_WEBP_QUALITY)
    lossless = False
    if len(data) > len(raw):
        # Flat graphics (logos, simple PNG posters) can grow under lossy
        # WebP; one lossless attempt, keep whichever is smaller.
        alt = _encode(lossless=True, quality=BOARD_IMAGE_WEBP_QUALITY)
        if len(alt) < len(data):
            data, lossless = alt, True
    return OptimizedImage(data, out.width, out.height, source_format, lossless)
