import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

# Known-insecure placeholder. If the running process ever uses this value in
# production the app must refuse to start (see ProductionConfig.init_app),
# because anyone reading the source can forge sessions and JWT tokens.
INSECURE_SECRET_PLACEHOLDER = 'dev-only-insecure-secret-change-me'


class Config:
    """Base configuration."""
    SECRET_KEY = os.environ.get('SECRET_KEY') or INSECURE_SECRET_PLACEHOLDER
    # Dedicated signing key for mobile JWTs. Falls back to SECRET_KEY when unset
    # so existing deployments keep working, but a distinct key is recommended.
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY') or None
    # Fernet key used to encrypt each school's public registration token at rest
    # so the Super Admin can copy the active link at any time. When unset, a key
    # is derived deterministically from SECRET_KEY (see
    # app/utils/registration_tokens.py). A dedicated, stable key is recommended
    # in production: if SECRET_KEY rotates, existing links stay VALID (the sha256
    # lookup hash is unaffected) but become non-recoverable for copy until
    # regenerated.
    REGISTRATION_TOKEN_KEY = os.environ.get('REGISTRATION_TOKEN_KEY') or None
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'app/static/uploads')
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024))
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx'}

    # Pagination
    ITEMS_PER_PAGE = 20

    # Session / cookie hardening (applies to all environments).
    PERMANENT_SESSION_LIFETIME = timedelta(hours=1)   # normal session timeout
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = 'Lax'
    # Duration of the Flask-Login "remember me" persistent cookie.
    # The session cookie is separate and still expires on browser close when
    # remember=False; this value controls the long-lived token cookie only.
    REMEMBER_COOKIE_DURATION = timedelta(days=30)
    # CSRF tokens stay valid for the life of the session rather than expiring
    # mid-form (avoids spurious 400s on long-open pages).
    WTF_CSRF_TIME_LIMIT = None

    # Supabase Storage — set these in Render environment variables for production
    SUPABASE_URL         = os.environ.get('SUPABASE_URL', '')
    # Accept the standard Supabase name SUPABASE_SERVICE_ROLE_KEY; fall back to
    # the legacy SUPABASE_SERVICE_KEY so existing deployments keep working.
    SUPABASE_SERVICE_KEY = (os.environ.get('SUPABASE_SERVICE_ROLE_KEY') or
                            os.environ.get('SUPABASE_SERVICE_KEY', ''))
    SUPABASE_BUCKET               = os.environ.get('SUPABASE_BUCKET', 'uploads')
    SUPABASE_STORAGE_BUCKET_MEDIA = os.environ.get('SUPABASE_STORAGE_BUCKET_MEDIA', 'school-media')
    # Public branding/identity bucket (Stage 2). School logos/favicons live here so
    # the 'uploads' and 'school-media' buckets can be flipped private without
    # breaking branding, PDFs, or the favicon. Stays PUBLIC.
    SUPABASE_PUBLIC_BRANDING_BUCKET = os.environ.get('SUPABASE_PUBLIC_BRANDING_BUCKET',
                                                     'public-branding')

    # ── Private-uploads master switch (Stage 2) ───────────────────────────────
    # When FALSE (default) every media URL resolves exactly as before Stage 2:
    # the stored public CDN URL is returned unchanged. When TRUE, private files
    # (student/employee photos & documents, receipts, homework/leave/complaint
    # attachments, and school identity/logo) resolve to short-lived signed URLs
    # — Flask-HMAC proxy for small files, Supabase-native signed URLs for
    # school-media (board media AND school identity/logo). public-branding only
    # ever holds the fixed global login assets (Core-School-logo.jpg,
    # core-school-background.png) and is never used for per-school identity.
    #
    # Flip this to 'true' once the public-branding bucket exists.
    # Unsetting it is an instant, code-level rollback independent of bucket state.
    PRIVATE_UPLOADS_ENABLED = (
        os.environ.get('PRIVATE_UPLOADS_ENABLED', 'false').lower() == 'true'
    )
    # Signed-URL lifetimes (seconds). Short by default — clients cache within TTL.
    SIGNED_FILE_TTL_SECONDS  = int(os.environ.get('SIGNED_FILE_TTL_SECONDS',  900))     # 15 min
    SIGNED_VIDEO_TTL_SECONDS = int(os.environ.get('SIGNED_VIDEO_TTL_SECONDS', 21600))   # 6 h

    # ── /media-proxy direct-delivery redirect (P0) ─────────────────────────────
    # When TRUE (default), /media-proxy verifies the HMAC token and then 302s to
    # a short-lived Supabase-native signed URL so the object streams from the
    # Supabase CDN instead of being buffered whole through a Flask worker thread.
    # Authorization is unchanged: the HMAC token is still required, and it is
    # only ever minted after route-level ownership checks. If native signing is
    # unavailable (no service key, Supabase error, legacy local-only file) the
    # route falls back to the previous authenticated streaming behaviour — never
    # to a raw or public URL. Set to 'false' for an instant rollback to the
    # pre-P0 proxy-streaming behaviour.
    MEDIA_PROXY_REDIRECT_ENABLED = (
        os.environ.get('MEDIA_PROXY_REDIRECT_ENABLED', 'true').lower() == 'true'
    )
    # Lifetime of the Supabase-native signed URL minted for a /media-proxy
    # redirect. Longer than SIGNED_FILE_TTL_SECONDS so the redirect target (and
    # its in-process cache entry) can be reused across many proxy hits.
    MEDIA_REDIRECT_SIGN_TTL_SECONDS = int(
        os.environ.get('MEDIA_REDIRECT_SIGN_TTL_SECONDS', 3600)
    )

    # ── Stable HMAC signed-URL windows (P1) ────────────────────────────────────
    # When TRUE (default), the exp embedded in /media-proxy HMAC URLs is
    # quantised to fixed windows (window = the URL's TTL) so repeated mints for
    # the same object inside one window produce a byte-identical URL. Stable
    # URLs let browser HTTP caches and the Flutter image cache actually hit
    # instead of re-downloading every photo on each request. Remaining validity
    # stays within [TTL, 2×TTL). Authorization is unchanged — the token still
    # grants exactly one (bucket, object) and is only minted after ownership
    # checks. Set to 'false' to restore per-request expiries (instant rollback).
    SIGNED_URL_STABLE_WINDOWS = (
        os.environ.get('SIGNED_URL_STABLE_WINDOWS', 'true').lower() == 'true'
    )

    # ── Backend read-cache layer (P2) ──────────────────────────────────────────
    # Master switch for the in-process TTL caches added in P2 (active academic
    # year id, school branding payload, mobile badge counts). When FALSE every
    # cached code path falls straight through to the previous per-request
    # database queries — an instant, behaviour-identical rollback. All caches
    # are per-process, keyed by their full isolation dimensions, and backed by
    # explicit invalidation hooks at the relevant write sites plus short TTLs
    # as the cross-worker staleness bound.
    BACKEND_CACHE_ENABLED = (
        os.environ.get('BACKEND_CACHE_ENABLED', 'true').lower() == 'true'
    )
    # Active-year id per school. Short: this value participates in tenant/year
    # scoping, so staleness after a rollover must stay tightly bounded even on
    # workers that missed the in-process invalidation hook.
    ACTIVE_YEAR_CACHE_TTL_SECONDS = int(
        os.environ.get('ACTIVE_YEAR_CACHE_TTL_SECONDS', 60)
    )
    # Serialized school branding block returned by /me (name, logo URL, colors,
    # contact). Display-only data; invalidated on school-settings save.
    SCHOOL_BRANDING_CACHE_TTL_SECONDS = int(
        os.environ.get('SCHOOL_BRANDING_CACHE_TTL_SECONDS', 300)
    )
    # Mobile badge-counts response. Matches the approved 30-60 s badge window
    # already used by the web sidebar badge cache; the user's own read/view
    # actions invalidate immediately.
    MOBILE_BADGE_CACHE_TTL_SECONDS = int(
        os.environ.get('MOBILE_BADGE_CACHE_TTL_SECONDS', 45)
    )

    # ── Observability (P3) ─────────────────────────────────────────────────────
    # Master switch for the in-process metrics layer: request timing,
    # slow-request / slow-query logging, and external-service (Supabase/FCM)
    # latency accounting. Aggregates only — no request bodies, no query
    # parameters, no tenant data are ever recorded. Set to 'false' for an
    # instant, behaviour-identical rollback.
    OBSERVABILITY_ENABLED = (
        os.environ.get('OBSERVABILITY_ENABLED', 'true').lower() == 'true'
    )
    # A request slower than this is logged once at WARNING (method, path
    # WITHOUT the query string, status, duration). 0 disables the log line.
    SLOW_REQUEST_MS = int(os.environ.get('SLOW_REQUEST_MS', 1500))
    # A single SQL statement slower than this is logged once at WARNING with
    # the statement truncated and WITHOUT bind parameters (no tenant data).
    # 0 disables the log line.
    SLOW_QUERY_MS = int(os.environ.get('SLOW_QUERY_MS', 500))
    # Shared secret for GET /ops/metrics and /ops/health/deep via the
    # X-Ops-Token header (for external monitoring agents). When unset, those
    # endpoints are reachable only by an authenticated super admin session —
    # fail closed, never open.
    OPS_METRICS_TOKEN = os.environ.get('OPS_METRICS_TOKEN', '')

    # ── Institute attendance notification outbox ──────────────────────────────
    # DEFAULT FALSE. While false, institute attendance behaves exactly as it
    # does today (in-app rows committed separately, Firebase called inline) and
    # nothing reads or writes the notification_outbox table — so this code is
    # safe to deploy before the migration is applied and before any worker
    # exists.
    #
    # While true, the parent in-app rows and the push-delivery jobs are written
    # inside the SAME transaction as the attendance rows, and the request never
    # contacts Firebase. Delivery is handled by the separate worker in
    # app/services/outbox_worker.py. There is no inline fallback: enabling this
    # flag without running the worker queues notifications durably rather than
    # sending them, which is a visible backlog, not silent loss.
    INSTITUTE_ATTENDANCE_OUTBOX_ENABLED = (
        os.environ.get('INSTITUTE_ATTENDANCE_OUTBOX_ENABLED', 'false')
        .strip().lower() == 'true'
    )

    # ── Durable notification outbox for NORMAL SCHOOL / AI Face attendance ────
    # Separate flag, separate rollout. Default FALSE, so deploying this code
    # changes nothing: the AI Face WebSocket path keeps its existing inline
    # notification behaviour exactly.
    #
    # While true, the check-in / check-out push jobs are written inside the SAME
    # transaction as the StudentAttendance change, and the device request never
    # contacts Firebase. The SAME worker that already serves institute
    # attendance (app/services/outbox_worker.py) delivers them. There is no
    # inline fallback: enqueue and inline-send together would double-deliver.
    #
    # This flag has no effect on institute attendance, and
    # INSTITUTE_ATTENDANCE_OUTBOX_ENABLED has no effect on AI Face.
    AIFACE_ATTENDANCE_OUTBOX_ENABLED = (
        os.environ.get('AIFACE_ATTENDANCE_OUTBOX_ENABLED', 'false')
        .strip().lower() == 'true'
    )

    # ── Durable notification outbox for MANUAL school student attendance ──────
    # Third independent flag, default FALSE. Covers ONLY the manual save handler
    # (POST /attendance/take/<section_id>). Automatic absence, the scheduler, AI
    # Face and institute attendance are NOT affected by it.
    #
    # While true, the parent in-app rows (absence only, as today) and the push
    # jobs are written inside the SAME transaction as the StudentAttendance
    # change, and the request never contacts Firebase. The shared worker
    # (app/services/outbox_worker.py) delivers them; it must receive the same
    # value, which it does through the shared .env it loads.
    MANUAL_ATTENDANCE_OUTBOX_ENABLED = (
        os.environ.get('MANUAL_ATTENDANCE_OUTBOX_ENABLED', 'false')
        .strip().lower() == 'true'
    )

    # ── Durable notification outbox for AUTOMATIC school absence ──────────────
    # Fourth independent flag, default FALSE. Covers every automatic-absence
    # producer: _run_auto_absent (GET /attendance/, "mark absent today", the
    # scheduler and its midnight catch-up) and the shift-mode producers in
    # app/services/auto_attendance.py. The absence decision itself is untouched.
    #
    # While true, the absence rows, the parent in-app rows and the push jobs
    # commit in ONE transaction per existing commit unit (school, shift or
    # shiftless fallback) and no Firebase call is made by the triggering
    # request or scheduler thread. The scheduler runs inside the web process,
    # so both read this same value.
    AUTO_ABSENCE_OUTBOX_ENABLED = (
        os.environ.get('AUTO_ABSENCE_OUTBOX_ENABLED', 'false')
        .strip().lower() == 'true'
    )

    # ── Redis coordination (P3) — OPTIONAL ────────────────────────────────────
    # When REDIS_URL is unset (the default) every Redis-backed feature silently
    # degrades to the existing in-process behaviour: pushes use the P0 thread
    # pool, caches stay per-process with their TTL staleness bound, rate
    # limiting stays per-worker. Core academic/financial/attendance operations
    # never depend on Redis availability.
    REDIS_URL = os.environ.get('REDIS_URL', '')
    # Namespace prefix so several environments (staging/prod) can share one
    # Redis instance without key collisions.
    REDIS_KEY_PREFIX = os.environ.get('REDIS_KEY_PREFIX', 'mecha')

    # ── Durable push queue (P3) ────────────────────────────────────────────────
    # When TRUE *and* REDIS_URL is configured, background push tasks
    # (send_push_batch and the registered notification fan-out tasks) are
    # enqueued as JSON jobs in a Redis list instead of the in-process thread
    # pool, so queued pushes survive Gunicorn worker recycling and restarts.
    # Payloads are primitives only; every consumer-side DB query keeps its
    # explicit ownership filters. Without Redis this flag has no effect.
    DURABLE_PUSH_QUEUE_ENABLED = (
        os.environ.get('DURABLE_PUSH_QUEUE_ENABLED', 'true').lower() == 'true'
    )
    # A job that raises (infra/DB failure — FCM per-item failures never raise)
    # is re-queued up to this many total attempts, then dropped with an ERROR log.
    DURABLE_QUEUE_MAX_ATTEMPTS = int(os.environ.get('DURABLE_QUEUE_MAX_ATTEMPTS', 3))

    # ── Mobile synchronization foundation (Part B1) — DISABLED BY DEFAULT ──────
    # These two flags gate work that is NOT implemented yet. B1 adds only the
    # additive `change_journal` / `sync_meta` tables; no capture hook, no
    # /sync/* endpoint, no cursor logic, and no signal service exist.
    #
    # Unlike the other switches in this file (which default to 'true' and are
    # flipped off for rollback), these default to 'false': the feature must be
    # explicitly opted into, so an upgrade can never silently start capturing
    # or expose a signal port.
    #
    # SYNC_JOURNAL_ENABLED — when true (Part B2), committed changes to
    #   whitelisted models will be captured into `change_journal` inside the
    #   SAME transaction as the business write. While false, nothing is written
    #   to the journal by any code path.
    # SYNC_SIGNAL_ENABLED  — when true (a later part), an authenticated
    #   foreground signal service may be started. While false, no listener,
    #   thread, or port is created.
    SYNC_JOURNAL_ENABLED = (
        os.environ.get('SYNC_JOURNAL_ENABLED', 'false').lower() == 'true'
    )
    SYNC_SIGNAL_ENABLED = (
        os.environ.get('SYNC_SIGNAL_ENABLED', 'false').lower() == 'true'
    )

    @staticmethod
    def init_app(app):
        pass


