"""
Mecha-School ERP — Database Models
===================================

Phase 6: Multi-Tenant + Academic Year Archiving
-------------------------------------------------
* School           — one row per physical school, holds capacity + white-label config
* AcademicYear     — now per-school (school_id FK); is_current is per-school
* User.school_id   — NULL for super-admin, set for all school staff
* Student          — gains school_id + academic_year_id
* Employee         — gains school_id
* StudentAttendance — gains school_id + academic_year_id
* EmployeeAttendance — gains school_id
* FeeRecord        — gains school_id (already had academic_year_id)
* Revenue/Expense  — gains school_id
* SalaryRecord     — gains school_id
* Notification/Announcement — gains school_id
* Device           — gains school_id
"""
from datetime import datetime, date
from decimal import Decimal

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from flask_bcrypt import Bcrypt

db = SQLAlchemy()
bcrypt = Bcrypt()

SUPER_ADMIN_ROLE = 'super_admin'
SCHOOL_ADMIN_ROLE = 'school_admin'
ADMIN_ROLE_NAMES = frozenset({SUPER_ADMIN_ROLE, SCHOOL_ADMIN_ROLE})

# School-scoped, read-only investor account. NOT an admin role: it never
# bypasses permission checks and only sees its own school's finance/dashboard
# data (auto-scoped by school_id through the ORM tenant guard). Managed
# exclusively by super_admin via the Super Admin portal.
INVESTOR_ROLE = 'investor_viewer'


# ═════════════════════════════════════════════════════════════════════════════
#  0. SCHOOL  (multi-tenant root entity)
# ═════════════════════════════════════════════════════════════════════════════

class School(db.Model):
    """
    One row per physical school.  Super-admin creates/edits these.
    Every other model is scoped to a school via school_id FK.
    capacity=0 means unlimited.
    """
    __tablename__ = 'schools'

    # institution_type values. NULL / anything else = school behaviour.
    INSTITUTION_SCHOOL    = 'school'
    INSTITUTION_INSTITUTE = 'institute'
    INSTITUTION_TYPES     = (INSTITUTION_SCHOOL, INSTITUTION_INSTITUTE)

    id              = db.Column(db.Integer, primary_key=True)
    school_name     = db.Column(db.String(200), nullable=False)
    school_name_ar  = db.Column(db.String(200), nullable=True)
    code            = db.Column(db.String(20),  unique=True, nullable=True)
    capacity        = db.Column(db.Integer, default=0)   # 0 = unlimited

    logo_path       = db.Column(db.String(255), nullable=True)
    favicon_path    = db.Column(db.String(255), nullable=True)
    primary_color   = db.Column(db.String(20),  default='#0d6efd')
    address         = db.Column(db.Text, nullable=True)
    phone           = db.Column(db.String(40),  nullable=True)
    email           = db.Column(db.String(180), nullable=True)
    website         = db.Column(db.String(180), nullable=True)
    currency_code   = db.Column(db.String(10),  default='IQD')
    currency_symbol = db.Column(db.String(10),  default='د.ع')
    timezone        = db.Column(db.String(50),  default='Asia/Baghdad')
    locale          = db.Column(db.String(10),  default='ar')
    receipt_footer  = db.Column(db.Text, nullable=True)

    att_start_time        = db.Column(db.Time, nullable=True)
    att_late_threshold    = db.Column(db.Time, nullable=True)
    att_absence_threshold = db.Column(db.Time, nullable=True)
    att_departure_time    = db.Column(db.Time, nullable=True)

    # Super-admin classification & billing fields
    governorate       = db.Column(db.String(100), nullable=True, index=True)
    price_per_student = db.Column(db.Numeric(12, 2), default=0)

    # Calendar: comma-separated weekday numbers (0=Mon … 6=Sun) that are off,
    # e.g. "4,5" for Friday+Saturday.  NULL means no weekly holidays configured.
    weekly_off_days   = db.Column(db.String(20), nullable=True)

    # HR: employee absence limit alerts
    emp_absence_limit         = db.Column(db.Integer,    nullable=True)
    emp_absence_period        = db.Column(db.String(20), nullable=True, default='monthly')
    emp_absence_alert_enabled = db.Column(db.Boolean,   default=True)

    # Fee installment reminder notifications
    fee_reminder_enabled      = db.Column(db.Boolean,    default=False)
    fee_reminder_before_value = db.Column(db.Integer,    default=3)    # legacy, kept for DB compat
    fee_reminder_before_unit  = db.Column(db.String(10), default='days')  # legacy, kept for DB compat
    fee_reminder_days_before  = db.Column(db.Integer,    default=3)    # days before due date to start
    fee_reminder_per_day      = db.Column(db.Integer,    default=1)    # reminder slots per day (1–6)

    # Optional per-school feature: building-based data access (multiple
    # branches/buildings inside one school account).  Default OFF so existing
    # schools behave exactly as before.  See SchoolBuilding / UserBuildingAccess.
    enable_buildings = db.Column(db.Boolean, default=False, nullable=False,
                                 server_default=db.false())

    # Optional per-school feature: two-shift attendance (morning/afternoon).
    # Default OFF so existing schools behave exactly as before.
    enable_attendance_shifts = db.Column(db.Boolean, default=False, nullable=False,
                                         server_default=db.false())

    # SHIFT MODE ONLY — the single automatic-absence cutoff shared by every
    # AttendanceShift of this school.  Replaces the former per-shift
    # AttendanceShift.absent_after_time as the behavioural source.
    #
    # NULL means "not configured yet": shift auto-absence is skipped entirely
    # (fail-closed).  It deliberately does NOT fall back to
    # att_absence_threshold or to AttendanceShift.absent_after_time, because an
    # incorrect absence triggers parent notifications that cannot be unsent.
    #
    # Unified (non-shift) mode is unaffected and keeps using
    # att_absence_threshold exactly as before.
    shift_absent_after_time = db.Column(db.Time, nullable=True)

    # Last applied feature package (nullable — no package = defaults apply)
    package_id  = db.Column(db.Integer, db.ForeignKey('feature_packages.id', ondelete='SET NULL'),
                            nullable=True)

    # Optional per-school configuration: which educational stages this school
    # runs.  Comma-separated canonical Arabic stage names, matching Grade.stage
    # exactly (see app/utils/school_stages.py).
    #
    # NULL  = LEGACY mode.  The school predates this feature: its grades,
    #         sections, subjects and external-registration behaviour are used
    #         exactly as they are today and are never filtered or provisioned
    #         by stage.  Existing schools are deliberately NOT backfilled.
    # value = MANAGED mode.  Only the selected stages' standard grades (each
    #         with section "أ") and their standard subjects are provisioned,
    #         and the public registration form shows only those grades.
    educational_stages = db.Column(db.String(120), nullable=True)

    # Optional per-institution classification.
    #
    # NULL  = LEGACY / default.  The row behaves EXACTLY as a school does today:
    #         student automatic absence runs with its existing settings.
    #         Existing rows are deliberately NOT backfilled.
    # 'school'    = explicitly a school — identical behaviour to NULL.
    # 'institute' = explicit opt-in.  Student AUTOMATIC absence generation is
    #         skipped entirely (scheduler, catch-up and web-triggered paths).
    #         Nothing else changes: daily manual attendance, historical records,
    #         att_* cutoff settings and reports are untouched, so switching back
    #         to 'school' restores the previous behaviour from the same stored
    #         settings.  See School.is_institute.
    institution_type = db.Column(db.String(20), nullable=True)

    # Optional per-school feature: external (public) student-registration link.
    # Default OFF so existing schools behave exactly as before. Only the Super
    # Admin enables/disables/regenerates the link.
    external_registration_enabled = db.Column(db.Boolean, default=False, nullable=False,
                                              server_default=db.false())
    # sha256(raw token) — used for lookup + verification at the public entry point.
    registration_token_hash       = db.Column(db.String(64), unique=True, nullable=True)
    # Fernet-encrypted raw token — authorized Super-Admin recovery (copy link later).
    registration_token_encrypted  = db.Column(db.Text, nullable=True)
    registration_token_created_at = db.Column(db.DateTime, nullable=True)

    is_active   = db.Column(db.Boolean, default=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow)

    # Relationships
    academic_years = db.relationship('AcademicYear', backref='school', lazy='dynamic')
    package        = db.relationship('FeaturePackage', foreign_keys=[package_id])

    @property
    def is_institute(self):
        """
        True ONLY when this institution is explicitly classified as an institute.

        NULL, '', 'school' and any unrecognised value all return False, so every
        existing row keeps its current behaviour without any backfill.
        """
        return (self.institution_type or '').strip().lower() == self.INSTITUTION_INSTITUTE

    @property
    def current_year(self):
        return AcademicYear.query.filter_by(school_id=self.id, is_current=True).first()

    @property
    def student_count(self):
        return Student.query.filter_by(school_id=self.id, status='active').count()

    @property
    def is_at_capacity(self):
        if not self.capacity:
            return False
        return self.student_count >= self.capacity

    def __repr__(self):
        return f'<School {self.id} – {self.school_name}>'


# ─────────────────────────────────────────────────────────────────────────────
#  SCHOOL BILLING  (super-admin billing records for each school's subscription)
# ─────────────────────────────────────────────────────────────────────────────

class SchoolBilling(db.Model):
    """
    System-level billing records for a school's subscription / service fees.
    Completely separate from student tuition fees (FeeRecord / FeeInstallment).
    Only the super_admin creates / manages these records.
    """
    __tablename__ = 'school_billing'

    BILLING_TYPES = ('subscription', 'setup', 'extra_students', 'service', 'other')
    STATUS_TYPES  = ('unpaid', 'partial', 'paid')

    id              = db.Column(db.Integer, primary_key=True)
    school_id       = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'),
                                nullable=False, index=True)
    amount_due      = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    amount_paid     = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    description     = db.Column(db.String(400), nullable=True)
    billing_type    = db.Column(db.String(30), nullable=False, default='subscription')
    due_date        = db.Column(db.Date, nullable=True)
    payment_date    = db.Column(db.Date, nullable=True)
    status          = db.Column(db.String(20), nullable=False, default='unpaid')
    notes           = db.Column(db.Text, nullable=True)
    created_by      = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
                                nullable=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow,
                                onupdate=datetime.utcnow)

    school   = db.relationship('School', backref=db.backref('billing_records',
                                                             cascade='all, delete-orphan',
                                                             lazy='dynamic'))
    creator  = db.relationship('User', foreign_keys=[created_by])

    @property
    def remaining(self):
        return (self.amount_due or Decimal('0')) - (self.amount_paid or Decimal('0'))

    def recompute_status(self):
        paid = self.amount_paid or Decimal('0')
        due  = self.amount_due  or Decimal('0')
        if paid <= 0:
            self.status = 'unpaid'
        elif paid >= due:
            self.status = 'paid'
        else:
            self.status = 'partial'

    def __repr__(self):
        return f'<SchoolBilling school={self.school_id} due={self.amount_due} status={self.status}>'


# ─────────────────────────────────────────────────────────────────────────────
#  SCHOOL BUILDINGS  (optional per-school branch/building data partitioning)
# ─────────────────────────────────────────────────────────────────────────────

class SchoolBuilding(db.Model):
    """
    A physical building / branch inside a single school account.

    Only relevant when School.enable_buildings is True.  Adds an OPTIONAL second
    isolation layer *below* school_id — never a replacement for it.  Students may
    be assigned to a building; users may be restricted to one or more buildings
    via UserBuildingAccess.
    """
    __tablename__ = 'school_buildings'
    __school_scoped__ = True
    # Not year-scoped: buildings are physical and persist across academic years.

    id          = db.Column(db.Integer, primary_key=True)
    school_id   = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'),
                            nullable=False, index=True)
    name        = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    is_active   = db.Column(db.Boolean, default=True, nullable=False,
                            server_default=db.true())
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow)

    school = db.relationship('School', foreign_keys=[school_id],
                             backref=db.backref('buildings', lazy='dynamic'))

    __table_args__ = (
        # Building name unique within a school (case-sensitive at DB level).
        db.UniqueConstraint('school_id', 'name', name='uq_building_school_name'),
    )

    def __repr__(self):
        return f'<SchoolBuilding {self.id} – {self.name} (school={self.school_id})>'


class UserBuildingAccess(db.Model):
    """
    Restricts a user to specific building(s) within their school.

    Semantics:
      * A user with NO rows here is UNRESTRICTED — sees all buildings within
        their normal permissions (current behaviour).
      * A user with one or more rows is RESTRICTED — sees only data belonging to
        the listed building(s).

    Always applied *after* school_id scoping, never instead of it.
    """
    __tablename__ = 'user_building_access'
    __school_scoped__ = True

    id          = db.Column(db.Integer, primary_key=True)
    school_id   = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'),
                            nullable=False, index=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
                            nullable=False, index=True)
    building_id = db.Column(db.Integer, db.ForeignKey('school_buildings.id', ondelete='CASCADE'),
                            nullable=False, index=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    user     = db.relationship('User', foreign_keys=[user_id],
                               backref=db.backref('building_access',
                                                  cascade='all, delete-orphan',
                                                  lazy='dynamic'))
    building = db.relationship('SchoolBuilding', foreign_keys=[building_id],
                               backref=db.backref('user_access',
                                                  cascade='all, delete-orphan',
                                                  lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('user_id', 'building_id', name='uq_user_building'),
    )

    def __repr__(self):
        return f'<UserBuildingAccess user={self.user_id} building={self.building_id}>'


class AttendanceShift(db.Model):
    """
    A named attendance shift (e.g. الدوام الصباحي / الدوام الظهري).

    Only relevant when School.enable_attendance_shifts is True.
    Sections are linked to a shift via Section.shift_id.

    Auto-absence is driven by the school-wide School.shift_absent_after_time —
    one cutoff shared by every shift.  `absent_after_time` below is RETAINED for
    rollback/audit of the previous per-shift behaviour and is NO LONGER read by
    any automatic-absence decision.  Do not reintroduce reads of it.
    """
    __tablename__ = 'attendance_shifts'
    __school_scoped__ = True

    id                = db.Column(db.Integer, primary_key=True)
    school_id         = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'),
                                  nullable=False, index=True)
    name              = db.Column(db.String(100), nullable=False)
    start_time        = db.Column(db.Time, nullable=False)
    # NULL = lateness is switched off for this shift.  Only an INSTITUTE can
    # store NULL here: the school shift forms and their server-side validation
    # still require a value, so existing schools are unaffected.
    late_after_time   = db.Column(db.Time, nullable=True)
    # LEGACY — historical per-shift cutoff, never read for behaviour (see
    # School.shift_absent_after_time).  Nullable only because the create path
    # derives it from late_after_time; when lateness is left blank there is no
    # honest value to store and none is invented.
    absent_after_time = db.Column(db.Time, nullable=True)
    dismissal_time    = db.Column(db.Time, nullable=True)
    is_active         = db.Column(db.Boolean, default=True, nullable=False,
                                  server_default=db.true())
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at        = db.Column(db.DateTime, default=datetime.utcnow,
                                  onupdate=datetime.utcnow)

    school   = db.relationship('School', foreign_keys=[school_id])

    __table_args__ = (
        db.UniqueConstraint('school_id', 'name', name='uq_shift_school_name'),
    )

    def __repr__(self):
        return f'<AttendanceShift {self.name} school={self.school_id}>'


# ═════════════════════════════════════════════════════════════════════════════
#  0b. FEATURE PACKAGES  (reusable named bundles of module/feature/form config)
# ═════════════════════════════════════════════════════════════════════════════

class FeaturePackage(db.Model):
    """
    A reusable named configuration bundle managed by Super Admin.

    config JSON structure::

        {
          "modules":  {"students": true, "employees": false, ...},
          "features": {"students.create": true, "attendance_devices.sync": false, ...},
          "student_form": {
              "hidden_sections": ["attendance_device"],
              "hidden_fields":   ["nationality"],
              "required_fields": ["phone"]
          }
        }

    Assigning a package to a school is a snapshot operation (Option B):
    the school's existing SchoolModule/SchoolFeature rows are updated from the
    package config at the moment of assignment.  Later package edits do NOT
    automatically re-apply to schools that already received the package.
    """
    __tablename__ = 'feature_packages'

    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=True)
    is_active   = db.Column(db.Boolean, nullable=False, default=True)
    config      = db.Column(db.JSON, nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<FeaturePackage {self.id} — {self.name}>'


# ─────────────────────────────────────────────────────────────────────────────
#  SCHOOL MODULES  (feature flags per school, managed by super admin only)
# ─────────────────────────────────────────────────────────────────────────────

class SchoolModule(db.Model):
    """
    One row per (school, module_key) pair.
    Super admin sets is_enabled; school managers have no access.

    No rows for a school = all modules enabled (backward compatibility with
    schools created before this feature was introduced).
    """
    __tablename__ = 'school_modules'

    id         = db.Column(db.Integer, primary_key=True)
    school_id  = db.Column(db.Integer,
                           db.ForeignKey('schools.id', ondelete='CASCADE'),
                           nullable=False, index=True)
    module_key = db.Column(db.String(50), nullable=False)
    is_enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('school_id', 'module_key', name='uq_school_module'),
    )

    school = db.relationship(
        'School',
        backref=db.backref('school_modules_list',
                           cascade='all, delete-orphan', lazy='dynamic'),
    )

    def __repr__(self):
        return (f'<SchoolModule school={self.school_id} '
                f'key={self.module_key} enabled={self.is_enabled}>')


class SchoolFeature(db.Model):
    """
    One row per (school, feature_key) pair — granular capability control.
    Super admin sets is_enabled; school managers have no access.

    No rows for a school = all features enabled (backward compatibility with
    schools created before this feature was introduced).

    If the parent module is disabled, all its features are considered disabled
    regardless of their individual is_enabled values.
    """
    __tablename__ = 'school_features'

    id          = db.Column(db.Integer, primary_key=True)
    school_id   = db.Column(db.Integer,
                            db.ForeignKey('schools.id', ondelete='CASCADE'),
                            nullable=False, index=True)
    feature_key = db.Column(db.String(100), nullable=False)
    is_enabled  = db.Column(db.Boolean, nullable=False, default=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('school_id', 'feature_key', name='uq_school_feature'),
    )

    school = db.relationship(
        'School',
        backref=db.backref('school_features_list',
                           cascade='all, delete-orphan', lazy='dynamic'),
    )

    def __repr__(self):
        return (f'<SchoolFeature school={self.school_id} '
                f'key={self.feature_key} enabled={self.is_enabled}>')


# ═════════════════════════════════════════════════════════════════════════════
#  1. PERMISSIONS & ROLES
# ═════════════════════════════════════════════════════════════════════════════

