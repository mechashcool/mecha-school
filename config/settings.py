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
    SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')
    SUPABASE_BUCKET      = os.environ.get('SUPABASE_BUCKET', 'uploads')

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
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
        'pool_recycle': int(os.environ.get('SQLALCHEMY_POOL_RECYCLE', 300)),
        'pool_size': int(os.environ.get('SQLALCHEMY_POOL_SIZE', 2)),
        'max_overflow': int(os.environ.get('SQLALCHEMY_MAX_OVERFLOW', 0)),
        'pool_timeout': int(os.environ.get('SQLALCHEMY_POOL_TIMEOUT', 10)),
        'connect_args': {
            'connect_timeout': int(os.environ.get('SQLALCHEMY_CONNECT_TIMEOUT', 10)),
            'keepalives': 1,
            'keepalives_idle': 30,
            'keepalives_interval': 10,
            'keepalives_count': 5,
        },
    }

    @classmethod
    def init_app(cls, app):
        Config.init_app(app)


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
