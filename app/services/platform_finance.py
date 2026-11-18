"""Small calculation/validation layer for the Super Admin platform account."""
from sqlalchemy import func

from app.models import (db, Student, AnnualSchoolAgreement, PlatformSchoolInvoice,
                        PlatformInvoiceStudentCoverage, PlatformSchoolPayment,
                        PlatformPaymentAllocation)


ACTIVE_INVOICE_STATUSES = ('unpaid', 'partial', 'paid')


def simple_school_summaries(schools, years_by_school):
    """Return the six dashboard metrics with aggregate queries only.

    Payments currently have no academic_year_id, so ``received`` is explicitly
    the school's all-time active platform receipts.  Callers must label that
    limitation instead of pretending it is a historical-year amount.
    """
    school_ids = [school.id for school in schools]
    if not school_ids:
        return {}

    student_counts = dict(
        db.session.query(Student.school_id, func.count(Student.id))
        .execution_options(bypass_tenant_scope=True, include_all_years=True)
        .filter(Student.school_id.in_(school_ids), Student.status == 'active')
        .group_by(Student.school_id).all()
    )
    received = dict(
        db.session.query(PlatformSchoolPayment.school_id,
                         func.coalesce(func.sum(PlatformSchoolPayment.amount), 0))
        .filter(PlatformSchoolPayment.school_id.in_(school_ids),
                PlatformSchoolPayment.status == 'active')
        .group_by(PlatformSchoolPayment.school_id).all()
    )

    year_ids = [year.id for year in years_by_school.values() if year]
    agreements = {}
    covered_counts = {}
    if year_ids:
        agreements = {
            (row.school_id, row.academic_year_id): row
            for row in AnnualSchoolAgreement.query.filter(
                AnnualSchoolAgreement.school_id.in_(school_ids),
                AnnualSchoolAgreement.academic_year_id.in_(year_ids),
                AnnualSchoolAgreement.status == 'active').all()
        }
        covered_counts = {
            (school_id, year_id): count
            for school_id, year_id, count in (
                db.session.query(PlatformInvoiceStudentCoverage.school_id,
                                 PlatformInvoiceStudentCoverage.academic_year_id,
                                 func.count(PlatformInvoiceStudentCoverage.student_id))
                .join(Student, Student.id == PlatformInvoiceStudentCoverage.student_id)
                .execution_options(bypass_tenant_scope=True, include_all_years=True)
                .filter(PlatformInvoiceStudentCoverage.school_id.in_(school_ids),
                        PlatformInvoiceStudentCoverage.academic_year_id.in_(year_ids),
                        PlatformInvoiceStudentCoverage.status == 'active',
                        Student.status == 'active')
                .group_by(PlatformInvoiceStudentCoverage.school_id,
                          PlatformInvoiceStudentCoverage.academic_year_id).all()
            )
        }

    result = {}
    for school in schools:
        year = years_by_school.get(school.id)
        agreement = agreements.get((school.id, year.id)) if year else None
        is_current = bool(year and year.is_current)
        # The model has no historical enrollment snapshots.  Only a current
        # year may present today's live count as the year's primary count.
        count = int(student_counts.get(school.id, 0)) if is_current else None
        price = int(agreement.annual_price_per_student) if agreement else 0
        expected = count * price if count is not None else None
        actual_received = int(received.get(school.id, 0))
        covered = int(covered_counts.get((school.id, year.id), 0)) if year else 0
        result[school.id] = {
            'agreement': agreement,
            'students': count,
            'price': price,
            'expected': expected,
            'received': actual_received,
            'remaining_expected': (expected - actual_received
                                   if expected is not None else None),
            'uncovered': (max(count - covered, 0) if count is not None else None),
            'received_scope': 'all_time',
            'is_current_year': is_current,
        }
    return result


def eligible_students(school_id):
    return (Student.query.execution_options(bypass_tenant_scope=True, include_all_years=True)
            .filter_by(school_id=school_id, status='active')
            .order_by(Student.full_name).all())


def uncovered_students(school_id, academic_year_id):
    covered = (db.session.query(PlatformInvoiceStudentCoverage.student_id)
               .filter_by(school_id=school_id, academic_year_id=academic_year_id,
                          status='active'))
    return (Student.query.execution_options(bypass_tenant_scope=True, include_all_years=True)
            .filter(Student.school_id == school_id, Student.status == 'active',
                    ~Student.id.in_(covered))
            .order_by(Student.full_name).all())


def invoice_allocated(invoice_id):
    return int(db.session.query(func.coalesce(func.sum(
        PlatformPaymentAllocation.allocated_amount), 0))
        .join(PlatformSchoolPayment)
        .filter(PlatformPaymentAllocation.invoice_id == invoice_id,
                PlatformSchoolPayment.status == 'active').scalar() or 0)


def payment_allocated(payment_id):
    return int(db.session.query(func.coalesce(func.sum(
        PlatformPaymentAllocation.allocated_amount), 0))
        .filter_by(payment_id=payment_id).scalar() or 0)


def refresh_invoice_status(invoice):
    if invoice.status == 'void':
        return
    allocated = invoice_allocated(invoice.id)
    invoice.status = ('unpaid' if allocated == 0 else
                      'paid' if allocated >= invoice.total_amount else 'partial')


def school_totals(school_id, academic_year_id=None):
    iq = PlatformSchoolInvoice.query.filter(
        PlatformSchoolInvoice.school_id == school_id,
        PlatformSchoolInvoice.status != 'void')
    if academic_year_id:
        iq = iq.filter(PlatformSchoolInvoice.academic_year_id == academic_year_id)
    invoices = iq.all()
    invoiced = sum(int(i.total_amount) for i in invoices)
    allocated = sum(invoice_allocated(i.id) for i in invoices)

    payments = PlatformSchoolPayment.query.filter_by(
        school_id=school_id, status='active').all()
    received = sum(int(p.amount) for p in payments)
    all_allocated = sum(payment_allocated(p.id) for p in payments)
    return {
        'invoiced': invoiced,
        'allocated': allocated,
        'received': received,
        'outstanding': invoiced - allocated,
        'unallocated_credit': received - all_allocated,
        'net_balance': invoiced - received,
    }
