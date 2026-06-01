import os


bind         = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
worker_class = 'gthread'           # threaded workers; good for AJAX/polling I/O
workers      = int(os.environ.get('WEB_CONCURRENCY', 2))
threads      = int(os.environ.get('GUNICORN_THREADS', 8))
timeout      = int(os.environ.get('GUNICORN_TIMEOUT', 120))

accesslog = '-'
errorlog = '-'
preload_app = False

max_requests = int(os.environ.get('GUNICORN_MAX_REQUESTS', 500))
max_requests_jitter = int(os.environ.get('GUNICORN_MAX_REQUESTS_JITTER', 50))


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
