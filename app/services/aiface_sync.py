"""
Shared AI Face device sync helper.

sync_person_to_device() is called by the attendance_devices blueprint
for both student and employee syncs so the photo normalisation and
setuserinfo / deleteuser / enableuser logic lives in exactly one place.
"""
import base64
import io
import logging
import os

log = logging.getLogger('aiface_sync')

# ─────────────────────────────────────────────────────────────────────────────
#  Photo helper
# ─────────────────────────────────────────────────────────────────────────────

def prepare_photo_for_device(photo, label: str = '') -> tuple:
    """
    Load a photo (Supabase object URL, external URL, or local path),
    EXIF-rotate, resize ≤ 640 px, return (jpeg_bytes_or_None, info_dict).
    info_dict always has diagnostic keys.

    Supabase-stored values are fetched server-side with the service key
    (private buckets reject plain GETs of the stored public-shaped URL).

    `label` is an optional string used only in log messages.
    """
    info: dict = {'photo': photo or '', 'label': label}

    if not photo:
        info['error'] = 'no_photo'
        log.info('[aiface_sync] %s: no photo', label)
        return None, info

    raw_bytes: bytes | None = None

    # Supabase-stored values first: production rows hold full URLs shaped
    # .../storage/v1/object/public/<bucket>/<key>. Since the private-bucket
    # cutover a plain GET of that stored URL returns 400, so the object must
    # be fetched server-side with the service key. _supabase_fetch tries the
    # authenticated endpoint first and falls back to the public endpoint, so
    # it works in both bucket states.
    from app.utils.upload_access import storage_ref_of, _local_ref_of
    from app.utils.helpers import _supabase_fetch

    ref = storage_ref_of(photo)
    if ref is not None:
        bucket, object_path = ref
        info['source'] = 'supabase'
        info['resolved'] = f'{bucket}/{object_path}'
        raw_bytes, _ = _supabase_fetch(object_path, bucket=bucket)
        if raw_bytes:
            log.info('[aiface_sync] %s: fetched supabase object %s/%s %d bytes',
                     label, bucket, object_path, len(raw_bytes))
        else:
            log.warning('[aiface_sync] %s: supabase fetch failed for %s/%s — '
                        'falling back to direct URL', label, bucket, object_path)

    if raw_bytes is None and photo.startswith(('http://', 'https://')):
        info['source'] = 'url'
        try:
            import requests as _req
            from datetime import datetime as _dt
            sep = '&' if '?' in photo else '?'
            url = f"{photo}{sep}_cb={int(_dt.utcnow().timestamp())}"
            info['resolved'] = url
            resp = _req.get(url, timeout=10)
            if resp.status_code == 200:
                raw_bytes = resp.content
                log.info('[aiface_sync] %s: fetched URL %d bytes', label, len(raw_bytes))
            else:
                info['error'] = f'http_{resp.status_code}'
                log.warning('[aiface_sync] %s: HTTP %d for URL %s', label, resp.status_code, photo)
                return None, info
        except Exception as exc:
            info['error'] = f'fetch_failed:{exc}'
            log.warning('[aiface_sync] %s: fetch failed: %s', label, exc)
            return None, info
    elif raw_bytes is None:
        info['source'] = 'local'
        from flask import current_app
        full_path = os.path.join(current_app.root_path, 'static', photo)
        info['resolved'] = full_path
        if os.path.isfile(full_path):
            with open(full_path, 'rb') as fh:
                raw_bytes = fh.read()
            log.info('[aiface_sync] %s: read local file %d bytes', label, len(raw_bytes))
        else:
            # Local file missing (ephemeral fs after redeploy): legacy relative
            # rows keep their bytes in the uploads bucket under the same key.
            lref = _local_ref_of(photo)
            if lref is not None:
                bucket, object_path = lref
                raw_bytes, _ = _supabase_fetch(object_path, bucket=bucket)
            if raw_bytes:
                info['source'] = 'supabase_uploads_fallback'
                info['resolved'] = f'{lref[0]}/{lref[1]}'
                log.info('[aiface_sync] %s: local file missing — fetched supabase '
                         'object %s/%s %d bytes', label, lref[0], lref[1], len(raw_bytes))
            else:
                info['error'] = 'file_not_found'
                log.warning('[aiface_sync] %s: file not found: %s', label, full_path)
                return None, info

    # ── Pillow normalisation ──────────────────────────────────────────────────
    try:
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(raw_bytes))
        info['orig_size'] = f'{img.width}x{img.height}'
        img = ImageOps.exif_transpose(img)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        if img.width > 640 or img.height > 640:
            img.thumbnail((640, 640), Image.LANCZOS)
        info['norm_size'] = f'{img.width}x{img.height}'
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=85, optimize=True)
        jpeg_bytes = buf.getvalue()
        info['norm_bytes'] = len(jpeg_bytes)
        log.info('[aiface_sync] %s: normalised → %s  %d bytes',
                 label, info['norm_size'], info['norm_bytes'])
        return jpeg_bytes, info
    except Exception as exc:
        info['error'] = f'image_decode_failed:{exc}'
        log.exception('[aiface_sync] %s: Pillow normalisation failed', label)
        return None, info


