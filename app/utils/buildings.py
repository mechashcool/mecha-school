"""
Mecha-School — Building-based data access helpers.

Optional second isolation layer *below* school_id.  Only active when a school
has School.enable_buildings=True.  Never replaces school_id scoping — always
apply school_id first, then (optionally) building scope.

Core rules
----------
* Feature OFF (school.enable_buildings is False)  → these helpers are no-ops:
  no filtering, every user sees everything as before.
* User with NO UserBuildingAccess rows            → UNRESTRICTED (sees all
  buildings within their normal permissions).
* User with one or more UserBuildingAccess rows   → RESTRICTED to those
  buildings only.
* Super admin is never building-restricted.

A restricted user only sees students whose building_id is in their allowed set;
students with no building (building_id IS NULL) are NOT visible to restricted
users by design.
"""
from __future__ import annotations

from flask_login import current_user


def school_buildings_enabled(school) -> bool:
    """True when the given School row has the buildings feature turned on."""
    return bool(school is not None and getattr(school, 'enable_buildings', False))


def get_active_buildings(school_id: int | None):
    """Return active SchoolBuilding rows for a school (ordered by name)."""
    if not school_id:
        return []
    from app.models import SchoolBuilding
    return (SchoolBuilding.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school_id, is_active=True)
            .order_by(SchoolBuilding.name)
            .all())


def get_all_buildings(school_id: int | None):
    """Return all SchoolBuilding rows (active + inactive) for a school."""
    if not school_id:
        return []
    from app.models import SchoolBuilding
    return (SchoolBuilding.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school_id)
            .order_by(SchoolBuilding.name)
            .all())


def user_allowed_building_ids(user, school) -> set | None:
    """
    Return the set of building IDs a user is restricted to, or None when the
    user is UNRESTRICTED (sees everything).

    None  → no restriction (feature off, super admin, or no access rows).
    set() → restricted but with no buildings (should not normally happen; the
            UI forbids saving an empty restricted set). Treated as "see nothing".
    {ids} → restricted to these buildings.
    """
    if not school_buildings_enabled(school):
        return None
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    if getattr(user, 'is_super_admin', False):
        return None

    from app.models import UserBuildingAccess
    rows = (UserBuildingAccess.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(user_id=user.id, school_id=school.id)
            .all())
    if not rows:
        return None  # unrestricted
    return {r.building_id for r in rows}


def user_is_building_restricted(user, school) -> bool:
    """True when the user is limited to a subset of buildings."""
    return user_allowed_building_ids(user, school) is not None


def apply_building_scope_to_students(query, user, school):
    """
    Restrict a Student query to the user's allowed buildings.

    No-op when the feature is off or the user is unrestricted.
    """
    allowed = user_allowed_building_ids(user, school)
    if allowed is None:
        return query
    from app.models import Student
    if not allowed:
        return query.filter(Student.id == -1)  # restricted but no buildings → nothing
    return query.filter(Student.building_id.in_(allowed))


def apply_building_scope_to_fees(query, user, school):
    """
    Restrict a FeeRecord query (already joined to Student) to the user's
    allowed buildings via the student → building relation.

    The caller MUST have joined Student onto the query first (fees.index does).
    No-op when the feature is off or the user is unrestricted.
    """
    allowed = user_allowed_building_ids(user, school)
    if allowed is None:
        return query
    from app.models import Student
    if not allowed:
        return query.filter(Student.id == -1)
    return query.filter(Student.building_id.in_(allowed))


def user_can_access_student(user, school, student) -> bool:
    """
    True when the user may access this specific student under building rules.

    Used for direct-URL guards (student / fee detail). Always returns True when
    the feature is off or the user is unrestricted.
    """
    allowed = user_allowed_building_ids(user, school)
    if allowed is None:
        return True
    if student is None:
        return False
    return student.building_id in allowed


def validate_building_for_school(building_id, school_id) -> int | None:
    """
    Return building_id if it is a valid active building of the school, else None.
    Accepts None/0 gracefully (returns None = "no building").
    """
    if not building_id or not school_id:
        return None
    from app.models import SchoolBuilding
    b = (SchoolBuilding.query
         .execution_options(bypass_tenant_scope=True)
         .filter_by(id=building_id, school_id=school_id, is_active=True)
         .first())
    return b.id if b else None
