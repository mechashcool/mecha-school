import os


def _int_env(name, default):
    """Return (value, explicitly_set) for an integer environment variable.

    An unparsable or negative value raises instead of silently falling back to
    the default. A typo such as GUNICORN_MAX_REQUESTS="5OO" must not quietly
    re-enable the worker recycling this file exists to disable: Gunicorn should
    refuse to start with a named, readable error rather than boot with an
    unsafe lifecycle setting nobody noticed.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default, False
    try:
        value = int(raw.strip())
    except ValueError:
        raise RuntimeError(
            f'{name}={raw!r} is not an integer. Fix the environment — refusing '
            f'to start with an ambiguous worker-lifecycle setting.'
        ) from None
    if value < 0:
        raise RuntimeError(
            f'{name}={value} must be zero or positive '
            f'(0 = disabled). Refusing to start.'
        )
    return value, True


bind         = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
worker_class = 'gthread'           # threaded workers; good for AJAX/polling I/O

# Render free-tier recommendation: 1 worker × 4 threads.
# Two workers double memory, DB connections, and background scheduler instances.
# Override with WEB_CONCURRENCY / GUNICORN_THREADS env vars if you need more.
workers, _ = _int_env('WEB_CONCURRENCY', 1)
threads, _ = _int_env('GUNICORN_THREADS', 4)
timeout, _ = _int_env('GUNICORN_TIMEOUT', 120)

# Push fix: background FCM dispatch threads (app/services/async_dispatch.py)
# drain their queue during interpreter shutdown when a worker recycles
# (max_requests below). The default graceful_timeout of 30 s could SIGKILL the
# worker mid-drain and drop queued pushes; 90 s gives the drain room while
# staying under the master's hard timeout above.
graceful_timeout, _ = _int_env('GUNICORN_GRACEFUL_TIMEOUT', 90)

accesslog = '-'
errorlog  = '-'
preload_app = False

# Worker recycling is only safe when ANOTHER worker can serve during the
# handover. Gunicorn does not pre-spawn the replacement: the arbiter starts a
# new worker only after the old one has exited. With workers = 1 every recycle
# is therefore a full service gap — HTTP requests sit in the master's accept
# backlog (measured 2026-09-18 on the load-test VPS: worker stopped accepting at
# 00:22:30, replacement booted at 00:22:58, parent-read p95 29.6 s) and, before
# the master-owned WS socket in when_ready() below, the AI Face listener died
# with the worker so device reconnects got ConnectionRefusedError.
# So: recycle by request count only in a multi-worker deployment. Single-worker
# deployments keep leak protection through restarts/deploys instead, and can
# still opt in explicitly with GUNICORN_MAX_REQUESTS.
_single_worker = workers <= 1
max_requests, _max_requests_explicit = _int_env(
    'GUNICORN_MAX_REQUESTS', 0 if _single_worker else 500)

# Jitter is ADDED to max_requests inside each worker, and the SUM becomes that
# worker's limit — Gunicorn only treats the setting as "unlimited" when the sum
# is zero. So a non-zero jitter on top of max_requests=0 does not mean "no
# recycling with a bit of spread": it means a small RANDOM limit, recycling
# every few dozen requests, which is far worse than the 500 being removed here.
# Jitter is therefore only meaningful while recycling is actually enabled.
max_requests_jitter, _jitter_explicit = _int_env(
    'GUNICORN_MAX_REQUESTS_JITTER', 0 if max_requests == 0 else 50)

# Belt and braces: an operator who disables recycling but leaves a stale
# GUNICORN_MAX_REQUESTS_JITTER in the environment gets the safe combination,
# not the random-limit trap above. Recorded so on_starting can say it happened.
_jitter_overridden_to_zero = 0
if max_requests == 0 and max_requests_jitter != 0:
    _jitter_overridden_to_zero = max_requests_jitter
    max_requests_jitter = 0


# ── Multi-worker scaling notes (P3) ─────────────────────────────────────────
# Before raising WEB_CONCURRENCY above 1, align ALL of the following:
#   * DB connections: each worker holds up to
#       SQLALCHEMY_POOL_SIZE + SQLALCHEMY_MAX_OVERFLOW (default 5+10)
#     connections. Total = workers × 15 must stay under the Postgres/Supabase
#     connection budget (Supabase free tier: 60 direct).
#   * Schedulers (auto-attendance, fee-reminder, hikvision) start in EVERY
#     worker process — with 2+ workers they would tick twice. Keep them in one
#     worker only: run the extra workers with ATTENDANCE_SCHEDULER_DISABLED=
#     true / FEE_REMINDER_SCHEDULER_DISABLED=true, or move schedulers to a
#     dedicated process before scaling.
#   * Rate limiting: Flask-Limiter storage defaults to per-worker memory://.
#     Set RATELIMIT_STORAGE_URI to the Redis URL so login throttling is
#     enforced globally, not per worker.
#   * In-process caches (badges, branding, active year, signed URLs) are
#     per-worker; their short TTLs are the documented cross-worker staleness
#     bound and the explicit invalidation hooks stay correct per worker.
#   * Push queue: with REDIS_URL set, queued pushes live in Redis and any
#     worker's consumer can process them — multi-worker safe by design.
# The on_starting hook below logs a WARNING when workers > 1 so a scale-up
# without this alignment is visible immediately in the logs.


def on_starting(server):
    """Log key env vars once when the Gunicorn master starts."""
    import logging
    if workers > 1:
        logging.getLogger('gunicorn.error').warning(
            '[startup] WEB_CONCURRENCY=%s (>1): verify DB pool budget '
            '(workers × (pool_size+max_overflow) connections), disable the '
            'attendance/fee-reminder schedulers in all but one worker, and set '
            'RATELIMIT_STORAGE_URI to Redis for global rate limits. '
            'See the P3 scaling notes in gunicorn.conf.py.', workers)
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
    # The effective worker-lifecycle values, so a recycling surprise is always
    # explainable from the logs alone.
    logging.getLogger('gunicorn.error').info(
        '[startup] worker lifecycle — max_requests=%s (%s)  '
        'max_requests_jitter=%s (%s)  timeout=%ss  graceful_timeout=%ss  '
        '%s',
        max_requests, 'explicit' if _max_requests_explicit else 'default',
        max_requests_jitter, 'explicit' if _jitter_explicit else 'default',
        timeout, graceful_timeout,
        'recycling DISABLED — the single worker serves until restart/deploy'
        if max_requests == 0 else
        'recycling enabled — ensure another worker can serve the handover',
    )
    if _jitter_overridden_to_zero:
        logging.getLogger('gunicorn.error').warning(
            '[startup] GUNICORN_MAX_REQUESTS_JITTER=%s was ignored because '
            'max_requests=0. Jitter is added to max_requests and the sum '
            'becomes the limit, so a non-zero jitter would have recycled the '
            'worker after a small random number of requests.',
            _jitter_overridden_to_zero)


def _worker_dotenv_path():
    """The .env file the WORKER will actually load.

    config/settings.py calls bare load_dotenv(), so python-dotenv searches upward
    from the directory of config/settings.py — not from the current working
    directory. Replicate that search, because the two can select different files
    (e.g. a config/.env would win for the worker and be missed here).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    start = os.path.join(here, 'config')
    directory = start if os.path.isdir(start) else here
    while True:
        candidate = os.path.join(directory, '.env')
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def _dotenv_as_worker_resolves_it(path):
    """Read .env with the WORKER's ${VARIABLE} expansion precedence.

    python-dotenv expands references differently depending on `override`:
      * load_dotenv(override=False) — what config/settings.py does — resolves a
        reference against os.environ FIRST, then the file;
      * dotenv_values() hardcodes override=True, resolving against the file
        first, then os.environ.
    When the referenced variable exists in both places the two disagree, so the
    master must use the worker's precedence or it can bind a different port.
    See https://bbc2.github.io/python-dotenv/#variable-expansion.
    """
    try:
        from dotenv.main import DotEnv
        return DotEnv(dotenv_path=path, override=False, interpolate=True,
                      encoding='utf-8').dict()
    except Exception:
        from dotenv import dotenv_values      # fallback: different expansion precedence
        return dotenv_values(path)


