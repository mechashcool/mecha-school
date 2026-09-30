"""
Legacy display-photo backfill — standalone maintenance tool.

Creates the display-only derivative (``photo_display``) for EXISTING students
and employees that were uploaded before derivatives existed, using exactly the
helpers new uploads use:

    students   → app.utils.student_display_photo.make_display_photo
    employees  → app.utils.employee_display_photo.make_employee_display_photo

Nothing here is imported by create_app(), exposed over HTTP, or started by
Gunicorn. It runs only when an operator invokes it:

    python -m app.services.display_photo_backfill --entity both            # DRY RUN
    python -m app.services.display_photo_backfill --entity employees \\
        --school-id 175 --batch-size 10 --apply --confirm-storage-host <host>

Safety contract
───────────────
* DRY RUN is the default. Without ``--apply`` the tool performs no Storage
  download, upload or delete and no database write: every transaction it opens
  is ``SET TRANSACTION READ ONLY``. It reads candidate rows and
  ``storage.objects`` metadata only, and reports what WOULD happen.
* The ORIGINAL (``photo``) is never updated, replaced, moved or deleted. It is
  the only AI Face / Face ID source. The one column this tool writes is
  ``photo_display``, and only while it is still NULL/empty.
* A derivative is accepted only as a confirmed Supabase object: the upload
  response must be the exact expected public URL AND ``storage.objects`` must
  hold the object with the uploaded size. ``save_uploaded_file`` (which falls
  back to local disk) is deliberately NOT used.
* Every write is per record: SELECT … FOR UPDATE by id + school_id, re-check
  that ``photo`` is unchanged and ``photo_display`` is still empty, update
  ``photo_display`` only, commit. A lost race rolls back and removes only the
  object this run just created.
* Cleanup may only ever delete a key matching THIS run's backfill prefix
  (``<entity>/display/bf<run_id>-<uuid>.webp``) and only after explicitly
  checking that neither ``students`` nor ``employees`` (photo or
  photo_display) references it. ``resolve_upload_owner`` is not used for this
  decision because it does not map ``Student.photo_display``.
* Relative/local originals are skipped (``local_original_unverified``) unless
  ``--include-local`` is given together with ``--expected-app-root`` equal to
  this application's real root on a POSIX host, and the file exists.
* Every run writes a restricted manifest (JSON lines, mode 0600) with ids,
  school ids, stored values, outcomes and sizes — never names or contact data.

Writing ``photo_display`` through the ORM also advances ``updated_at``
(its ``onupdate``); that is expected and intentionally not suppressed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

log = logging.getLogger('mecha.display_backfill')

# ── Outcomes ──────────────────────────────────────────────────────────────────
OK = 'ok'
WOULD_PROCESS = 'would_process'                    # dry run only
ORIGINAL_MISSING = 'original_missing'
LOCAL_ORIGINAL_UNVERIFIED = 'local_original_unverified'
DECODE_FAILED = 'decode_failed'
UNSUPPORTED_IMAGE = 'unsupported_image'
OVER_PIXEL_LIMIT = 'over_pixel_limit'
STORAGE_FETCH_FAILED = 'storage_fetch_failed'
STORAGE_UPLOAD_FAILED = 'storage_upload_failed'
CONFLICT_SKIPPED = 'conflict_skipped'
DB_UPDATE_FAILED = 'db_update_failed'
ORPHAN_CANDIDATE = 'orphan_candidate'
UNEXPECTED_ERROR = 'unexpected_error'
UNSUPPORTED_ORIGINAL_REF = 'unsupported_original_ref'
STORAGE_HOST_MISMATCH = 'storage_host_mismatch'
METADATA_UNAVAILABLE = 'metadata_unavailable'      # dry run only

#: Outcomes that are expected, record-level skips — never counted as errors.
SKIP_OUTCOMES = frozenset({LOCAL_ORIGINAL_UNVERIFIED, CONFLICT_SKIPPED})
NON_ERROR_OUTCOMES = frozenset({OK, WOULD_PROCESS}) | SKIP_OUTCOMES

EXIT_OK, EXIT_REFUSED, EXIT_WITH_ERRORS, EXIT_MAX_ERRORS = 0, 2, 3, 4

_PUBLIC_MARKER = '/storage/v1/object/public/'


class ConfigError(RuntimeError):
    """The run is refused before touching anything (fail closed)."""


@dataclass(frozen=True)
class EntitySpec:
    name: str
    subfolder: str

    @property
    def model(self):
        from app.models import Employee, Student
        return Student if self.name == 'students' else Employee

    def derive(self, raw: bytes) -> bytes:
        # Looked up at call time: the EXISTING helpers, never a copy of them.
        if self.name == 'students':
            from app.utils import student_display_photo
            return student_display_photo.make_display_photo(raw)
        from app.utils import employee_display_photo
        return employee_display_photo.make_employee_display_photo(raw)


def _specs():
    from app.utils.employee_display_photo import EMPLOYEE_DISPLAY_SUBFOLDER
    from app.utils.student_display_photo import STUDENT_DISPLAY_SUBFOLDER
    return {'students': EntitySpec('students', STUDENT_DISPLAY_SUBFOLDER),
            'employees': EntitySpec('employees', EMPLOYEE_DISPLAY_SUBFOLDER)}


def _has_value(value) -> bool:
    return value is not None and str(value).strip() != ''


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# ── Original classification (no I/O) ─────────────────────────────────────────

@dataclass(frozen=True)
class Original:
    kind: str                   # 'supabase' | 'local' | 'unsupported'
    bucket: str | None = None
    key: str | None = None
    host: str | None = None
    rel: str | None = None      # local: 'uploads/...'


def classify_original(value: str) -> Original:
    from app.utils.upload_access import object_path_of, storage_ref_of
    v = (value or '').strip()
    if v.startswith(('http://', 'https://')):
        ref = storage_ref_of(v)
        if ref is None or _PUBLIC_MARKER not in v:
            return Original('unsupported')
        return Original('supabase', bucket=ref[0], key=ref[1],
                        host=urlsplit(v).netloc.lower())
    op = object_path_of(v)
    if op and op.startswith('uploads/'):
        parts = op.split('/')
        if len(parts) >= 2 and not any(p in ('', '.', '..') for p in parts) \
                and '\\' not in op and ':' not in op:
            return Original('local', rel=op)
    return Original('unsupported')


# ── Storage metadata (database reads of storage.objects — no egress) ──────────

class StorageObjectsMetadata:
    """Object existence/size from Supabase's ``storage.objects`` table."""

    def __init__(self, db):
        self.db = db
        self._available = None

    def available(self) -> bool:
        if self._available is None:
            from sqlalchemy import text
            try:
                reg = self.db.session.execute(
                    text("select to_regclass('storage.objects')")).scalar()
                if reg is not None:
                    self.db.session.execute(text('select 1 from storage.objects limit 0'))
                self._available = reg is not None
            except Exception:
                self._available = False
            finally:
                self.db.session.rollback()
        return self._available

    def sizes(self, pairs) -> dict:
        """{(bucket, key): size_or_None} for the objects that exist."""
        from sqlalchemy import text
        by_bucket: dict = {}
        for bucket, key in pairs:
            by_bucket.setdefault(bucket, set()).add(key)
        found = {}
        for bucket, keys in by_bucket.items():
            rows = self.db.session.execute(
                text("select name, (metadata->>'size')::bigint from storage.objects "
                     "where bucket_id = :b and name = any(:names)"),
                {'b': bucket, 'names': sorted(keys)}).all()
            for name, size in rows:
                found[(bucket, name)] = size
        return found


