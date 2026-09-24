"""Windows-only stand-in for `gunicorn -c gunicorn.conf.py wsgi:application`.

Gunicorn cannot run on Windows. For LOCAL TOOLING VALIDATION this wrapper keeps
the parts of the production process model that affect the measurement:
  * one process, 4 request threads (gthread 1 worker x 4 threads)
  * the exact logging configuration from gunicorn.conf.py (same log volume)
  * a gunicorn-style access-log line per request on stderr
  * create_app() runs in this process, so the AI Face WebSocket thread,
    DB pool and GIL are shared with HTTP exactly as in production
Not reproduced (documented as a difference): max_requests worker recycling,
graceful_timeout, gunicorn's master/worker split.
Must be started with cwd = the archived app source.
"""
import logging
import logging.config
import os
import runpy
import sys
import time

sys.path.insert(0, os.getcwd())
conf = runpy.run_path(os.path.join(os.getcwd(), 'gunicorn.conf.py'))
logging.config.dictConfig(conf['logconfig_dict'])
access = logging.getLogger('gunicorn.access')

from wsgi import application  # noqa: E402  (create_app runs here)
from waitress import serve  # noqa: E402


class AccessLog:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        t0 = time.perf_counter()
        status_holder = {}

        def _sr(status, headers, exc_info=None):
            status_holder['s'] = status.split(' ', 1)[0]
            return start_response(status, headers, exc_info)

        try:
            return self.app(environ, _sr)
        finally:
            access.info('%s - - "%s %s %s" %s - %.0fms', environ.get('REMOTE_ADDR'),
                        environ.get('REQUEST_METHOD'), environ.get('PATH_INFO'),
                        environ.get('SERVER_PROTOCOL'), status_holder.get('s', '-'),
                        (time.perf_counter() - t0) * 1000)


threads = int(os.environ.get('GUNICORN_THREADS', '4'))
logging.getLogger('gunicorn.error').info('[attlt] waitress stand-in: threads=%s port=%s',
                                         threads, os.environ['PORT'])
serve(AccessLog(application), host='127.0.0.1', port=int(os.environ['PORT']), threads=threads,
      connection_limit=1000, channel_timeout=120, backlog=2048, ident='attlt')