class Permission(db.Model):
    __tablename__ = 'permissions'

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(100), unique=True, nullable=False)
    label      = db.Column(db.String(150), nullable=False)
    category   = db.Column(db.String(80),  nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<Permission {self.name}>'


class Role(db.Model):
    __tablename__ = 'roles'

    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(80), unique=True, nullable=False)
    label       = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    is_admin    = db.Column(db.Boolean, default=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    permissions = db.relationship('Permission', secondary='role_permissions',
                                  backref=db.backref('roles', lazy='dynamic'))
    users       = db.relationship('User', back_populates='role', lazy='dynamic')
    # Schools explicitly allowed to use this custom role. Managed per-school
    # from the Super Admin school-details page. Built-in system roles never use
    # this table and are always available (see is_available_to_school).
    schools     = db.relationship('School', secondary='role_schools',
                                  lazy='selectin',
                                  backref=db.backref('custom_roles', lazy='selectin'))

    @property
    def is_builtin(self):
        """True for the fixed system roles that are never school-scoped."""
        from app.utils.permissions_catalog import BUILTIN_ROLE_NAMES
        return (self.name or '') in BUILTIN_ROLE_NAMES

    def is_available_to_school(self, school_id):
        """Whether this role may be assigned to a user of the given school.

        Built-in system roles are always available (unchanged behaviour).
        A custom role is available only to the schools explicitly linked via
        role_schools. A NULL school_id (super-admin account) is not a school
        context and is never gated here.
        """
        if self.is_builtin:
            return True
        if school_id is None:
            return False
        return any(s.id == school_id for s in self.schools)

    def __repr__(self):
        return f'<Role {self.name}>'


role_permissions = db.Table(
    'role_permissions',
    db.Column('role_id',       db.Integer, db.ForeignKey('roles.id'),       primary_key=True),
    db.Column('permission_id', db.Integer, db.ForeignKey('permissions.id'), primary_key=True),
)

# Custom-role → school assignment (many-to-many). A row means the custom role
# is offered to that school's user-creation forms. Built-in roles never use
# this table; custom roles marked all_schools ignore it.
role_schools = db.Table(
    'role_schools',
    db.Column('role_id',   db.Integer, db.ForeignKey('roles.id',   ondelete='CASCADE'), primary_key=True),
    db.Column('school_id', db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'), primary_key=True),
)

user_permissions = db.Table(
    'user_permissions',
    db.Column('user_id',       db.Integer, db.ForeignKey('users.id'),        primary_key=True),
    db.Column('permission_id', db.Integer, db.ForeignKey('permissions.id'),  primary_key=True),
)


# ═════════════════════════════════════════════════════════════════════════════
#  2. USERS  (Admin / Teacher / Accountant / Parent / ...)
# ═════════════════════════════════════════════════════════════════════════════

class User(UserMixin, db.Model):
    """
    school_id = NULL  → super-admin (can see all schools).
    school_id = N     → staff/parent scoped to school N only.
    """
    __tablename__ = 'users'
    __school_scoped__ = True

    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80),  unique=True, nullable=False, index=True)
    email         = db.Column(db.String(180), unique=True, nullable=True, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    full_name     = db.Column(db.String(200), nullable=False)
    role_id       = db.Column(db.Integer, db.ForeignKey('roles.id'), nullable=False)
    school_id     = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=True, index=True)
    is_active     = db.Column(db.Boolean, default=True)
    avatar        = db.Column(db.String(255), nullable=True)
    phone         = db.Column(db.String(30),  nullable=True)
    last_login    = db.Column(db.DateTime, nullable=True)

    device_token  = db.Column(db.String(512), nullable=True, index=True)
    locale        = db.Column(db.String(10),  default='ar')

    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow,
                              onupdate=datetime.utcnow)

    extra_permissions = db.relationship(
        'Permission', secondary='user_permissions',
        backref=db.backref('users', lazy='dynamic'),
    )
    children = db.relationship(
        'Student', secondary='parent_students',
        backref=db.backref('parents', lazy='dynamic'),
    )

    school = db.relationship('School', foreign_keys=[school_id],
                             backref=db.backref('users', lazy='dynamic'))
    role   = db.relationship('Role', foreign_keys=[role_id], back_populates='users')

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)

    def has_permission(self, perm_name):
        if self.role and self.role.name in ADMIN_ROLE_NAMES:
            return True
        role_perms = {p.name for p in self.role.permissions} if self.role else set()
        user_perms = {p.name for p in self.extra_permissions}
        return perm_name in (role_perms | user_perms)

    def get_all_permissions(self):
        role_perms = {p.name for p in self.role.permissions} if self.role else set()
        user_perms = {p.name for p in self.extra_permissions}
        return role_perms | user_perms

    @property
    def is_parent(self):
        return bool(self.role and self.role.name == 'parent')

    @property
    def is_super_admin(self):
        """True for the system owner account only."""
        return bool(
            self.role and self.role.name == SUPER_ADMIN_ROLE
            and self.school_id is None
        )

    @property
    def is_school_admin(self):
        """True for a school-level manager bound to one school."""
        return bool(
            self.role and self.role.name == SCHOOL_ADMIN_ROLE
            and self.school_id is not None
        )

    @property
    def is_admin_user(self):
        """True for either admin tier, based on explicit role names."""
        return bool(self.is_super_admin or self.is_school_admin)

    @property
    def is_investor(self):
        """True for a school-scoped read-only investor account bound to one school."""
        return bool(
            self.role and self.role.name == INVESTOR_ROLE
            and self.school_id is not None
        )

    @property
    def is_accountant(self):
        """True for a finance-scoped accountant account.

        Role-name based (not permission based) so the accountant confinement
        guard always confines these accounts regardless of any legacy
        permissions the role may carry.
        """
        return bool(self.role and self.role.name == 'accountant')

    def __repr__(self):
        return f'<User {self.username}>'


parent_students = db.Table(
    'parent_students',
    db.Column('user_id',    db.Integer, db.ForeignKey('users.id'),    primary_key=True),
    db.Column('student_id', db.Integer, db.ForeignKey('students.id'), primary_key=True),
    db.Column('relation',   db.String(30), default='guardian'),
    db.Column('created_at', db.DateTime, default=datetime.utcnow),
)


# ═════════════════════════════════════════════════════════════════════════════
#  3. ACADEMIC STRUCTURE
# ═════════════════════════════════════════════════════════════════════════════

class AcademicYear(db.Model):
    """
    Now per-school.  is_current means 'active for THIS school'.
    Use School.current_year to get it.
    """
    __tablename__ = 'academic_years'
    __school_scoped__ = True

    id         = db.Column(db.Integer, primary_key=True)
    school_id  = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    name       = db.Column(db.String(50), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date   = db.Column(db.Date, nullable=False)
    is_current = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    grades = db.relationship('Grade', backref='academic_year', lazy='dynamic')

    __table_args__ = (
        db.UniqueConstraint('school_id', 'name', name='uq_academic_year_school_name'),
    )

    def __repr__(self):
        return f'<AcademicYear {self.name}>'


class Grade(db.Model):
    __tablename__ = 'grades'
    __school_scoped__ = True
    __year_scoped__ = True

    id               = db.Column(db.Integer, primary_key=True)
    name             = db.Column(db.String(100), nullable=False)
    stage            = db.Column(db.String(50),  nullable=True)
    school_id        = db.Column(db.Integer, db.ForeignKey('schools.id'),
                                 nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False)
    # Optional shift fallback — used when a student's section has no shift_id.
    # Section.shift_id always takes priority.
    shift_id         = db.Column(db.Integer, db.ForeignKey('attendance_shifts.id'),
                                 nullable=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    sections = db.relationship('Section', backref='grade', lazy='dynamic')
    school   = db.relationship('School', foreign_keys=[school_id],
                               backref=db.backref('grades', lazy='dynamic'))
    shift    = db.relationship('AttendanceShift', foreign_keys=[shift_id])

    __table_args__ = (
        db.UniqueConstraint('school_id', 'academic_year_id', 'name',
                            name='uq_grade_school_year_name'),
    )

    def __repr__(self):
        return f'<Grade {self.name}>'


class Section(db.Model):
    __tablename__ = 'sections'
    __school_scoped__ = True
    __year_scoped__ = True

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(50), nullable=False)
    school_id  = db.Column(db.Integer, db.ForeignKey('schools.id'),
                           nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    grade_id   = db.Column(db.Integer, db.ForeignKey('grades.id'), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=True)
    capacity   = db.Column(db.Integer, default=30)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Optional shift assignment — only used when School.enable_attendance_shifts=True.
    shift_id   = db.Column(db.Integer, db.ForeignKey('attendance_shifts.id'), nullable=True)

    students = db.relationship('Student', backref='section', lazy='dynamic')
    school   = db.relationship('School', foreign_keys=[school_id],
                               backref=db.backref('sections', lazy='dynamic'))
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id],
                                    backref=db.backref('sections', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('school_id', 'academic_year_id', 'grade_id', 'name',
                            name='uq_section_school_year_grade_name'),
    )

    def __repr__(self):
        return f'<Section {self.name}>'


class Subject(db.Model):
    __tablename__ = 'subjects'
    __school_scoped__ = True
    __year_scoped__ = True

    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(150), nullable=False)
    code        = db.Column(db.String(20),  nullable=True)
    school_id   = db.Column(db.Integer, db.ForeignKey('schools.id'),
                            nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    stage       = db.Column(db.String(50),  nullable=True)
    grade_id    = db.Column(db.Integer, db.ForeignKey('grades.id'), nullable=True, index=True)
    total_marks = db.Column(db.Numeric(8, 2), nullable=True)
    pass_marks  = db.Column(db.Numeric(8, 2), nullable=True)
    description = db.Column(db.Text, nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    school = db.relationship('School', foreign_keys=[school_id],
                             backref=db.backref('subjects', lazy='dynamic'))
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id],
                                    backref=db.backref('subjects', lazy='dynamic'))
    grade = db.relationship('Grade', foreign_keys=[grade_id],
                            backref=db.backref('subjects', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('school_id', 'academic_year_id', 'code',
                            name='uq_subject_school_year_code'),
    )

    def __repr__(self):
        return f'<Subject {self.name}>'


teacher_subjects = db.Table(
    'teacher_subjects',
    db.Column('employee_id', db.Integer, db.ForeignKey('employees.id'), primary_key=True),
    db.Column('subject_id',  db.Integer, db.ForeignKey('subjects.id'),  primary_key=True),
    db.Column('section_id',  db.Integer, db.ForeignKey('sections.id'),  primary_key=True),
)


# ═════════════════════════════════════════════════════════════════════════════
#  3b. INSTITUTE STUDY GROUPS  (institute institutions only — School.is_institute)
# ═════════════════════════════════════════════════════════════════════════════
#
# An institute organises teaching as  subject → study group → instructor,
# instead of the school model  stage → grade → section.  These two tables are
# entirely separate from Grade / Section / Student.section_id / teacher_subjects,
# which are left exactly as they are: a school row never gets an institute row
# and nothing here is read by any school code path.
#
# Instructors reuse Employee.  Subjects reuse Subject.  No parallel teacher or
# subject entity is introduced.
#
# CROSS-SCHOOL SAFETY IS ENFORCED BY THE DATABASE, not only by route checks.
# Each table carries its own school_id and pairs it with the referenced row's
# school_id in a COMPOSITE foreign key against a UNIQUE (id, school_id) key on
# the parent table.  A group can therefore never point at another school's
# subject or employee, and an enrollment can never join a student to a group
# from a different school — the insert is rejected by PostgreSQL itself.
#
# Because school_id participates in several of those composite keys, every
# relationship below is viewonly=True with an explicit primaryjoin.  The
# scalar FK columns are what routes assign; the relationships are read-only
# conveniences, so SQLAlchemy never tries to write school_id through two
# different relationships at once.


class InstituteStudyGroup(db.Model):
    """One study group: a named cohort of an institute's subject, led by one
    instructor, inside one academic year.

    A group is never hard-deleted from the normal interface — is_active is
    toggled instead, so its enrollment history stays intact and auditable.
    Deactivating a group does NOT end or remove its enrollments.
    """
    __tablename__ = 'institute_study_groups'
    __school_scoped__ = True
    __year_scoped__ = True

    id               = db.Column(db.Integer, primary_key=True)
    school_id        = db.Column(db.Integer, nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    subject_id       = db.Column(db.Integer, nullable=False, index=True)
    # Nullable at DB level so deleting an employee clears the assignment rather
    # than blocking the delete.  An ACTIVE group must still have an instructor
    # — that rule is enforced in the institute_groups routes, not by the column,
    # because ON DELETE SET NULL must remain able to null it.
    instructor_id    = db.Column(db.Integer, nullable=True, index=True)
    name             = db.Column(db.String(150), nullable=False)
    start_date       = db.Column(db.Date, nullable=True)
    end_date         = db.Column(db.Date, nullable=True)
    is_active        = db.Column(db.Boolean, nullable=False, default=True,
                                 server_default=db.true())
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow,
                                 onupdate=datetime.utcnow)

    school        = db.relationship(
        'School', viewonly=True,
        primaryjoin='foreign(InstituteStudyGroup.school_id) == School.id')
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    subject       = db.relationship(
        'Subject', viewonly=True,
        primaryjoin='foreign(InstituteStudyGroup.subject_id) == Subject.id')
    instructor    = db.relationship(
        'Employee', viewonly=True,
        primaryjoin='foreign(InstituteStudyGroup.instructor_id) == Employee.id')

    __table_args__ = (
        db.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                name='fk_institute_group_school'),
        # Same-school ownership of the subject, enforced by PostgreSQL.
        db.ForeignKeyConstraint(
            ['subject_id', 'school_id'], ['subjects.id', 'subjects.school_id'],
            name='fk_institute_group_subject_school'),
        # Same-school ownership of the instructor.  The column-list form of
        # ON DELETE SET NULL (PostgreSQL 15+) nulls ONLY instructor_id; a plain
        # SET NULL would also try to null the NOT NULL school_id and would turn
        # employee deletion into a constraint error.
        db.ForeignKeyConstraint(
            ['instructor_id', 'school_id'], ['employees.id', 'employees.school_id'],
            name='fk_institute_group_instructor_school',
            ondelete='SET NULL (instructor_id)'),
        db.UniqueConstraint('school_id', 'academic_year_id', 'subject_id', 'name',
                            name='uq_institute_group_school_year_subject_name'),
        # Parent side of the enrollment composite FK.
        db.UniqueConstraint('id', 'school_id', name='uq_institute_group_id_school'),
        db.Index('ix_institute_group_school_year_active',
                 'school_id', 'academic_year_id', 'is_active'),
    )

    def __repr__(self):
        return f'<InstituteStudyGroup {self.id} – {self.name} (school={self.school_id})>'


class InstituteGroupEnrollment(db.Model):
    """One student's membership of one study group.

    A student may hold several ACTIVE enrollments at once (one per group).
    Membership is never deleted: leaving a group sets status='ended' and
    stamps ended_at, so the history row survives and the student can be
    re-enrolled later as a new row.

    School-scoped but deliberately NOT year-scoped: the academic year comes
    from the group, and Student itself is a master record that persists across
    years (see app/utils/scoping.py).  A second academic_year_id here would be
    a duplicate source of truth.
    """
    __tablename__ = 'institute_group_enrollments'
    __school_scoped__ = True

    STATUS_ACTIVE = 'active'
    STATUS_ENDED  = 'ended'

    id          = db.Column(db.Integer, primary_key=True)
    school_id   = db.Column(db.Integer, nullable=False, index=True)
    group_id    = db.Column(db.Integer, nullable=False, index=True)
    student_id  = db.Column(db.Integer, nullable=False, index=True)
    enrolled_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    ended_at    = db.Column(db.DateTime, nullable=True)
    status      = db.Column(db.String(20), nullable=False,
                            default=STATUS_ACTIVE, server_default=STATUS_ACTIVE)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow)

    school  = db.relationship(
        'School', viewonly=True,
        primaryjoin='foreign(InstituteGroupEnrollment.school_id) == School.id')
    group   = db.relationship(
        'InstituteStudyGroup', viewonly=True,
        primaryjoin=('foreign(InstituteGroupEnrollment.group_id) '
                     '== InstituteStudyGroup.id'),
        backref=db.backref('enrollments', viewonly=True, lazy='dynamic'))
    student = db.relationship(
        'Student', viewonly=True,
        primaryjoin='foreign(InstituteGroupEnrollment.student_id) == Student.id',
        backref=db.backref('institute_enrollments', viewonly=True, lazy='dynamic'))

    __table_args__ = (
        db.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                name='fk_institute_enrollment_school'),
        # RESTRICT on both sides: a group or student with enrollment history
        # can never be silently orphaned by a cascade.
        db.ForeignKeyConstraint(
            ['group_id', 'school_id'],
            ['institute_study_groups.id', 'institute_study_groups.school_id'],
            name='fk_institute_enrollment_group_school', ondelete='RESTRICT'),
        db.ForeignKeyConstraint(
            ['student_id', 'school_id'], ['students.id', 'students.school_id'],
            name='fk_institute_enrollment_student_school', ondelete='RESTRICT'),
        db.CheckConstraint(
            "(status = 'active' AND ended_at IS NULL) OR "
            "(status = 'ended' AND ended_at IS NOT NULL)",
            name='ck_institute_enrollment_status_ended_at'),
        db.Index('ix_institute_enrollment_school_student_status',
                 'school_id', 'student_id', 'status'),
        db.Index('ix_institute_enrollment_group_status', 'group_id', 'status'),
        # At most ONE active enrollment per (group, student). Partial unique
        # index — ended rows are excluded, so re-enrollment stays possible.
        db.Index('uq_institute_enrollment_active', 'group_id', 'student_id',
                 unique=True, postgresql_where=db.text("status = 'active'")),
    )

    def __repr__(self):
        return (f'<InstituteGroupEnrollment {self.id} group={self.group_id} '
                f'student={self.student_id} status={self.status}>')


# ═════════════════════════════════════════════════════════════════════════════
#  3c. INSTITUTE WEEKLY SCHEDULES AND MANUAL ATTENDANCE (institutes only)
# ═════════════════════════════════════════════════════════════════════════════
#
# An institute group meets on a RECURRING weekly pattern, e.g.
#     Sunday 16:00-18:00, Monday 17:00-19:00, Thursday 16:00-18:00.
#
# Those patterns are stored as RULES (InstituteGroupSchedule), never as
# pre-generated rows. Concrete occurrences are computed on demand for display
# and MATERIALIZED as an InstituteAttendanceSession only when somebody actually
# opens or records attendance. There is no cron job, no pre-creation and no
# background sweep, which is what makes the next rule enforceable:
#
#   PASSING THE SCHEDULED TIME NEVER MARKS ANYONE ABSENT.
#
# A session that nobody recorded simply does not exist as a row, or exists with
# status 'not_recorded'. Absence is only ever an explicit human choice.
#
# Attendance is NOT stored in student_attendance: that table carries
# UNIQUE (student_id, date), i.e. one record per student per DAY, while an
# institute student may legitimately attend two different groups on the same
# day. A separate per-session table is therefore required, not a preference.


