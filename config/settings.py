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
    # attachments) resolve to short-lived signed URLs — Flask-HMAC proxy for
    # small files, Supabase-native signed URLs for board media — and school
    # identity resolves against SUPABASE_PUBLIC_BRANDING_BUCKET.
    #
    # Flip this to 'true' ONLY AFTER the public-branding bucket exists and the
    # identity objects have been copied into it (see the Stage 2 cutover steps).
    # Unsetting it is an instant, code-level rollback independent of bucket state.
    PRIVATE_UPLOADS_ENABLED = (
        os.environ.get('PRIVATE_UPLOADS_ENABLED', 'false').lower() == 'true'
    )
    # Signed-URL lifetimes (seconds). Short by default — clients cache within TTL.
    SIGNED_FILE_TTL_SECONDS  = int(os.environ.get('SIGNED_FILE_TTL_SECONDS',  900))     # 15 min
    SIGNED_VIDEO_TTL_SECONDS = int(os.environ.get('SIGNED_VIDEO_TTL_SECONDS', 21600))   # 6 h

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
    """Testing configuration."""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'postgresql://postgres:password@localhost:5432/almuhandis_test'
    WTF_CSRF_ENABLED = False


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}
