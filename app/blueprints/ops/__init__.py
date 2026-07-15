"""Operational health & metrics endpoints (P3).

GET /ops/health        — unauthenticated liveness probe. Returns only
                         {'ok': true, 'status': 'up'} — no DB access, no
                         configuration, no tenant data, nothing enumerable.
GET /ops/health/deep   — guarded readiness/diagnostics: DB round-trip time,
                         Redis reachability, durable-queue depth. Component
                         states only — never tenant data.
GET /ops/metrics       — guarded aggregate metrics snapshot from
                         app/utils/observability.py (endpoint timings,
                         external-service latency, counters).

Guard (fail closed):
  * an authenticated super-admin web session, OR
  * header X-Ops-Token equal (constant-time compare) to OPS_METRICS_TOKEN when
    that config value is non-empty.
Anything else → 404, so unauthorized callers cannot even confirm the
endpoints exist. GET-only and side-effect free; no CSRF surface.
"""
import hmac
import time

from flask import Blueprint, current_app, jsonify, request

ops_bp = Blueprint('ops', __name__)


def _authorized() -> bool:
    token = current_app.config.get('OPS_METRICS_TOKEN') or ''
    supplied = request.headers.get('X-Ops-Token', '')
    if token and supplied and hmac.compare_digest(token, supplied):
        return True
    try:
        from flask_login import current_user
        return bool(current_user.is_authenticated
                    and getattr(current_user, 'is_super_admin', False))
    except Exception:
        return False


@ops_bp.route('/ops/health')
def health():
    return jsonify({'ok': True, 'status': 'up'})


@ops_bp.route('/ops/health/deep')
def health_deep():
    if not _authorized():
        return jsonify({'ok': False, 'error': 'not_found'}), 404

    checks = {}

    # Database round trip — the one external dependency core operations need.
    try:
        from sqlalchemy import text
        from app.models import db
        t0 = time.perf_counter()
        db.session.execute(text('SELECT 1'))
        checks['database'] = {'up': True,
                              'latency_ms': round((time.perf_counter() - t0) * 1000, 1)}
    except Exception as exc:
        checks['database'] = {'up': False, 'error': type(exc).__name__}

    # Redis — optional; 'configured': False is a normal state, not a failure.
    try:
        from app.services.redis_client import get_redis, redis_configured
        if not redis_configured():
            checks['redis'] = {'configured': False}
        else:
            r = get_redis()
            checks['redis'] = {'configured': True, 'up': r is not None}
    except Exception as exc:
        checks['redis'] = {'configured': True, 'up': False, 'error': type(exc).__name__}

    # Durable push queue depth (0/absent when Redis is not in use).
    try:
        from app.services import durable_queue
        checks['push_queue'] = durable_queue.stats()
    except Exception as exc:
        checks['push_queue'] = {'error': type(exc).__name__}

    degraded = not checks.get('database', {}).get('up', False)
    return jsonify({'ok': not degraded, 'checks': checks}), (503 if degraded else 200)


@ops_bp.route('/ops/metrics')
def metrics():
    if not _authorized():
        return jsonify({'ok': False, 'error': 'not_found'}), 404
    from app.utils import observability
    return jsonify({'ok': True, 'metrics': observability.snapshot()})
