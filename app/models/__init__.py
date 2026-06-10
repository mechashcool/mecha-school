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
    fee_reminder_before_value = db.Column(db.Integer,    default=3)
    fee_reminder_before_unit  = db.Column(db.String(10), default='days')  # 'days' | 'hours'

    # Optional per-school feature: building-based data access (multiple
    # branches/buildings inside one school account).  Default OFF so existing
    # schools behave exactly as before.  See SchoolBuilding / UserBuildingAccess.
    enable_buildings = db.Column(db.Boolean, default=False, nullable=False,
                                 server_default=db.false())

    # Optional per-school feature: two-shift attendance (morning/afternoon).
    # Default OFF so existing schools behave exactly as before.
    enable_attendance_shifts = db.Column(db.Boolean, default=False, nullable=False,
                                         server_default=db.false())

    # Last applied feature package (nullable — no package = defaults apply)
    package_id  = db.Column(db.Integer, db.ForeignKey('feature_packages.id', ondelete='SET NULL'),
                            nullable=True)

    is_active   = db.Column(db.Boolean, default=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow)

    # Relationships
    academic_years = db.relationship('AcademicYear', backref='school', lazy='dynamic')
    package        = db.relationship('FeaturePackage', foreign_keys=[package_id])

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
    Auto-absence checks each shift's absent_after_time independently so morning
    students are never marked absent by an afternoon cutoff and vice-versa.
    """
    __tablename__ = 'attendance_shifts'
    __school_scoped__ = True

    id                = db.Column(db.Integer, primary_key=True)
    school_id         = db.Column(db.Integer, db.ForeignKey('schools.id', ondelete='CASCADE'),
                                  nullable=False, index=True)
    name              = db.Column(db.String(100), nullable=False)
    start_time        = db.Column(db.Time, nullable=False)
    late_after_time   = db.Column(db.Time, nullable=False)
    absent_after_time = db.Column(db.Time, nullable=False)
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

    def __repr__(self):
        return f'<Role {self.name}>'


role_permissions = db.Table(
    'role_permissions',
    db.Column('role_id',       db.Integer, db.ForeignKey('roles.id'),       primary_key=True),
    db.Column('permission_id', db.Integer, db.ForeignKey('permissions.id'), primary_key=True),
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
#  4. STUDENTS  (with RFID + school + year)
# ═════════════════════════════════════════════════════════════════════════════

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

    school = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])

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
    parent_id        = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    student_id       = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False, index=True)
    school_id        = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'), nullable=False, index=True)
    leave_type       = db.Column(db.String(30), nullable=False)
    from_date        = db.Column(db.Date, nullable=False)
    to_date          = db.Column(db.Date, nullable=False)
    notes            = db.Column(db.Text, nullable=True)
    attachment_path  = db.Column(db.String(500), nullable=True)
    status           = db.Column(db.String(30), nullable=False, default='pending', index=True)
    manager_note     = db.Column(db.Text, nullable=True)
    reviewed_by      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    reviewed_at      = db.Column(db.DateTime, nullable=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    parent = db.relationship('User', foreign_keys=[parent_id],
                             backref=db.backref('leave_requests', lazy='dynamic'))
    student = db.relationship('Student', foreign_keys=[student_id])
    school = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])
    reviewer = db.relationship('User', foreign_keys=[reviewed_by])

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
    evaluations      = db.relationship('EmployeeEvaluation', backref='employee', lazy='dynamic')
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

    installments  = db.relationship('FeeInstallment', backref='fee_record', lazy='dynamic',
                                    cascade='all, delete-orphan')
    academic_year = db.relationship('AcademicYear', backref='fee_records')
    school        = db.relationship('School', foreign_keys=[school_id],
                                    backref=db.backref('fee_records', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('student_id', 'fee_type_id', 'academic_year_id',
                            name='uq_fee_record_student_type_year'),
    )

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
    Tracks which reminders have already been sent to prevent duplicates.
    Unique on (installment_id, parent_user_id, reminder_value, reminder_unit)
    — one reminder per parent per installment per configured window.
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
    reminder_value   = db.Column(db.Integer,    nullable=False)
    reminder_unit    = db.Column(db.String(10), nullable=False)
    due_date         = db.Column(db.Date,       nullable=False)
    sent_at          = db.Column(db.DateTime,   default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint(
            'installment_id', 'parent_user_id', 'reminder_value', 'reminder_unit',
            name='uq_fee_reminder_log',
        ),
    )


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

    recorder = db.relationship('User', foreign_keys=[recorded_by])
    school   = db.relationship('School', foreign_keys=[school_id],
                               backref=db.backref('revenues', lazy='dynamic'))
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id],
                                    backref=db.backref('revenues', lazy='dynamic'))


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
    allowances  = db.Column(db.Numeric(12, 2), default=0)
    deductions  = db.Column(db.Numeric(12, 2), default=0)
    net_salary  = db.Column(db.Numeric(12, 2), nullable=False)
    paid_date   = db.Column(db.Date, nullable=True)
    status      = db.Column(db.String(20), default='pending')
    payment_method = db.Column(db.String(20), nullable=True)
    notes       = db.Column(db.Text, nullable=True)

    expense_id  = db.Column(db.Integer, db.ForeignKey('expenses.id'), nullable=True)
    created_by  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    creator = db.relationship('User',    foreign_keys=[created_by])
    expense = db.relationship('Expense', foreign_keys=[expense_id])
    school  = db.relationship('School',  foreign_keys=[school_id],
                              backref=db.backref('salary_records', lazy='dynamic'))
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id],
                                    backref=db.backref('salary_records', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('employee_id', 'month', 'year',
                            name='uq_salary_month_year'),
    )


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
    section_id       = db.Column(db.Integer, db.ForeignKey('sections.id'),       nullable=False)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'), nullable=False)
    exam_date        = db.Column(db.Date, nullable=False)
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
    current_quantity = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    minimum_quantity = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    purchase_price = db.Column(db.Numeric(12, 2), nullable=True)
    supplier = db.Column(db.String(200), nullable=True)
    notes = db.Column(db.Text, nullable=True)
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


class InventoryMovement(db.Model):
    __tablename__ = 'inventory_movements'
    __school_scoped__ = True
    __year_scoped__ = True

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False, index=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'), nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('inventory_items.id'), nullable=False, index=True)
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
    title         = db.Column(db.String(200), nullable=False)
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
