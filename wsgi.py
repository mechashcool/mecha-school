"""
WSGI entry point for production deployment.
Usage: gunicorn -w 4 -b 0.0.0.0:5000 wsgi:application
"""
import os
from app import create_app

application = create_app(os.environ.get('FLASK_ENV', 'production'))

if __name__ == '__main__':
    application.run()