class InstituteGroupSchedule(db.Model):
    """One recurring weekly slot of an institute study group.

    The rule repeats every week for as long as the group runs; the group's own
    start_date / end_date and is_active flag bound it, so no extra date fields
    are duplicated here.

    Editing or deleting a slot only changes FUTURE computed occurrences. It can
    never touch a stored InstituteAttendanceSession, which keeps its own
    date/time snapshot — that is why schedule_id is nullable ON DELETE SET NULL
    on the session side.
    """
    __tablename__ = 'institute_group_schedules'
    __school_scoped__ = True
    __year_scoped__ = True

    # 0 = Sunday … 6 = Saturday, identical to Schedule.day_of_week and to the
    # DAYS list in the schedules blueprint. Not re-invented.
    DAY_MIN, DAY_MAX = 0, 6

    id               = db.Column(db.Integer, primary_key=True)
    school_id        = db.Column(db.Integer, nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    group_id         = db.Column(db.Integer, nullable=False, index=True)
    day_of_week      = db.Column(db.Integer, nullable=False)
    start_time       = db.Column(db.Time, nullable=False)
    end_time         = db.Column(db.Time, nullable=False)
    is_active        = db.Column(db.Boolean, nullable=False, default=True,
                                 server_default=db.true())
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow,
                                 onupdate=datetime.utcnow)

    school = db.relationship(
        'School', viewonly=True,
        primaryjoin='foreign(InstituteGroupSchedule.school_id) == School.id')
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    group  = db.relationship(
        'InstituteStudyGroup', viewonly=True,
        primaryjoin=('foreign(InstituteGroupSchedule.group_id) '
                     '== InstituteStudyGroup.id'),
        backref=db.backref('schedule_slots', viewonly=True, lazy='dynamic'))

    __table_args__ = (
        db.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                name='fk_institute_schedule_school'),
        # Same-school ownership enforced by PostgreSQL, not only by routes.
        # RESTRICT: a group carrying schedule rules cannot be silently removed.
        db.ForeignKeyConstraint(
            ['group_id', 'school_id'],
            ['institute_study_groups.id', 'institute_study_groups.school_id'],
            name='fk_institute_schedule_group_school', ondelete='RESTRICT'),
        # One slot per group per weekday per start time. Matches the existing
        # uq_schedule_section_subject_day_start convention and makes
        # (group, date, start_time) a sound session key further down.
        db.UniqueConstraint('group_id', 'day_of_week', 'start_time',
                            name='uq_institute_schedule_group_day_start'),
        db.CheckConstraint('start_time < end_time',
                           name='ck_institute_schedule_time_order'),
        db.CheckConstraint('day_of_week >= 0 AND day_of_week <= 6',
                           name='ck_institute_schedule_day_range'),
        db.Index('ix_institute_schedule_group_active', 'group_id', 'is_active'),
    )

    def __repr__(self):
        return (f'<InstituteGroupSchedule {self.id} g={self.group_id} '
                f'd={self.day_of_week} {self.start_time}-{self.end_time}>')


class InstituteAttendanceSession(db.Model):
    """One materialized occurrence of a group meeting.

    Created ONLY when a human opens or records attendance for it — never by a
    scheduler. Until then the occurrence exists purely as a computed value.

    start_time / end_time / instructor_id are HISTORICAL SNAPSHOTS taken when
    the session is materialized. Editing the weekly rule afterwards, or
    reassigning the group's instructor, never rewrites a stored session.
    """
    __tablename__ = 'institute_attendance_sessions'
    __school_scoped__ = True
    __year_scoped__ = True

    STATUS_NOT_RECORDED = 'not_recorded'
    STATUS_RECORDED     = 'recorded'
    STATUSES = (STATUS_NOT_RECORDED, STATUS_RECORDED)

    # Attendance source. Only the two manual values are produced in this phase;
    # 'card' and 'device' are declared so a future hardware integration can
    # target an existing session without a schema change or a data migration.
    SOURCE_MANUAL_ADMIN      = 'manual_admin'
    SOURCE_MANUAL_INSTRUCTOR = 'manual_instructor'
    SOURCE_CARD              = 'card'
    SOURCE_DEVICE            = 'device'
    SOURCES = (SOURCE_MANUAL_ADMIN, SOURCE_MANUAL_INSTRUCTOR,
               SOURCE_CARD, SOURCE_DEVICE)

    id               = db.Column(db.Integer, primary_key=True)
    school_id        = db.Column(db.Integer, nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    group_id         = db.Column(db.Integer, nullable=False, index=True)
    # Nullable so deleting a weekly rule never deletes recorded history, and so
    # an ad-hoc (unscheduled) session can exist.
    schedule_id      = db.Column(db.Integer,
                                 db.ForeignKey('institute_group_schedules.id',
                                               ondelete='SET NULL'),
                                 nullable=True, index=True)
    session_date     = db.Column(db.Date, nullable=False)
    start_time       = db.Column(db.Time, nullable=False)
    end_time         = db.Column(db.Time, nullable=False)
    instructor_id    = db.Column(db.Integer, nullable=True, index=True)
    status           = db.Column(db.String(20), nullable=False,
                                 default=STATUS_NOT_RECORDED,
                                 server_default=STATUS_NOT_RECORDED)
    source           = db.Column(db.String(20), nullable=True)
    recorded_by      = db.Column(db.Integer, db.ForeignKey('users.id'),
                                 nullable=True)
    recorded_at      = db.Column(db.DateTime, nullable=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow,
                                 onupdate=datetime.utcnow)

    school = db.relationship(
        'School', viewonly=True,
        primaryjoin='foreign(InstituteAttendanceSession.school_id) == School.id')
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    group  = db.relationship(
        'InstituteStudyGroup', viewonly=True,
        primaryjoin=('foreign(InstituteAttendanceSession.group_id) '
                     '== InstituteStudyGroup.id'),
        backref=db.backref('attendance_sessions', viewonly=True, lazy='dynamic'))
    schedule   = db.relationship('InstituteGroupSchedule',
                                 foreign_keys=[schedule_id])
    instructor = db.relationship(
        'Employee', viewonly=True,
        primaryjoin=('foreign(InstituteAttendanceSession.instructor_id) '
                     '== Employee.id'))
    recorder   = db.relationship('User', foreign_keys=[recorded_by])

    @property
    def is_recorded(self) -> bool:
        return self.status == self.STATUS_RECORDED

    __table_args__ = (
        db.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                name='fk_institute_session_school'),
        db.ForeignKeyConstraint(
            ['group_id', 'school_id'],
            ['institute_study_groups.id', 'institute_study_groups.school_id'],
            name='fk_institute_session_group_school', ondelete='RESTRICT'),
        # ONE session per group occurrence. This is the concurrency guarantee:
        # two simultaneous "open attendance" requests race to INSERT and the
        # loser gets an IntegrityError it can recover from by re-selecting,
        # rather than both succeeding.
        db.UniqueConstraint('group_id', 'session_date', 'start_time',
                            name='uq_institute_session_group_date_start'),
        db.CheckConstraint('start_time < end_time',
                           name='ck_institute_session_time_order'),
        db.CheckConstraint(
            "status IN ('not_recorded', 'recorded')",
            name='ck_institute_session_status'),
        db.CheckConstraint(
            "source IS NULL OR source IN "
            "('manual_admin', 'manual_instructor', 'card', 'device')",
            name='ck_institute_session_source'),
        # A recorded session must say who recorded it and how.
        db.CheckConstraint(
            "status = 'not_recorded' OR "
            "(source IS NOT NULL AND recorded_at IS NOT NULL)",
            name='ck_institute_session_recorded_fields'),
        db.Index('ix_institute_session_school_date',
                 'school_id', 'session_date'),
        db.Index('ix_institute_session_group_date', 'group_id', 'session_date'),
        # Parent key for the per-student attendance rows' composite FK.
        db.UniqueConstraint('id', 'school_id',
                            name='uq_institute_session_id_school'),
    )

    def __repr__(self):
        return (f'<InstituteAttendanceSession {self.id} g={self.group_id} '
                f'{self.session_date} {self.start_time} {self.status}>')


class InstituteAttendanceRecord(db.Model):
    """One student's explicit attendance status in one session.

    A row exists ONLY because a human chose a status. There is no "implicitly
    absent" state: an unmarked student simply has no row, which is what keeps
    "not recorded" and "recorded absent" permanently distinguishable.

    Rows are never deleted when a membership ends or a student is deactivated;
    the history survives exactly as the exam results do.
    """
    __tablename__ = 'institute_attendance_records'
    __school_scoped__ = True

    # Reuses the vocabulary Core School already stores in
    # student_attendance.status. 'on_leave' is deliberately NOT offered here:
    # it is produced by the school-day leave-request integration, not by a
    # per-session choice, and inventing it here would fork that semantics.
    STATUS_PRESENT = 'present'
    STATUS_ABSENT  = 'absent'
    STATUS_LATE    = 'late'
    STATUS_EXCUSED = 'excused'
    STATUSES = (STATUS_PRESENT, STATUS_ABSENT, STATUS_LATE, STATUS_EXCUSED)

    id          = db.Column(db.Integer, primary_key=True)
    school_id   = db.Column(db.Integer, nullable=False, index=True)
    session_id  = db.Column(db.Integer, nullable=False, index=True)
    student_id  = db.Column(db.Integer, nullable=False, index=True)
    status      = db.Column(db.String(20), nullable=False)
    source      = db.Column(db.String(20), nullable=False)
    recorded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    recorded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    notes       = db.Column(db.Text, nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow)

    school  = db.relationship(
        'School', viewonly=True,
        primaryjoin='foreign(InstituteAttendanceRecord.school_id) == School.id')
    session = db.relationship(
        'InstituteAttendanceSession', viewonly=True,
        primaryjoin=('foreign(InstituteAttendanceRecord.session_id) '
                     '== InstituteAttendanceSession.id'),
        backref=db.backref('records', viewonly=True, lazy='dynamic'))
    student = db.relationship(
        'Student', viewonly=True,
        primaryjoin=('foreign(InstituteAttendanceRecord.student_id) '
                     '== Student.id'),
        backref=db.backref('institute_attendance', viewonly=True,
                           lazy='dynamic'))
    recorder = db.relationship('User', foreign_keys=[recorded_by])

    __table_args__ = (
        db.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                name='fk_institute_attendance_school'),
        db.ForeignKeyConstraint(
            ['session_id', 'school_id'],
            ['institute_attendance_sessions.id',
             'institute_attendance_sessions.school_id'],
            name='fk_institute_attendance_session_school', ondelete='RESTRICT'),
        db.ForeignKeyConstraint(
            ['student_id', 'school_id'], ['students.id', 'students.school_id'],
            name='fk_institute_attendance_student_school', ondelete='RESTRICT'),
        # ONE result per (session, student). Makes a retried or concurrent
        # submission idempotent at the database level rather than by hope.
        db.UniqueConstraint('session_id', 'student_id',
                            name='uq_institute_attendance_session_student'),
        db.CheckConstraint(
            "status IN ('present', 'absent', 'late', 'excused')",
            name='ck_institute_attendance_status'),
        db.CheckConstraint(
            "source IN ('manual_admin', 'manual_instructor', 'card', 'device')",
            name='ck_institute_attendance_source'),
        db.Index('ix_institute_attendance_student_status',
                 'school_id', 'student_id', 'status'),
    )

    def __repr__(self):
        return (f'<InstituteAttendanceRecord {self.id} s={self.session_id} '
                f'stu={self.student_id} {self.status}>')


# ═════════════════════════════════════════════════════════════════════════════
#  4. STUDENTS  (with RFID + school + year)
# ═════════════════════════════════════════════════════════════════════════════

class ResidentialArea(db.Model):
    """
    A residential area (منطقة سكن) defined by one school for its own students.

    Purely a per-school lookup list: each school manages its own areas and a
    student may optionally be linked to one area of the SAME school. The
    existing free-text Student.address field is unchanged and unrelated.
    """
    __tablename__ = 'residential_areas'
    __school_scoped__ = True
    # Not year-scoped: areas are geographic and persist across academic years.

    id          = db.Column(db.Integer, primary_key=True)
    school_id   = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'),
                            nullable=False, index=True)
    name        = db.Column(db.String(200), nullable=False)
    is_active   = db.Column(db.Boolean, default=True, nullable=False,
                            server_default=db.true())
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow)

    school = db.relationship('School', foreign_keys=[school_id],
                             backref=db.backref('residential_areas', lazy='dynamic'))

    __table_args__ = (
        # Area name unique within a school (case-sensitive at DB level).
        db.UniqueConstraint('school_id', 'name', name='uq_residential_area_school_name'),
    )

    def __repr__(self):
        return f'<ResidentialArea {self.id} – {self.name} (school={self.school_id})>'


class Student(db.Model):
    __tablename__ = 'students'
    __school_scoped__ = True
    # Not year-scoped: students persist across academic years.
    # academic_year_id records the enrollment year and is kept for reference.

    id            = db.Column(db.Integer, primary_key=True)
    student_id    = db.Column(db.String(40), nullable=False, index=True)
    full_name     = db.Column(db.String(200), nullable=False)
    date_of_birth = db.Column(db.Date, nullable=True)
    gender        = db.Column(db.String(10), nullable=True)
    nationality   = db.Column(db.String(80), nullable=True)
    address       = db.Column(db.Text, nullable=True)
    phone         = db.Column(db.String(30), nullable=True)
    photo         = db.Column(db.String(255), nullable=True)

    rfid_tag_id   = db.Column(db.String(64), nullable=True, index=True)

    # Multi-tenant scoping
    school_id          = db.Column(db.Integer, db.ForeignKey('schools.id'),
                                   nullable=False, index=True)
    academic_year_id   = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                   nullable=False, index=True)

    section_id    = db.Column(db.Integer, db.ForeignKey('sections.id'), nullable=True)

    # Optional building/branch assignment — only used when the school has
    # School.enable_buildings=True.  NULL means "no building" (default for all
    # existing students; the column stays unused when the feature is off).
    building_id   = db.Column(db.Integer, db.ForeignKey('school_buildings.id'),
                              nullable=True, index=True)

    # Optional residential-area link (same school only, validated in routes).
    # NULL means "no area" — all existing students stay valid without one.
    residential_area_id = db.Column(db.Integer, db.ForeignKey('residential_areas.id'),
                                    nullable=True, index=True)

    guardian_name     = db.Column(db.String(200), nullable=True)
    guardian_phone    = db.Column(db.String(30),  nullable=True)
    guardian_email    = db.Column(db.String(180), nullable=True)
    guardian_relation = db.Column(db.String(50),  nullable=True)

    status          = db.Column(db.String(20), default='active')
    enrollment_date = db.Column(db.Date, default=date.today)
    notes           = db.Column(db.Text, nullable=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow,
                                onupdate=datetime.utcnow)

    attendances  = db.relationship('StudentAttendance', backref='student', lazy='dynamic',
                                   cascade='all, delete-orphan')
    fee_records  = db.relationship('FeeRecord',         backref='student', lazy='dynamic',
                                   cascade='all, delete-orphan')
    exam_results = db.relationship('ExamResult',        backref='student', lazy='dynamic',
                                   cascade='all, delete-orphan')
    documents    = db.relationship('StudentDocument',   backref='student', lazy='dynamic',
                                   cascade='all, delete-orphan')
    suspensions  = db.relationship('StudentSuspension', backref='student', lazy='dynamic',
                                   cascade='all, delete-orphan')

    school       = db.relationship('School', foreign_keys=[school_id],
                                   backref=db.backref('students', lazy='dynamic'))
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id],
                                    backref=db.backref('students', lazy='dynamic'))
    building     = db.relationship('SchoolBuilding', foreign_keys=[building_id])
    residential_area = db.relationship('ResidentialArea',
                                       foreign_keys=[residential_area_id])

    __table_args__ = (
        # student_id is unique per school regardless of year (students persist).
        db.UniqueConstraint('school_id', 'student_id',
                            name='uq_student_school_student_id'),
        db.UniqueConstraint('school_id', 'rfid_tag_id',
                            name='uq_student_school_rfid_tag'),
    )

    def __repr__(self):
        return f'<Student {self.student_id} – {self.full_name}>'


class StudentDocument(db.Model):
    __tablename__ = 'student_documents'
    __school_scoped__ = True
    # Not year-scoped: documents belong to the student for their school lifetime.

    id            = db.Column(db.Integer, primary_key=True)
    student_id    = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    school_id     = db.Column(db.Integer, db.ForeignKey('schools.id'),
                              nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    document_type = db.Column(db.String(100), nullable=False)
    file_path     = db.Column(db.String(255), nullable=False)
    uploaded_at   = db.Column(db.DateTime, default=datetime.utcnow)

    # ── Soft delete (student documents only — NOT a system-wide pattern) ──────
    # An active document is deleted_at IS NULL. A deleted or replaced document
    # keeps its row AND its original file_path forever, so the stored object
    # never becomes unreferenced and the document stays restorable. Nothing is
    # ever purged automatically. All three columns are nullable, so every
    # pre-existing row is active without any backfill.
    deleted_at         = db.Column(db.DateTime, nullable=True)
    deleted_by_user_id = db.Column(db.Integer,
                                   db.ForeignKey('users.id', ondelete='SET NULL'),
                                   nullable=True)
    # Replacement history: the old row points at the new active row. ON DELETE
    # SET NULL keeps this self-reference from adding any delete-ordering
    # dependency to student deletion or school cleanup.
    replaced_by_id     = db.Column(db.Integer,
                                   db.ForeignKey('student_documents.id',
                                                 ondelete='SET NULL'),
                                   nullable=True)

    school = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    deleted_by = db.relationship('User', foreign_keys=[deleted_by_user_id])

    __table_args__ = (
        # Serves the hot "active documents of this student" query.
        db.Index('ix_student_documents_student_active',
                 'student_id', 'deleted_at'),
    )

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    def __repr__(self):
        return f'<StudentDocument {self.document_type} for {self.student_id}>'


class StudentSuspension(db.Model):
    __tablename__ = 'student_suspensions'
    __school_scoped__ = True
    # Not year-scoped: suspensions are bounded by explicit date ranges, not year scope.

    id               = db.Column(db.Integer, primary_key=True)
    student_id       = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    school_id        = db.Column(db.Integer, db.ForeignKey('schools.id'),
                                 nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    start_date       = db.Column(db.Date, nullable=False)
    end_date         = db.Column(db.Date, nullable=False)
    reason           = db.Column(db.Text, nullable=True)
    created_by       = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    creator       = db.relationship('User', foreign_keys=[created_by])
    school        = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])

    @property
    def is_active(self):
        today = date.today()
        return self.start_date <= today <= self.end_date

    def __repr__(self):
        return f'<StudentSuspension student={self.student_id} {self.start_date}–{self.end_date}>'


# ═════════════════════════════════════════════════════════════════════════════
#  5. EMPLOYEES
# ═════════════════════════════════════════════════════════════════════════════

