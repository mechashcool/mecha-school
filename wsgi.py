"""
WSGI entry point for production deployment.
Usage: gunicorn -w 4 -b 0.0.0.0:5000 wsgi:application
"""
import os

# Declared BEFORE create_app() so the background-service gate in
# app/lifecycle.py sees it. This is the one process that SHOULD start the
# attendance scheduler, the AI Face receiver, the Hikvision sync loop, the
# fee-reminder scheduler and the durable-push consumer — stating it explicitly
# keeps that behaviour independent of argv guesswork.
from app.lifecycle import ROLE_WEB, set_role

set_role(ROLE_WEB)

from app import create_app  # noqa: E402  — must follow set_role()

application = create_app(os.environ.get('FLASK_ENV', 'production'))

if __name__ == '__main__':
    application.run()
