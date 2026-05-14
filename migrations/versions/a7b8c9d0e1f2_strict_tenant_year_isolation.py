"""Strict tenant and academic-year isolation

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-05-01
"""

from alembic import op
import sqlalchemy as sa


revision = 'a7b8c9d0e1f2'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None


def _insp():
    return sa.inspect(op.get_bind())


def _has_table(table):
    return _insp().has_table(table)


def _has_column(table, column):
    if not _has_table(table):
        return False
    return column in {c['name'] for c in _insp().get_columns(table)}


def _add_column(table, column):
    if not _has_column(table, column.name):
        op.add_column(table, column)


def _fk_names(table):
    return {fk['name'] for fk in _insp().get_foreign_keys(table)}


def _create_fk(name, table, referred, local_cols, remote_cols, **kwargs):
    if name not in _fk_names(table):
        op.create_foreign_key(name, table, referred, local_cols, remote_cols, **kwargs)


def _index_names(table):
    return {idx['name'] for idx in _insp().get_indexes(table)}


def _create_index(name, table, cols, unique=False, **kwargs):
    if name not in _index_names(table):
        op.create_index(name, table, cols, unique=unique, **kwargs)


def _uq_names(table):
    return {uq['name'] for uq in _insp().get_unique_constraints(table)}


def _create_uq(name, table, cols):
    if name not in _uq_names(table):
        op.create_unique_constraint(name, table, cols)


def _create_uq_or_index(name, table, cols):
    """Create a unique constraint only when existing data is clean.

    Some live installs already have duplicated operational records. We keep
    those rows intact and still add a supporting index so future service-level
    validation can police new writes without destroying history.
    """
    cols_sql = ', '.join(cols)
    duplicate = op.get_bind().execute(sa.text(f"""
        SELECT 1
        FROM (
            SELECT {cols_sql}, COUNT(*) AS n
            FROM {table}
            GROUP BY {cols_sql}
            HAVING COUNT(*) > 1
        ) d
        LIMIT 1
    """)).fetchone()
    if duplicate:
        _create_index(name.replace('uq_', 'ix_') + '_nonunique', table, cols)
    else:
        _create_uq(name, table, cols)


def _ck_names(table):
    return {ck['name'] for ck in _insp().get_check_constraints(table)}


def _create_ck(name, table, condition):
    if name not in _ck_names(table):
        op.create_check_constraint(name, table, condition)


def _alter_not_null(table, column):
    op.alter_column(table, column, existing_type=sa.Integer(), nullable=False)