class Complaint(db.Model):
    __tablename__ = 'complaints'
    __school_scoped__ = True
    __year_scoped__ = True

    id               = db.Column(db.Integer, primary_key=True)
    parent_id        = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    student_id       = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False, index=True)
    school_id        = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'), nullable=False, index=True)
    title            = db.Column(db.String(200), nullable=False)
    complaint_type   = db.Column(db.String(30), nullable=False)
    details          = db.Column(db.Text, nullable=False)
    attachment_path  = db.Column(db.String(500), nullable=True)
    status           = db.Column(db.String(30), nullable=False, default='new', index=True)
    manager_reply    = db.Column(db.Text, nullable=True)
    replied_by       = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    replied_at       = db.Column(db.DateTime, nullable=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    parent = db.relationship('User', foreign_keys=[parent_id],
                             backref=db.backref('complaints', lazy='dynamic'))
    student = db.relationship('Student', foreign_keys=[student_id])
    school = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    replier = db.relationship('User', foreign_keys=[replied_by])

    def __repr__(self):
        return f'<Complaint {self.id} student={self.student_id}>'


class LeaveRequest(db.Model):
    __tablename__ = 'leave_requests'
    __school_scoped__ = True
    __year_scoped__ = True

    id               = db.Column(db.Integer, primary_key=True)
    parent_id        = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    student_id       = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False, index=True)
    school_id        = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'), nullable=False, index=True)
    leave_type       = db.Column(db.String(30), nullable=False)
    from_date        = db.Column(db.Date, nullable=False)
    to_date          = db.Column(db.Date, nullable=False)
    notes            = db.Column(db.Text, nullable=True)
    attachment_path  = db.Column(db.String(500), nullable=True)
    attachment_name  = db.Column(db.String(255), nullable=True)
    attachment_mime  = db.Column(db.String(100), nullable=True)
    attachment_size  = db.Column(db.Integer,     nullable=True)
    status           = db.Column(db.String(30), nullable=False, default='pending', index=True)
    manager_note     = db.Column(db.Text, nullable=True)
    reviewed_by      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    reviewed_at      = db.Column(db.DateTime, nullable=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    source             = db.Column(db.String(20), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    parent = db.relationship('User', foreign_keys=[parent_id],
                             backref=db.backref('leave_requests', lazy='dynamic'))
    student = db.relationship('Student', foreign_keys=[student_id])
    school = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    reviewer = db.relationship('User', foreign_keys=[reviewed_by])
    created_by_user = db.relationship('User', foreign_keys=[created_by_user_id])

    def __repr__(self):
        return f'<LeaveRequest {self.id} student={self.student_id}>'


class Employee(db.Model):
    __tablename__ = 'employees'
    __school_scoped__ = True

    id            = db.Column(db.Integer, primary_key=True)
    employee_id   = db.Column(db.String(40), nullable=False, index=True)
    full_name     = db.Column(db.String(200), nullable=False)
    job_title     = db.Column(db.String(150), nullable=True)
    department    = db.Column(db.String(100), nullable=True)
    date_of_birth = db.Column(db.Date, nullable=True)
    gender        = db.Column(db.String(10), nullable=True)
    nationality   = db.Column(db.String(80), nullable=True)
    phone         = db.Column(db.String(30), nullable=True)
    email         = db.Column(db.String(180), nullable=True, unique=True)
    address       = db.Column(db.Text, nullable=True)
    photo         = db.Column(db.String(255), nullable=True)

    base_salary   = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    hire_date     = db.Column(db.Date, default=date.today)
    contract_type = db.Column(db.String(30), nullable=True)

    # ── Payroll profile (Phase: professional payroll) ─────────────────────────
    # salary_type:    'monthly' | 'daily' | 'per_session'
    # payment_method: 'cash' | 'bank' | 'wallet'
    # payroll_status: 'active' | 'suspended' | 'resigned' | 'inactive'
    salary_type        = db.Column(db.String(20),  nullable=True, default='monthly')
    pay_method         = db.Column(db.String(20),  nullable=True)
    bank_account       = db.Column(db.String(120), nullable=True)
    salary_start_date  = db.Column(db.Date,        nullable=True)
    payroll_status     = db.Column(db.String(20),  nullable=True, default='active')

    status        = db.Column(db.String(20), default='active')
    notes         = db.Column(db.Text, nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow,
                              onupdate=datetime.utcnow)

    user_id       = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    school_id     = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)

    sections_managed = db.relationship('Section', backref='teacher', lazy='dynamic',
                                        foreign_keys='Section.teacher_id')
    salary_records   = db.relationship('SalaryRecord',       backref='employee', lazy='dynamic')
    salary_components = db.relationship('SalaryComponent',   backref='employee', lazy='dynamic')
    evaluations      = db.relationship('EmployeeEvaluation', backref='employee', lazy='dynamic')

    @property
    def is_payroll_active(self) -> bool:
        """True when this employee should be included in payroll generation."""
        return (self.status == 'active'
                and (self.payroll_status in (None, '', 'active')))
    attendances      = db.relationship('EmployeeAttendance', back_populates='employee', lazy='dynamic')

    school = db.relationship('School', foreign_keys=[school_id],
                             backref=db.backref('employees', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('school_id', 'employee_id',
                            name='uq_employee_school_employee_id'),
    )

    def __repr__(self):
        return f'<Employee {self.employee_id} – {self.full_name}>'


class EmployeeDocument(db.Model):
    __tablename__ = 'employee_documents'
    __school_scoped__ = True

    id          = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False)
    school_id   = db.Column(db.Integer, db.ForeignKey('schools.id'),
                            nullable=False, index=True)
    title       = db.Column(db.String(200), nullable=False)
    file_path   = db.Column(db.String(255), nullable=False)
    doc_type    = db.Column(db.String(80), nullable=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    employee = db.relationship('Employee', backref='documents')
    school   = db.relationship('School', foreign_keys=[school_id])


# ═════════════════════════════════════════════════════════════════════════════
#  6. FEES
# ═════════════════════════════════════════════════════════════════════════════

class FeeType(db.Model):
    __tablename__ = 'fee_types'
    __school_scoped__ = True
    __year_scoped__ = True

    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(150), nullable=False)
    school_id   = db.Column(db.Integer, db.ForeignKey('schools.id'),
                            nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    fee_records = db.relationship('FeeRecord', backref='fee_type', lazy='dynamic')
    school = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])

    __table_args__ = (
        db.UniqueConstraint('school_id', 'academic_year_id', 'name',
                            name='uq_fee_type_school_year_name'),
    )


class FeeRecord(db.Model):
    __tablename__ = 'fee_records'
    __school_scoped__ = True
    __year_scoped__ = True

    id               = db.Column(db.Integer, primary_key=True)
    student_id       = db.Column(db.Integer, db.ForeignKey('students.id'),       nullable=False)
    fee_type_id      = db.Column(db.Integer, db.ForeignKey('fee_types.id'),      nullable=False)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'), nullable=False)
    school_id        = db.Column(db.Integer, db.ForeignKey('schools.id'),        nullable=False, index=True)
    total_amount     = db.Column(db.Numeric(12, 2), nullable=False)
    discount         = db.Column(db.Numeric(12, 2), default=0)
    notes            = db.Column(db.Text, nullable=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    # Full-fee cancellation (see FeeRefundEvent). A cancelled fee is preserved
    # as financial history but no longer counts as an active fee, outstanding
    # balance, collectible amount, or paid income, and rejects new payments.
    # The identity of the user who performed the cancellation is deliberately
    # NOT stored — only the fact (cancelled_at) and the reason are retained.
    cancelled_at        = db.Column(db.DateTime, nullable=True)
    cancellation_reason = db.Column(db.Text, nullable=True)

    installments  = db.relationship('FeeInstallment', backref='fee_record', lazy='dynamic',
                                    cascade='all, delete-orphan')
    academic_year = db.relationship('AcademicYear', backref='fee_records')
    school        = db.relationship('School', foreign_keys=[school_id],
                                    backref=db.backref('fee_records', lazy='dynamic'))

    # Uniqueness is enforced ONLY across ACTIVE (non-cancelled) fees: a student
    # may hold at most one non-cancelled fee of a given type per academic year,
    # but any number of previously cancelled fees may coexist as history so the
    # same fee can be re-created after cancellation. Implemented as a PARTIAL
    # unique index (``cancelled_at IS NULL``) rather than a plain unique
    # constraint, which would forbid a replacement fee alongside a cancelled one.
    __table_args__ = (
        db.Index(
            'uq_fee_record_active_student_type_year',
            'student_id', 'fee_type_id', 'academic_year_id',
            unique=True,
            postgresql_where=db.text('cancelled_at IS NULL'),
            sqlite_where=db.text('cancelled_at IS NULL'),
        ),
    )

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled_at is not None

    @property
    def net_amount(self) -> Decimal:
        return Decimal(self.total_amount or 0) - Decimal(self.discount or 0)

    @property
    def total_paid(self) -> Decimal:
        return sum(
            (Decimal(i.received_amount or 0) for i in self.installments),
            Decimal('0'),
        )

    @property
    def remaining(self) -> Decimal:
        return self.net_amount - self.total_paid

    @property
    def is_fully_paid(self) -> bool:
        return self.remaining <= Decimal('0')


class FeeInstallment(db.Model):
    __tablename__ = 'fee_installments'
    __school_scoped__ = True
    __year_scoped__ = True

    id              = db.Column(db.Integer, primary_key=True)
    fee_record_id   = db.Column(db.Integer, db.ForeignKey('fee_records.id'), nullable=False)
    school_id       = db.Column(db.Integer, db.ForeignKey('schools.id'),
                                nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    installment_no  = db.Column(db.Integer, nullable=False)
    amount          = db.Column(db.Numeric(12, 2), nullable=False)
    received_amount = db.Column(db.Numeric(12, 2), default=0, nullable=False)
    due_date        = db.Column(db.Date, nullable=False)
    paid_date       = db.Column(db.Date, nullable=True)
    status          = db.Column(db.String(20), default='pending')
    payment_method  = db.Column(db.String(20), nullable=True)
    receipt_no      = db.Column(db.String(50), unique=True, nullable=True)
    collected_by    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    notes           = db.Column(db.Text, nullable=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow,
                                onupdate=datetime.utcnow)

    collector = db.relationship('User', foreign_keys=[collected_by])
    school = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])

    def recompute_status(self):
        received = Decimal(self.received_amount or 0)
        amount   = Decimal(self.amount or 0)
        if received <= 0:
            self.status = 'overdue' if self.due_date and self.due_date < date.today() else 'pending'
        elif received >= amount:
            self.status    = 'paid'
            self.paid_date = self.paid_date or date.today()
        else:
            self.status = 'partial'


class FeeReminderLog(db.Model):
    """
    Tracks sent installment reminders for duplicate prevention.

    Each row represents one delivered reminder slot.  Unique on
    (installment_id, parent_user_id, reminder_date, slot_index) — one
    send per parent per installment per calendar date per daily slot.

    Legacy rows from before the v2 redesign may have reminder_date=NULL
    and slot_index=NULL; those do not participate in the v2 constraint
    (NULL != NULL in PostgreSQL unique indexes).
    """
    __tablename__ = 'fee_reminder_logs'

    id               = db.Column(db.Integer, primary_key=True)
    school_id        = db.Column(db.Integer, db.ForeignKey('schools.id'),
                                 nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=True)
    student_id       = db.Column(db.Integer, db.ForeignKey('students.id'),
                                 nullable=False, index=True)
    installment_id   = db.Column(db.Integer, db.ForeignKey('fee_installments.id'),
                                 nullable=False, index=True)
    parent_user_id   = db.Column(db.Integer, db.ForeignKey('users.id'),
                                 nullable=False)
    # v2 slot tracking — NULL on legacy rows
    reminder_date    = db.Column(db.Date,     nullable=True)
    slot_index       = db.Column(db.Integer,  nullable=True)
    due_date         = db.Column(db.Date,     nullable=False)
    sent_at          = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint(
            'installment_id', 'parent_user_id', 'reminder_date', 'slot_index',
            name='uq_fee_reminder_log_v2',
        ),
    )


class FeeRefundEvent(db.Model):
    """One auditable refund / full-fee-cancellation event.

    Records WHO reversed WHAT, WHEN and WHY. It is a separate financial-audit
    ledger, not a second payment system: it never moves money on its own. The
    actual reversal is expressed by flagging the exact original ``Revenue``
    allocation rows (``Revenue.refunded_at`` + ``Revenue.refund_event_id``) and
    restoring the affected installment / fee fields — all inside one atomic
    transaction with this row.

      * ``event_type='installment_refund'`` — one installment's active received
        amount fully reversed; ``installment_id`` is set.
      * ``event_type='fee_cancellation'``   — the whole fee and all its
        installments cancelled; ``installment_id`` is NULL.

    ``op_refs`` stores the comma-joined payment operation references (op_ref /
    ``[TXN:...]`` tags) that were reversed, so the event stays linked to the
    exact original operations with no timestamp/heuristic guessing.
    """
    __tablename__ = 'fee_refund_events'
    __school_scoped__ = True
    __year_scoped__ = True

    id               = db.Column(db.Integer, primary_key=True)
    school_id        = db.Column(db.Integer, db.ForeignKey('schools.id'),
                                 nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    student_id       = db.Column(db.Integer, db.ForeignKey('students.id'),
                                 nullable=False, index=True)
    fee_record_id    = db.Column(db.Integer, db.ForeignKey('fee_records.id'),
                                 nullable=False, index=True)
    installment_id   = db.Column(db.Integer, db.ForeignKey('fee_installments.id'),
                                 nullable=True, index=True)
    event_type       = db.Column(db.String(30), nullable=False)  # installment_refund | fee_cancellation
    amount           = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    reason           = db.Column(db.Text, nullable=False)
    op_refs          = db.Column(db.Text, nullable=True)
    # The identity of the user who performed the refund / cancellation is
    # deliberately NOT stored on the operation record. Only the fact, the reason,
    # the timestamp and the financial linkage are retained.
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    school        = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    student       = db.relationship('Student', foreign_keys=[student_id])
    fee_record    = db.relationship('FeeRecord', foreign_keys=[fee_record_id])
    installment   = db.relationship('FeeInstallment', foreign_keys=[installment_id])


# ═════════════════════════════════════════════════════════════════════════════
#  7. GENERAL INCOME (Revenue) & EXPENSES
# ═════════════════════════════════════════════════════════════════════════════

class RevenueCategory(db.Model):
    __tablename__ = 'revenue_categories'
    __school_scoped__ = True

    id        = db.Column(db.Integer, primary_key=True)
    name      = db.Column(db.String(150), nullable=False)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'),
                          nullable=False, index=True)

    revenues = db.relationship('Revenue', backref='category', lazy='dynamic')
    school   = db.relationship('School', foreign_keys=[school_id],
                               backref=db.backref('revenue_categories', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('school_id', 'name', name='uq_revenue_category_school_name'),
    )


class Revenue(db.Model):
    __tablename__ = 'revenues'
    __school_scoped__ = True
    __year_scoped__ = True

    id          = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey('revenue_categories.id'), nullable=False)
    school_id   = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    amount      = db.Column(db.Numeric(12, 2), nullable=False)
    description = db.Column(db.Text, nullable=True)
    date        = db.Column(db.Date, nullable=False, default=date.today)
    recorded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    # Refund reversal (see FeeRefundEvent). A revenue allocation with
    # refunded_at set is preserved as history but is NO LONGER active income:
    # every "active revenue" total in the system filters on refunded_at IS NULL.
    refunded_at     = db.Column(db.DateTime, nullable=True)
    refund_event_id = db.Column(db.Integer, db.ForeignKey('fee_refund_events.id'),
                                nullable=True, index=True)

    recorder = db.relationship('User', foreign_keys=[recorded_by])
    school   = db.relationship('School', foreign_keys=[school_id],
                               backref=db.backref('revenues', lazy='dynamic'))
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id],
                                    backref=db.backref('revenues', lazy='dynamic'))
    refund_event  = db.relationship('FeeRefundEvent', foreign_keys=[refund_event_id],
                                    backref=db.backref('reversed_revenues', lazy='dynamic'))

    @property
    def is_refunded(self) -> bool:
        return self.refunded_at is not None

    @property
    def display_description(self):
        """Human-facing description with the internal ``[TXN:...]`` payment-
        operation tag removed. The stored ``description`` keeps the tag — it is
        the persistent identifier the fees module uses to resolve a payment's
        receipt (see ``resolve_payment_amount_for_receipt``) — so only display
        surfaces (revenue lists, exports) use this property. Rows without a tag
        (manual revenue, historical fee payments) are returned unchanged."""
        import re
        return re.sub(r'\s*\[TXN:[^\]]+\]', '', self.description or '').strip()


class ExpenseCategory(db.Model):
    __tablename__ = 'expense_categories'
    __school_scoped__ = True

    id        = db.Column(db.Integer, primary_key=True)
    name      = db.Column(db.String(150), nullable=False)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'),
                          nullable=False, index=True)
    is_system = db.Column(db.Boolean, default=False)

    expenses = db.relationship('Expense', backref='category', lazy='dynamic')
    school   = db.relationship('School', foreign_keys=[school_id],
                               backref=db.backref('expense_categories', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('school_id', 'name', name='uq_expense_category_school_name'),
    )


class Expense(db.Model):
    __tablename__ = 'expenses'
    __school_scoped__ = True
    __year_scoped__ = True

    id          = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey('expense_categories.id'), nullable=False)
    school_id   = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    amount      = db.Column(db.Numeric(12, 2), nullable=False)
    description = db.Column(db.Text, nullable=True)
    date        = db.Column(db.Date, nullable=False, default=date.today)
    approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_by  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    receipt     = db.Column(db.String(255), nullable=True)

    payment_method = db.Column(db.String(20), default='cash')
    reference_no   = db.Column(db.String(64), nullable=True)
    source      = db.Column(db.String(20), default='manual')
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                             onupdate=datetime.utcnow)

    approver = db.relationship('User', foreign_keys=[approved_by])
    creator  = db.relationship('User', foreign_keys=[created_by])
    school   = db.relationship('School', foreign_keys=[school_id],
                               backref=db.backref('expenses', lazy='dynamic'))
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id],
                                    backref=db.backref('expenses', lazy='dynamic'))


# ═════════════════════════════════════════════════════════════════════════════
#  8. SALARY SYSTEM
# ═════════════════════════════════════════════════════════════════════════════

class SalaryRecord(db.Model):
    __tablename__ = 'salary_records'
    __school_scoped__ = True
    __year_scoped__ = True

    id          = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False)
    school_id   = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    month       = db.Column(db.Integer, nullable=False)
    year        = db.Column(db.Integer, nullable=False)
    base_salary = db.Column(db.Numeric(12, 2), nullable=False)
    # allowances / deductions are kept as cached TOTALS (sum of PayrollItem lines)
    # for backward compatibility with reports, Excel/PDF exports and old records.
    allowances  = db.Column(db.Numeric(12, 2), default=0)
    deductions  = db.Column(db.Numeric(12, 2), default=0)
    net_salary  = db.Column(db.Numeric(12, 2), nullable=False)
    paid_date   = db.Column(db.Date, nullable=True)
    # status: 'draft' | 'approved' | 'paid' | 'cancelled'
    # ('pending' from the legacy system is treated as 'draft'.)
    status      = db.Column(db.String(20), default='draft')
    payment_method = db.Column(db.String(20), nullable=True)
    notes       = db.Column(db.Text, nullable=True)

    # ── Snapshots (keep history correct even if the employee changes later) ────
    employee_name_snapshot = db.Column(db.String(200), nullable=True)
    job_title_snapshot     = db.Column(db.String(150), nullable=True)
    department_snapshot    = db.Column(db.String(100), nullable=True)

    # ── Attendance breakdown (informational counts; money lives in PayrollItem)─
    absence_days       = db.Column(db.Integer, default=0)
    late_count         = db.Column(db.Integer, default=0)
    early_leave_count  = db.Column(db.Integer, default=0)

    # ── Workflow audit ────────────────────────────────────────────────────────
    approved_by  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    approved_at  = db.Column(db.DateTime, nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)

    expense_id  = db.Column(db.Integer, db.ForeignKey('expenses.id'), nullable=True)
    created_by  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow)

    creator  = db.relationship('User', foreign_keys=[created_by])
    approver = db.relationship('User', foreign_keys=[approved_by])
    expense  = db.relationship('Expense', foreign_keys=[expense_id])
    school   = db.relationship('School',  foreign_keys=[school_id],
                               backref=db.backref('salary_records', lazy='dynamic'))
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id],
                                    backref=db.backref('salary_records', lazy='dynamic'))
    items = db.relationship('PayrollItem', back_populates='salary_record',
                            cascade='all, delete-orphan', lazy='select',
                            order_by='PayrollItem.id')

    __table_args__ = (
        db.UniqueConstraint('employee_id', 'month', 'year',
                            name='uq_salary_month_year'),
    )

    # ── Display helpers (prefer snapshot, fall back to live employee) ──────────
    @property
    def employee_name(self) -> str:
        return self.employee_name_snapshot or (
            self.employee.full_name if self.employee else f'#{self.employee_id}')

    @property
    def job_title(self) -> str:
        return self.job_title_snapshot or (
            self.employee.job_title if self.employee else '') or ''

    @property
    def department(self) -> str:
        return self.department_snapshot or (
            self.employee.department if self.employee else '') or ''

    @property
    def is_locked(self) -> bool:
        """Approved/Paid records are locked from normal editing."""
        return self.status in ('approved', 'paid')

    @property
    def addition_items(self):
        return [i for i in self.items if i.item_type == 'addition']

    @property
    def deduction_items(self):
        return [i for i in self.items if i.item_type == 'deduction']

    def recompute(self):
        """Recalculate cached allowances/deductions/net from line items."""
        add = sum((Decimal(i.amount or 0) for i in self.items
                   if i.item_type == 'addition'), Decimal('0'))
        ded = sum((Decimal(i.amount or 0) for i in self.items
                   if i.item_type == 'deduction'), Decimal('0'))
        self.allowances = add
        self.deductions = ded
        self.net_salary = (Decimal(self.base_salary or 0)) + add - ded
        return self.net_salary