class DevelopmentConfig(Config):
    """Development configuration."""
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'postgresql://postgres:password@localhost:5432/almuhandis_db'
    SQLALCHEMY_ECHO = False


class ProductionConfig(Config):
    """Production configuration."""
    DEBUG = False
    # Cookies must only travel over HTTPS in production.
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = 'https'
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL')
    # Pool math (per Gunicorn worker process):
    #   web threads:       WEB_CONCURRENCY(2) × GUNICORN_THREADS(2) = 4
    #   scheduler threads: fee_reminder + auto_attendance + hikvision = up to 3
    #   headroom:          max_overflow covers transient bursts
    # Supabase free tier: 60 direct connections, so 5 × 2 workers = 10 base is safe.
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
        'pool_recycle':  int(os.environ.get('SQLALCHEMY_POOL_RECYCLE',  1800)),
        'pool_size':     int(os.environ.get('SQLALCHEMY_POOL_SIZE',     5)),
        'max_overflow':  int(os.environ.get('SQLALCHEMY_MAX_OVERFLOW',  10)),
        'pool_timeout':  int(os.environ.get('SQLALCHEMY_POOL_TIMEOUT',  30)),
        'connect_args': {
            'connect_timeout': int(os.environ.get('SQLALCHEMY_CONNECT_TIMEOUT', 10)),
            'keepalives':          1,
            'keepalives_idle':    30,
            'keepalives_interval': 10,
            'keepalives_count':    5,
        },
    }

    @classmethod
    def init_app(cls, app):
        Config.init_app(app)
        import logging

        # ── Fail-fast: never run production with a publicly-known secret ──────
        # A missing or placeholder SECRET_KEY lets anyone forge session cookies
        # and mobile JWTs. Refuse to boot rather than serve in that state.
        secret = app.config.get('SECRET_KEY')
        if not secret or secret == INSECURE_SECRET_PLACEHOLDER:
            raise RuntimeError(
                'SECRET_KEY is not set (or uses the insecure default). '
                'Set a strong, random SECRET_KEY environment variable before '
                'starting the application in production.'
            )
        if len(secret) < 32:
            logging.getLogger('mecha').warning(
                '[security] SECRET_KEY is shorter than 32 characters; '
                'use a longer random value for stronger protection.'
            )
        if not app.config.get('DATABASE_URL') and not app.config.get('SQLALCHEMY_DATABASE_URI'):
            logging.getLogger('mecha').warning('[security] DATABASE_URL is not set.')

        opts = cls.SQLALCHEMY_ENGINE_OPTIONS
        logging.getLogger('mecha').warning(
            '[DB] pool_size=%s  max_overflow=%s  pool_timeout=%s  pool_recycle=%s',
            opts.get('pool_size'), opts.get('max_overflow'),
            opts.get('pool_timeout'), opts.get('pool_recycle'),
        )


