"""
Mobile API — Driver endpoints (transport, Phase 1: routes + trip lifecycle)
==========================================================================
All routes require:  Authorization: Bearer <access_token>   (role: driver)

Endpoint map
────────────
GET  /driver/routes                        this driver's active assigned routes
POST /driver/routes/<route_id>/trips/start start (or return) the route's active trip
POST /driver/trips/<trip_id>/end           end this driver's trip (idempotent)

Security rules
──────────────
• The driver identity is ALWAYS derived server-side: authenticated User →
  the single active Employee with Employee.user_id == user.id in the user's own
  school. No school_id / driver_id / employee_id is ever read from the request.
• Every route / trip query is pinned to that Employee AND its school_id, so an
  ID-swap to another driver's or another school's route/trip returns 404.
• No location data exists in this phase.
"""
from datetime import datetime, timezone

from flask import g
from sqlalchemy import and_
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