class PayrollSettings(db.Model):
    """
    Per-school payroll configuration (one row per school).

    School-scoped only — settings persist across academic years.  Created lazily
    via PayrollSettings.get_or_create(school_id) the first time the payroll
    settings page or a generation run needs them.
    """
    __tablename__ = 'payroll_settings'
    __school_scoped__ = True

    id        = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'),
                          nullable=False, unique=True, index=True)

    # General
    payroll_calculation_day = db.Column(db.Integer, default=28)
    default_payment_day     = db.Column(db.Integer, default=1)
    allow_edit_draft        = db.Column(db.Boolean, default=True)

    # Attendance-based deductions
    attendance_deduction_enabled = db.Column(db.Boolean, default=False)
    # absence_method: 'fixed' (flat amount per absent day) | 'divider' (base / working_days)
    absence_method        = db.Column(db.String(20),  default='fixed')
    absence_fixed_amount  = db.Column(db.Numeric(12, 2), default=0)
    monthly_working_days  = db.Column(db.Integer, default=26)

    late_deduction_enabled = db.Column(db.Boolean, default=False)
    # late_method: 'fixed_each' | 'per_minute' | 'per_group'
    late_method        = db.Column(db.String(20),  default='fixed_each')
    late_amount        = db.Column(db.Numeric(12, 2), default=0)
    late_allowed_count = db.Column(db.Integer, default=0)
    late_group_size    = db.Column(db.Integer, default=3)

    early_leave_deduction_enabled = db.Column(db.Boolean, default=False)
    early_leave_amount = db.Column(db.Numeric(12, 2), default=0)

    # When True, days with an approved paid leave are NOT counted as absence.
    unpaid_leave_deduction_enabled = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    school = db.relationship('School', foreign_keys=[school_id],
                             backref=db.backref('payroll_settings', uselist=False))

    @classmethod
    def get_or_create(cls, school_id):
        """Return the school's settings row, creating defaults if missing.
        Does NOT commit — the caller is responsible for committing."""
        row = (cls.query.execution_options(bypass_tenant_scope=True)
               .filter_by(school_id=school_id).first())
        if row is None:
            row = cls(school_id=school_id)
            db.session.add(row)
            db.session.flush()
        return row


class SalaryComponent(db.Model):
    """
    Reusable salary component definition (allowance or deduction).

    School-scoped only (definitions persist across years).  Recurring components
    are auto-applied to matching employees during payroll generation; one-time
    components are added manually to a single payroll record.
    """
    __tablename__ = 'salary_components'
    __school_scoped__ = True

    id        = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'),
                          nullable=False, index=True)
    name      = db.Column(db.String(150), nullable=False)
    # component_type: 'addition' | 'deduction'
    component_type = db.Column(db.String(20), nullable=False, default='addition')
    # amount_type: 'fixed' | 'variable'  (variable = leave amount blank until applied)
    amount_type    = db.Column(db.String(20), nullable=False, default='fixed')
    default_amount = db.Column(db.Numeric(12, 2), default=0)
    # recurrence: 'recurring' (auto every month) | 'one_time'
    recurrence     = db.Column(db.String(20), nullable=False, default='recurring')
    # scope: 'general' (all active employees) | 'employee' (one employee)
    scope          = db.Column(db.String(20), nullable=False, default='general')
    employee_id    = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=True)
    is_active      = db.Column(db.Boolean, default=True)
    notes          = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    school = db.relationship('School', foreign_keys=[school_id],
                             backref=db.backref('salary_components', lazy='dynamic'))

    def __repr__(self):
        return f'<SalaryComponent {self.name} ({self.component_type})>'


class PayrollItem(db.Model):
    """
    A single addition/deduction line on one SalaryRecord.

    School + year scoped (it belongs to a payroll record for a specific year).
    Money lives here; SalaryRecord.allowances/deductions/net_salary are cached
    totals derived from these lines via SalaryRecord.recompute().
    """
    __tablename__ = 'payroll_items'
    __school_scoped__ = True
    __year_scoped__ = True

    id        = db.Column(db.Integer, primary_key=True)
    salary_record_id = db.Column(db.Integer,
                                 db.ForeignKey('salary_records.id', ondelete='CASCADE'),
                                 nullable=False, index=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'),
                          nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    component_id = db.Column(db.Integer,
                             db.ForeignKey('salary_components.id', ondelete='SET NULL'),
                             nullable=True)
    name      = db.Column(db.String(150), nullable=False)
    # item_type: 'addition' | 'deduction'
    item_type = db.Column(db.String(20), nullable=False)
    amount    = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    # source: 'recurring' | 'one_time' | 'attendance' | 'manual'
    source    = db.Column(db.String(20), default='manual')
    notes     = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    salary_record = db.relationship('SalaryRecord', foreign_keys=[salary_record_id],
                                    back_populates='items')
    component     = db.relationship('SalaryComponent', foreign_keys=[component_id])
    school        = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])

    def __repr__(self):
        return f'<PayrollItem {self.name} {self.item_type} {self.amount}>'


# ═════════════════════════════════════════════════════════════════════════════
#  9. ATTENDANCE  (school + year scoped)
# ═════════════════════════════════════════════════════════════════════════════

class StudentAttendance(db.Model):
    __tablename__ = 'student_attendance'
    __school_scoped__ = True
    __year_scoped__ = True

    id               = db.Column(db.Integer, primary_key=True)
    student_id       = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    school_id        = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'), nullable=False, index=True)
    date             = db.Column(db.Date, nullable=False, default=date.today)
    status           = db.Column(db.String(20), nullable=False)
    check_in         = db.Column(db.Time, nullable=True)
    check_out        = db.Column(db.Time, nullable=True)
    source           = db.Column(db.String(20), default='manual')
    device_id        = db.Column(db.Integer, db.ForeignKey('devices.id'), nullable=True)
    recorded_by      = db.Column(db.Integer, db.ForeignKey('users.id'),   nullable=True)
    notes            = db.Column(db.Text, nullable=True)
    # Shift that determined this record's absence/status — NULL for non-shift schools
    # and for records created before shifts were enabled.
    shift_id         = db.Column(db.Integer, db.ForeignKey('attendance_shifts.id'), nullable=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    recorder      = db.relationship('User',   foreign_keys=[recorded_by])
    device        = db.relationship('Device', foreign_keys=[device_id])
    school        = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    shift         = db.relationship('AttendanceShift', foreign_keys=[shift_id])

    __table_args__ = (
        db.UniqueConstraint('student_id', 'date', name='uq_student_date'),
    )


class EmployeeAttendance(db.Model):
    __tablename__ = 'employee_attendance'
    __school_scoped__ = True
    __year_scoped__ = True

    id          = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False)
    school_id   = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    date        = db.Column(db.Date, nullable=False, default=date.today)
    status      = db.Column(db.String(20), nullable=False)
    check_in    = db.Column(db.Time, nullable=True)
    check_out   = db.Column(db.Time, nullable=True)
    recorded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    notes       = db.Column(db.Text, nullable=True)
    source      = db.Column(db.String(30), nullable=True)   # 'manual', 'aiface', etc.
    device_id   = db.Column(db.Integer,
                            db.ForeignKey('attendance_devices.id', ondelete='SET NULL'),
                            nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    school        = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    employee      = db.relationship('Employee', foreign_keys=[employee_id],
                                    back_populates='attendances')
    device        = db.relationship('AttendanceDevice', foreign_keys=[device_id])

    __table_args__ = (
        db.UniqueConstraint('employee_id', 'date', name='uq_employee_date'),
    )


# ═════════════════════════════════════════════════════════════════════════════
#  10. HARDWARE — ESP32 / Arduino device registry
# ═════════════════════════════════════════════════════════════════════════════

class Device(db.Model):
    __tablename__ = 'devices'
    __school_scoped__ = True

    id          = db.Column(db.Integer, primary_key=True)
    device_id   = db.Column(db.String(64),  unique=True, nullable=False, index=True)
    name        = db.Column(db.String(120), nullable=False)
    location    = db.Column(db.String(120), nullable=True)
    api_key     = db.Column(db.String(128), unique=True, nullable=False, index=True)
    purpose     = db.Column(db.String(30),  default='attendance')
    is_active   = db.Column(db.Boolean, default=True)
    last_seen   = db.Column(db.DateTime, nullable=True)
    firmware    = db.Column(db.String(30), nullable=True)
    school_id   = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    created_by  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow)

    creator = db.relationship('User', foreign_keys=[created_by])
    school  = db.relationship('School', foreign_keys=[school_id],
                              backref=db.backref('devices', lazy='dynamic'))

    def __repr__(self):
        return f'<Device {self.device_id} ({self.name})>'


# ═════════════════════════════════════════════════════════════════════════════
#  11. EXAMS & GRADES
# ═════════════════════════════════════════════════════════════════════════════

class ExamType(db.Model):
    __tablename__ = 'exam_types'

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(100), nullable=False)
    weight     = db.Column(db.Numeric(5, 2), default=100)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Exam(db.Model):
    __tablename__ = 'exams'
    __school_scoped__ = True
    __year_scoped__ = True

    id               = db.Column(db.Integer, primary_key=True)
    school_id        = db.Column(db.Integer, db.ForeignKey('schools.id'),
                                 nullable=False, index=True)
    exam_type_id     = db.Column(db.Integer, db.ForeignKey('exam_types.id'),     nullable=True)
    exam_name        = db.Column(db.String(200), nullable=True)
    subject_id       = db.Column(db.Integer, db.ForeignKey('subjects.id'),       nullable=False)
    # Nullable ONLY so an institute exam can target a study group instead of a
    # section. Every school exam still sets it, and ck_exam_single_target makes
    # a school exam without a section impossible at the database level.
    section_id       = db.Column(db.Integer, db.ForeignKey('sections.id'),       nullable=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'), nullable=False)
    # ── Institute target (School.is_institute only) ───────────────────────────
    # An institute organises teaching as subject -> study group -> instructor and
    # its students carry no section, so an institute exam cannot use section_id.
    institute_group_id = db.Column(db.Integer, nullable=True, index=True)
    exam_date        = db.Column(db.Date,    nullable=False)
    exam_time        = db.Column(db.Time,    nullable=True)
    duration_minutes = db.Column(db.Integer, nullable=True)
    max_marks        = db.Column(db.Numeric(6, 2), nullable=False, default=100)
    pass_marks       = db.Column(db.Numeric(6, 2), nullable=False, default=50)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    exam_type     = db.relationship('ExamType',     backref='exams')
    subject       = db.relationship('Subject',      backref='exams')
    section       = db.relationship('Section',      backref='exams')
    academic_year = db.relationship('AcademicYear', backref='exams')
    school        = db.relationship('School',       foreign_keys=[school_id],
                                    backref=db.backref('exams', lazy='dynamic'))
    results       = db.relationship('ExamResult',   backref='exam', lazy='dynamic')
    # Read-only with an explicit primaryjoin: school_id already participates in
    # the composite FK below, so SQLAlchemy must never try to write it through a
    # second relationship. Same convention as the institute models themselves.
    institute_group = db.relationship(
        'InstituteStudyGroup', viewonly=True,
        primaryjoin='foreign(Exam.institute_group_id) == InstituteStudyGroup.id')

    __table_args__ = (
        # Cross-school safety enforced by PostgreSQL, not only by route checks:
        # an exam of school A can never point at a study group of school B.
        # The parent key uq_institute_group_id_school comes from k1n2s3t4g5r6.
        #
        # ON DELETE RESTRICT, deliberately NOT a cascade and NOT SET NULL: an
        # exam (and therefore its results) must never be deleted or silently
        # detached because a group was removed. A group that carries exams
        # cannot be deleted at all — the same dependency-guard posture the
        # enrollment table already uses.
        db.ForeignKeyConstraint(
            ['institute_group_id', 'school_id'],
            ['institute_study_groups.id', 'institute_study_groups.school_id'],
            name='fk_exam_institute_group_school', ondelete='RESTRICT'),
        # Exactly one target, never both and never neither. Safe to enforce in
        # the database because section_id was NOT NULL until this revision, so
        # every pre-existing row has a section and a NULL group — the predicate
        # holds for 100% of legacy rows by construction. Verified by validating
        # the constraint against the isolated local instance.
        db.CheckConstraint(
            '(section_id IS NOT NULL AND institute_group_id IS NULL) OR '
            '(section_id IS NULL AND institute_group_id IS NOT NULL)',
            name='ck_exam_single_target'),
        db.Index('ix_exam_school_year_group',
                 'school_id', 'academic_year_id', 'institute_group_id'),
    )

    @property
    def display_name(self) -> str:
        return self.exam_name or (self.exam_type.name if self.exam_type else 'اختبار')


class ExamResult(db.Model):
    __tablename__ = 'exam_results'
    __school_scoped__ = True
    __year_scoped__ = True

    id           = db.Column(db.Integer, primary_key=True)
    exam_id      = db.Column(db.Integer, db.ForeignKey('exams.id'),     nullable=False)
    student_id   = db.Column(db.Integer, db.ForeignKey('students.id'),  nullable=False)
    school_id    = db.Column(db.Integer, db.ForeignKey('schools.id'),
                             nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    marks        = db.Column(db.Numeric(6, 2), nullable=False)
    grade_letter = db.Column(db.String(5),  nullable=True)
    is_pass      = db.Column(db.Boolean,    nullable=True)
    rank         = db.Column(db.Integer,    nullable=True)
    entered_by   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    notes        = db.Column(db.Text, nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    enterer = db.relationship('User', foreign_keys=[entered_by])
    school = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])

    __table_args__ = (
        db.UniqueConstraint('exam_id', 'student_id', name='uq_exam_student'),
    )


# ═════════════════════════════════════════════════════════════════════════════
#  12. EMPLOYEE EVALUATION
# ═════════════════════════════════════════════════════════════════════════════

class EmployeeEvaluation(db.Model):
    __tablename__ = 'employee_evaluations'
    __school_scoped__ = True
    __year_scoped__ = True

    id               = db.Column(db.Integer, primary_key=True)
    employee_id      = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False)
    evaluator_id     = db.Column(db.Integer, db.ForeignKey('users.id'),     nullable=False)
    school_id        = db.Column(db.Integer, db.ForeignKey('schools.id'),
                                 nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    period           = db.Column(db.String(50), nullable=False)
    performance      = db.Column(db.Integer, nullable=False)
    discipline       = db.Column(db.Integer, nullable=False)
    attendance_score = db.Column(db.Integer, nullable=False)
    final_score      = db.Column(db.Numeric(5, 2), nullable=True)
    notes            = db.Column(db.Text, nullable=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    evaluator = db.relationship('User', foreign_keys=[evaluator_id])
    school = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])


