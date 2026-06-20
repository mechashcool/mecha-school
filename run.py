"""
Al-Muhandis School Management System
Entry Point — run with: python run.py
"""
import logging
import os

from app import create_app
from app.utils.seeder import register_commands

_env = os.environ.get('FLASK_ENV', 'development')
app = create_app(_env)
register_commands(app)

if __name__ == '__main__':
    _pid             = os.getpid()
    _reloader_child  = os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
    _startup_log     = logging.getLogger('mecha.startup')

    # Emit a single-line banner so it is easy to spot which process is the
    # authoritative one in the terminal output.
    _startup_log.info(
        '[run.py] pid=%d  env=%s  reloader_child=%s  '
        'Flask HTTP on :5000  AI Face WS on :7788  use_reloader=False',
        _pid, _env, _reloader_child,
    )

    # use_reloader=False is mandatory for this application.
    #
    # With the Werkzeug debug reloader ON (the default when debug=True):
    #   - create_app() runs in BOTH the reloader supervisor and the HTTP worker
    #   - The WS server starts in the supervisor process (first bind wins)
    #   - The HTTP worker probe fails (EADDRINUSE) → WS server absent in HTTP worker
    #   - start_auto_attendance_scheduler() fires in both processes (duplicates)
    #   - Attendance logs written by the supervisor are invisible in the worker logs
    #   - The web UI's in-memory device status is in the supervisor — the HTTP
    #     worker sees an empty _connections dict and reports every device as offline
    #
    # With use_reloader=False:
    #   - One process owns port 5000 (Flask HTTP) and port 7788 (AI Face WS)
    #   - All schedulers start exactly once
    #   - All logs go to one stdout stream
    #   - The in-memory WS state and Flask requests live in the same process
    app.run(
        host='0.0.0.0',
        port=5000,
        debug=(_env == 'development'),
        use_reloader=False,
    )