# ── Storage operations (apply mode only) ─────────────────────────────────────

class SupabaseStorage:
    """Thin counting wrapper over the existing Supabase helpers."""

    def __init__(self):
        self.fetches = self.uploads = self.deletes = 0
        self.fetched_bytes = 0

    def fetch(self, key: str, bucket: str) -> bytes | None:
        from app.utils import helpers
        self.fetches += 1
        raw, _ = helpers._supabase_fetch(key, bucket=bucket)
        if raw:
            self.fetched_bytes += len(raw)
        return raw

    def upload(self, data: bytes, key: str, bucket: str) -> str | None:
        from app.utils import helpers
        self.uploads += 1
        return helpers._supabase_upload(data, key, 'image/webp', bucket=bucket)

    def delete(self, key: str, bucket: str) -> bool:
        from app.utils import helpers
        self.deletes += 1
        return bool(helpers._supabase_delete(key, bucket=bucket))


class NoStorage:
    """Dry-run storage: any call is a programming error and is refused."""
    fetches = uploads = deletes = fetched_bytes = 0

    def _refuse(self, *_a, **_k):
        raise RuntimeError('storage access is not allowed in dry-run mode')

    fetch = upload = delete = _refuse


# ── Manifest ──────────────────────────────────────────────────────────────────

