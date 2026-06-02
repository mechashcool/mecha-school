import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    """Base configuration."""
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'fallback-secret-key'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'app/static/uploads')
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024))
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx'}

    # Pagination
    ITEMS_PER_PAGE = 20

    # Session
    PERMANENT_SESSION_LIFETIME = 3600  # 1 hour

    # Supabase Storage — set these in Render environment variables for production
    SUPABASE_URL         = os.environ.get('SUPABASE_URL', '')
    # Accept the standard Supabase name SUPABASE_SERVICE_ROLE_KEY; fall back to
    # the legacy SUPABASE_SERVICE_KEY so existing deployments keep working.
    SUPABASE_SERVICE_KEY = (os.environ.get('SUPABASE_SERVICE_ROLE_KEY') or
                            os.environ.get('SUPABASE_SERVICE_KEY', ''))
    SUPABASE_BUCKET               = os.environ.get('SUPABASE_BUCKET', 'uploads')
    SUPABASE_STORAGE_BUCKET_MEDIA = os.environ.get('SUPABASE_STORAGE_BUCKET_MEDIA', 'school-media')

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