class EmployeeLeaveRequest(db.Model):
    """Leave requests submitted by employees (e.g. teachers) for admin review.

    Distinct from the parent/student ``LeaveRequest`` model: ownership is the
    Employee, not a parent + student. School-scoped only (employees persist
    across academic years); ``academic_year_id`` is recorded for reporting but
    queries are not year-filtered so a teacher always sees their full history.
    """
    __tablename__ = 'employee_leave_requests'
    __school_scoped__ = True

    id               = db.Column(db.Integer, primary_key=True)
    employee_id      = db.Column(db.Integer, db.ForeignKey('employees.id'),
                                 nullable=False, index=True)
    school_id        = db.Column(db.Integer, db.ForeignKey('schools.id'),
                                 nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=True, index=True)
    leave_type       = db.Column(db.String(30), nullable=False)
    from_date        = db.Column(db.Date, nullable=False)
    to_date          = db.Column(db.Date, nullable=False)
    reason           = db.Column(db.Text, nullable=False)
    details          = db.Column(db.Text, nullable=True)
    attachment_path  = db.Column(db.String(500), nullable=True)
    status           = db.Column(db.String(30), nullable=False,
                                 default='pending', index=True)
    admin_response   = db.Column(db.Text, nullable=True)
    rejection_reason = db.Column(db.Text, nullable=True)
    reviewed_by      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    reviewed_at      = db.Column(db.DateTime, nullable=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow,
                                 onupdate=datetime.utcnow)

    source             = db.Column(db.String(20), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    employee      = db.relationship('Employee', foreign_keys=[employee_id],
                                    backref=db.backref('leave_requests', lazy='dynamic'))
    school        = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    reviewer      = db.relationship('User', foreign_keys=[reviewed_by])
    created_by_user = db.relationship('User', foreign_keys=[created_by_user_id])

    def __repr__(self):
        return f'<EmployeeLeaveRequest {self.id} employee={self.employee_id}>'


# ═════════════════════════════════════════════════════════════════════════════
#  13. IN-APP NOTIFICATIONS
# ═════════════════════════════════════════════════════════════════════════════

class Notification(db.Model):
    __tablename__ = 'notifications'
    __school_scoped__ = True

    id             = db.Column(db.Integer, primary_key=True)
    school_id      = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    title          = db.Column(db.String(200), nullable=False)
    body           = db.Column(db.Text, nullable=False)
    ntype          = db.Column(db.String(50), nullable=False)
    target_role    = db.Column(db.String(50), nullable=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    is_read        = db.Column(db.Boolean, default=False)
    created_by     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    creator     = db.relationship('User', foreign_keys=[created_by],
                                  backref='sent_notifications')
    target_user = db.relationship('User', foreign_keys=[target_user_id],
                                  backref='targeted_notifications')
    reads   = db.relationship('NotificationRead', backref='notification',
                              lazy='dynamic', cascade='all, delete-orphan')
    school  = db.relationship('School', foreign_keys=[school_id])


class NotificationRead(db.Model):
    __tablename__ = 'notification_reads'

    id              = db.Column(db.Integer, primary_key=True)
    notification_id = db.Column(db.Integer,
                                db.ForeignKey('notifications.id', ondelete='CASCADE'),
                                nullable=False)
    user_id         = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    read_at         = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('notification_id', 'user_id', name='uq_notif_user'),
    )


# ═════════════════════════════════════════════════════════════════════════════
#  14. ADMIN BROADCASTS
# ═════════════════════════════════════════════════════════════════════════════

class Announcement(db.Model):
    __tablename__ = 'announcements'
    __school_scoped__ = True

    id            = db.Column(db.Integer, primary_key=True)
    school_id     = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    title         = db.Column(db.String(200), nullable=False)
    body          = db.Column(db.Text, nullable=False)
    audience      = db.Column(db.String(20), default='all_parents')
    target_role   = db.Column(db.String(50), nullable=True)
    scheduled_at  = db.Column(db.DateTime, nullable=True)
    sent_at       = db.Column(db.DateTime, nullable=True)
    status        = db.Column(db.String(20), default='draft')
    created_by    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    creator = db.relationship('User', foreign_keys=[created_by])
    targets = db.relationship('AnnouncementTarget', backref='announcement',
                               cascade='all, delete-orphan', lazy='dynamic')
    school  = db.relationship('School', foreign_keys=[school_id])

    def __repr__(self):
        return f'<Announcement {self.id} — {self.title}>'


class AnnouncementTarget(db.Model):
    __tablename__ = 'announcement_targets'

    id              = db.Column(db.Integer, primary_key=True)
    announcement_id = db.Column(db.Integer,
                                db.ForeignKey('announcements.id', ondelete='CASCADE'),
                                nullable=False)
    user_id         = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    __table_args__ = (
        db.UniqueConstraint('announcement_id', 'user_id',
                            name='uq_announcement_target'),
    )


# ═════════════════════════════════════════════════════════════════════════════
#  15. PUSH NOTIFICATIONS — per-user FCM delivery log
# ═════════════════════════════════════════════════════════════════════════════

class PushNotification(db.Model):
    __tablename__ = 'push_notifications'
    __school_scoped__ = True

    id             = db.Column(db.Integer, primary_key=True)
    user_id        = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    school_id      = db.Column(db.Integer, db.ForeignKey('schools.id'),
                               nullable=False, index=True)
    title          = db.Column(db.String(200), nullable=False)
    body           = db.Column(db.Text, nullable=False)
    data_json      = db.Column(db.Text, nullable=True)
    ntype          = db.Column(db.String(50), nullable=False)
    status         = db.Column(db.String(20), default='queued')
    fcm_message_id = db.Column(db.String(200), nullable=True)
    error          = db.Column(db.Text, nullable=True)
    sent_at        = db.Column(db.DateTime, nullable=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', foreign_keys=[user_id],
                           backref=db.backref('push_log', lazy='dynamic'))
    school = db.relationship('School', foreign_keys=[school_id])


# ═════════════════════════════════════════════════════════════════════════════
#  15b. MOBILE DEVICE TOKENS  (FCM tokens registered by the Flutter app)
# ═════════════════════════════════════════════════════════════════════════════

class MobileDeviceToken(db.Model):
    """
    One row per (user, device) pair.  A user may have multiple active devices.

    The fcm_token column is globally unique — if a token that was previously
    registered to user A is later submitted by user B (device transferred / app
    reinstalled under a different account), the row is reassigned to user B.

    The existing User.device_token field is kept in sync so the legacy
    notification service (which reads user.device_token) keeps working without
    any changes.
    """
    __tablename__ = 'mobile_device_tokens'

    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer,
                            db.ForeignKey('users.id', ondelete='CASCADE'),
                            nullable=False, index=True)
    school_id   = db.Column(db.Integer,
                            db.ForeignKey('schools.id', ondelete='CASCADE'),
                            nullable=False, index=True)
    fcm_token   = db.Column(db.String(512), nullable=False, unique=True)
    platform    = db.Column(db.String(20),  nullable=False, default='android')
    device_name = db.Column(db.String(200), nullable=True)
    is_active   = db.Column(db.Boolean,     nullable=False, default=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen_at = db.Column(db.DateTime, default=datetime.utcnow)

    user   = db.relationship('User',   foreign_keys=[user_id],
                             backref=db.backref('device_tokens',
                                                lazy='dynamic',
                                                cascade='all, delete-orphan'))
    school = db.relationship('School', foreign_keys=[school_id])

    def touch(self):
        """Update last_seen_at to now and ensure the token is active."""
        self.last_seen_at = datetime.utcnow()
        self.is_active    = True

    def __repr__(self):
        return (f'<MobileDeviceToken user={self.user_id} '
                f'platform={self.platform} active={self.is_active}>')


# ═════════════════════════════════════════════════════════════════════════════
#  15b. NOTIFICATION OUTBOX  (durable push delivery — transactional)
# ═════════════════════════════════════════════════════════════════════════════
#
# One row = one push to ONE device token for ONE notification event.
#
# Why a dedicated table rather than reusing push_notifications: that table is
# the delivery LOG (one row written after an attempt, status 'sent'/'failed')
# and carries no attempt counter, no lease and no due time. Adding those to a
# large, actively written production table would mean ALTERs and new indexes on
# populated data. This table is new and empty, so its migration creates only
# the table and its indexes — no lock on anything that already holds rows.
#
# Why per DEVICE TOKEN and not per user: a parent with two phones must not have
# a successful delivery repeated because the other phone timed out. Each token
# carries its own status, attempts and backoff.
#
# The full token string is deliberately NOT stored here — only the FK to
# mobile_device_tokens. The worker resolves it at send time, so a dead token
# that is later deactivated cannot leave a copy of itself behind in this table.
#
# Every FK is ON DELETE CASCADE so school cleanup can never be blocked by an
# outbox row: deleting the users (or the school) removes the pending jobs with
# them. See app/utils/school_cleanup.py — it needs no entry for this table.

class NotificationOutbox(db.Model):
    """A durable, transactional push-delivery job.

    Written inside the SAME transaction as the business change that caused it,
    so attendance and its notifications commit together or not at all. Redis is
    not involved: PostgreSQL is the source of truth and a worker sweeps it.

    Guarantee: durable at-least-once delivery with deduplicated enqueueing.
    NOT exactly-once — a crash after FCM accepts a message but before this row
    is marked sent will re-deliver it. Push notifications are display-only, so
    a rare duplicate is the correct trade against silently losing one.
    """
    __tablename__ = 'notification_outbox'
    __school_scoped__ = True

    # ── Status lifecycle ────────────────────────────────────────────────────
    #   pending ──claim──► processing ──┬──► sent       (terminal, success)
    #      ▲                            ├──► retry ──► pending (via due time)
    #      └────────────────────────────┴──► dead       (terminal, failure)
    #   cancelled is terminal and set only by an operator/cleanup decision.
    STATUS_PENDING    = 'pending'
    STATUS_PROCESSING = 'processing'
    STATUS_RETRY      = 'retry'
    STATUS_SENT       = 'sent'
    STATUS_DEAD       = 'dead'
    STATUS_CANCELLED  = 'cancelled'
    STATUSES = (STATUS_PENDING, STATUS_PROCESSING, STATUS_RETRY,
                STATUS_SENT, STATUS_DEAD, STATUS_CANCELLED)
    # Statuses the worker may pick up.
    DUE_STATUSES = (STATUS_PENDING, STATUS_RETRY)
    TERMINAL_STATUSES = (STATUS_SENT, STATUS_DEAD, STATUS_CANCELLED)

    # Semantic event type. A plain String(60), so a new producer needs no
    # migration — only a new constant here.
    EVENT_INSTITUTE_ABSENCE = 'institute_attendance_absent'
    # Normal school / AI Face device scan (check-in or check-out). Same table,
    # same state machine, same worker; only the producer differs.
    EVENT_SCHOOL_ATTENDANCE_SCAN = 'school_attendance_scan'
    # Manual school attendance (POST /attendance/take): check-in, check-out and
    # absence recorded by staff. Same table, state machine and worker.
    EVENT_SCHOOL_ATTENDANCE_MANUAL = 'school_attendance_manual'
    # Automatic school absence (cutoff passed, no attendance row): GET
    # /attendance/, "mark absent today", the scheduler and shift-mode runs.
    EVENT_SCHOOL_ATTENDANCE_AUTO_ABSENCE = 'school_attendance_auto_absence'

    id = db.Column(db.BigInteger, primary_key=True)

    school_id = db.Column(db.Integer,
                          db.ForeignKey('schools.id', ondelete='CASCADE'),
                          nullable=False, index=True)
    event_type = db.Column(db.String(60), nullable=False)

    # Delivery target. user_id is the recipient; device_token_id is the exact
    # registration this row delivers to.
    user_id = db.Column(db.Integer,
                        db.ForeignKey('users.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    device_token_id = db.Column(
        db.Integer,
        db.ForeignKey('mobile_device_tokens.id', ondelete='CASCADE'),
        nullable=False, index=True)

    # Immutable snapshot of what to send. Rendered at enqueue time so a later
    # edit to the student or group cannot rewrite history.
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    data_json = db.Column(db.Text, nullable=True)
    ntype = db.Column(db.String(50), nullable=False, default='attendance')

    # Deduplicated enqueueing. Globally unique; see the service for how the key
    # is built and why it does NOT suppress a legitimate later transition.
    dedup_key = db.Column(db.String(190), nullable=False, unique=True)

    status = db.Column(db.String(20), nullable=False,
                       default=STATUS_PENDING, server_default=STATUS_PENDING)
    attempts = db.Column(db.SmallInteger, nullable=False,
                         default=0, server_default=db.text('0'))
    next_attempt_at = db.Column(db.DateTime, nullable=True)

    # Lease: which worker holds this row and since when. A crashed worker's
    # lease expires and the row is reclaimed.
    locked_by = db.Column(db.String(80), nullable=True)
    locked_at = db.Column(db.DateTime, nullable=True)

    # Short, safe classification — never a token, credential or payload.
    last_error = db.Column(db.String(200), nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    processed_at = db.Column(db.DateTime, nullable=True)   # last attempt
    completed_at = db.Column(db.DateTime, nullable=True)   # terminal state

    __table_args__ = (
        db.CheckConstraint(
            "status IN ('pending','processing','retry','sent','dead','cancelled')",
            name='ck_notification_outbox_status'),
        db.CheckConstraint('attempts >= 0', name='ck_notification_outbox_attempts'),
        # Claiming due work: the worker's hot path.
        db.Index('ix_notification_outbox_due', 'status', 'next_attempt_at'),
        # Reclaiming leases abandoned by a dead worker.
        db.Index('ix_notification_outbox_lease', 'status', 'locked_at'),
        # Per-tenant operational inspection.
        db.Index('ix_notification_outbox_school_status', 'school_id', 'status'),
        # Retention sweeps over terminal rows.
        db.Index('ix_notification_outbox_completed', 'status', 'completed_at'),
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES

    def __repr__(self):
        return (f'<NotificationOutbox {self.id} {self.event_type} '
                f'status={self.status} attempts={self.attempts}>')


# ═════════════════════════════════════════════════════════════════════════════
#  16. SCHEDULES
# ═════════════════════════════════════════════════════════════════════════════

class Schedule(db.Model):
    __tablename__ = 'schedules'
    __school_scoped__ = True
    __year_scoped__ = True

    id          = db.Column(db.Integer, primary_key=True)
    school_id   = db.Column(db.Integer, db.ForeignKey('schools.id'),
                            nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    # A schedule entry targets EITHER a section (section-based, the original
    # behaviour) OR a grade (grade-based, for schools that do not use sections).
    # Exactly one of section_id / grade_id is set; both are nullable so either
    # mode works. Enforced in the schedules blueprint.
    section_id  = db.Column(db.Integer, db.ForeignKey('sections.id'), nullable=True)
    grade_id    = db.Column(db.Integer, db.ForeignKey('grades.id'), nullable=True, index=True)
    subject_id  = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=False)
    teacher_id  = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=True)
    day_of_week = db.Column(db.Integer, nullable=False)
    start_time  = db.Column(db.Time, nullable=False)
    end_time    = db.Column(db.Time, nullable=False)
    room        = db.Column(db.String(50), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    section = db.relationship('Section', backref='schedules')
    grade   = db.relationship('Grade', foreign_keys=[grade_id], backref='schedules')
    subject = db.relationship('Subject', backref='schedules')
    teacher = db.relationship('Employee', backref='schedules', foreign_keys=[teacher_id])
    school = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])

    __table_args__ = (
        # Section-based uniqueness (original). NULL section_id rows (grade-based)
        # are treated as distinct by the DB, so they never collide here.
        db.UniqueConstraint('section_id', 'subject_id', 'day_of_week', 'start_time',
                            name='uq_schedule_section_subject_day_start'),
        # Grade-based uniqueness (parallel). NULL grade_id rows (section-based)
        # are distinct, so this never collides with section schedules.
        db.UniqueConstraint('grade_id', 'subject_id', 'day_of_week', 'start_time',
                            name='uq_schedule_grade_subject_day_start'),
    )


# ═════════════════════════════════════════════════════════════════════════════
#  17. AUDIT LOG
# ═════════════════════════════════════════════════════════════════════════════

class AuditLog(db.Model):
    __tablename__ = 'audit_logs'
    __school_scoped__ = True

    id          = db.Column(db.Integer, primary_key=True)
    school_id   = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=True, index=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    action      = db.Column(db.String(100), nullable=False)
    resource    = db.Column(db.String(100), nullable=True)
    resource_id = db.Column(db.Integer, nullable=True)
    details     = db.Column(db.Text, nullable=True)
    ip_address  = db.Column(db.String(50), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    user = db.relationship('User', foreign_keys=[user_id], backref='audit_logs')
    school = db.relationship('School', foreign_keys=[school_id])


# ═════════════════════════════════════════════════════════════════════════════
#  18. TRANSPORT ROUTES  (school-scoped, not year-scoped)
# ═════════════════════════════════════════════════════════════════════════════

class TransportRoute(db.Model):
    """One bus/van route operated by the school."""
    __tablename__ = 'transport_routes'
    __school_scoped__ = True

    id             = db.Column(db.Integer, primary_key=True)
    school_id      = db.Column(db.Integer, db.ForeignKey('schools.id'),
                               nullable=False, index=True)
    name           = db.Column(db.String(150), nullable=False)
    route_number   = db.Column(db.String(30),  nullable=True)
    driver_name    = db.Column(db.String(200), nullable=False)
    driver_phone   = db.Column(db.String(30),  nullable=False)
    supervisor     = db.Column(db.String(200), nullable=True)   # المشرفة / المرافق
    vehicle_type   = db.Column(db.String(80),  nullable=False)
    vehicle_number = db.Column(db.String(30),  nullable=False)
    capacity       = db.Column(db.Integer,     nullable=False, default=1)
    status         = db.Column(db.String(20),  nullable=False, default='active')  # active|inactive
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow,
                               onupdate=datetime.utcnow)

    school         = db.relationship('School', foreign_keys=[school_id],
                                     backref=db.backref('transport_routes', lazy='dynamic'))
    students_links = db.relationship('StudentTransport', backref='route',
                                     cascade='all, delete-orphan', lazy='dynamic')

    __table_args__ = (
        db.UniqueConstraint('school_id', 'name', name='uq_transport_route_school_name'),
    )

    def __repr__(self):
        return f'<TransportRoute {self.name}>'


class StudentTransport(db.Model):
    """Links a student to a transport route with subscription details."""
    __tablename__ = 'student_transport'
    __school_scoped__ = True

    id         = db.Column(db.Integer, primary_key=True)
    school_id  = db.Column(db.Integer, db.ForeignKey('schools.id'),
                           nullable=False, index=True)
    route_id   = db.Column(db.Integer, db.ForeignKey('transport_routes.id'),
                           nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'),
                           nullable=False, index=True)
    status     = db.Column(db.String(20), nullable=False, default='active')  # active|inactive
    start_date = db.Column(db.Date,  nullable=True)
    notes      = db.Column(db.Text,  nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    school  = db.relationship('School',  foreign_keys=[school_id])
    student = db.relationship('Student', foreign_keys=[student_id],
                              backref=db.backref('transport_links', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('route_id', 'student_id',
                            name='uq_student_transport_route'),
    )

    def __repr__(self):
        return f'<StudentTransport student={self.student_id} route={self.route_id}>'


# ═════════════════════════════════════════════════════════════════════════════
#  19. WHITE-LABEL / SCHOOL SETTINGS  (global fallback — one row)
# ═════════════════════════════════════════════════════════════════════════════

class InventoryCategory(db.Model):
    __tablename__ = 'inventory_categories'
    __school_scoped__ = True
    __year_scoped__ = True

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    school = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])

    __table_args__ = (
        db.UniqueConstraint('school_id', 'academic_year_id', 'name',
                            name='uq_inventory_category_school_year_name'),
    )

    def __repr__(self):
        return f'<InventoryCategory {self.name}>'


class InventoryWarehouse(db.Model):
    """Physical stock location within a school. Persists across academic
    years (like SchoolBuilding) — a warehouse is not re-created every year."""
    __tablename__ = 'inventory_warehouses'
    __school_scoped__ = True

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    is_default = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    school = db.relationship('School', foreign_keys=[school_id])

    __table_args__ = (
        db.UniqueConstraint('school_id', 'name', name='uq_inventory_warehouse_school_name'),
    )

    def __repr__(self):
        return f'<InventoryWarehouse {self.name}>'


class InventoryItem(db.Model):
    __tablename__ = 'inventory_items'
    __school_scoped__ = True
    __year_scoped__ = True

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'), nullable=False, index=True)
    category_id = db.Column(db.Integer, db.ForeignKey('inventory_categories.id'), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    item_code = db.Column(db.String(80), nullable=True)
    unit = db.Column(db.String(40), nullable=False)
    # Aggregate across all InventoryItemStock rows for this item — kept in
    # sync whenever a stock row changes. Source of truth for quantity is now
    # InventoryItemStock; these two columns exist so existing reports/filters
    # that read the item-level total keep working unchanged.
    current_quantity = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    minimum_quantity = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    purchase_price = db.Column(db.Numeric(12, 2), nullable=True)
    supplier = db.Column(db.String(200), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    size = db.Column(db.String(20), nullable=True)
    image_path = db.Column(db.String(500), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    school = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    category = db.relationship('InventoryCategory', foreign_keys=[category_id],
                               backref=db.backref('items', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('school_id', 'academic_year_id', 'item_code',
                            name='uq_inventory_item_school_year_code'),
    )

    @property
    def is_low_stock(self):
        return (self.current_quantity or 0) <= (self.minimum_quantity or 0)

    def __repr__(self):
        return f'<InventoryItem {self.name}>'


class InventoryItemStock(db.Model):
    """Per-(item, warehouse) quantity and reorder threshold. This is the
    actual source of truth for stock quantity; InventoryItem.current_quantity
    is a denormalized aggregate kept in sync from these rows."""
    __tablename__ = 'inventory_item_stocks'
    __school_scoped__ = True
    __year_scoped__ = True

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('inventory_items.id'), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('inventory_warehouses.id'), nullable=False, index=True)
    quantity = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    minimum_quantity = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    school = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    item = db.relationship('InventoryItem', foreign_keys=[item_id],
                           backref=db.backref('stocks', lazy='dynamic'))
    warehouse = db.relationship('InventoryWarehouse', foreign_keys=[warehouse_id],
                                backref=db.backref('stocks', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('item_id', 'warehouse_id', name='uq_inventory_item_stock_item_warehouse'),
    )

    @property
    def is_low_stock(self):
        return (self.quantity or 0) <= (self.minimum_quantity or 0)

    def __repr__(self):
        return f'<InventoryItemStock item={self.item_id} warehouse={self.warehouse_id}>'


class InventoryMovement(db.Model):
    __tablename__ = 'inventory_movements'
    __school_scoped__ = True
    __year_scoped__ = True

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('inventory_items.id'), nullable=False, index=True)
    # Source warehouse for 'in'/'out'/'transfer'; to_warehouse_id is only used
    # for 'transfer' (destination). Nullable to accommodate historical rows
    # created before the warehouse feature existed (backfilled by migration).
    warehouse_id = db.Column(db.Integer, db.ForeignKey('inventory_warehouses.id'), nullable=True, index=True)
    to_warehouse_id = db.Column(db.Integer, db.ForeignKey('inventory_warehouses.id'), nullable=True, index=True)
    movement_type = db.Column(db.String(20), nullable=False)
    reason = db.Column(db.String(60), nullable=False)
    quantity = db.Column(db.Numeric(12, 2), nullable=False)
    movement_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    recipient = db.Column(db.String(200), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    attachment_path = db.Column(db.String(500), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    school = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    item = db.relationship('InventoryItem', foreign_keys=[item_id],
                           backref=db.backref('movements', lazy='dynamic'))
    warehouse = db.relationship('InventoryWarehouse', foreign_keys=[warehouse_id])
    to_warehouse = db.relationship('InventoryWarehouse', foreign_keys=[to_warehouse_id])
    creator = db.relationship('User', foreign_keys=[created_by])

    def __repr__(self):
        return f'<InventoryMovement item={self.item_id} type={self.movement_type}>'


class InventoryCount(db.Model):
    __tablename__ = 'inventory_counts'
    __school_scoped__ = True
    __year_scoped__ = True

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('inventory_items.id'), nullable=False, index=True)
    # Nullable for historical rows created before the warehouse feature
    # existed (backfilled by migration); required for new counts.
    warehouse_id = db.Column(db.Integer, db.ForeignKey('inventory_warehouses.id'), nullable=True, index=True)
    system_quantity = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    actual_quantity = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    difference = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, index=True)
    reason = db.Column(db.String(200), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    counted_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    count_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    school = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    item = db.relationship('InventoryItem', foreign_keys=[item_id],
                           backref=db.backref('counts', lazy='dynamic'))
    warehouse = db.relationship('InventoryWarehouse', foreign_keys=[warehouse_id])
    counter = db.relationship('User', foreign_keys=[counted_by])

    def __repr__(self):
        return f'<InventoryCount item={self.item_id} date={self.count_date}>'


class SchoolSettings(db.Model):
    """
    Legacy single-row global settings kept for backward compatibility.
    Per-school configuration now lives in School.  This table is used
    only as a fallback when no School is found (e.g., fresh installs).
    """
    __tablename__ = 'school_settings'

    id              = db.Column(db.Integer, primary_key=True)
    school_name     = db.Column(db.String(200), nullable=False, default='Mecha-School')
    school_name_ar  = db.Column(db.String(200), nullable=True)
    logo_path       = db.Column(db.String(255), nullable=True)
    favicon_path    = db.Column(db.String(255), nullable=True)
    primary_color   = db.Column(db.String(20),  default='#0d6efd')
    address         = db.Column(db.Text, nullable=True)
    phone           = db.Column(db.String(40),  nullable=True)
    email           = db.Column(db.String(180), nullable=True)
    website         = db.Column(db.String(180), nullable=True)
    currency_code   = db.Column(db.String(10),  default='SAR')
    currency_symbol = db.Column(db.String(10),  default='﷼')
    timezone        = db.Column(db.String(50),  default='Asia/Baghdad')
    locale          = db.Column(db.String(10),  default='ar')
    receipt_footer  = db.Column(db.Text, nullable=True)
    att_start_time        = db.Column(db.Time, nullable=True)
    att_late_threshold    = db.Column(db.Time, nullable=True)
    att_absence_threshold = db.Column(db.Time, nullable=True)
    att_departure_time    = db.Column(db.Time, nullable=True)
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow,
                                onupdate=datetime.utcnow)

    @classmethod
    def get(cls):
        obj = cls.query.first()
        if obj is None:
            obj = cls()
            db.session.add(obj)
            db.session.commit()
        return obj


# ═════════════════════════════════════════════════════════════════════════════
#  22a. SCHOOL STUDENT FORM CONFIG  (per-school field visibility / required)
# ═════════════════════════════════════════════════════════════════════════════

class SchoolStudentFormConfig(db.Model):
    """
    Per-school configuration of which fields/sections appear on the
    student create/edit form.  Managed exclusively by Super Admin.

    hidden_sections : JSON list of section keys to hide entirely.
    hidden_fields   : JSON list of individual field keys to hide.
    required_fields : JSON list of field keys to mark as required
                      (beyond the hardcoded full_name requirement).

    When the row does not exist for a school, all defaults apply
    (every section/field visible, no extra required fields) so
    existing schools are not affected.
    """
    __tablename__ = 'school_student_form_config'

    id         = db.Column(db.Integer, primary_key=True)
    school_id  = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'),
                           unique=True, nullable=False, index=True)
    hidden_sections = db.Column(db.JSON, nullable=True)
    hidden_fields   = db.Column(db.JSON, nullable=True)
    required_fields = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ═════════════════════════════════════════════════════════════════════════════
#  22b. SCHOOL MODULE CONFIG  (per-school per-module section/field/action config)
# ═════════════════════════════════════════════════════════════════════════════

class SchoolModuleConfig(db.Model):
    """
    Per-school, per-module configuration of which sections/fields/actions are
    visible or enabled.  Managed exclusively by Super Admin.

    module_key examples: 'employees', 'employee_attendance', 'attendance_devices'
    (students use SchoolStudentFormConfig for backward compat)

    config JSON structure::

        {
          "hidden_sections": ["system_account", "teacher_assignment"],
          "hidden_fields":   ["base_salary", "nationality"],
          "required_fields": ["phone"],
          "disabled_actions": ["delete", "export_excel"]
        }

    No row for a (school, module_key) = everything visible/enabled (fail-open).
    """
    __tablename__ = 'school_module_configs'

    id         = db.Column(db.Integer, primary_key=True)
    school_id  = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'),
                           nullable=False, index=True)
    module_key = db.Column(db.String(50), nullable=False)
    config     = db.Column(db.JSON, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('school_id', 'module_key', name='uq_school_module_config'),
    )

    school = db.relationship('School',
                             backref=db.backref('module_configs',
                                                cascade='all, delete-orphan',
                                                lazy='dynamic'))

    def __repr__(self):
        return f'<SchoolModuleConfig school={self.school_id} module={self.module_key}>'


# ═════════════════════════════════════════════════════════════════════════════
#  22. ATTENDANCE DEVICES  (Hikvision face / card / fingerprint readers)
# ═════════════════════════════════════════════════════════════════════════════

class AttendanceDevice(db.Model):
    """
    Physical Hikvision device registered per school.
    Not year-scoped — the same device serves multiple academic years.
    """
    __tablename__ = 'attendance_devices'
    __school_scoped__ = True

    id               = db.Column(db.Integer, primary_key=True)
    school_id        = db.Column(db.Integer, db.ForeignKey('schools.id'),
                                 nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=True)
    name             = db.Column(db.String(150), nullable=False)
    device_type      = db.Column(db.String(30),  nullable=False, default='hikvision')
    device_scope     = db.Column(db.String(20),  nullable=False, default='students',
                                 server_default='students')
    ip_address       = db.Column(db.String(45),  nullable=False)
    port             = db.Column(db.Integer,     nullable=False, default=80)
    username         = db.Column(db.String(80),  nullable=False, default='admin')
    password         = db.Column(db.String(200), nullable=False)
    device_sn        = db.Column(db.String(100), nullable=False)
    is_active        = db.Column(db.Boolean,     default=True,  nullable=False, index=True)
    last_sync_at     = db.Column(db.DateTime,    nullable=True)
    notes            = db.Column(db.Text,        nullable=True)
    created_at       = db.Column(db.DateTime,    default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime,    default=datetime.utcnow,
                                 onupdate=datetime.utcnow)

    school        = db.relationship('School', foreign_keys=[school_id],
                                    backref=db.backref('attendance_devices', lazy='dynamic'))
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    event_logs    = db.relationship('DeviceEventLog', backref='device',
                                    cascade='all, delete-orphan', lazy='dynamic')
    mappings      = db.relationship('DeviceStudentMapping', backref='device',
                                    cascade='all, delete-orphan', lazy='dynamic')

    def __repr__(self):
        return f'<AttendanceDevice {self.name} ip={self.ip_address}>'


class DeviceEventLog(db.Model):
    """
    Raw access-event record fetched from a Hikvision device.
    Deduplicated by (device_id, serial_no).
    status: raw → unmatched / processed / duplicate / error
    """
    __tablename__ = 'device_event_logs'
    __school_scoped__ = True

    id                 = db.Column(db.Integer,   primary_key=True)
    school_id          = db.Column(db.Integer,   db.ForeignKey('schools.id'),
                                   nullable=False, index=True)
    academic_year_id   = db.Column(db.Integer,   db.ForeignKey('academic_years.id'),
                                   nullable=True)
    device_id          = db.Column(db.Integer,   db.ForeignKey('attendance_devices.id',
                                   ondelete='CASCADE'), nullable=False, index=True)
    serial_no          = db.Column(db.BigInteger, nullable=False)
    employee_no_string = db.Column(db.String(50), nullable=True)
    person_name        = db.Column(db.String(200), nullable=True)
    event_time         = db.Column(db.DateTime,  nullable=True)
    major              = db.Column(db.Integer,   nullable=True)
    minor              = db.Column(db.Integer,   nullable=True)
    verify_mode        = db.Column(db.String(80), nullable=True)
    picture_url        = db.Column(db.String(500), nullable=True)
    raw_json           = db.Column(db.Text,      nullable=True)
    status             = db.Column(db.String(20), nullable=False, default='raw', index=True)
    error_message      = db.Column(db.Text,      nullable=True)
    created_at         = db.Column(db.DateTime,  default=datetime.utcnow)

    school        = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])

    __table_args__ = (
        db.UniqueConstraint('device_id', 'serial_no',
                            name='uq_device_event_log_device_serial'),
    )

    def __repr__(self):
        return f'<DeviceEventLog device={self.device_id} sn={self.serial_no} status={self.status}>'


class DeviceStudentMapping(db.Model):
    """
    Maps a Hikvision numeric employeeNoString to a student within a device.
    Not year-scoped — mappings persist across years; a student keeps the
    same device number from year to year.
    Unique per (device_id, employee_no_string).
    """
    __tablename__ = 'device_student_mappings'
    __school_scoped__ = True

    id                 = db.Column(db.Integer,   primary_key=True)
    school_id          = db.Column(db.Integer,   db.ForeignKey('schools.id'),
                                   nullable=False, index=True)
    device_id          = db.Column(db.Integer,   db.ForeignKey('attendance_devices.id',
                                   ondelete='CASCADE'), nullable=False, index=True)
    employee_no_string = db.Column(db.String(50), nullable=False)
    student_id         = db.Column(db.Integer,   db.ForeignKey('students.id',
                                   ondelete='CASCADE'), nullable=False, index=True)
    is_active          = db.Column(db.Boolean,   default=True, nullable=False)
    created_at         = db.Column(db.DateTime,  default=datetime.utcnow)
    updated_at         = db.Column(db.DateTime,  default=datetime.utcnow,
                                   onupdate=datetime.utcnow)

    school   = db.relationship('School', foreign_keys=[school_id])
    student  = db.relationship('Student', foreign_keys=[student_id],
                               backref=db.backref('device_mappings', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('device_id', 'employee_no_string',
                            name='uq_device_student_mapping_device_empno'),
    )

    def __repr__(self):
        return (f'<DeviceStudentMapping device={self.device_id} '
                f'emp={self.employee_no_string} student={self.student_id}>')


class DeviceEmployeeMapping(db.Model):
    """
    Maps a device enrollment number to an employee for AI Face / Hikvision devices.
    Not year-scoped — mappings persist across academic years.
    Unique per (device_id, enrollment_no) to prevent duplicate enrollment IDs on one device.
    """
    __tablename__ = 'device_employee_mappings'
    __school_scoped__ = True

    id            = db.Column(db.Integer,    primary_key=True)
    school_id     = db.Column(db.Integer,    db.ForeignKey('schools.id'),
                               nullable=False, index=True)
    device_id     = db.Column(db.Integer,    db.ForeignKey('attendance_devices.id',
                               ondelete='CASCADE'), nullable=False, index=True)
    employee_id   = db.Column(db.Integer,    db.ForeignKey('employees.id',
                               ondelete='CASCADE'), nullable=False, index=True)
    enrollment_no = db.Column(db.String(50), nullable=False)
    is_active     = db.Column(db.Boolean,    default=True, nullable=False)
    created_at    = db.Column(db.DateTime,   default=datetime.utcnow)

    school   = db.relationship('School', foreign_keys=[school_id])
    device   = db.relationship('AttendanceDevice', foreign_keys=[device_id],
                                backref=db.backref('employee_mappings', lazy='dynamic'))
    employee = db.relationship('Employee', foreign_keys=[employee_id],
                                backref=db.backref('device_mappings', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('device_id', 'enrollment_no',
                             name='uq_device_employee_enrollid'),
    )

    def __repr__(self):
        return (f'<DeviceEmployeeMapping device={self.device_id} '
                f'enrollid={self.enrollment_no} employee={self.employee_id}>')


# ═════════════════════════════════════════════════════════════════════════════
#  24. SCHOOL CALENDAR — holidays & breaks
# ═════════════════════════════════════════════════════════════════════════════

class SchoolHoliday(db.Model):
    """
    Date-range holiday or school break.

    school_id=NULL  → global holiday that applies to every school.
    school_id=<id>  → school-specific holiday.

    NOT __school_scoped__: school_id is intentionally nullable here, so the
    automatic tenant filter would hide global rows.  Queries must be written
    explicitly (bypass_tenant_scope + OR school_id IS NULL).
    """
    __tablename__ = 'school_holidays'

    HOLIDAY_TYPES = ('official', 'summer', 'emergency', 'custom')

    id               = db.Column(db.Integer, primary_key=True)
    school_id        = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'),
                                 nullable=True, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id', ondelete='SET NULL'),
                                 nullable=True, index=True)
    name             = db.Column(db.String(200), nullable=False)
    start_date       = db.Column(db.Date, nullable=False, index=True)
    end_date         = db.Column(db.Date, nullable=False)
    holiday_type     = db.Column(db.String(20), nullable=False, default='official')
    notes            = db.Column(db.Text, nullable=True)
    is_active        = db.Column(db.Boolean, nullable=False, default=True)
    created_by       = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
                                 nullable=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    school        = db.relationship('School', foreign_keys=[school_id],
                                    backref=db.backref('school_holidays',
                                                       cascade='all, delete-orphan',
                                                       lazy='dynamic'))
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    creator       = db.relationship('User', foreign_keys=[created_by])

    @property
    def is_single_day(self):
        return self.start_date == self.end_date

    def __repr__(self):
        scope = f'school={self.school_id}' if self.school_id else 'global'
        return f'<SchoolHoliday {self.name} {self.start_date}–{self.end_date} {scope}>'


# ═════════════════════════════════════════════════════════════════════════════
#  24. HOMEWORK
# ═════════════════════════════════════════════════════════════════════════════

class Homework(db.Model):
    """
    Homework assignments created by teachers for specific sections/subjects.
    Scoped per school and academic year.
    attachment_type: 'image' | 'pdf' | None
    """
    __tablename__ = 'homework'
    __school_scoped__ = True
    __year_scoped__ = True

    id               = db.Column(db.Integer, primary_key=True)
    school_id        = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'),
                                 nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id', ondelete='CASCADE'),
                                 nullable=False, index=True)
    teacher_id       = db.Column(db.Integer, db.ForeignKey('employees.id', ondelete='SET NULL'),
                                 nullable=True, index=True)
    subject_id       = db.Column(db.Integer, db.ForeignKey('subjects.id', ondelete='SET NULL'),
                                 nullable=True, index=True)
    section_id       = db.Column(db.Integer, db.ForeignKey('sections.id', ondelete='SET NULL'),
                                 nullable=True, index=True)
    # ── Institute target (School.is_institute only) ───────────────────────────
    # A school assignment keeps targeting section_id exactly as before and
    # leaves this NULL.  An institute assignment targets a study group instead,
    # because an institute student belongs to several groups and carries no
    # section.  Exactly one of the two is set by the application; no CHECK
    # constraint is added because every pre-existing row legitimately has
    # institute_group_id IS NULL while section_id may already be NULL too
    # (ON DELETE SET NULL on sections), and such a legacy row must stay
    # readable.  The one-target rule is enforced in the homework routes.
    institute_group_id = db.Column(db.Integer, nullable=True, index=True)
    title            = db.Column(db.String(300), nullable=False)
    description      = db.Column(db.Text, nullable=True)
    publish_date     = db.Column(db.Date, nullable=False)
    due_date         = db.Column(db.Date, nullable=False)
    attachment_path  = db.Column(db.String(500), nullable=True)
    attachment_type  = db.Column(db.String(20), nullable=True)  # image | pdf
    is_active        = db.Column(db.Boolean, nullable=False, default=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    school        = db.relationship('School', foreign_keys=[school_id],
                                    backref=db.backref('homework_list',
                                                       cascade='all, delete-orphan',
                                                       lazy='dynamic'))
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    teacher       = db.relationship('Employee', foreign_keys=[teacher_id],
                                    backref=db.backref('homework_list', lazy='dynamic'))
    subject       = db.relationship('Subject', foreign_keys=[subject_id])
    section       = db.relationship('Section', foreign_keys=[section_id],
                                    backref=db.backref('homework_list', lazy='dynamic'))
    # Read-only, explicit primaryjoin: school_id already participates in the
    # composite FK below, so SQLAlchemy must never try to write it through a
    # second relationship.  Same convention as the institute models themselves.
    institute_group = db.relationship(
        'InstituteStudyGroup', viewonly=True,
        primaryjoin='foreign(Homework.institute_group_id) == InstituteStudyGroup.id')

    __table_args__ = (
        # Cross-school safety enforced by PostgreSQL, not only by route checks:
        # a homework row of school A can never point at a study group of school
        # B, because (institute_group_id, school_id) must match a real
        # (id, school_id) pair in institute_study_groups.
        #
        # The column-list form of ON DELETE SET NULL (PostgreSQL 15+) nulls ONLY
        # institute_group_id.  A plain SET NULL would also try to null the NOT
        # NULL school_id and would turn group deletion into a constraint error.
        # Groups are never hard-deleted through the interface (is_active is
        # toggled instead); this path exists only for a full-school teardown,
        # where the homework rows are removed moments later anyway.
        db.ForeignKeyConstraint(
            ['institute_group_id', 'school_id'],
            ['institute_study_groups.id', 'institute_study_groups.school_id'],
            name='fk_homework_institute_group_school',
            ondelete='SET NULL (institute_group_id)'),
        db.Index('ix_homework_school_year_group',
                 'school_id', 'academic_year_id', 'institute_group_id'),
    )

    def __repr__(self):
        return f'<Homework {self.id} — {self.title}>'


# ═══════════════════════════════════════════════════════════════════════════════
#  Chat / Messaging Module
# ═══════════════════════════════════════════════════════════════════════════════

class ChatRoom(db.Model):
    """
    A chat room (private, group, or announcement).
    type:  'private' | 'group' | 'announcement'
    scope: 'school' | 'stage' | 'grade' | 'section' | 'subject' | 'custom' | 'private'
    """
    __tablename__ = 'chat_rooms'
    __school_scoped__ = True

    id                  = db.Column(db.Integer, primary_key=True)
    school_id           = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'),
                                    nullable=False, index=True)
    academic_year_id    = db.Column(db.Integer, db.ForeignKey('academic_years.id', ondelete='SET NULL'),
                                    nullable=True)
    name                = db.Column(db.String(200), nullable=False)
    type                = db.Column(db.String(20),  nullable=False, default='group')
    scope               = db.Column(db.String(20),  nullable=False, default='custom')
    stage               = db.Column(db.String(50),  nullable=True)
    grade_id            = db.Column(db.Integer, db.ForeignKey('grades.id', ondelete='SET NULL'),
                                    nullable=True)
    section_id          = db.Column(db.Integer, db.ForeignKey('sections.id', ondelete='SET NULL'),
                                    nullable=True)
    subject_id          = db.Column(db.Integer, db.ForeignKey('subjects.id', ondelete='SET NULL'),
                                    nullable=True)
    created_by_user_id  = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
                                    nullable=True)
    is_active           = db.Column(db.Boolean, nullable=False, default=True)
    is_closed           = db.Column(db.Boolean, nullable=False, default=False)
    is_announcement_only = db.Column(db.Boolean, nullable=False, default=False)
    allow_replies       = db.Column(db.Boolean, nullable=False, default=True)
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at          = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    school        = db.relationship('School',        foreign_keys=[school_id],
                                    backref=db.backref('chat_rooms', lazy='dynamic',
                                                       cascade='all, delete-orphan'))
    academic_year = db.relationship('AcademicYear',  foreign_keys=[academic_year_id])
    grade         = db.relationship('Grade',         foreign_keys=[grade_id])
    section       = db.relationship('Section',       foreign_keys=[section_id])
    subject       = db.relationship('Subject',       foreign_keys=[subject_id])
    created_by    = db.relationship('User',          foreign_keys=[created_by_user_id])
    members       = db.relationship('ChatRoomMember',
                                    backref='room', lazy='dynamic',
                                    cascade='all, delete-orphan')
    messages      = db.relationship('ChatMessage',
                                    backref='room', lazy='dynamic',
                                    cascade='all, delete-orphan')
    schedules     = db.relationship('ChatRoomSchedule',
                                    backref='room', lazy='dynamic',
                                    cascade='all, delete-orphan')

    def __repr__(self):
        return f'<ChatRoom {self.id} {self.name!r}>'