class Manifest:
    """Append-only JSON-lines manifest, flushed per record, mode 0600."""

    def __init__(self, directory: str, run_id: str, mode: str):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        base = os.path.join(directory, f'display-photo-backfill-{run_id}-{mode}')
        self.path = base + '.jsonl'
        self.summary_path = base + '.summary.json'
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._fh = os.fdopen(fd, 'w', encoding='utf-8')

    def write(self, record: dict) -> None:
        self._fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def finish(self, summary: dict) -> None:
        self._fh.close()
        fd = os.open(self.summary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2, sort_keys=True)


# ── Runner ───────────────────────────────────────────────────────────────────

class BackfillRunner:
    """Must be constructed and run inside an application context."""

    def __init__(self, *, entities, apply=False, school_id=None,
                 exclude_school_ids=(), limit=None, batch_size=25,
                 resume_after_id=None, max_errors=10, sleep_ms=0,
                 include_local=False, expected_app_root=None,
                 confirm_storage_host=None, manifest_dir=None,
                 metadata=None, storage=None, run_id=None):
        from flask import current_app
        from app.models import db
        self.app = current_app._get_current_object()
        self.db = db
        specs = _specs()
        self.entities = [specs[e] for e in entities]
        self.apply = bool(apply)
        self.school_id = school_id
        self.exclude_school_ids = tuple(exclude_school_ids or ())
        self.limit = limit
        self.batch_size = batch_size
        self.resume_after_id = resume_after_id
        self.max_errors = max_errors
        self.sleep_ms = sleep_ms
        self.include_local = include_local
        self.expected_app_root = expected_app_root
        self.confirm_storage_host = confirm_storage_host
        self.run_id = run_id or datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
        self.prefix = f'bf{self.run_id}'
        self.key_re = re.compile(
            r'^(students|employees)/display/' + re.escape(self.prefix)
            + r'-[0-9a-f]{32}\.webp$')
        self.metadata = metadata if metadata is not None else StorageObjectsMetadata(db)
        self.storage = storage if storage is not None else (
            SupabaseStorage() if self.apply else NoStorage())
        self.manifest_dir = manifest_dir or os.path.join(
            os.path.dirname(self.app.root_path), 'instance', 'display_photo_backfill')
        cfg = self.app.config
        self.base_url = (cfg.get('SUPABASE_URL') or '').rstrip('/')
        self.storage_host = urlsplit(self.base_url).netloc.lower() if self.base_url else None
        self.upload_bucket = cfg.get('SUPABASE_BUCKET', 'uploads')
        self.original_buckets = {cfg.get('SUPABASE_BUCKET', 'uploads'),
                                 cfg.get('SUPABASE_STORAGE_BUCKET_MEDIA', 'school-media')}
        self.max_original_bytes = int(cfg.get('MAX_CONTENT_LENGTH') or 16 * 1024 * 1024)
        self.db_writes = 0
        self.errors = 0
        self.processed = 0
        self.stopped_reason = None
        self._read_only_listener = None

    # ── validation (fail closed, before anything is touched) ──────────────────

    def _validate(self):
        if self.batch_size is None or self.batch_size < 1:
            raise ConfigError('--batch-size must be >= 1')
        if self.max_errors is None or self.max_errors < 1:
            raise ConfigError('--max-errors must be >= 1')
        if self.limit is not None and self.limit < 1:
            raise ConfigError('--limit must be >= 1')
        if self.sleep_ms is None or self.sleep_ms < 0:
            raise ConfigError('--sleep-ms must be >= 0')
        if self.resume_after_id is not None and len(self.entities) != 1:
            raise ConfigError('--resume-after-id requires a single --entity')
        if self.include_local:
            root = os.path.realpath(self.app.root_path)
            if os.name != 'posix':
                raise ConfigError('--include-local is only allowed on the POSIX server')
            if not self.expected_app_root or \
                    os.path.realpath(self.expected_app_root) != root:
                raise ConfigError('--include-local requires --expected-app-root equal '
                                  "to this application's real root")
        if self.apply:
            if not self.base_url or not self.app.config.get('SUPABASE_SERVICE_KEY'):
                raise ConfigError('--apply requires SUPABASE_URL and the service key')
            if (self.confirm_storage_host or '').strip().lower() != self.storage_host:
                raise ConfigError('--confirm-storage-host does not match the configured '
                                  'Supabase host')
            if not self.metadata.available():
                raise ConfigError('--apply requires readable storage.objects metadata')

    # ── read-only enforcement for dry runs ────────────────────────────────────

    def _enforce_read_only(self):
        from sqlalchemy import event
        session = self.db.session()

        def _set_read_only(_session, _transaction, connection):
            connection.exec_driver_sql('SET TRANSACTION READ ONLY')

        self.db.session.rollback()
        event.listen(session, 'after_begin', _set_read_only)
        self._read_only_listener = (session, _set_read_only)

    def _release_read_only(self):
        if self._read_only_listener:
            from sqlalchemy import event
            session, fn = self._read_only_listener
            self.db.session.rollback()
            event.remove(session, 'after_begin', fn)
            self._read_only_listener = None

    # ── queries (explicitly scoped; tenant filters bypassed on purpose) ───────

    _OPTS = {'bypass_tenant_scope': True, 'include_all_years': True}

    def _scope(self, query, model):
        if self.school_id is not None:
            query = query.filter(model.school_id == self.school_id)
        if self.exclude_school_ids:
            query = query.filter(model.school_id.notin_(self.exclude_school_ids))
        return query

    def _candidate_filter(self, query, model):
        from sqlalchemy import func, or_
        return (query.filter(model.photo.isnot(None), func.btrim(model.photo) != '')
                .filter(or_(model.photo_display.is_(None),
                            func.btrim(model.photo_display) == '')))

    def _candidates(self, spec, after_id, size):
        model = spec.model
        q = (self.db.session.query(model.id, model.school_id, model.photo)
             .execution_options(**self._OPTS))
        q = self._candidate_filter(self._scope(q, model), model)
        return q.filter(model.id > after_id).order_by(model.id).limit(size).all()

    def _already_has_display(self, spec) -> int:
        from sqlalchemy import func
        model = spec.model
        q = (self.db.session.query(func.count(model.id)).execution_options(**self._OPTS)
             .filter(model.photo.isnot(None), func.btrim(model.photo) != '')
             .filter(model.photo_display.isnot(None), func.btrim(model.photo_display) != ''))
        return self._scope(q, model).scalar() or 0

    def _current(self, spec, cid, sid):
        model = spec.model
        row = (self.db.session.query(model.photo, model.photo_display)
               .execution_options(**self._OPTS)
               .filter(model.id == cid, model.school_id == sid).first())
        self.db.session.rollback()
        return row

    def _lock(self, spec, cid, sid):
        from sqlalchemy.orm import load_only
        model = spec.model
        return (self.db.session.query(model)
                .execution_options(**self._OPTS)
                .options(load_only(model.id, model.school_id, model.photo,
                                   model.photo_display))
                .filter(model.id == cid, model.school_id == sid)
                .populate_existing()
                .with_for_update(of=model)
                .first())

    def _commit(self):
        self.db.session.commit()

    def _reference_count(self, url: str, key: str) -> int:
        """Rows in BOTH tables (photo or photo_display) naming this object."""
        from sqlalchemy import func, or_
        from app.models import Employee, Student
        total = 0
        for model in (Student, Employee):
            conds = []
            for col in (model.photo, model.photo_display):
                conds.append(col == url)
                conds.append(col.endswith('/' + key, autoescape=True))
                conds.append(col == key)
            total += (self.db.session.query(func.count(model.id))
                      .execution_options(**self._OPTS)
                      .filter(or_(*conds)).scalar() or 0)
        self.db.session.rollback()
        return total

    # ── cleanup of objects created by THIS run only ───────────────────────────

    def _object_url(self, key: str) -> str:
        return f'{self.base_url}{_PUBLIC_MARKER}{self.upload_bucket}/{key}'

    def _cleanup(self, key: str) -> str:
        """'deleted' | 'not_created' | 'kept_referenced' | 'delete_failed' | 'refused'."""
        if not key or not self.key_re.match(key):
            return 'refused'                      # never an original, never another run
        try:
            if self._reference_count(self._object_url(key), key):
                return 'kept_referenced'
            exists = self.metadata.sizes([(self.upload_bucket, key)])
            self.db.session.rollback()
            if (self.upload_bucket, key) not in exists:
                return 'not_created'
            return 'deleted' if self.storage.delete(key, self.upload_bucket) else 'delete_failed'
        except Exception:
            try:
                self.db.session.rollback()
            except Exception:
                pass
            log.warning('[display-backfill] cleanup check failed', exc_info=True)
            return 'delete_failed'

    def _finish_with_cleanup(self, rec, key, outcome, error_code):
        cleanup = self._cleanup(key)
        rec['cleanup'] = cleanup
        if cleanup in ('delete_failed', 'refused'):
            rec['outcome'], rec['error_code'] = ORPHAN_CANDIDATE, outcome
        else:
            rec['outcome'], rec['error_code'] = outcome, error_code
        return rec

    # ── per-record processing ─────────────────────────────────────────────────

    def _record(self, spec, cand):
        return {'run_id': self.run_id, 'mode': 'apply' if self.apply else 'dry_run',
                'entity': spec.name, 'id': cand.id, 'school_id': cand.school_id,
                'original_value': cand.photo, 'source': None,
                'new_display_value': None, 'new_object_key': None,
                'outcome': None, 'error_code': None, 'cleanup': None,
                'original_size_bytes': None, 'display_size_bytes': None,
                'timestamp': _now()}

    def _local_path(self, rel):
        return os.path.join(self.app.root_path, 'static', *rel.split('/'))

    def _classify_skip(self, rec, orig, sizes):
        """Outcome for records that must not be read, or None to proceed."""
        rec['source'] = orig.kind
        if orig.kind == 'unsupported':
            return UNSUPPORTED_ORIGINAL_REF, 'not_a_public_storage_or_upload_value'
        if orig.kind == 'local':
            if not self.include_local:
                return LOCAL_ORIGINAL_UNVERIFIED, 'include_local_not_set'
            path = self._local_path(orig.rel)
            if not os.path.isfile(path):
                return ORIGINAL_MISSING, 'local_file_missing'
            rec['original_size_bytes'] = os.path.getsize(path)
            if rec['original_size_bytes'] > self.max_original_bytes:
                return UNSUPPORTED_IMAGE, 'original_too_large'
            return None
        if orig.bucket not in self.original_buckets:
            return UNSUPPORTED_ORIGINAL_REF, 'unexpected_bucket'
        if self.storage_host and orig.host != self.storage_host:
            return STORAGE_HOST_MISMATCH, 'original_host_differs_from_config'
        if sizes is None:
            return METADATA_UNAVAILABLE, 'storage_objects_unreadable'
        if (orig.bucket, orig.key) not in sizes:
            return ORIGINAL_MISSING, 'no_storage_object'
        rec['original_size_bytes'] = sizes[(orig.bucket, orig.key)]
        if (rec['original_size_bytes'] or 0) > self.max_original_bytes:
            return UNSUPPORTED_IMAGE, 'original_too_large'
        return None

    def _dry_one(self, spec, cand, sizes):
        rec = self._record(spec, cand)
        skip = self._classify_skip(rec, classify_original(cand.photo), sizes)
        rec['outcome'], rec['error_code'] = skip if skip else (WOULD_PROCESS, None)
        return rec

    def _derive(self, spec, raw):
        from PIL import Image, UnidentifiedImageError
        try:
            data = spec.derive(raw)
        except Image.DecompressionBombError:
            return None, OVER_PIXEL_LIMIT, 'decompression_bomb'
        except UnidentifiedImageError:
            return None, UNSUPPORTED_IMAGE, 'unidentified_image'
        except ValueError as exc:
            if 'unsupported dimensions' in str(exc):
                return None, OVER_PIXEL_LIMIT, 'unsupported_dimensions'
            return None, DECODE_FAILED, type(exc).__name__
        except Exception as exc:                  # truncated / corrupt data
            return None, DECODE_FAILED, type(exc).__name__
        if not data:
            return None, DECODE_FAILED, 'empty_output'
        return data, None, None

    def _apply_one(self, spec, cand, sizes):
        rec = self._record(spec, cand)
        try:
            return self._apply_steps(spec, cand, sizes, rec)
        except Exception as exc:
            try:
                self.db.session.rollback()
            except Exception:
                pass
            log.warning('[display-backfill] unexpected error entity=%s id=%s',
                        spec.name, cand.id, exc_info=True)
            if rec['new_object_key'] and rec['outcome'] != OK:
                return self._finish_with_cleanup(rec, rec['new_object_key'],
                                                 UNEXPECTED_ERROR, type(exc).__name__)
            rec['outcome'], rec['error_code'] = UNEXPECTED_ERROR, type(exc).__name__
            return rec

    def _apply_steps(self, spec, cand, sizes, rec):
        orig = classify_original(cand.photo)
        skip = self._classify_skip(rec, orig, sizes)
        if skip:
            rec['outcome'], rec['error_code'] = skip
            return rec

        # Skip before downloading when the row already moved on.
        current = self._current(spec, cand.id, cand.school_id)
        if current is None or current[0] != cand.photo or _has_value(current[1]):
            rec['outcome'], rec['error_code'] = CONFLICT_SKIPPED, 'changed_before_read'
            return rec

        if orig.kind == 'supabase':
            raw = self.storage.fetch(orig.key, orig.bucket)
            if not raw:
                rec['outcome'], rec['error_code'] = STORAGE_FETCH_FAILED, 'fetch_returned_nothing'
                return rec
        else:
            with open(self._local_path(orig.rel), 'rb') as fh:
                raw = fh.read(self.max_original_bytes + 1)
            if len(raw) > self.max_original_bytes:
                rec['outcome'], rec['error_code'] = UNSUPPORTED_IMAGE, 'original_too_large'
                return rec
        rec['original_size_bytes'] = len(raw)

        data, outcome, code = self._derive(spec, raw)
        del raw
        if data is None:
            rec['outcome'], rec['error_code'] = outcome, code
            return rec
        rec['display_size_bytes'] = len(data)

        key = f'{spec.subfolder}/{self.prefix}-{uuid.uuid4().hex}.webp'
        expected_url = self._object_url(key)
        rec['new_object_key'] = key
        url = self.storage.upload(data, key, self.upload_bucket)
        if url != expected_url:
            # None (failed / timed out) or anything that is not the confirmed
            # Supabase object — including a local-disk path — is refused.
            return self._finish_with_cleanup(
                rec, key, STORAGE_UPLOAD_FAILED,
                'upload_failed' if url is None else 'unexpected_upload_result')
        stored = self.metadata.sizes([(self.upload_bucket, key)])
        self.db.session.rollback()
        if stored.get((self.upload_bucket, key), -1) != len(data):
            return self._finish_with_cleanup(rec, key, STORAGE_UPLOAD_FAILED,
                                             'upload_unverified')

        try:
            row = self._lock(spec, cand.id, cand.school_id)
            if row is None or row.photo != cand.photo or _has_value(row.photo_display):
                self.db.session.rollback()
                return self._finish_with_cleanup(rec, key, CONFLICT_SKIPPED,
                                                 'changed_under_lock')
            row.photo_display = expected_url      # the ONLY column written
            self._commit()
        except Exception as exc:
            try:
                self.db.session.rollback()
            except Exception:
                pass
            return self._finish_with_cleanup(rec, key, DB_UPDATE_FAILED, type(exc).__name__)

        self.db_writes += 1
        rec['outcome'], rec['new_display_value'] = OK, expected_url
        return rec

    # ── run ───────────────────────────────────────────────────────────────────

    def run(self) -> dict:
        self._validate()
        manifest = Manifest(self.manifest_dir, self.run_id,
                            'apply' if self.apply else 'dry_run')
        if not self.apply:
            self._enforce_read_only()
        started = time.monotonic()
        summary = {'run_id': self.run_id, 'mode': 'apply' if self.apply else 'dry_run',
                   'identity': self._identity(), 'entities': {}, 'per_school': {},
                   'manifest': manifest.path}
        try:
            meta_ok = self.metadata.available()
            for spec in self.entities:
                ent = summary['entities'].setdefault(spec.name, {
                    'candidates_seen': 0, 'outcomes': {}, 'by_source': {},
                    'estimated_supabase_read_bytes': 0, 'estimated_local_read_bytes': 0,
                    'expected_new_objects': 0,
                    'already_has_display': self._already_has_display(spec)})
                self.db.session.rollback()
                if self._process_entity(spec, ent, summary, manifest, meta_ok):
                    break
        finally:
            self._release_read_only()
            summary.update({
                'processed': self.processed, 'errors': self.errors,
                'stopped_reason': self.stopped_reason, 'db_writes': self.db_writes,
                'storage_fetches': self.storage.fetches,
                'storage_fetched_bytes': self.storage.fetched_bytes,
                'storage_uploads': self.storage.uploads,
                'storage_deletes': self.storage.deletes,
                'elapsed_seconds': round(time.monotonic() - started, 2)})
            manifest.finish(summary)
        return summary

    def _process_entity(self, spec, ent, summary, manifest, meta_ok) -> bool:
        """Process one entity. True when the whole run must stop."""
        after_id = self.resume_after_id or 0
        while True:
            size = self.batch_size
            if self.limit is not None:
                size = min(size, self.limit - self.processed)
                if size <= 0:
                    self.stopped_reason = self.stopped_reason or 'limit_reached'
                    return True
            batch = self._candidates(spec, after_id, size)
            refs = [(o.bucket, o.key) for o in (classify_original(c.photo) for c in batch)
                    if o.kind == 'supabase']
            sizes = self.metadata.sizes(refs) if (meta_ok and refs) else ({} if meta_ok else None)
            self.db.session.rollback()
            if not batch:
                return False
            for cand in batch:
                after_id = cand.id
                t0 = time.monotonic()
                try:
                    rec = (self._apply_one if self.apply else self._dry_one)(spec, cand, sizes)
                except Exception as exc:
                    try:
                        self.db.session.rollback()
                    except Exception:
                        pass
                    rec = self._record(spec, cand)
                    rec['outcome'], rec['error_code'] = UNEXPECTED_ERROR, type(exc).__name__
                    log.warning('[display-backfill] unexpected error entity=%s id=%s',
                                spec.name, cand.id, exc_info=True)
                self._account(spec, ent, summary, rec)
                manifest.write(rec)
                log.info('[display-backfill] entity=%s id=%s school_id=%s outcome=%s '
                         'original_bytes=%s display_bytes=%s elapsed_ms=%d',
                         spec.name, rec['id'], rec['school_id'], rec['outcome'],
                         rec['original_size_bytes'], rec['display_size_bytes'],
                         (time.monotonic() - t0) * 1000)
                if self.apply and self.errors >= self.max_errors:
                    self.stopped_reason = 'max_errors_reached'
                    return True
                if self.limit is not None and self.processed >= self.limit:
                    self.stopped_reason = 'limit_reached'
                    return True
                if self.apply and self.sleep_ms:
                    time.sleep(self.sleep_ms / 1000.0)

    def _account(self, spec, ent, summary, rec):
        self.processed += 1
        outcome = rec['outcome']
        if outcome not in NON_ERROR_OUTCOMES:
            self.errors += 1
        ent['candidates_seen'] += 1
        ent['outcomes'][outcome] = ent['outcomes'].get(outcome, 0) + 1
        src = rec['source'] or 'unknown'
        ent['by_source'][src] = ent['by_source'].get(src, 0) + 1
        if outcome in (WOULD_PROCESS, OK):
            ent['expected_new_objects'] += 1
            size = rec['original_size_bytes'] or 0
            ent['estimated_supabase_read_bytes' if src == 'supabase'
                else 'estimated_local_read_bytes'] += size
        school = summary['per_school'].setdefault(spec.name, {}).setdefault(
            str(rec['school_id']), {})
        school[outcome] = school.get(outcome, 0) + 1

    def _identity(self) -> dict:
        """Where this run points — hosts and revision only, never credentials."""
        from sqlalchemy import text
        url = self.db.engine.url
        user = url.username or ''
        ident = {'db_host': url.host, 'db_name': url.database,
                 'db_project_ref': user.split('.', 1)[1] if '.' in user else None,
                 'storage_host': self.storage_host,
                 'service_key_configured': bool(self.app.config.get('SUPABASE_SERVICE_KEY')),
                 'storage_metadata_available': self.metadata.available()}
        try:
            ident['alembic_revision'] = self.db.session.execute(
                text('select version_num from alembic_version')).scalar()
        except Exception:
            ident['alembic_revision'] = None
        finally:
            self.db.session.rollback()
        return ident


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args(argv):
    p = argparse.ArgumentParser(
        prog='display_photo_backfill',
        description='Backfill photo_display for legacy students/employees. '
                    'DRY RUN unless --apply is given.')
    p.add_argument('--entity', choices=('students', 'employees', 'both'), required=True)
    p.add_argument('--school-id', type=int)
    p.add_argument('--exclude-school-id', type=int, action='append', default=[])
    p.add_argument('--limit', type=int)
    p.add_argument('--batch-size', type=int, default=25)
    p.add_argument('--resume-after-id', type=int)
    p.add_argument('--max-errors', type=int, default=10)
    p.add_argument('--sleep-ms', type=int, default=0)
    p.add_argument('--include-local', action='store_true', default=False)
    p.add_argument('--expected-app-root')
    p.add_argument('--manifest-dir')
    p.add_argument('--apply', action='store_true', default=False,
                   help='Perform Storage reads/uploads and write photo_display.')
    p.add_argument('--confirm-storage-host',
                   help='Required with --apply: the Supabase host this run must target.')
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    # Declared before create_app(): this process starts NO background services.
    from app.lifecycle import ROLE_CLI, set_role
    set_role(ROLE_CLI)
    from app import create_app
    app = create_app(os.environ.get('FLASK_ENV', 'production'))

    entities = ('students', 'employees') if args.entity == 'both' else (args.entity,)
    with app.app_context():
        runner = BackfillRunner(
            entities=entities, apply=args.apply, school_id=args.school_id,
            exclude_school_ids=args.exclude_school_id, limit=args.limit,
            batch_size=args.batch_size, resume_after_id=args.resume_after_id,
            max_errors=args.max_errors, sleep_ms=args.sleep_ms,
            include_local=args.include_local, expected_app_root=args.expected_app_root,
            confirm_storage_host=args.confirm_storage_host, manifest_dir=args.manifest_dir)
        try:
            summary = runner.run()
        except ConfigError as exc:
            log.error('[display-backfill] refused: %s', exc)
            return EXIT_REFUSED
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if summary['stopped_reason'] == 'max_errors_reached':
        return EXIT_MAX_ERRORS
    return EXIT_WITH_ERRORS if summary['errors'] else EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
