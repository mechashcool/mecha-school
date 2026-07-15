"""Durable Redis-backed queue for background push work (P3).

Why: the P0 in-process dispatch pool loses every queued task when a Gunicorn
worker recycles (max_requests=500), restarts, or crashes. With Redis
configured, registered tasks are enqueued as JSON jobs in a Redis list
instead, so queued pushes survive worker lifecycle events and temporary
process death. Without Redis (REDIS_URL unset, package missing, server down)
nothing changes: async_dispatch falls straight through to the P0 thread pool.

Task contract (same as async_dispatch, plus serialization):
  * Registered by explicit stable name via @durable_task('name') — an
    allow-list. Job payloads name a task; anything not registered is logged
    and dropped, never resolved dynamically (no import-by-string, no eval).
  * args/kwargs must be JSON-serializable primitives. Tuples become lists
    across the round trip — every registered task must accept that.
  * Ownership/isolation lives INSIDE the task: every DB query a task performs
    carries explicit school/user filters (there is no request context in the
    consumer). The queue itself is tenant-neutral transport; jobs are only
    ever produced by server-side code after authorization, never from client
    input.

Delivery semantics (honest statement):
  * Queued jobs are durable in Redis until executed.
  * Execution is at-least-once: a job is moved to a per-consumer processing
    list (BRPOPLPUSH) and removed only after it finishes. If a worker dies
    mid-job, the reclaimer moves it back to the main queue once the consumer's
    heartbeat key expires — so a crash at the exact wrong moment can deliver a
    push twice. Push notifications are display-only, so a rare duplicate is
    acceptable; tasks with non-idempotent side effects must NOT be registered.
  * A job that raises is retried up to DURABLE_QUEUE_MAX_ATTEMPTS total
    attempts (FCM per-device failures never raise — retries only cover
    infra/DB errors), then dropped with an ERROR log.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import socket
import threading
import time
import uuid

from app.services import redis_client
from app.utils.observability import inc

log = logging.getLogger('mecha.durable_queue')

_TASKS: dict[str, callable] = {}

_HEARTBEAT_TTL = 60      # s — orphaned processing lists reclaimable after this
_RECLAIM_EVERY = 60      # s between orphan scans
_POP_TIMEOUT = 5         # s — also bounds shutdown latency

_consumer_thread: threading.Thread | None = None
_stop = threading.Event()
_consumer_id = None


def durable_task(name: str):
    """Register a function as durably-queueable under a stable name."""
    def decorator(fn):
        existing = _TASKS.get(name)
        if existing is not None and existing is not fn:
            raise RuntimeError(f'durable task name already registered: {name}')
        _TASKS[name] = fn
        fn._durable_task_name = name
        return fn
    return decorator


def _main_key() -> str:
    return redis_client.key('pushq', 'v1')


def _processing_key(consumer_id: str) -> str:
    return redis_client.key('pushq', 'v1', 'processing', consumer_id)


def _alive_key(consumer_id: str) -> str:
    return redis_client.key('pushq', 'v1', 'alive', consumer_id)


def enabled(app) -> bool:
    return bool(app.config.get('DURABLE_PUSH_QUEUE_ENABLED')
                and app.config.get('REDIS_URL'))


def try_enqueue(app, fn, args, kwargs) -> bool:
    """Enqueue fn as a durable job. Returns False (caller falls back to the
    thread pool) whenever anything prevents durable queueing. Never raises."""
    try:
        name = getattr(fn, '_durable_task_name', None)
        if not name or _TASKS.get(name) is not fn or not enabled(app):
            return False
        r = redis_client.get_redis()
        if r is None:
            return False
        job = json.dumps({
            'id': uuid.uuid4().hex,
            'task': name,
            'args': list(args),
            'kwargs': dict(kwargs or {}),
            'attempts': 0,
            'enqueued_at': time.time(),
        })
        r.lpush(_main_key(), job)
        inc('durable.enqueued')
        return True
    except (TypeError, ValueError):
        log.error('[durable] task %s payload not JSON-serializable — '
                  'falling back to thread pool', getattr(fn, '__name__', fn))
        return False
    except Exception as exc:
        redis_client.mark_failed()
        log.warning('[durable] enqueue failed (%s) — falling back to thread pool',
                    type(exc).__name__)
        return False


def _execute(app, job: dict) -> None:
    """Run one job inside a fresh app context. Raises on task failure so the
    caller can decide about retry."""
    fn = _TASKS.get(job.get('task'))
    if fn is None:
        # Allow-list miss: never resolve dynamically. Dropped loudly.
        log.error('[durable] unknown task %r — job dropped', job.get('task'))
        inc('durable.unknown_task')
        return
    with app.app_context():
        try:
            fn(*job.get('args', []), **job.get('kwargs', {}))
        finally:
            try:
                from app.models import db
                db.session.remove()
            except Exception:
                pass


def _handle_failure(r, job: dict, exc: Exception, max_attempts: int = 3) -> None:
    attempts = int(job.get('attempts', 0)) + 1
    if attempts < max_attempts:
        job['attempts'] = attempts
        try:
            r.lpush(_main_key(), json.dumps(job))
            inc('durable.retried')
            log.warning('[durable] task %s failed (%s) — re-queued, attempt %d/%d',
                        job.get('task'), type(exc).__name__, attempts, max_attempts)
            return
        except Exception:
            redis_client.mark_failed()
    inc('durable.dropped')
    log.error('[durable] task %s failed permanently after %d attempts: %s',
              job.get('task'), attempts, exc)


def _reclaim_orphans(r, own_id: str) -> None:
    """Move jobs stuck in processing lists of dead consumers back to the main
    queue. A consumer is dead when its heartbeat key has expired. RPOPLPUSH is
    atomic, so concurrent reclaimers move each job exactly once."""
    try:
        pattern = redis_client.key('pushq', 'v1', 'processing', '*')
        for pkey in r.scan_iter(match=pattern, count=50):
            consumer_id = pkey.rsplit(':', 1)[-1]
            if consumer_id == own_id:
                continue
            if r.exists(_alive_key(consumer_id)):
                continue
            moved = 0
            while r.rpoplpush(pkey, _main_key()) is not None:
                moved += 1
                if moved >= 1000:   # safety bound per scan pass
                    break
            if moved:
                inc('durable.reclaimed', moved)
                log.warning('[durable] reclaimed %d job(s) from dead consumer %s',
                            moved, consumer_id)
    except Exception as exc:
        log.warning('[durable] orphan reclaim failed: %s', type(exc).__name__)


def _consumer_loop(app) -> None:
    own_id = _consumer_id
    processing = _processing_key(own_id)
    last_reclaim = 0.0
    max_attempts = int(app.config.get('DURABLE_QUEUE_MAX_ATTEMPTS', 3))
    while not _stop.is_set():
        r = redis_client.get_redis()
        if r is None:
            # Redis down — jobs (if any) wait safely inside Redis; producers
            # fall back to the thread pool meanwhile. Sleep in short slices so
            # shutdown stays responsive.
            _stop.wait(_POP_TIMEOUT)
            continue
        try:
            r.set(_alive_key(own_id), '1', ex=_HEARTBEAT_TTL)
            if time.time() - last_reclaim > _RECLAIM_EVERY:
                _reclaim_orphans(r, own_id)
                last_reclaim = time.time()

            raw = r.brpoplpush(_main_key(), processing, timeout=_POP_TIMEOUT)
            if raw is None:
                continue
            try:
                job = json.loads(raw)
            except ValueError:
                log.error('[durable] undecodable job dropped')
                r.lrem(processing, 1, raw)
                continue
            try:
                _execute(app, job)
                inc('durable.processed')
            except Exception as exc:
                _handle_failure(r, job, exc, max_attempts)
            finally:
                try:
                    r.lrem(processing, 1, raw)
                except Exception:
                    redis_client.mark_failed()
        except Exception as exc:
            redis_client.mark_failed()
            log.warning('[durable] consumer iteration failed (%s) — backing off',
                        type(exc).__name__)
            _stop.wait(_POP_TIMEOUT)
    # Graceful exit: drop the heartbeat so any leftover processing entry (none
    # in the normal path) is reclaimed quickly by another consumer.
    try:
        r = redis_client.get_redis()
        if r is not None:
            r.delete(_alive_key(own_id))
    except Exception:
        pass


def start_consumer(app) -> bool:
    """Start the per-worker consumer thread. No-op (False) when the durable
    queue is disabled or Redis is not configured."""
    global _consumer_thread, _consumer_id
    if not enabled(app):
        return False
    if _consumer_thread is not None and _consumer_thread.is_alive():
        return True
    _stop.clear()
    _consumer_id = f'{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}'
    real_app = app
    _consumer_thread = threading.Thread(
        target=_consumer_loop, args=(real_app,),
        name='mecha-durable-consumer', daemon=True,
    )
    _consumer_thread.start()
    atexit.register(stop_consumer)
    log.info('[durable] consumer started id=%s', _consumer_id)
    return True


def stop_consumer(timeout: float = 30.0) -> None:
    """Signal the consumer to finish its current job and exit. Called at
    interpreter shutdown; bounded well inside gunicorn's graceful_timeout."""
    _stop.set()
    t = _consumer_thread
    if t is not None and t.is_alive():
        t.join(timeout)


def stats() -> dict:
    """Queue depth for /ops — component state only, no job contents."""
    try:
        from flask import current_app
        if not enabled(current_app):
            return {'enabled': False}
        r = redis_client.get_redis()
        if r is None:
            return {'enabled': True, 'redis_up': False}
        return {'enabled': True, 'redis_up': True,
                'depth': r.llen(_main_key()),
                'consumer_alive': bool(_consumer_thread and _consumer_thread.is_alive())}
    except Exception as exc:
        return {'enabled': True, 'error': type(exc).__name__}