# ─────────────────────────────────────────────────────────────────────────────
#  Device sync
# ─────────────────────────────────────────────────────────────────────────────

def sync_person_to_device(device, enrollid: int, name: str, photo,
                           entity_type: str = 'student') -> dict:
    """
    Push one person (student or employee) to an AI Face device.

    Steps:
      1. deleteuser(enrollid)        — force clean re-enroll
                                       (non-fatal for TimeoutError and generic errors;
                                        FATAL only if device is offline at this point)
      2. prepare_photo_for_device()  — EXIF-rotate, resize, JPEG
      3. setuserinfo(enrollid, name, backupnum=50, record=<b64>)
      4. enableuser(enrollid)        — enable face access (non-fatal)

    Returns a comprehensive dict with ok, message, error_message_ar, device_sn,
    enrollid, entity_type, backupnum, photo_info, deleteuser_result,
    setuserinfo_result, enableuser_result, connected_sns_at_sync.
    Raises nothing — all exceptions are caught and returned in the dict.
    """
    from app.services.ai_face_ws import (send_command_to_device, DeviceOfflineError,
                                          get_connected_sns)

    sn    = device.device_sn
    label = f'{entity_type}_{enrollid}'

    connected_sns = get_connected_sns()
    log.info(
        '[aiface_cmd] sync start device_id=%s device_sn=%s device_scope=%s '
        'entity_type=%s enrollid=%d connected_sns=%s',
        getattr(device, 'id', '?'), sn, getattr(device, 'device_scope', '?'),
        entity_type, enrollid, connected_sns,
    )

    base = {
        'device_sn':            sn,
        'enrollid':             enrollid,
        'entity_type':          entity_type,
        'name':                 name,
        'connected_sns_at_sync': connected_sns,
    }

    # ── Step 1: deleteuser ────────────────────────────────────────────────────
    # DeviceOfflineError → FAIL immediately (device is not connected at all).
    # TimeoutError or other exceptions → non-fatal, log and continue to setuserinfo.
    deleteuser_result = None
    del_cmd = {'cmd': 'deleteuser', 'enrollid': enrollid}
    log.info('[aiface_cmd] send cmd=deleteuser sn=%s enrollid=%d', sn, enrollid)
    try:
        deleteuser_result = send_command_to_device(sn, del_cmd, timeout=8)
        log.info('[aiface_cmd] response cmd=deleteuser ret=%s result=%s full=%s',
                 deleteuser_result.get('ret'), deleteuser_result.get('result'), deleteuser_result)
    except DeviceOfflineError:
        return {
            **base,
            'ok': False, 'offline': True, 'error_type': 'device_offline',
            'error_message_ar': (
                'الجهاز غير متصل حالياً — تأكد من اتصال الجهاز بالشبكة '
                f'وأن الرقم التسلسلي ({sn}) صحيح. '
                f'الأجهزة المتصلة الآن: {connected_sns or "لا يوجد"}'
            ),
            'message': 'الجهاز غير متصل حالياً',
        }
    except TimeoutError:
        log.warning(
            '[aiface_cmd] timeout cmd=deleteuser sn=%s enrollid=%d — non-fatal, continuing to setuserinfo',
            sn, enrollid)
        deleteuser_result = {
            'result': None, 'timeout': True,
            'note': 'non-fatal — continued to setuserinfo',
        }
    except Exception as exc:
        log.warning('[aiface_cmd] deleteuser error enrollid=%d sn=%s: %s — non-fatal, continuing',
                    enrollid, sn, exc)
        deleteuser_result = {
            'result': None, 'error': str(exc),
            'note': 'non-fatal — continued to setuserinfo',
        }

    # ── Step 2: prepare photo ─────────────────────────────────────────────────
    jpeg_bytes, photo_info = prepare_photo_for_device(photo, label)

    cmd: dict = {
        'cmd':      'setuserinfo',
        'enrollid': enrollid,
        'name':     name,
        'admin':    0,
    }
    backupnum = None
    if jpeg_bytes:
        backupnum = 50
        cmd['backupnum'] = backupnum
        cmd['record']    = base64.b64encode(jpeg_bytes).decode('ascii')
        b64_len = len(cmd['record'])
        log.info('[aiface_cmd] send cmd=setuserinfo sn=%s enrollid=%d backupnum=%d b64_len=%d',
                 sn, enrollid, backupnum, b64_len)
    else:
        log.info('[aiface_cmd] send cmd=setuserinfo sn=%s enrollid=%d (no photo error=%s)',
                 sn, enrollid, photo_info.get('error', 'unknown'))

    # ── Step 3: setuserinfo ───────────────────────────────────────────────────
    setuserinfo_result = None
    try:
        setuserinfo_result = send_command_to_device(sn, cmd, timeout=25)
        log.info('[aiface_cmd] response cmd=setuserinfo result=%s full=%s',
                 setuserinfo_result.get('result'), setuserinfo_result)
    except DeviceOfflineError:
        return {
            **base,
            'ok': False, 'offline': True, 'error_type': 'device_offline',
            'error_message_ar': 'انقطع اتصال الجهاز أثناء إرسال بيانات المستخدم',
            'message': 'انقطع اتصال الجهاز أثناء setuserinfo',
            'backupnum': backupnum,
            'deleteuser_result': deleteuser_result,
        }
    except TimeoutError as exc:
        return {
            **base,
            'ok': False, 'error_type': 'setuserinfo_timeout',
            'error_message_ar': (
                'لم يرد الجهاز خلال 25 ثانية عند إرسال بيانات المستخدم — '
                'قد تكون الصورة كبيرة جداً أو هناك ضغط على الشبكة'
            ),
            'message': f'انتهت مهلة setuserinfo: {exc}',
            'backupnum': backupnum,
            'deleteuser_result': deleteuser_result,
        }
    except Exception as exc:
        log.exception('[aiface_sync] unexpected error on setuserinfo enrollid=%d', enrollid)
        return {
            **base,
            'ok': False, 'error_type': 'internal_error',
            'error_message_ar': 'خطأ داخلي أثناء إرسال البيانات للجهاز',
            'message': str(exc),
            'backupnum': backupnum,
            'deleteuser_result': deleteuser_result,
        }

    if not setuserinfo_result.get('result'):
        return {
            **base,
            'ok': False, 'error_type': 'device_rejected_user',
            'error_message_ar': (
                'الجهاز رفض بيانات المستخدم (setuserinfo result=false) — '
                'تأكد من صلاحية الصورة وتنسيقها، أو جرب بدون صورة'
            ),
            'message': f'الجهاز رفض إرسال بيانات {name} (enrollid={enrollid})',
            'backupnum': backupnum,
            'deleteuser_result': deleteuser_result,
            'setuserinfo_result': setuserinfo_result,
        }

    # ── Step 4: enableuser — activate the user for face recognition ───────────
    # Non-fatal: failure here does not roll back the successful setuserinfo.
    enableuser_result = None
    enable_cmd = {'cmd': 'enableuser', 'enrollid': enrollid, 'enflag': 1}
    log.info('[aiface_cmd] send cmd=enableuser enrollid=%d sn=%s', enrollid, sn)
    try:
        enableuser_result = send_command_to_device(sn, enable_cmd, timeout=10)
        log.info('[aiface_cmd] response cmd=enableuser result=%s full=%s',
                 enableuser_result.get('result'), enableuser_result)
        if not enableuser_result.get('result'):
            log.warning('[aiface_cmd] enableuser returned result=false enrollid=%d — '
                        'user may show red-box on device; sync still succeeded', enrollid)
    except DeviceOfflineError:
        log.warning('[aiface_cmd] enableuser: device went offline enrollid=%d — non-fatal', enrollid)
        enableuser_result = {'result': None, 'offline': True, 'note': 'non-fatal'}
    except TimeoutError:
        log.warning('[aiface_cmd] timeout cmd=enableuser enrollid=%d sn=%s — non-fatal', enrollid, sn)
        enableuser_result = {'result': None, 'timeout': True, 'note': 'non-fatal'}
    except Exception as exc:
        log.warning('[aiface_cmd] enableuser error enrollid=%d: %s — non-fatal', enrollid, exc)
        enableuser_result = {'result': None, 'error': str(exc), 'note': 'non-fatal'}

    msg = f'تم إرسال {name} (رقم {enrollid}) للجهاز بنجاح'
    if not jpeg_bytes:
        msg += f' — (بدون صورة: {photo_info.get("error", "unknown")})'
    if enableuser_result and not enableuser_result.get('result'):
        msg += ' — تنبيه: enableuser لم ينجح، قد يظهر الوجه بإطار أحمر'

    safe_photo_info = {k: v for k, v in photo_info.items() if k != 'photo'}

    return {
        **base,
        'ok': True,
        'message': msg,
        'backupnum': backupnum,
        'photo_info': safe_photo_info,
        'deleteuser_result': deleteuser_result,
        'setuserinfo_result': setuserinfo_result,
        'enableuser_result': enableuser_result,
    }