def _effective_setting(key, default=None):
    """Resolve a setting exactly the way the WORKER will resolve it.

    config/settings.py calls load_dotenv() when the application is imported, and
    with preload_app=False that happens in the worker — never in this master
    process. So a value that lives only in .env is invisible to os.environ here.
    Reading os.environ alone would let the master bind a port the worker never
    uses (or bind one at all when the receiver is disabled in .env).

    A real environment variable wins over the file, matching
    load_dotenv(override=False). Only the WS port, the enabled flag and PORT are
    ever read, and no value is logged.
    """
    if key in os.environ:
        return os.environ[key]
    try:
        path = _worker_dotenv_path()
        if path:
            value = _dotenv_as_worker_resolves_it(path).get(key)
            if value is not None:
                return value
    except Exception:
        pass            # python-dotenv absent or .env unreadable → fall back
    return default


def when_ready(server):
    """Bind the AI Face WebSocket port in the MASTER, before any worker forks.

    The receiver itself keeps running inside the worker (it needs the Flask app
    context), but the LISTENING socket now belongs to the arbiter and is
    inherited by every worker across fork — the same ownership model Gunicorn
    already uses for the HTTP listener. Consequences:

      * a worker recycle or crash no longer closes the listener, so AI Face
        devices reconnecting during the gap queue in the backlog instead of
        getting ConnectionRefusedError;
      * workers no longer race to bind the port, which removes the EADDRINUSE
        path that silently left a worker without a receiver.

    Only the fd is published (AIFACE_WS_FD); a worker validates it before use
    and falls back to binding the port itself if anything does not match.
    NOTE for WEB_CONCURRENCY > 1: every worker would then accept device
    connections, but send_command_to_device()/queue_command_for_device() are
    per-process, so outbound commands only reach devices attached to the worker
    handling that request. Cross-worker command routing must be solved before
    scaling out; see the P3 notes above.
    """
    import logging
    import socket
    log = logging.getLogger('gunicorn.error')
    # Resolve exactly what the worker will resolve (environment first, then .env).
    if str(_effective_setting('AIFACE_WS_ENABLED', 'true')).lower() in ('0', 'false', 'no'):
        log.info('[aiface] receiver disabled — master is not binding the WS port')
        return
    if getattr(server, '_aiface_ws_sock', None) is not None:
        return                                  # reload: keep the socket we already own
    try:
        port = int(str(_effective_setting('AIFACE_WS_PORT', 7788)).strip())
    except (TypeError, ValueError):
        log.error('[aiface] AIFACE_WS_PORT is not an integer — master is not binding the WS port; '
                  'the worker will report the same problem.')
        return
    if not 1 <= port <= 65535:
        # bind() raises OverflowError (not OSError) for out-of-range ports, which
        # would escape when_ready() and abort the master. Reject it as what it is:
        # a configuration error.
        log.error('[aiface] AIFACE_WS_PORT=%s is outside the valid range 1-65535 — master is not '
                  'binding the WS port; fix the configuration.', port)
        return
    web_port = _effective_setting('PORT')
    if web_port and str(port) == str(web_port):
        log.error('[aiface] AIFACE_WS_PORT (%s) equals PORT — not binding the WS listener '
                  'in the master; fix the environment.', port)
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('0.0.0.0', port))
        sock.listen(128)
        sock.set_inheritable(True)
    except (OSError, OverflowError) as exc:
        sock.close()
        log.error('[aiface] master could not bind the AI Face WS port %s (%s) — workers will '
                  'fall back to binding it themselves.', port, exc)
        return
    server._aiface_ws_sock = sock               # keep a reference: closing it would kill the listener
    os.environ['AIFACE_WS_FD'] = str(sock.fileno())
    log.info('[aiface] master owns the AI Face WS listening socket on 0.0.0.0:%s (fd=%s) — '
             'it survives worker recycling', port, sock.fileno())


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
