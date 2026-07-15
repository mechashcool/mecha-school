"""Bounded background executor for post-commit side effects (FCM pushes,
notification fan-out).

Why this exists (P0): FCM delivery is one blocking HTTPS round-trip to Google
per device token. Grade entry, exam creation, homework creation, and chat
message sends were performing dozens of those calls inside the request thread
(gunicorn runs 1 worker × 4 threads in production), pinning latency-sensitive
API requests for the whole fan-out. Tasks submitted here run on a small
dedicated thread pool instead, AFTER the core database work has committed.

Contract for submitted tasks — read before adding a caller:
  * Submit ONLY after the core operation's own commit. A task failure can then
    never corrupt or roll back the committed operation.
  * Pass PRIMITIVES ONLY (ints, strs, dicts) — never ORM objects. The task runs
    in a fresh app context with its own scoped session; request-session objects
    would be detached/expired there.
  * Every DB query inside a task must carry explicit ownership filters
    (school_id equality, ownership junctions, bypass_tenant_scope=True where
    the helper is documented context-independent). There is NO request context
    and therefore NO ORM tenant scope in the background thread — implicit
    scoping must never be relied on.
  * Tasks must be idempotent or tolerate at-most-once execution: on worker
    recycle/shutdown a queued task may be lost (documented limitation); tasks
    are never executed twice by this module.

Failure/saturation semantics:
  * A task exception is logged and swallowed — it never propagates to a request.
  * When the pending-task bound is reached, the task runs INLINE in the caller
    (the exact pre-P0 behaviour) rather than being dropped — work is never
    silently discarded.
  * With ``app.testing`` or config ``ASYNC_DISPATCH_SYNC=True`` tasks always
    run inline, keeping tests deterministic.
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger('mecha.async_dispatch')

_MAX_WORKERS = max(1, int(os.environ.get('ASYNC_DISPATCH_WORKERS', '2')))
_MAX_PENDING = max(1, int(os.environ.get('ASYNC_DISPATCH_MAX_PENDING', '200')))

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_pending = threading.BoundedSemaphore(_MAX_PENDING)


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=_MAX_WORKERS,
                    thread_name_prefix='mecha-dispatch',
                )
    return _executor


def _inc(name: str) -> None:
    """Best-effort observability counter (P3) — never affects dispatch."""
    try:
        from app.utils.observability import inc
        inc(name)
    except Exception:
        pass


def _run_inline(fn, args, kwargs) -> bool:
    """Execute fn synchronously in the caller's context (pre-P0 behaviour)."""
    _inc('dispatch.inline')
    try:
        fn(*args, **kwargs)
    except Exception:
        log.exception('[dispatch] inline task %s failed',
                      getattr(fn, '__name__', repr(fn)))
    return False


def submit(fn, *args, **kwargs) -> bool:
    """Run ``fn(*args, **kwargs)`` on a background thread inside a fresh app
    context. Must be called from inside a Flask request/app context (the real
    app object is captured via ``current_app``).

    Returns True when the task was queued for background execution, False when
    it ran inline (testing mode, saturation, or executor unavailable). In every
    case the work is executed exactly once and never dropped.
    """
    from flask import current_app
    app = current_app._get_current_object()

    if app.testing or app.config.get('ASYNC_DISPATCH_SYNC'):
        return _run_inline(fn, args, kwargs)

    # P3: registered tasks go to the durable Redis queue when available, so
    # queued work survives worker recycling/restarts. Falls through to the P0
    # thread pool on ANY durable-path miss (not registered, Redis off/down,
    # serialization failure) — behaviour then identical to pre-P3.
    try:
        from app.services import durable_queue
        if durable_queue.try_enqueue(app, fn, args, kwargs):
            return True
    except Exception:
        log.exception('[dispatch] durable enqueue path failed — using thread pool')

    if not _pending.acquire(blocking=False):
        _inc('dispatch.saturated')
        log.warning('[dispatch] queue saturated (%d pending) — running %s inline',
                    _MAX_PENDING, getattr(fn, '__name__', repr(fn)))
        return _run_inline(fn, args, kwargs)

    def _task():
        try:
            with app.app_context():
                try:
                    fn(*args, **kwargs)
                except Exception:
                    log.exception('[dispatch] background task %s failed',
                                  getattr(fn, '__name__', repr(fn)))
                finally:
                    # The background thread gets its own scoped session; always
                    # return its connection to the pool.
                    try:
                        from app.models import db
                        db.session.remove()
                    except Exception:
                        pass
        finally:
            _pending.release()

    try:
        _get_executor().submit(_task)
        _inc('dispatch.queued')
        return True
    except Exception:
        # Interpreter shutting down / executor unavailable — last-resort inline.
        _pending.release()
        return _run_inline(fn, args, kwargs)
