"""
School-aware collision-safe code generator.

Produces IDs and usernames scoped to a school prefix:
  NHD-STU-000001   student code
  NHD-EMP-000001   employee code
  NHD-PAR-000001   parent username
  NHD-TCH-000001   teacher username
  NHD-MGR-000001   school manager username

Rules
-----
- The prefix comes from School.code (set by Super Admin).
  If the school has no code, we fall back to SCH{school_id}.
- Each entity type has its own sequence, counted per-school.
- Legacy records (e.g. STU-00001) are NOT counted in the new sequences;
  new records simply start at 000001 for each school.
- The generator verifies the candidate does not already exist anywhere
  in the database before returning it, so it is safe against:
    * any remaining global unique index on the column
    * two schools accidentally sharing the same prefix
    * concurrent requests (optimistic – DB constraint is still the final guard)
"""
import re


# ─────────────────────────────────────────────────────────────────────────────
# School prefix
# ─────────────────────────────────────────────────────────────────────────────

def get_school_prefix(school_id: int) -> str:
    """Return the school's configured code or a safe numeric fallback."""
    from app.models import db, School
    school = db.session.get(School, school_id)
    if school and school.code:
        cleaned = re.sub(r'[^A-Z0-9]', '', school.code.upper())
        if cleaned:
            return cleaned[:20]
    return f'SCH{school_id}'


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _max_suffix(rows, pattern: str) -> int:
    """
    Scan (value,) tuples for strings matching '{pattern}{NNNN}' and
    return the highest trailing integer found, or 0 if none match.
    """
    num_re = re.compile(rf'^{re.escape(pattern)}(\d+)$')
    max_num = 0
    for (val,) in rows:
        if val:
            m = num_re.match(str(val))
            if m:
                n = int(m.group(1))
                if n > max_num:
                    max_num = n
    return max_num


def _globally_taken(model, field: str, value: str) -> bool:
    """
    Return True if *any* row in the table (across all schools and years)
    already has field=value.  This catches:
      - any leftover global unique index on the column
      - prefix collisions between schools (e.g. school code 'SCH5' clashes
        with the fallback prefix for school id 5)
    """
    from app.models import db
    col = getattr(model, field)
    return (
        db.session.query(col)
        .filter(col == value)
        .execution_options(bypass_tenant_scope=True, include_all_years=True)
        .first()
    ) is not None


# ─────────────────────────────────────────────────────────────────────────────
# Student IDs
# ─────────────────────────────────────────────────────────────────────────────

def generate_student_id(school_id: int) -> str:
    """
    Generate the next unique student code for a school.
    Format: {PREFIX}-STU-000001

    Walks forward from MAX+1 until it finds a value that does not exist
    anywhere in the database, so it is robust against global unique indexes
    and prefix collisions between schools.
    """
    from app.models import db, Student
    prefix  = get_school_prefix(school_id)
    pattern = f'{prefix}-STU-'

    rows = (
        db.session.query(Student.student_id)
        .filter(Student.school_id == school_id)
        .filter(Student.student_id.like(f'{pattern}%'))
        .execution_options(bypass_tenant_scope=True, include_all_years=True)
        .all()
    )
    n = _max_suffix(rows, pattern) + 1

    for _ in range(200):
        candidate = f'{pattern}{n:06d}'
        if not _globally_taken(Student, 'student_id', candidate):
            return candidate
        n += 1

    raise RuntimeError(
        f'generate_student_id: could not find a free slot after 200 attempts '
        f'(school_id={school_id}, prefix={prefix})'
    )


# ─────────────────────────────────────────────────────────────────────────────
# Employee IDs
# ─────────────────────────────────────────────────────────────────────────────

def generate_employee_id(school_id: int) -> str:
    """
    Generate the next unique employee code for a school.
    Format: {PREFIX}-EMP-000001
    """
    from app.models import db, Employee
    prefix  = get_school_prefix(school_id)
    pattern = f'{prefix}-EMP-'

    rows = (
        db.session.query(Employee.employee_id)
        .filter(Employee.school_id == school_id)
        .filter(Employee.employee_id.like(f'{pattern}%'))
        .execution_options(bypass_tenant_scope=True)
        .all()
    )
    n = _max_suffix(rows, pattern) + 1

    for _ in range(200):
        candidate = f'{pattern}{n:06d}'
        if not _globally_taken(Employee, 'employee_id', candidate):
            return candidate
        n += 1

    raise RuntimeError(
        f'generate_employee_id: could not find a free slot after 200 attempts '
        f'(school_id={school_id}, prefix={prefix})'
    )


# ─────────────────────────────────────────────────────────────────────────────
# User usernames
# ─────────────────────────────────────────────────────────────────────────────

_ROLE_TYPE_CODES: dict[str, str] = {
    'parent':          'PAR',
    'teacher':         'TCH',
    'school_admin':    'MGR',
    'investor_viewer': 'INV',
}


def generate_username(school_id: int, role_name: str) -> str:
    """
    Generate the next unique login username for a school user account.

    role_name → type code:
      parent       → PAR
      teacher      → TCH
      school_admin → MGR
      (any other)  → USR

    Format: {PREFIX}-{TYPE}-000001
    """
    from app.models import db, User
    prefix    = get_school_prefix(school_id)
    type_code = _ROLE_TYPE_CODES.get(role_name, 'USR')
    pattern   = f'{prefix}-{type_code}-'

    rows = (
        db.session.query(User.username)
        .filter(User.school_id == school_id)
        .filter(User.username.like(f'{pattern}%'))
        .execution_options(bypass_tenant_scope=True)
        .all()
    )
    n = _max_suffix(rows, pattern) + 1

    for _ in range(200):
        candidate = f'{pattern}{n:04d}'
        if not _globally_taken(User, 'username', candidate):
            return candidate
        n += 1

    raise RuntimeError(
        f'generate_username: could not find a free slot after 200 attempts '
        f'(school_id={school_id}, role={role_name})'
    )