class TestingConfig(Config):
    """Testing configuration.

    The database URI comes from ``TEST_DATABASE_URL`` and from nowhere else.
    It deliberately does NOT fall back to ``DATABASE_URL``, to ``.env``, or to
    a built-in default: the previous hardcoded
    ``postgresql://postgres:password@localhost:5432/almuhandis_test`` pointed at
    the developer's normal PostgreSQL service on port 5432, one typo away from
    the real databases living on that same instance.

    ``init_app`` validates the URI and aborts before SQLAlchemy can build an
    engine.  ``tests/conftest.py`` performs the same validation independently
    and earlier; both layers are intentional, because this class is also
    reachable outside pytest (``create_app('testing')`` from a script).
    """
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = os.environ.get('TEST_DATABASE_URL')

    # Integrations are forced off at the config layer as well. Most consumers
    # read os.environ directly (fcm_service at import time, redis_client per
    # call), so conftest scrubbing is the real boundary — these values close the
    # gap for the config-driven consumers.
    SUPABASE_URL              = ''
    SUPABASE_SERVICE_KEY      = ''
    REDIS_URL                 = ''
    PRIVATE_UPLOADS_ENABLED   = False
    DURABLE_PUSH_QUEUE_ENABLED = False
    OBSERVABILITY_ENABLED     = False
    # NOTE: deliberately no ASYNC_DISPATCH_SYNC here. async_dispatch already
    # runs inline when ``app.testing`` is set, and tests that exercise the real
    # background pool switch it off with ``app.testing = False`` — a config key
    # would override that and make the pool untestable.

    @classmethod
    def init_app(cls, app):
        Config.init_app(app)

        uri = app.config.get('SQLALCHEMY_DATABASE_URI')
        if not uri:
            raise RuntimeError(
                'TEST_DATABASE_URL is not set. The testing configuration has '
                'no default database and never falls back to DATABASE_URL or '
                '.env. Point TEST_DATABASE_URL at an isolated local test '
                'database and set TEST_DATABASE_APPROVED to its name.'
            )

        from urllib.parse import urlsplit, unquote
        parts = urlsplit(uri)
        host = (parts.hostname or '').lower()
        name = unquote((parts.path or '').lstrip('/'))

        if host not in ('127.0.0.1', 'localhost', '::1'):
            raise RuntimeError(
                f'Refusing to run tests against non-loopback host {host!r}.'
            )
        if not name.endswith('_test'):
            raise RuntimeError(
                f'Refusing to run tests against database {name!r}: a test '
                f'database name must end with "_test".'
            )
        if (os.environ.get('TEST_DATABASE_APPROVED') or '').strip() != name:
            raise RuntimeError(
                f'Database {name!r} is not explicitly approved for testing. '
                f'Set TEST_DATABASE_APPROVED={name} to confirm it is a '
                f'disposable, isolated test database.'
            )


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}
