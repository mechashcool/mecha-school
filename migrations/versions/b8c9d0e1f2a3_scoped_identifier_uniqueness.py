"""Scope legacy global identifiers by school and year

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-05-01
"""

from alembic import op
import sqlalchemy as sa


revision = 'b8c9d0e1f2a3'
down_revision = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None


def _insp():
    return sa.inspect(op.get_bind())


def _index_names(table):
    return {idx['name'] for idx in _insp().get_indexes(table)}


def _uq_names(table):
    return {uq['name'] for uq in _insp().get_unique_constraints(table)}


def _drop_index_if_exists(name, table):
    if name in _index_names(table):
        op.drop_index(name, table_name=table)


def _drop_uq_if_exists(name, table):
    if name in _uq_names(table):
        op.drop_constraint(name, table, type_='unique')


def _create_index_if_missing(name, table, cols, unique=False, **kwargs):
    if name not in _index_names(table):
        op.create_index(name, table, cols, unique=unique, **kwargs)


def _create_uq_if_missing(name, table, cols):
    if name not in _uq_names(table):
        op.create_unique_constraint(name, table, cols)


def upgrade():
    # Students: student_id is unique per school/year; RFID is unique per school.
    _drop_index_if_exists('ix_students_student_id', 'students')
    _create_index_if_missing('ix_students_student_id', 'students', ['student_id'])
    _drop_index_if_exists('ix_students_rfid_tag_id', 'students')
    _create_index_if_missing(
        'uq_student_school_rfid_tag',
        'students',
        ['school_id', 'rfid_tag_id'],
        unique=True,
        postgresql_where=sa.text('rfid_tag_id IS NOT NULL'),
    )

    # Employees: employee_id is unique only inside a school.
    _drop_index_if_exists('ix_employees_employee_id', 'employees')
    _create_index_if_missing('ix_employees_employee_id', 'employees', ['employee_id'])
    _create_uq_if_missing('uq_employee_school_employee_id', 'employees',
                          ['school_id', 'employee_id'])

    # Subjects: subject code is unique per school/year.
    _drop_uq_if_exists('subjects_code_key', 'subjects')
    _create_uq_if_missing('uq_subject_school_year_code', 'subjects',
                          ['school_id', 'academic_year_id', 'code'])


def downgrade():
    _drop_uq_if_exists('uq_subject_school_year_code', 'subjects')
    _create_uq_if_missing('subjects_code_key', 'subjects', ['code'])

    _drop_uq_if_exists('uq_employee_school_employee_id', 'employees')
    _drop_index_if_exists('ix_employees_employee_id', 'employees')
    _create_index_if_missing('ix_employees_employee_id', 'employees',
                             ['employee_id'], unique=True)

    _drop_index_if_exists('uq_student_school_rfid_tag', 'students')
    _drop_index_if_exists('ix_students_student_id', 'students')
    _create_index_if_missing('ix_students_student_id', 'students',
                             ['student_id'], unique=True)
    _create_index_if_missing('ix_students_rfid_tag_id', 'students',
                             ['rfid_tag_id'], unique=True)