class ChatRoomMember(db.Model):
    """Membership of a user in a chat room with role and block status."""
    __tablename__ = 'chat_room_members'

    id                = db.Column(db.Integer, primary_key=True)
    room_id           = db.Column(db.Integer, db.ForeignKey('chat_rooms.id', ondelete='CASCADE'),
                                  nullable=False, index=True)
    user_id           = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
                                  nullable=False, index=True)
    role              = db.Column(db.String(20), nullable=False, default='member')
    is_muted          = db.Column(db.Boolean, nullable=False, default=False)
    is_blocked        = db.Column(db.Boolean, nullable=False, default=False)
    joined_at         = db.Column(db.DateTime, default=datetime.utcnow)
    blocked_at        = db.Column(db.DateTime, nullable=True)
    blocked_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
                                   nullable=True)

    user       = db.relationship('User', foreign_keys=[user_id])
    blocked_by = db.relationship('User', foreign_keys=[blocked_by_user_id])

    __table_args__ = (
        db.UniqueConstraint('room_id', 'user_id', name='uq_chat_room_member'),
    )

    def __repr__(self):
        return f'<ChatRoomMember room={self.room_id} user={self.user_id} role={self.role}>'


class ChatMessage(db.Model):
    """A message inside a chat room. Soft-deletable."""
    __tablename__ = 'chat_messages'

    id                = db.Column(db.Integer, primary_key=True)
    room_id           = db.Column(db.Integer, db.ForeignKey('chat_rooms.id', ondelete='CASCADE'),
                                  nullable=False, index=True)
    sender_user_id    = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
                                  nullable=True, index=True)
    body              = db.Column(db.Text, nullable=True)
    message_type      = db.Column(db.String(20), nullable=False, default='text')
    attachment_url    = db.Column(db.String(500), nullable=True)
    attachment_name   = db.Column(db.String(200), nullable=True)
    attachment_mime   = db.Column(db.String(100), nullable=True)
    attachment_size   = db.Column(db.Integer,     nullable=True)
    is_deleted        = db.Column(db.Boolean, nullable=False, default=False)
    deleted_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'),
                                   nullable=True)
    deleted_at        = db.Column(db.DateTime, nullable=True)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at        = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    sender     = db.relationship('User', foreign_keys=[sender_user_id])
    deleted_by = db.relationship('User', foreign_keys=[deleted_by_user_id])
    reads      = db.relationship('ChatMessageRead',
                                 backref='message', lazy='dynamic',
                                 cascade='all, delete-orphan')

    def __repr__(self):
        return f'<ChatMessage {self.id} room={self.room_id}>'