def upgrade():
    conn = op.get_bind()

    # Make sure every existing school has at least one current year.
    conn.execute(sa.text("""
        INSERT INTO academic_years
            (school_id, name, start_date, end_date, is_current, created_at, updated_at)
        SELECT s.id, '2025-2026', DATE '2025-08-01', DATE '2026-06-30',
               TRUE, NOW(), NOW()
        FROM schools s
        WHERE NOT EXISTS (
            SELECT 1 FROM academic_years ay WHERE ay.school_id = s.id
        )
    """))

    # New direct scope columns.
    _add_column('grades', sa.Column('school_id', sa.Integer(), nullable=True))
    _add_column('sections', sa.Column('school_id', sa.Integer(), nullable=True))
    _add_column('sections', sa.Column('academic_year_id', sa.Integer(), nullable=True))
    _add_column('subjects', sa.Column('school_id', sa.Integer(), nullable=True))
    _add_column('subjects', sa.Column('academic_year_id', sa.Integer(), nullable=True))
    _add_column('student_documents', sa.Column('school_id', sa.Integer(), nullable=True))
    _add_column('student_documents', sa.Column('academic_year_id', sa.Integer(), nullable=True))
    _add_column('employee_documents', sa.Column('school_id', sa.Integer(), nullable=True))
    _add_column('fee_types', sa.Column('school_id', sa.Integer(), nullable=True))
    _add_column('fee_types', sa.Column('academic_year_id', sa.Integer(), nullable=True))
    _add_column('fee_installments', sa.Column('school_id', sa.Integer(), nullable=True))
    _add_column('fee_installments', sa.Column('academic_year_id', sa.Integer(), nullable=True))
    _add_column('revenues', sa.Column('academic_year_id', sa.Integer(), nullable=True))
    _add_column('expenses', sa.Column('academic_year_id', sa.Integer(), nullable=True))
    _add_column('salary_records', sa.Column('academic_year_id', sa.Integer(), nullable=True))
    _add_column('employee_attendance', sa.Column('academic_year_id', sa.Integer(), nullable=True))
    _add_column('exams', sa.Column('school_id', sa.Integer(), nullable=True))
    _add_column('exam_results', sa.Column('school_id', sa.Integer(), nullable=True))
    _add_column('exam_results', sa.Column('academic_year_id', sa.Integer(), nullable=True))
    _add_column('employee_evaluations', sa.Column('school_id', sa.Integer(), nullable=True))
    _add_column('employee_evaluations', sa.Column('academic_year_id', sa.Integer(), nullable=True))
    _add_column('push_notifications', sa.Column('school_id', sa.Integer(), nullable=True))
    _add_column('schedules', sa.Column('school_id', sa.Integer(), nullable=True))
    _add_column('schedules', sa.Column('academic_year_id', sa.Integer(), nullable=True))
    _add_column('audit_logs', sa.Column('school_id', sa.Integer(), nullable=True))

    # Existing nullable scope columns from Phase 6 are now mandatory for tenant data.
    conn.execute(sa.text("""
        UPDATE academic_years
        SET school_id = (SELECT id FROM schools ORDER BY id LIMIT 1)
        WHERE school_id IS NULL
    """))
    conn.execute(sa.text("""
        UPDATE users u
        SET school_id = (SELECT id FROM schools ORDER BY id LIMIT 1)
        WHERE u.school_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM roles r WHERE r.id = u.role_id AND r.is_admin = TRUE
          )
    """))
    for table in (
        'employees', 'students', 'fee_records', 'revenues', 'expenses',
        'salary_records', 'student_attendance', 'employee_attendance',
        'devices', 'notifications', 'announcements'
    ):
        conn.execute(sa.text(f"""
            UPDATE {table}
            SET school_id = (SELECT id FROM schools ORDER BY id LIMIT 1)
            WHERE school_id IS NULL
        """))

    # Backfill school/year from strong parent records.
    conn.execute(sa.text("""
        UPDATE grades g
        SET school_id = ay.school_id
        FROM academic_years ay
        WHERE g.academic_year_id = ay.id AND g.school_id IS NULL
    """))
    conn.execute(sa.text("""
        UPDATE sections s
        SET school_id = g.school_id,
            academic_year_id = g.academic_year_id
        FROM grades g
        WHERE s.grade_id = g.id
          AND (s.school_id IS NULL OR s.academic_year_id IS NULL)
    """))
    conn.execute(sa.text("""
        UPDATE students st
        SET academic_year_id = COALESCE(
            st.academic_year_id,
            (SELECT ay.id FROM academic_years ay
             WHERE ay.school_id = st.school_id AND ay.is_current = TRUE
             ORDER BY ay.start_date DESC LIMIT 1),
            (SELECT ay.id FROM academic_years ay
             WHERE ay.school_id = st.school_id
             ORDER BY ay.start_date DESC LIMIT 1)
        )
        WHERE st.academic_year_id IS NULL
    """))
    conn.execute(sa.text("""
        UPDATE subjects sub
        SET school_id = COALESCE(sub.school_id, ds.id),
            academic_year_id = COALESCE(sub.academic_year_id, ay.id)
        FROM (SELECT id FROM schools ORDER BY id LIMIT 1) ds
        LEFT JOIN LATERAL (
            SELECT id FROM academic_years
            WHERE school_id = ds.id
            ORDER BY is_current DESC, start_date DESC
            LIMIT 1
        ) ay ON TRUE
        WHERE sub.school_id IS NULL OR sub.academic_year_id IS NULL
    """))
    conn.execute(sa.text("""
        UPDATE student_documents d
        SET school_id = s.school_id,
            academic_year_id = s.academic_year_id
        FROM students s
        WHERE d.student_id = s.id
          AND (d.school_id IS NULL OR d.academic_year_id IS NULL)
    """))
    conn.execute(sa.text("""
        UPDATE employee_documents d
        SET school_id = e.school_id
        FROM employees e
        WHERE d.employee_id = e.id AND d.school_id IS NULL
    """))
    conn.execute(sa.text("""
        UPDATE fee_types ft
        SET school_id = COALESCE(ft.school_id, ds.id),
            academic_year_id = COALESCE(ft.academic_year_id, ay.id)
        FROM (SELECT id FROM schools ORDER BY id LIMIT 1) ds
        LEFT JOIN LATERAL (
            SELECT id FROM academic_years
            WHERE school_id = ds.id
            ORDER BY is_current DESC, start_date DESC
            LIMIT 1
        ) ay ON TRUE
        WHERE ft.school_id IS NULL OR ft.academic_year_id IS NULL
    """))
    conn.execute(sa.text("""
        UPDATE fee_installments i
        SET school_id = r.school_id,
            academic_year_id = r.academic_year_id
        FROM fee_records r
        WHERE i.fee_record_id = r.id
          AND (i.school_id IS NULL OR i.academic_year_id IS NULL)
    """))
    for table in ('revenues', 'expenses'):
        conn.execute(sa.text(f"""
            UPDATE {table} r
            SET academic_year_id = COALESCE(
                (SELECT ay.id FROM academic_years ay
                 WHERE ay.school_id = r.school_id
                   AND r.date BETWEEN ay.start_date AND ay.end_date
                 ORDER BY ay.is_current DESC, ay.start_date DESC LIMIT 1),
                (SELECT ay.id FROM academic_years ay
                 WHERE ay.school_id = r.school_id
                 ORDER BY ay.is_current DESC, ay.start_date DESC LIMIT 1)
            )
            WHERE r.academic_year_id IS NULL
        """))
    conn.execute(sa.text("""
        UPDATE salary_records sr
        SET academic_year_id = COALESCE(
            (SELECT ay.id FROM academic_years ay
             WHERE ay.school_id = sr.school_id
               AND make_date(sr.year, sr.month, 1) BETWEEN ay.start_date AND ay.end_date
             ORDER BY ay.is_current DESC, ay.start_date DESC LIMIT 1),
            (SELECT ay.id FROM academic_years ay
             WHERE ay.school_id = sr.school_id
             ORDER BY ay.is_current DESC, ay.start_date DESC LIMIT 1)
        )
        WHERE sr.academic_year_id IS NULL
    """))
    conn.execute(sa.text("""
        UPDATE student_attendance sa
        SET academic_year_id = COALESCE(
            sa.academic_year_id,
            s.academic_year_id,
            (SELECT ay.id FROM academic_years ay
             WHERE ay.school_id = sa.school_id
               AND sa.date BETWEEN ay.start_date AND ay.end_date
             ORDER BY ay.is_current DESC, ay.start_date DESC LIMIT 1)
        )
        FROM students s
        WHERE sa.student_id = s.id AND sa.academic_year_id IS NULL
    """))
    conn.execute(sa.text("""
        UPDATE employee_attendance ea
        SET academic_year_id = COALESCE(
            (SELECT ay.id FROM academic_years ay
             WHERE ay.school_id = ea.school_id
               AND ea.date BETWEEN ay.start_date AND ay.end_date
             ORDER BY ay.is_current DESC, ay.start_date DESC LIMIT 1),
            (SELECT ay.id FROM academic_years ay
             WHERE ay.school_id = ea.school_id
             ORDER BY ay.is_current DESC, ay.start_date DESC LIMIT 1)
        )
        WHERE ea.academic_year_id IS NULL
    """))
    conn.execute(sa.text("""
        UPDATE exams e
        SET school_id = ay.school_id
        FROM academic_years ay
        WHERE e.academic_year_id = ay.id AND e.school_id IS NULL
    """))
    conn.execute(sa.text("""
        UPDATE exam_results r
        SET school_id = e.school_id,
            academic_year_id = e.academic_year_id
        FROM exams e
        WHERE r.exam_id = e.id
          AND (r.school_id IS NULL OR r.academic_year_id IS NULL)
    """))
    conn.execute(sa.text("""
        UPDATE employee_evaluations ev
        SET school_id = emp.school_id,
            academic_year_id = COALESCE(
                (SELECT ay.id FROM academic_years ay
                 WHERE ay.school_id = emp.school_id AND ay.is_current = TRUE
                 ORDER BY ay.start_date DESC LIMIT 1),
                (SELECT ay.id FROM academic_years ay
                 WHERE ay.school_id = emp.school_id
                 ORDER BY ay.start_date DESC LIMIT 1)
            )
        FROM employees emp
        WHERE ev.employee_id = emp.id
          AND (ev.school_id IS NULL OR ev.academic_year_id IS NULL)
    """))
    conn.execute(sa.text("""
        UPDATE push_notifications pn
        SET school_id = COALESCE(u.school_id, (SELECT id FROM schools ORDER BY id LIMIT 1))
        FROM users u
        WHERE pn.user_id = u.id AND pn.school_id IS NULL
    """))
    conn.execute(sa.text("""
        UPDATE schedules sc
        SET school_id = sec.school_id,
            academic_year_id = sec.academic_year_id
        FROM sections sec
        WHERE sc.section_id = sec.id
          AND (sc.school_id IS NULL OR sc.academic_year_id IS NULL)
    """))
    conn.execute(sa.text("""
        UPDATE audit_logs al
        SET school_id = u.school_id
        FROM users u
        WHERE al.user_id = u.id AND al.school_id IS NULL
    """))

    # Foreign keys and indexes.
    scoped_fks = [
        ('grades', 'school_id', 'fk_grades_school_id', 'schools'),
        ('sections', 'school_id', 'fk_sections_school_id', 'schools'),
        ('sections', 'academic_year_id', 'fk_sections_academic_year_id', 'academic_years'),
        ('subjects', 'school_id', 'fk_subjects_school_id', 'schools'),
        ('subjects', 'academic_year_id', 'fk_subjects_academic_year_id', 'academic_years'),
        ('student_documents', 'school_id', 'fk_student_documents_school_id', 'schools'),
        ('student_documents', 'academic_year_id', 'fk_student_documents_academic_year_id', 'academic_years'),
        ('employee_documents', 'school_id', 'fk_employee_documents_school_id', 'schools'),
        ('fee_types', 'school_id', 'fk_fee_types_school_id', 'schools'),
        ('fee_types', 'academic_year_id', 'fk_fee_types_academic_year_id', 'academic_years'),
        ('fee_installments', 'school_id', 'fk_fee_installments_school_id', 'schools'),
        ('fee_installments', 'academic_year_id', 'fk_fee_installments_academic_year_id', 'academic_years'),
        ('revenues', 'academic_year_id', 'fk_revenues_academic_year_id', 'academic_years'),
        ('expenses', 'academic_year_id', 'fk_expenses_academic_year_id', 'academic_years'),
        ('salary_records', 'academic_year_id', 'fk_salary_records_academic_year_id', 'academic_years'),
        ('employee_attendance', 'academic_year_id', 'fk_employee_attendance_academic_year_id', 'academic_years'),
        ('exams', 'school_id', 'fk_exams_school_id', 'schools'),
        ('exam_results', 'school_id', 'fk_exam_results_school_id', 'schools'),
        ('exam_results', 'academic_year_id', 'fk_exam_results_academic_year_id', 'academic_years'),
        ('employee_evaluations', 'school_id', 'fk_employee_evaluations_school_id', 'schools'),
        ('employee_evaluations', 'academic_year_id', 'fk_employee_evaluations_academic_year_id', 'academic_years'),
        ('push_notifications', 'school_id', 'fk_push_notifications_school_id', 'schools'),
        ('schedules', 'school_id', 'fk_schedules_school_id', 'schools'),
        ('schedules', 'academic_year_id', 'fk_schedules_academic_year_id', 'academic_years'),
        ('audit_logs', 'school_id', 'fk_audit_logs_school_id', 'schools'),
    ]
    for table, col, name, referred in scoped_fks:
        _create_fk(name, table, referred, [col], ['id'])
        _create_index(f'ix_{table}_{col}', table, [col])

    # Tighten required scope columns.
    for table, col in [
        ('academic_years', 'school_id'),
        ('employees', 'school_id'),
        ('grades', 'school_id'),
        ('sections', 'school_id'),
        ('sections', 'academic_year_id'),
        ('subjects', 'school_id'),
        ('subjects', 'academic_year_id'),
        ('students', 'school_id'),
        ('students', 'academic_year_id'),
        ('student_documents', 'school_id'),
        ('student_documents', 'academic_year_id'),
        ('employee_documents', 'school_id'),
        ('fee_types', 'school_id'),
        ('fee_types', 'academic_year_id'),
        ('fee_records', 'school_id'),
        ('fee_installments', 'school_id'),
        ('fee_installments', 'academic_year_id'),
        ('revenues', 'school_id'),
        ('revenues', 'academic_year_id'),
        ('expenses', 'school_id'),
        ('expenses', 'academic_year_id'),
        ('salary_records', 'school_id'),
        ('salary_records', 'academic_year_id'),
        ('student_attendance', 'school_id'),
        ('student_attendance', 'academic_year_id'),
        ('employee_attendance', 'school_id'),
        ('employee_attendance', 'academic_year_id'),
        ('devices', 'school_id'),
        ('exams', 'school_id'),
        ('exam_results', 'school_id'),
        ('exam_results', 'academic_year_id'),
        ('employee_evaluations', 'school_id'),
        ('employee_evaluations', 'academic_year_id'),
        ('notifications', 'school_id'),
        ('announcements', 'school_id'),
        ('push_notifications', 'school_id'),
        ('schedules', 'school_id'),
        ('schedules', 'academic_year_id'),
    ]:
        _alter_not_null(table, col)

    # Uniqueness and data-shape safeguards.
    _create_uq_or_index('uq_academic_year_school_name', 'academic_years', ['school_id', 'name'])
    _create_index(
        'uq_academic_year_current_per_school',
        'academic_years',
        ['school_id'],
        unique=True,
        postgresql_where=sa.text('is_current = TRUE'),
    )
    _create_uq_or_index('uq_grade_school_year_name', 'grades',
                        ['school_id', 'academic_year_id', 'name'])
    _create_uq_or_index('uq_section_school_year_grade_name', 'sections',
                        ['school_id', 'academic_year_id', 'grade_id', 'name'])
    _create_uq_or_index('uq_student_school_year_student_id', 'students',
                        ['school_id', 'academic_year_id', 'student_id'])
    _create_uq_or_index('uq_fee_type_school_year_name', 'fee_types',
                        ['school_id', 'academic_year_id', 'name'])
    _create_uq_or_index('uq_fee_record_student_type_year', 'fee_records',
                        ['student_id', 'fee_type_id', 'academic_year_id'])
    _create_uq_or_index('uq_schedule_section_subject_day_start', 'schedules',
                        ['section_id', 'subject_id', 'day_of_week', 'start_time'])

    _create_ck('ck_school_capacity_nonnegative', 'schools', 'capacity >= 0')
    _create_ck('ck_academic_year_dates', 'academic_years', 'start_date <= end_date')
    _create_ck('ck_section_capacity_positive', 'sections', 'capacity > 0')


