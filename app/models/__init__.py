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

    is_active   = db.Column(db.Boolean, default=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow)

    # Relationships
    academic_years = db.relationship('AcademicYear', backref='school', lazy='dynamic')

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
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    sections = db.relationship('Section', backref='grade', lazy='dynamic')
    school   = db.relationship('School', foreign_keys=[school_id],
                               backref=db.backref('grades', lazy='dynamic'))

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
    code        = db.Column(db.String(20),  nullable=False)
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
    student_id    = db.Column(db.String(20), nullable=False, index=True)
    full_name     = db.Column(db.String(200), nullable=False)
    date_of_birth = db.Column(db.Date, nullable=False)
    gender        = db.Column(db.String(10), nullable=False)
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

class Employee(db.Model):
    __tablename__ = 'employees'
    __school_scoped__ = True

    id            = db.Column(db.Integer, primary_key=True)
    employee_id   = db.Column(db.String(20), nullable=False, index=True)
    full_name     = db.Column(db.String(200), nullable=False)
    job_title     = db.Column(db.String(150), nullable=False)
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
    attendances      = db.relationship('EmployeeAttendance', backref='employee', lazy='dynamic')

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
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    recorder      = db.relationship('User',   foreign_keys=[recorded_by])
    device        = db.relationship('Device', foreign_keys=[device_id])
    school        = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])

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
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    school = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])

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
    section_id  = db.Column(db.Integer, db.ForeignKey('sections.id'), nullable=False)
    subject_id  = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=False)
    teacher_id  = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=True)
    day_of_week = db.Column(db.Integer, nullable=False)
    start_time  = db.Column(db.Time, nullable=False)
    end_time    = db.Column(db.Time, nullable=False)
    room        = db.Column(db.String(50), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    section = db.relationship('Section', backref='schedules')
    subject = db.relationship('Subject', backref='schedules')
    teacher = db.relationship('Employee', backref='schedules', foreign_keys=[teacher_id])
    school = db.relationship('School', foreign_keys=[school_id])
    academic_year = db.relationship('AcademicYear', foreign_keys=[academic_year_id])

    __table_args__ = (
        db.UniqueConstraint('section_id', 'subject_id', 'day_of_week', 'start_time',
                            name='uq_schedule_section_subject_day_start'),
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
