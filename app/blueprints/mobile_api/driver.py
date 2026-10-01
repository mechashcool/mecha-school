"""
Mobile API — Driver endpoints (transport, Phase 1: routes + trip lifecycle)
==========================================================================
All routes require:  Authorization: Bearer <access_token>   (role: driver)

Endpoint map
────────────
GET  /driver/routes                        this driver's active assigned routes
POST /driver/routes/<route_id>/trips/start start (or return) the route's active trip
POST /driver/trips/<trip_id>/end           end this driver's trip (idempotent)
POST /driver/trips/<trip_id>/location      latest GPS fix (in-place UPDATE, Phase 2)

Security rules
──────────────
• The driver identity is ALWAYS derived server-side: authenticated User →
  the single active Employee with Employee.user_id == user.id in the user's own
  school. No school_id / driver_id / employee_id is ever read from the request.
• Every route / trip query is pinned to that Employee AND its school_id, so an
  ID-swap to another driver's or another school's route/trip returns 404.
• Only the LATEST location is stored, on the trip row itself: a GPS ping is
  one UPDATE, never an INSERT, and is not audit-logged.
"""
import math
from datetime import datetime, timezone

from flask import g, request
from sqlalchemy import and_, exists, update
from sqlalchemy.exc import IntegrityError

from app.models import db, Employee, TransportRoute, TransportTrip
from app.utils.audit import log_action

from . import mobile_api_bp
from .utils import jwt_required, role_required, ok, err

_OPTS = {'bypass_tenant_scope': True}


def _driver_employee() -> Employee | None:
    """The authenticated driver's Employee, or None (fail closed).

    Pinned to the user's own school and to an active Employee. If more than one
    Employee is linked to the same User (Employee.user_id is not unique in the
    schema) the identity is ambiguous, so access is refused.
    """
    user = g.mobile_user
    if not user.school_id:
        return None
    rows = (Employee.query
            .execution_options(**_OPTS)
            .filter(Employee.user_id == user.id,
                    Employee.school_id == user.school_id,
                    Employee.status == 'active')
            .limit(2)
            .all())
    return rows[0] if len(rows) == 1 else None


def _utc_iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def _trip_payload(trip: TransportTrip) -> dict:
    return {
        'id':         trip.id,
        'route_id':   trip.route_id,
        'status':     trip.status,
        'started_at': _utc_iso(trip.started_at),
        'ended_at':   _utc_iso(trip.ended_at),
    }


def _active_trip(route_id: int, school_id: int) -> TransportTrip | None:
    return (TransportTrip.query
            .execution_options(**_OPTS)
            .filter_by(route_id=route_id, school_id=school_id, status='active')
            .first())


# ─── Assigned routes ──────────────────────────────────────────────────────────

@mobile_api_bp.route('/driver/routes', methods=['GET'])
@jwt_required()
@role_required('driver')
def driver_routes():
    """Active routes assigned to the authenticated driver, each with its active
    trip (or null). ONE set-based query (route LEFT JOIN active trip)."""
    emp = _driver_employee()
    if emp is None:
        return err('driver_not_linked', 403)

    rows = (db.session.query(TransportRoute, TransportTrip)
            .outerjoin(TransportTrip, and_(
                TransportTrip.route_id == TransportRoute.id,
                TransportTrip.school_id == TransportRoute.school_id,
                TransportTrip.status == 'active'))
            .filter(TransportRoute.school_id == emp.school_id,
                    TransportRoute.driver_employee_id == emp.id,
                    TransportRoute.status == 'active')
            .order_by(TransportRoute.name, TransportRoute.id)
            .execution_options(**_OPTS)
            .all())

    return ok(routes=[
        {
            'id':           route.id,
            'name':         route.name,
            'vehicle_name': route.vehicle_type,
            'active_trip':  _trip_payload(trip) if trip else None,
        }
        for route, trip in rows
    ])


# ─── Start trip ───────────────────────────────────────────────────────────────

