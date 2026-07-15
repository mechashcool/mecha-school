"""In-process observability layer (P3).

What it records — aggregates ONLY, in bounded process memory:
  * per-endpoint request timing (count, errors, total/max duration, slow count),
  * per-service external-call latency (Supabase sign/fetch/upload/delete, FCM),
  * named counters (dispatch queue activity, durable-queue activity, slow queries).

What it deliberately NEVER records — this module must stay safe to expose to a
super admin / monitoring token without any tenant filtering:
  * request bodies, query strings, headers, cookies, tokens,
  * SQL bind parameters or result rows,
  * any school / user / student identifier.
Keys are Flask endpoint names (code identifiers), service names, and counter
names — never data values.

Isolation note: the numbers aggregate traffic from ALL schools of this
deployment. That is operational telemetry about the service itself, not school
data, and it is only served by the guarded /ops endpoints (super admin or
OPS_METRICS_TOKEN — see app/blueprints/ops).

Failure semantics: every hook is wrapped so a metrics failure can never break
a request, a query, or an external call. With OBSERVABILITY_ENABLED=false the
hooks are not registered at all (behaviour-identical rollback).
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

log = logging.getLogger('mecha.observability')

# Hard bound on distinct aggregation keys per family, so a scanner probing
# thousands of nonexistent URLs cannot grow process memory (unmatched routes
# aggregate under one key anyway because endpoint is None → '_unrouted').
_MAX_KEYS = 300

_lock = threading.Lock()
_started_at = time.time()

# key → [count, error_count, total_ms, max_ms, slow_count]
_requests: dict[str, list] = {}
# service → [count, error_count, total_ms, max_ms]
_external: dict[str, list] = {}
# name → int
_counters: dict[str, int] = {}


def _bucket(store: dict, key: str):
    if key not in store and len(store) >= _MAX_KEYS:
        key = '_overflow'
    return store.setdefault(key, [0, 0, 0.0, 0.0, 0])


def record_request(endpoint: str, status: int, dur_ms: float, slow: bool) -> None:
    try:
        with _lock:
            b = _bucket(_requests, endpoint or '_unrouted')
            b[0] += 1
            if status >= 500:
                b[1] += 1
            b[2] += dur_ms
            if dur_ms > b[3]:
                b[3] = dur_ms
            if slow:
                b[4] += 1
    except Exception:
        pass


def record_external(service: str, dur_ms: float, ok: bool) -> None:
    try:
        with _lock:
            b = _bucket(_external, service)
            b[0] += 1
            if not ok:
                b[1] += 1
            b[2] += dur_ms
            if dur_ms > b[3]:
                b[3] = dur_ms
    except Exception:
        pass


def inc(name: str, n: int = 1) -> None:
    try:
        with _lock:
            if name not in _counters and len(_counters) >= _MAX_KEYS:
                name = '_overflow'
            _counters[name] = _counters.get(name, 0) + n
    except Exception:
        pass


@contextmanager
def observe_external(service: str):
    """Time an outbound call. The wrapped exception (if any) still propagates —
    this context manager only records, it never changes control flow."""
    t0 = time.perf_counter()
    ok = True
    try:
        yield
    except Exception:
        ok = False
        raise
    finally:
        record_external(service, (time.perf_counter() - t0) * 1000.0, ok)


def snapshot() -> dict:
    """Aggregate view for /ops/metrics. Contains no tenant data (see module doc)."""
    with _lock:
        reqs = {
            k: {
                'count': v[0], 'errors_5xx': v[1],
                'avg_ms': round(v[2] / v[0], 1) if v[0] else 0.0,
                'max_ms': round(v[3], 1), 'slow': v[4],
            }
            for k, v in _requests.items()
        }
        ext = {
            k: {
                'count': v[0], 'failures': v[1],
                'avg_ms': round(v[2] / v[0], 1) if v[0] else 0.0,
                'max_ms': round(v[3], 1),
            }
            for k, v in _external.items()
        }
        counters = dict(_counters)
    return {
        'uptime_seconds': int(time.time() - _started_at),
        'requests': reqs,
        'external': ext,
        'counters': counters,
    }


def reset_for_tests() -> None:
    with _lock:
        _requests.clear()
        _external.clear()
        _counters.clear()


# ─── Flask / SQLAlchemy wiring ────────────────────────────────────────────────

_sql_listeners_registered = False
# Module-level thresholds refreshed by init_app so the (global, class-level)
# SQLAlchemy listeners don't need an app context on every query.
_slow_query_ms = 500


def _register_sql_listeners() -> None:
    """Class-level cursor-timing listeners: attach once per process, no engine
    (and therefore no DB connection) is created here. Bind parameters are never
    read or logged — only the statement text, truncated."""
    global _sql_listeners_registered
    if _sql_listeners_registered:
        return
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    @event.listens_for(Engine, 'before_cursor_execute')
    def _before_cursor(conn, cursor, statement, parameters, context, executemany):
        conn.info.setdefault('_mecha_q_start', []).append(time.perf_counter())

    @event.listens_for(Engine, 'after_cursor_execute')
    def _after_cursor(conn, cursor, statement, parameters, context, executemany):
        try:
            starts = conn.info.get('_mecha_q_start')
            if not starts:
                return
            dur_ms = (time.perf_counter() - starts.pop()) * 1000.0
            if _slow_query_ms and dur_ms >= _slow_query_ms:
                inc('db.slow_queries')
                log.warning('[slow-query] dur_ms=%d stmt=%s',
                            int(dur_ms), ' '.join(statement.split())[:300])
        except Exception:
            pass

    _sql_listeners_registered = True


def init_app(app) -> None:
    """Register request-timing hooks and slow-query listeners.

    No-op when OBSERVABILITY_ENABLED is false — nothing is registered and the
    request path is byte-identical to pre-P3 behaviour.
    """
    if not app.config.get('OBSERVABILITY_ENABLED', True):
        return

    global _slow_query_ms
    _slow_query_ms = int(app.config.get('SLOW_QUERY_MS', 500))
    slow_request_ms = int(app.config.get('SLOW_REQUEST_MS', 1500))

    _register_sql_listeners()

    from flask import g, request

    @app.before_request
    def _obs_start():
        g._obs_t0 = time.perf_counter()

    @app.after_request
    def _obs_finish(response):
        try:
            t0 = getattr(g, '_obs_t0', None)
            if t0 is None or request.endpoint == 'static':
                return response
            dur_ms = (time.perf_counter() - t0) * 1000.0
            slow = bool(slow_request_ms) and dur_ms >= slow_request_ms
            record_request(request.endpoint, response.status_code, dur_ms, slow)
            if slow:
                # request.path only — NEVER the query string (it can carry
                # signed-URL tokens) and never body/header content.
                log.warning('[slow-request] %s %s status=%s dur_ms=%d',
                            request.method, request.path,
                            response.status_code, int(dur_ms))
        except Exception:
            pass
        return response