def downgrade():
    # Keep downgrade conservative: remove constraints and direct scope columns
    # added by this revision. Data itself is left intact.
    for table, name in [
        ('schools', 'ck_school_capacity_nonnegative'),
        ('academic_years', 'ck_academic_year_dates'),
        ('sections', 'ck_section_capacity_positive'),
    ]:
        try:
            op.drop_constraint(name, table, type_='check')
        except Exception:
            pass

    for table, name in [
        ('schedules', 'uq_schedule_section_subject_day_start'),
        ('fee_records', 'uq_fee_record_student_type_year'),
        ('fee_types', 'uq_fee_type_school_year_name'),
        ('students', 'uq_student_school_year_student_id'),
        ('sections', 'uq_section_school_year_grade_name'),
        ('grades', 'uq_grade_school_year_name'),
        ('academic_years', 'uq_academic_year_school_name'),
    ]:
        try:
            op.drop_constraint(name, table, type_='unique')
        except Exception:
            pass

    try:
        op.drop_index('uq_academic_year_current_per_school', table_name='academic_years')
    except Exception:
        pass

    for table, col in [
        ('audit_logs', 'school_id'),
        ('schedules', 'academic_year_id'),
        ('schedules', 'school_id'),
        ('push_notifications', 'school_id'),
        ('employee_evaluations', 'academic_year_id'),
        ('employee_evaluations', 'school_id'),
        ('exam_results', 'academic_year_id'),
        ('exam_results', 'school_id'),
        ('exams', 'school_id'),
        ('employee_attendance', 'academic_year_id'),
        ('salary_records', 'academic_year_id'),
        ('expenses', 'academic_year_id'),
        ('revenues', 'academic_year_id'),
        ('fee_installments', 'academic_year_id'),
        ('fee_installments', 'school_id'),
        ('fee_types', 'academic_year_id'),
        ('fee_types', 'school_id'),
        ('employee_documents', 'school_id'),
        ('student_documents', 'academic_year_id'),
        ('student_documents', 'school_id'),
        ('subjects', 'academic_year_id'),
        ('subjects', 'school_id'),
        ('sections', 'academic_year_id'),
        ('sections', 'school_id'),
        ('grades', 'school_id'),
    ]:
        if _has_column(table, col):
            try:
                op.drop_column(table, col)
            except Exception:
                pass
