"""Per-school context cache (P2): active academic-year id + branding payload.

Both values are strictly PER-SCHOOL facts — the cache key is
``('<name>', school_id)`` and carries no user/role/child dimension because the
value is identical for every authorized member of that school. Callers are
responsible for having already resolved ``school_id`` from the authenticated
server-side context (JWT user row / session), never from client input; every
existing caller satisfies this.

Failure semantics: a loader error propagates exactly as the underlying query
error always did — nothing is cached, the next request retries the database.
With ``BACKEND_CACHE_ENABLED = False`` every helper runs its query directly
(byte-identical pre-P2 behaviour).

Invalidation: ``invalidate_school_context(school_id)`` is called from every
academic-year activation route and every school-settings/branding save route
(admin, schools, super_admin blueprints). It is per-process; the short TTLs
(60 s year / 300 s branding) bound staleness on other gunicorn workers.
"""
from __future__ import annotations

from flask import current_app

from app.utils import ttl_cache


def _enabled() -> bool:
    try:
        return bool(current_app.config.get('BACKEND_CACHE_ENABLED', True))
    except Exception:
        return False


# ── Active academic year ──────────────────────────────────────────────────────

def _load_active_year_id(school_id: int):
    from app.models import AcademicYear
    return (
        AcademicYear.query
        .execution_options(bypass_tenant_scope=True)
        .with_entities(AcademicYear.id)
        .filter_by(school_id=school_id, is_current=True)
        .scalar()
    )


def get_active_year_id(school_id: int | None):
    """Id of the school's currently active academic year (or None).

    Same query, same result as the inline lookups it replaces — the value is
    cached per school for ACTIVE_YEAR_CACHE_TTL_SECONDS. This value feeds the
    ORM year scope, so it is invalidated explicitly by every year-activation
    route in addition to its short TTL.
    """
    if not school_id:
        return None
    if not _enabled():
        return _load_active_year_id(school_id)
    ttl = current_app.config.get('ACTIVE_YEAR_CACHE_TTL_SECONDS', 60)
    return ttl_cache.get_or_set(
        ('active_year_id', school_id),
        lambda: _load_active_year_id(school_id),
        ttl,
    )


# ── School branding payload (mobile /me "school" block) ──────────────────────

def _load_school_branding(school_id: int):
    from app.models import School
    from app.blueprints.mobile_api.utils import photo_url

    school = (School.query
              .execution_options(bypass_tenant_scope=True)
              .filter_by(id=school_id)
              .first())
    if not school:
        return None
    return {
        'id':            school.id,
        'name':          school.school_name,
        'name_ar':       school.school_name_ar,
        'logo':          photo_url(school.logo_path),
        'primary_color': school.primary_color,
        'currency':      school.currency_symbol,
        'currency_code': school.currency_code,
        'phone':         school.phone,
        'email':         school.email,
        'address':       school.address,
    }


def get_school_branding(school_id: int | None):
    """Serialized branding/identity block for the caller's OWN school.

    Callers must pass the authenticated user's server-side school_id. The
    cached dict is defensively copied on every return so request code can
    never mutate the shared entry. The logo URL inside is window-stable
    (SIGNED_URL_STABLE_WINDOWS) and its signature outlives this cache's TTL,
    so a cached URL is always still valid when served.
    """
    if not school_id:
        return None
    if not _enabled():
        data = _load_school_branding(school_id)
        return dict(data) if data is not None else None
    ttl = current_app.config.get('SCHOOL_BRANDING_CACHE_TTL_SECONDS', 300)
    data = ttl_cache.get_or_set(
        ('school_branding', school_id),
        lambda: _load_school_branding(school_id),
        ttl,
    )
    return dict(data) if data is not None else None


# ── Invalidation ──────────────────────────────────────────────────────────────

def invalidate_school_context(school_id: int) -> int:
    """Drop all cached context for one school. Call AFTER committing an
    academic-year activation or a school-settings/branding change. Never
    raises. Returns the number of entries removed."""
    try:
        return ttl_cache.invalidate(
            lambda k: isinstance(k, tuple) and len(k) == 2
            and k[0] in ('active_year_id', 'school_branding')
            and k[1] == school_id
        )
    except Exception:
        return 0
