import os


bind         = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
worker_class = 'gthread'           # threaded workers; good for AJAX/polling I/O

# Render free-tier recommendation: 1 worker × 4 threads.
# Two workers double memory, DB connections, and background scheduler instances.
# Override with WEB_CONCURRENCY / GUNICORN_THREADS env vars if you need more.
workers = int(os.environ.get('WEB_CONCURRENCY', 1))
threads = int(os.environ.get('GUNICORN_THREADS', 4))
timeout = int(os.environ.get('GUNICORN_TIMEOUT', 120))

# Push fix: background FCM dispatch threads (app/services/async_dispatch.py)
# drain their queue during interpreter shutdown when a worker recycles
# (max_requests below). The default graceful_timeout of 30 s could SIGKILL the
# worker mid-drain and drop queued pushes; 90 s gives the drain room while
# staying under the master's hard timeout above.
graceful_timeout = int(os.environ.get('GUNICORN_GRACEFUL_TIMEOUT', 90))

accesslog = '-'
errorlog  = '-'
preload_app = False

max_requests        = int(os.environ.get('GUNICORN_MAX_REQUESTS', 500))
max_requests_jitter = int(os.environ.get('GUNICORN_MAX_REQUESTS_JITTER', 50))


def on_starting(server):
    """Log key env vars once when the Gunicorn master starts."""
    import logging
    logging.getLogger('gunicorn.error').info(
        '[startup] Gunicorn master starting — '
        'PORT=%s  WEB_CONCURRENCY=%s (effective workers=%s)  '
        'GUNICORN_THREADS=%s (effective threads=%s)  '
        'AIFACE_WS_ENABLED=%s  AIFACE_WS_PORT=%s  '
        'ATTENDANCE_SCHEDULER_DISABLED=%s  FEE_REMINDER_SCHEDULER_DISABLED=%s',
        os.environ.get('PORT', '5000'),
        os.environ.get('WEB_CONCURRENCY', '(not set)'), workers,
        os.environ.get('GUNICORN_THREADS',  '(not set)'), threads,
        os.environ.get('AIFACE_WS_ENABLED', '(not set)'),
        os.environ.get('AIFACE_WS_PORT',    '(not set, default=7788)'),
        os.environ.get('ATTENDANCE_SCHEDULER_DISABLED', '(not set, default=false)'),
        os.environ.get('FEE_REMINDER_SCHEDULER_DISABLED', '(not set, default=false)'),
    )


def post_fork(server, worker):
    """Log pool config after each worker forks — useful for verifying Render env vars."""
    import logging
    pool_size    = int(os.environ.get('SQLALCHEMY_POOL_SIZE', 5))
    max_overflow = int(os.environ.get('SQLALCHEMY_MAX_OVERFLOW', 10))
    pool_timeout = int(os.environ.get('SQLALCHEMY_POOL_TIMEOUT', 30))
    logging.getLogger('gunicorn.error').info(
        '[worker %s] DB pool_size=%s  max_overflow=%s  pool_timeout=%s  '
        'web_threads=%s  max_conn_per_worker=%s',
        worker.pid, pool_size, max_overflow, pool_timeout,
        threads, pool_size + max_overflow,
    )


# Logging — set mecha.* and gunicorn loggers to INFO so FCM/notification
# events appear in Render logs without noise from the root logger.
# websockets.server is silenced at ERROR level to suppress noisy HEAD-request
# handshake-failed warnings from health checkers hitting the WS port.
logconfig_dict = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'default': {
            'format': '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'stream': 'ext://sys.stderr',
            'formatter': 'default',
        },
    },
    'loggers': {
        'mecha': {
            'level': 'INFO',
            'handlers': ['console'],
            'propagate': False,
        },
        'gunicorn.error': {
            'level': 'INFO',
            'handlers': ['console'],
            'propagate': False,
        },
        'gunicorn.access': {
            'level': 'INFO',
            'handlers': ['console'],
            'propagate': False,
        },
        # Attendance scheduler and blueprint — must be INFO so per-tick diagnostics
        # (cutoff passed/skipped, absent count, holiday, no-cutoff) are visible on the VPS.
        # Without these entries both loggers fall through to root at WARNING and all
        # INFO-level scheduler messages are silently dropped.
        'app.services.auto_attendance': {
            'level': 'INFO',
            'handlers': ['console'],
            'propagate': False,
        },
        'app.blueprints.attendance': {
            'level': 'INFO',
            'handlers': ['console'],
            'propagate': False,
        },
        # Suppress "opening handshake failed / unsupported HTTP method HEAD"
        # spam from health-checker probes hitting the AI Face WebSocket port.
        'websockets': {
            'level': 'ERROR',
            'handlers': ['console'],
            'propagate': False,
        },
        'websockets.server': {
            'level': 'ERROR',
            'handlers': ['console'],
            'propagate': False,
        },
    },
    'root': {
        'level': 'WARNING',
        'handlers': ['console'],
    },
}