class ChatMessageRead(db.Model):
    """Read receipt — one row per (message, user)."""
    __tablename__ = 'chat_message_reads'

    id         = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('chat_messages.id', ondelete='CASCADE'),
                           nullable=False, index=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'),
                           nullable=False, index=True)
    read_at    = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('message_id', 'user_id', name='uq_chat_message_read'),
    )

    def __repr__(self):
        return f'<ChatMessageRead msg={self.message_id} user={self.user_id}>'


class ChatRoomSchedule(db.Model):
    """Allowed sending-time window for a chat room (per day of week)."""
    __tablename__ = 'chat_room_schedules'

    id           = db.Column(db.Integer, primary_key=True)
    room_id      = db.Column(db.Integer, db.ForeignKey('chat_rooms.id', ondelete='CASCADE'),
                             nullable=False, index=True)
    day_of_week  = db.Column(db.Integer, nullable=False)  # 0=Sunday … 6=Saturday
    open_time    = db.Column(db.Time, nullable=False)
    close_time   = db.Column(db.Time, nullable=False)
    is_enabled   = db.Column(db.Boolean, nullable=False, default=True)

    def __repr__(self):
        return f'<ChatRoomSchedule room={self.room_id} day={self.day_of_week}>'


# ═════════════════════════════════════════════════════════════════════════════
#  SCHOOL BOARD — Videos, Announcements, and Read Tracking
# ═════════════════════════════════════════════════════════════════════════════

class SchoolVideo(db.Model):
    __tablename__ = 'school_videos'
    __school_scoped__ = True

    id            = db.Column(db.Integer, primary_key=True)
    school_id     = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    title         = db.Column(db.String(200), nullable=True)
    description   = db.Column(db.Text, nullable=True)
    media_type    = db.Column(db.String(20),  nullable=False, default='video')
    video_url     = db.Column(db.String(500), nullable=False)
    thumbnail_url = db.Column(db.String(500), nullable=True)
    audience      = db.Column(db.String(20), nullable=False, default='all')
    is_featured   = db.Column(db.Boolean, nullable=False, default=False)
    is_active     = db.Column(db.Boolean, nullable=False, default=True)
    publish_at    = db.Column(db.DateTime, nullable=True)
    expires_at    = db.Column(db.DateTime, nullable=True)
    created_by    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    creator = db.relationship('User', foreign_keys=[created_by])
    school  = db.relationship('School', foreign_keys=[school_id])

    def __repr__(self):
        return f'<SchoolVideo {self.id} — {self.title}>'


class SchoolAnnouncement(db.Model):
    __tablename__ = 'school_announcements'
    __school_scoped__ = True

    id            = db.Column(db.Integer, primary_key=True)
    school_id     = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    title         = db.Column(db.String(200), nullable=False)
    body          = db.Column(db.Text, nullable=False)
    media_url     = db.Column(db.String(500), nullable=True)
    media_type    = db.Column(db.String(20), nullable=False, default='none')
    thumbnail_url = db.Column(db.String(500), nullable=True)
    audience      = db.Column(db.String(20), nullable=False, default='all')
    is_featured   = db.Column(db.Boolean, nullable=False, default=False)
    is_active     = db.Column(db.Boolean, nullable=False, default=True)
    publish_at    = db.Column(db.DateTime, nullable=True)
    expires_at    = db.Column(db.DateTime, nullable=True)
    created_by    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    creator = db.relationship('User', foreign_keys=[created_by])
    school  = db.relationship('School', foreign_keys=[school_id])

    def __repr__(self):
        return f'<SchoolAnnouncement {self.id} — {self.title}>'


class SchoolContentRead(db.Model):
    """Per-user read receipt for school board videos and announcements."""
    __tablename__ = 'school_content_reads'

    id           = db.Column(db.Integer, primary_key=True)
    school_id    = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    content_type = db.Column(db.String(20), nullable=False)  # 'video' or 'announcement'
    content_id   = db.Column(db.Integer, nullable=False, index=True)
    read_at      = db.Column(db.DateTime, default=datetime.utcnow)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'content_type', 'content_id',
                            name='uq_school_content_read'),
    )

    def __repr__(self):
        return f'<SchoolContentRead {self.content_type}={self.content_id} user={self.user_id}>'


# ═════════════════════════════════════════════════════════════════════════════
#  STUDENT REGISTRATION RECORD  (سجل قيد الطالب)
# ═════════════════════════════════════════════════════════════════════════════

class StudentRegistrationRecord(db.Model):
    """
    Official registration card (سجل القيد) for a student.
    Stores a snapshot of the student, guardian and placement data so the
    record remains stable even if the live student profile changes later.
    One record per student per school (unique constraint).
    """
    __tablename__ = 'student_registration_records'
    __school_scoped__ = True

    id        = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'),
                          nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'),
                           nullable=False, index=True)

    # ── Student snapshot ──────────────────────────────────────────────────────
    snap_full_name       = db.Column(db.String(200), nullable=False)
    snap_student_number  = db.Column(db.String(40),  nullable=True)
    snap_gender          = db.Column(db.String(10),  nullable=True)
    snap_date_of_birth   = db.Column(db.Date,        nullable=True)
    snap_nationality     = db.Column(db.String(80),  nullable=True)
    snap_address         = db.Column(db.Text,        nullable=True)
    snap_phone           = db.Column(db.String(30),  nullable=True)
    snap_status          = db.Column(db.String(20),  nullable=True)
    snap_enrollment_date = db.Column(db.Date,        nullable=True)

    # ── Guardian / parent snapshot ────────────────────────────────────────────
    snap_guardian_name     = db.Column(db.String(200), nullable=True)
    snap_guardian_phone    = db.Column(db.String(30),  nullable=True)
    snap_guardian_email    = db.Column(db.String(180), nullable=True)
    snap_guardian_relation = db.Column(db.String(50),  nullable=True)
    snap_guardian_address  = db.Column(db.Text,        nullable=True)

    # ── Academic placement snapshot ───────────────────────────────────────────
    snap_school_name    = db.Column(db.String(200), nullable=True)
    snap_school_name_ar = db.Column(db.String(200), nullable=True)
    snap_year_name      = db.Column(db.String(50),  nullable=True)
    snap_grade_name     = db.Column(db.String(100), nullable=True)
    snap_stage          = db.Column(db.String(50),  nullable=True)
    snap_section_name   = db.Column(db.String(50),  nullable=True)

    # ── Admission information (user-editable) ─────────────────────────────────
    admission_date  = db.Column(db.Date,        nullable=True)
    document_number = db.Column(db.String(100), nullable=True)
    previous_school = db.Column(db.String(200), nullable=True)
    transfer_reason = db.Column(db.Text,        nullable=True)
    admission_notes = db.Column(db.Text,        nullable=True)

    # ── Document checklist ────────────────────────────────────────────────────
    has_birth_cert       = db.Column(db.Boolean, default=False)
    has_id_card          = db.Column(db.Boolean, default=False)
    has_prev_certificate = db.Column(db.Boolean, default=False)
    has_photo            = db.Column(db.Boolean, default=False)
    document_notes       = db.Column(db.Text,    nullable=True)

    # ── Academic history — subject×year grade grid ────────────────────────────
    # New format: {"years": [{class, year, s0_n, s0_t, ..., total_n, total_t,
    #   behavior, result, notes_results, final_result, principal_sig,
    #   col_notes, extra: [{name,n,t}]}]}
    academic_history_json = db.Column(db.Text, nullable=True)

    # ── Extra official-form fields (JSON) ─────────────────────────────────────
    # Stores: record_number, father_name, father_house_num, father_mahalla,
    #   father_occupation, guardian_house_num, guardian_mahalla,
    #   civil_registry_num, birth_place, religion, departure_date,
    #   departure_reason
    extra_fields_json = db.Column(db.Text, nullable=True)

    # ── Notes and signatures ──────────────────────────────────────────────────
    general_notes    = db.Column(db.Text,        nullable=True)
    signature_admin  = db.Column(db.String(200), nullable=True)
    signature_parent = db.Column(db.String(200), nullable=True)

    # ── Audit ─────────────────────────────────────────────────────────────────
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    student = db.relationship('Student', foreign_keys=[student_id],
                              backref=db.backref('registration_record', uselist=False))
    school  = db.relationship('School', foreign_keys=[school_id])
    creator = db.relationship('User',   foreign_keys=[created_by])

    __table_args__ = (
        db.UniqueConstraint('school_id', 'student_id',
                            name='uq_registration_record_school_student'),
    )

    @property
    def academic_history(self):
        import json
        if self.academic_history_json:
            try:
                data = json.loads(self.academic_history_json)
                if isinstance(data, dict):
                    return data
                # Old list format — discard, return empty grid
                return {'years': []}
            except Exception:
                pass
        return {'years': []}

    @academic_history.setter
    def academic_history(self, value):
        import json
        self.academic_history_json = (
            json.dumps(value, ensure_ascii=False) if value is not None else None
        )

    @property
    def extra_fields(self):
        import json
        if self.extra_fields_json:
            try:
                return json.loads(self.extra_fields_json)
            except Exception:
                return {}
        return {}

    @extra_fields.setter
    def extra_fields(self, value):
        import json
        self.extra_fields_json = (
            json.dumps(value, ensure_ascii=False) if value is not None else None
        )

    def __repr__(self):
        return f'<StudentRegistrationRecord {self.id} student={self.student_id}>'


# ═════════════════════════════════════════════════════════════════════════════
#  MOBILE BADGE SYSTEM  — per-user module last-viewed timestamps
# ═════════════════════════════════════════════════════════════════════════════

class MobileModuleView(db.Model):
    """
    Tracks when a mobile user last opened each badge-tracked module.

    One row per (user_id, module).  The badge-count endpoint counts records
    created or meaningfully updated AFTER last_viewed_at.  When no row exists
    for a module, the badge count for that module is 0 (new users are not
    flooded with historical data on first login).

    Module names correspond 1-to-1 with the badge keys returned by
    GET /api/mobile/v1/me/badge-counts:
        grades, homework, exams, attendance, fees, leave_requests, complaints
    """
    __tablename__ = 'mobile_module_views'

    id             = db.Column(db.Integer, primary_key=True)
    user_id        = db.Column(db.Integer,
                               db.ForeignKey('users.id', ondelete='CASCADE'),
                               nullable=False, index=True)
    school_id      = db.Column(db.Integer,
                               db.ForeignKey('schools.id', ondelete='CASCADE'),
                               nullable=False, index=True)
    module         = db.Column(db.String(50), nullable=False)
    last_viewed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user   = db.relationship('User', foreign_keys=[user_id],
                             backref=db.backref('module_views',
                                                cascade='all, delete-orphan',
                                                lazy='dynamic'))
    school = db.relationship('School', foreign_keys=[school_id])

    __table_args__ = (
        db.UniqueConstraint('user_id', 'module', name='uq_mobile_module_view'),
    )

    def __repr__(self):
        return f'<MobileModuleView user={self.user_id} module={self.module}>'


# ═════════════════════════════════════════════════════════════════════════════
#  EXTERNAL STUDENT REGISTRATION  — public intake requests + their documents
# ═════════════════════════════════════════════════════════════════════════════

class StudentRegistrationRequest(db.Model):
    """
    A public (external) pre-registration application submitted by a guardian via
    the school's secure registration link, BEFORE any Student exists.

    School staff review each request; on approval the internal creation logic
    produces the real Student (and documents) and creates/links the guardian's
    parent account. School-scoped (NOT year-scoped) so staff see all of their
    school's requests regardless of the selected view year; academic_year_id
    records the target year resolved server-side at submission time.

    Security: contains NO credentials — the public site never stores a plaintext
    password. tracking_token_hash is sha256 of the per-request tracking token
    (the raw token lives only in the guardian's URL, never in the database).
    """
    __tablename__ = 'student_registration_requests'
    __school_scoped__ = True

    STATUSES = ('pending', 'approved', 'rejected')

    id               = db.Column(db.Integer, primary_key=True)
    school_id        = db.Column(db.Integer, db.ForeignKey('schools.id'),
                                 nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'),
                                 nullable=False, index=True)
    desired_grade_id = db.Column(db.Integer, db.ForeignKey('grades.id'),
                                 nullable=False, index=True)

    # ── Submitted student data (mirrors the public-allowed Add Student fields) ──
    full_name          = db.Column(db.String(200), nullable=False)
    date_of_birth      = db.Column(db.Date,        nullable=True)
    gender             = db.Column(db.String(10),  nullable=True)
    nationality        = db.Column(db.String(80),  nullable=True)
    address            = db.Column(db.Text,        nullable=True)
    phone              = db.Column(db.String(30),  nullable=True)
    notes              = db.Column(db.Text,        nullable=True)
    student_photo_path = db.Column(db.String(255), nullable=True)
    # Optional same-school residential-area selection (reuses the internal Add
    # Student selector). Validated server-side against the school's active areas.
    residential_area_id = db.Column(db.Integer, db.ForeignKey('residential_areas.id'),
                                    nullable=True, index=True)

    # ── Submitted guardian data ────────────────────────────────────────────────
    guardian_name     = db.Column(db.String(200), nullable=True)
    guardian_phone    = db.Column(db.String(30),  nullable=True)
    guardian_email    = db.Column(db.String(180), nullable=True)
    guardian_relation = db.Column(db.String(50),  nullable=True)

    # ── Workflow / review ───────────────────────────────────────────────────────
    status              = db.Column(db.String(20), nullable=False, default='pending',
                                    server_default='pending', index=True)
    tracking_token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    submission_nonce    = db.Column(db.String(64), nullable=True)
    rejection_reason    = db.Column(db.Text, nullable=True)   # parent-facing only
    internal_notes      = db.Column(db.Text, nullable=True)   # staff-only, never shown to parent
    submission_ip       = db.Column(db.String(64), nullable=True)  # abuse audit, never displayed

    reviewed_at            = db.Column(db.DateTime, nullable=True)
    reviewed_by            = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    approved_student_id    = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=True)
    linked_parent_id       = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    parent_account_created = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    school           = db.relationship('School', foreign_keys=[school_id])
    academic_year    = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    desired_grade    = db.relationship('Grade', foreign_keys=[desired_grade_id])
    residential_area = db.relationship('ResidentialArea', foreign_keys=[residential_area_id])
    reviewer         = db.relationship('User', foreign_keys=[reviewed_by])
    approved_student = db.relationship('Student', foreign_keys=[approved_student_id])
    linked_parent    = db.relationship('User', foreign_keys=[linked_parent_id])
    documents        = db.relationship('StudentRegistrationRequestDocument',
                                       backref='request', lazy='dynamic',
                                       cascade='all, delete-orphan')

    __table_args__ = (
        db.CheckConstraint("status IN ('pending','approved','rejected')",
                           name='ck_reg_request_status'),
        db.UniqueConstraint('school_id', 'submission_nonce',
                            name='uq_reg_request_school_nonce'),
        db.Index('ix_reg_request_school_status', 'school_id', 'status'),
    )

    def __repr__(self):
        return (f'<StudentRegistrationRequest {self.id} '
                f'school={self.school_id} status={self.status}>')


class StudentRegistrationRequestDocument(db.Model):
    """
    A document uploaded with a public registration request (before a Student
    exists). Converted into a StudentDocument for the created student on
    approval. school_id is stored so upload_access can resolve ownership and
    enforce school isolation on downloads.
    """
    __tablename__ = 'student_registration_request_documents'
    __school_scoped__ = True

    id            = db.Column(db.Integer, primary_key=True)
    request_id    = db.Column(db.Integer,
                              db.ForeignKey('student_registration_requests.id',
                                            ondelete='CASCADE'),
                              nullable=False, index=True)
    school_id     = db.Column(db.Integer, db.ForeignKey('schools.id'),
                              nullable=False, index=True)
    document_type = db.Column(db.String(100), nullable=False)
    file_path     = db.Column(db.String(255), nullable=False)
    uploaded_at   = db.Column(db.DateTime, default=datetime.utcnow)

    school = db.relationship('School', foreign_keys=[school_id])

    def __repr__(self):
        return (f'<StudentRegistrationRequestDocument {self.id} '
                f'request={self.request_id}>')