@mobile_api_bp.route('/driver/routes/<int:route_id>/trips/start', methods=['POST'])
@jwt_required()
@role_required('driver')
def driver_trip_start(route_id):
    """Start a trip on an ACTIVE route assigned to this driver.

    200 + existing trip when this driver already has the route's active trip
    (no duplicate). 201 + new trip otherwise. 409 when the route is inactive or
    its active trip belongs to another driver. 404 for any route that is not
    this driver's (same response whether it exists elsewhere or not).
    """
    emp = _driver_employee()
    if emp is None:
        return err('driver_not_linked', 403)

    route = (TransportRoute.query
             .execution_options(**_OPTS)
             .filter_by(id=route_id, school_id=emp.school_id,
                        driver_employee_id=emp.id)
             .first())
    if route is None:
        return err('route_not_found', 404)
    if route.status != 'active':
        return err('route_inactive', 409)

    existing = _active_trip(route.id, emp.school_id)
    if existing is not None:
        if existing.driver_employee_id != emp.id:
            return err('trip_active_other_driver', 409)
        return ok(trip=_trip_payload(existing))

    trip = TransportTrip(school_id=emp.school_id, route_id=route.id,
                         driver_employee_id=emp.id, status='active',
                         started_at=datetime.utcnow())
    db.session.add(trip)
    try:
        db.session.commit()
    except IntegrityError:
        # Concurrent start: the partial unique index allowed only one.
        db.session.rollback()
        existing = _active_trip(route.id, emp.school_id)
        if existing is not None and existing.driver_employee_id == emp.id:
            return ok(trip=_trip_payload(existing))
        return err('trip_active_other_driver', 409)

    payload = _trip_payload(trip)
    log_action('trip_start', 'transport_trip', payload['id'],
               details=f'route={payload["route_id"]} driver_employee={emp.id}')
    resp = ok(trip=payload)
    resp.status_code = 201
    return resp


# ─── End trip ─────────────────────────────────────────────────────────────────

@mobile_api_bp.route('/driver/trips/<int:trip_id>/end', methods=['POST'])
@jwt_required()
@role_required('driver')
def driver_trip_end(trip_id):
    """End this driver's trip. Idempotent: an already-ended trip is returned
    unchanged with no write. Allowed even if the route was since reassigned or
    deactivated — a driver can always close their own open trip."""
    emp = _driver_employee()
    if emp is None:
        return err('driver_not_linked', 403)

    trip = (TransportTrip.query
            .execution_options(**_OPTS)
            .filter_by(id=trip_id, school_id=emp.school_id,
                       driver_employee_id=emp.id)
            .first())
    if trip is None:
        return err('trip_not_found', 404)

    if trip.status != 'active':
        return ok(trip=_trip_payload(trip))

    trip.status = 'ended'
    trip.ended_at = datetime.utcnow()
    payload = _trip_payload(trip)
    db.session.commit()
    log_action('trip_end', 'transport_trip', payload['id'],
               details=f'route={payload["route_id"]} driver_employee={emp.id}')
    return ok(trip=payload)


# ─── Latest GPS location (Phase 2) ────────────────────────────────────────────

