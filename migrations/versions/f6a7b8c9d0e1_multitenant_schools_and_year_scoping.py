"""Phase 6: Multi-Tenant Schools + Academic Year Scoping

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-05-01

What this migration does
-------------------------
1. Creates the `schools` table (the new multi-tenant root).
2. Inserts a default school migrated from the existing school_settings row.
3. Adds school_id FK to:  users, employees, academic_years, student_attendance,
   employee_attendance, fee_records, revenues, expenses, salary_records,
   notifications, announcements, devices.
4. Adds academic_year_id FK to: students, student_attendance.
5. Back-fills school_id = 1 on all existing rows.
6. Back-fills academic_year_id = (first academic year) on students and attendance.
"""

from alembic import op
import sqlalchemy as sa
from datetime import datetime

revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. Create schools table ────────────────────────────────────────────
    op.create_table(
        'schools',
        sa.Column('id',              sa.Integer(),     nullable=False),
        sa.Column('school_name',     sa.String(200),   nullable=False),
        sa.Column('school_name_ar',  sa.String(200),   nullable=True),
        sa.Column('code',            sa.String(20),    nullable=True),
        sa.Column('capacity',        sa.Integer(),     nullable=True, server_default='0'),
        sa.Column('logo_path',       sa.String(255),   nullable=True),
        sa.Column('favicon_path',    sa.String(255),   nullable=True),
        sa.Column('primary_color',   sa.String(20),    nullable=True, server_default='#0d6efd'),
        sa.Column('address',         sa.Text(),        nullable=True),
        sa.Column('phone',           sa.String(40),    nullable=True),
        sa.Column('email',           sa.String(180),   nullable=True),
        sa.Column('website',         sa.String(180),   nullable=True),
        sa.Column('currency_code',   sa.String(10),    nullable=True, server_default='IQD'),
        sa.Column('currency_symbol', sa.String(10),    nullable=True, server_default='د.ع'),
        sa.Column('timezone',        sa.String(50),    nullable=True, server_default='Asia/Baghdad'),
        sa.Column('locale',          sa.String(10),    nullable=True, server_default='ar'),
        sa.Column('receipt_footer',  sa.Text(),        nullable=True),
        sa.Column('att_start_time',        sa.Time(), nullable=True),
        sa.Column('att_late_threshold',    sa.Time(), nullable=True),
        sa.Column('att_absence_threshold', sa.Time(), nullable=True),
        sa.Column('is_active',       sa.Boolean(),     nullable=True, server_default='true'),
        sa.Column('created_at',      sa.DateTime(),    nullable=True),
        sa.Column('updated_at',      sa.DateTime(),    nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code', name='uq_school_code'),
    )

    # ── 2. Seed default school from school_settings ────────────────────────
    conn = op.get_bind()

    # Try to read existing school_settings for the first school
    result = conn.execute(sa.text(
        "SELECT school_name, school_name_ar, logo_path, favicon_path, "
        "primary_color, address, phone, email, website, "
        "currency_code, currency_symbol, timezone, locale, receipt_footer, "
        "att_start_time, att_late_threshold, att_absence_threshold "
        "FROM school_settings LIMIT 1"
    ))
    row = result.fetchone()

    if row:
        conn.execute(sa.text(
            "INSERT INTO schools (id, school_name, school_name_ar, logo_path, favicon_path, "
            "primary_color, address, phone, email, website, currency_code, currency_symbol, "
            "timezone, locale, receipt_footer, att_start_time, att_late_threshold, "
            "att_absence_threshold, capacity, is_active, created_at, updated_at) "
            "VALUES (1, :sn, :snar, :logo, :fav, :pc, :addr, :phone, :email, :web, "
            ":cc, :cs, :tz, :loc, :rf, :ast, :alt, :aat, 0, true, :now, :now)"
        ), {
            'sn':   row[0] or 'Mecha-School',
            'snar': row[1],
            'logo': row[2],
            'fav':  row[3],
            'pc':   row[4] or '#0d6efd',
            'addr': row[5],
            'phone': row[6],
            'email': row[7],
            'web':  row[8],
            'cc':   row[9] or 'IQD',
            'cs':   row[10] or 'د.ع',
            'tz':   row[11] or 'Asia/Baghdad',
            'loc':  row[12] or 'ar',
            'rf':   row[13],
            'ast':  row[14],
            'alt':  row[15],
            'aat':  row[16],
            'now':  datetime.utcnow(),
        })
    else:
        conn.execute(sa.text(
            "INSERT INTO schools (id, school_name, capacity, is_active, created_at, updated_at) "
            "VALUES (1, 'Mecha-School', 0, true, :now, :now)"
        ), {'now': datetime.utcnow()})

    # ── 3a. Add school_id to users ─────────────────────────────────────────
    op.add_column('users',
        sa.Column('school_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_users_school_id', 'users', 'schools', ['school_id'], ['id'])
    op.create_index('ix_users_school_id', 'users', ['school_id'])
    # Super-admin (username='admin') stays NULL; all others get school 1
    conn.execute(sa.text(
        "UPDATE users SET school_id = 1 WHERE username != 'admin'"
    ))

    # ── 3b. Add school_id to employees ─────────────────────────────────────
    op.add_column('employees',
        sa.Column('school_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_employees_school_id', 'employees', 'schools', ['school_id'], ['id'])
    op.create_index('ix_employees_school_id', 'employees', ['school_id'])
    conn.execute(sa.text("UPDATE employees SET school_id = 1"))

    # ── 3c. Add school_id to academic_years ────────────────────────────────
    op.add_column('academic_years',
        sa.Column('school_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_academic_years_school_id', 'academic_years', 'schools',
                          ['school_id'], ['id'])
    op.create_index('ix_academic_years_school_id', 'academic_years', ['school_id'])
    conn.execute(sa.text("UPDATE academic_years SET school_id = 1"))

    # ── 3d. Add academic_year_id + school_id to students ───────────────────
    op.add_column('students',
        sa.Column('school_id', sa.Integer(), nullable=True))
    op.add_column('students',
        sa.Column('academic_year_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_students_school_id', 'students', 'schools',
                          ['school_id'], ['id'])
    op.create_foreign_key('fk_students_academic_year_id', 'students', 'academic_years',
                          ['academic_year_id'], ['id'])
    op.create_index('ix_students_school_id', 'students', ['school_id'])
    op.create_index('ix_students_academic_year_id', 'students', ['academic_year_id'])
    conn.execute(sa.text("UPDATE students SET school_id = 1"))
    # Back-fill academic_year_id with the current year for school 1
    conn.execute(sa.text(
        "UPDATE students SET academic_year_id = ("
        "  SELECT id FROM academic_years WHERE school_id = 1 AND is_current = true LIMIT 1"
        ") WHERE academic_year_id IS NULL"
    ))
    # If no current year exists, use any year for school 1
    conn.execute(sa.text(
        "UPDATE students SET academic_year_id = ("
        "  SELECT id FROM academic_years WHERE school_id = 1 LIMIT 1"
        ") WHERE academic_year_id IS NULL"
    ))

    # ── 3e. Add school_id + academic_year_id to student_attendance ─────────
    op.add_column('student_attendance',
        sa.Column('school_id', sa.Integer(), nullable=True))
    op.add_column('student_attendance',
        sa.Column('academic_year_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_sa_school_id', 'student_attendance', 'schools',
                          ['school_id'], ['id'])
    op.create_foreign_key('fk_sa_academic_year_id', 'student_attendance', 'academic_years',
                          ['academic_year_id'], ['id'])
    op.create_index('ix_student_attendance_school_id', 'student_attendance', ['school_id'])
    op.create_index('ix_student_attendance_year_id', 'student_attendance', ['academic_year_id'])
    conn.execute(sa.text("UPDATE student_attendance SET school_id = 1"))
    conn.execute(sa.text(
        "UPDATE student_attendance SET academic_year_id = ("
        "  SELECT id FROM academic_years WHERE school_id = 1 AND is_current = true LIMIT 1"
        ") WHERE academic_year_id IS NULL"
    ))
    conn.execute(sa.text(
        "UPDATE student_attendance SET academic_year_id = ("
        "  SELECT id FROM academic_years WHERE school_id = 1 LIMIT 1"
        ") WHERE academic_year_id IS NULL"
    ))

    # ── 3f. Add school_id to employee_attendance ───────────────────────────
    op.add_column('employee_attendance',
        sa.Column('school_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_ea_school_id', 'employee_attendance', 'schools',
                          ['school_id'], ['id'])
    conn.execute(sa.text("UPDATE employee_attendance SET school_id = 1"))

    # ── 3g. Add school_id to fee_records ───────────────────────────────────
    op.add_column('fee_records',
        sa.Column('school_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_fee_records_school_id', 'fee_records', 'schools',
                          ['school_id'], ['id'])
    op.create_index('ix_fee_records_school_id', 'fee_records', ['school_id'])
    conn.execute(sa.text("UPDATE fee_records SET school_id = 1"))

    # ── 3h. Add school_id to revenues ──────────────────────────────────────
    op.add_column('revenues',
        sa.Column('school_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_revenues_school_id', 'revenues', 'schools',
                          ['school_id'], ['id'])
    op.create_index('ix_revenues_school_id', 'revenues', ['school_id'])
    conn.execute(sa.text("UPDATE revenues SET school_id = 1"))

    # ── 3i. Add school_id to expenses ──────────────────────────────────────
    op.add_column('expenses',
        sa.Column('school_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_expenses_school_id', 'expenses', 'schools',
                          ['school_id'], ['id'])
    op.create_index('ix_expenses_school_id', 'expenses', ['school_id'])
    conn.execute(sa.text("UPDATE expenses SET school_id = 1"))

    # ── 3j. Add school_id to salary_records ────────────────────────────────
    op.add_column('salary_records',
        sa.Column('school_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_salary_records_school_id', 'salary_records', 'schools',
                          ['school_id'], ['id'])
    op.create_index('ix_salary_records_school_id', 'salary_records', ['school_id'])
    conn.execute(sa.text("UPDATE salary_records SET school_id = 1"))

    # ── 3k. Add school_id to notifications ─────────────────────────────────
    op.add_column('notifications',
        sa.Column('school_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_notifications_school_id', 'notifications', 'schools',
                          ['school_id'], ['id'])
    op.create_index('ix_notifications_school_id', 'notifications', ['school_id'])
    conn.execute(sa.text("UPDATE notifications SET school_id = 1"))

    # ── 3l. Add school_id to announcements ─────────────────────────────────
    op.add_column('announcements',
        sa.Column('school_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_announcements_school_id', 'announcements', 'schools',
                          ['school_id'], ['id'])
    op.create_index('ix_announcements_school_id', 'announcements', ['school_id'])
    conn.execute(sa.text("UPDATE announcements SET school_id = 1"))

    # ── 3m. Add school_id to devices ───────────────────────────────────────
    op.add_column('devices',
        sa.Column('school_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_devices_school_id', 'devices', 'schools',
                          ['school_id'], ['id'])
    op.create_index('ix_devices_school_id', 'devices', ['school_id'])
    conn.execute(sa.text("UPDATE devices SET school_id = 1"))


def downgrade():
    # Remove school_id and academic_year_id from all tables (reverse order)
    for table, col in [
        ('devices', 'school_id'),
        ('announcements', 'school_id'),
        ('notifications', 'school_id'),
        ('salary_records', 'school_id'),
        ('expenses', 'school_id'),
        ('revenues', 'school_id'),
        ('fee_records', 'school_id'),
        ('employee_attendance', 'school_id'),
        ('student_attendance', 'academic_year_id'),
        ('student_attendance', 'school_id'),
        ('students', 'academic_year_id'),
        ('students', 'school_id'),
        ('academic_years', 'school_id'),
        ('employees', 'school_id'),
        ('users', 'school_id'),
    ]:
        try:
            op.drop_column(table, col)
        except Exception:
            pass

    op.drop_table('schools')