def _finite_number(value):
    """A real int/float (not bool, not string) that is finite, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _parse_recorded_at(value):
    """Device timestamp → naive UTC datetime, or None.

    Metadata only: a missing, malformed or absurd value is dropped (stored as
    NULL) instead of failing the update — location_updated_at (server time) is
    what freshness and every check rely on.
    """
    if not isinstance(value, str) or not value or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed if 2000 <= parsed.year <= 2100 else None


def _parse_location(body):
    """Validate the payload BEFORE any DB access. Returns (values, error)."""
    if not isinstance(body, dict):
        return None, 'invalid_payload'
    lat = _finite_number(body.get('latitude'))
    lng = _finite_number(body.get('longitude'))
    if lat is None or not -90.0 <= lat <= 90.0:
        return None, 'invalid_latitude'
    if lng is None or not -180.0 <= lng <= 180.0:
        return None, 'invalid_longitude'
    accuracy = body.get('accuracy')
    if accuracy is not None:
        accuracy = _finite_number(accuracy)
        if accuracy is None or accuracy < 0:
            return None, 'invalid_accuracy'
    return {
        'latitude':             lat,
        'longitude':            lng,
        'location_accuracy':    accuracy,
        'location_recorded_at': _parse_recorded_at(body.get('recorded_at')),
    }, None


@mobile_api_bp.route('/driver/trips/<int:trip_id>/location', methods=['POST'])
@jwt_required()
@role_required('driver')
def driver_trip_location(trip_id):
    """Store the trip's LATEST location (overwrite in place — no history row,
    no audit row). Called every ~10–15 s while a trip is active.

    Hot path = ONE conditional UPDATE whose WHERE carries every check: the trip
    is this school's, active, driven by an active Employee linked to the
    authenticated user, on an active route of the same school still assigned
    to that Employee. Only when no row matches are the (rare) diagnostic reads
    run to pick the error: 403 driver_not_linked / 404 trip_not_found (other
    driver, other school or unknown — indistinguishable) / 409 trip_not_active
    / 409 route_inactive / 409 route_unassigned.

    Body: {"latitude": float, "longitude": float,
           "accuracy": float ≥ 0 (optional), "recorded_at": ISO-8601 (optional)}
    Any identity field in the body (school_id, driver_id, route_id …) is ignored.
    """
    values, error = _parse_location(request.get_json(silent=True))
    if error:
        return err(error, 400)

    user = g.mobile_user
    if not user.school_id:
        return err('driver_not_linked', 403)

    now = datetime.utcnow()
    route_ok = exists().where(
        TransportRoute.id == TransportTrip.route_id,
        TransportRoute.school_id == TransportTrip.school_id,
        TransportRoute.status == 'active',
        TransportRoute.driver_employee_id == TransportTrip.driver_employee_id,
    ).correlate(TransportTrip)
    driver_ok = exists().where(
        Employee.id == TransportTrip.driver_employee_id,
        Employee.school_id == TransportTrip.school_id,
        Employee.user_id == user.id,
        Employee.status == 'active',
    ).correlate(TransportTrip)
    result = db.session.execute(
        update(TransportTrip)
        .where(TransportTrip.id == trip_id,
               TransportTrip.school_id == user.school_id,
               TransportTrip.status == 'active',
               driver_ok, route_ok)
        .values(location_updated_at=now, **values)
        .execution_options(synchronize_session=False))
    if result.rowcount == 1:
        # Commit WITHOUT expiring the request's already-loaded objects (the
        # authenticated User / Role). The UPDATE above is a bulk statement, so no
        # loaded object is stale — but a normal commit would expire them and the
        # global after-request badge hook would then re-SELECT user + role +
        # role_schools on every ping (3 extra statements, ~every 10 s per bus).
        sess = db.session()
        previous = sess.expire_on_commit
        sess.expire_on_commit = False
        try:
            sess.commit()
        finally:
            sess.expire_on_commit = previous
        return ok(location_updated_at=_utc_iso(now))

    # ── No row updated: diagnose (rare path, read-only) ──────────────────────
    db.session.rollback()
    emp = _driver_employee()
    if emp is None:
        return err('driver_not_linked', 403)
    row = (db.session.query(TransportTrip.status, TransportRoute.status,
                            TransportRoute.driver_employee_id)
           .join(TransportRoute, TransportRoute.id == TransportTrip.route_id)
           .filter(TransportTrip.id == trip_id,
                   TransportTrip.school_id == emp.school_id,
                   TransportTrip.driver_employee_id == emp.id,
                   TransportRoute.school_id == emp.school_id)
           .execution_options(**_OPTS)
           .first())
    if row is None:
        return err('trip_not_found', 404)
    trip_status, route_status, route_driver = row
    if trip_status != 'active':
        return err('trip_not_active', 409)
    if route_status != 'active':
        return err('route_inactive', 409)
    if route_driver != emp.id:
        return err('route_unassigned', 409)
    return err('trip_not_found', 404)
