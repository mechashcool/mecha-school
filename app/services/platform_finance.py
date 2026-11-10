"""Small calculation/validation layer for the Super Admin platform account."""
from sqlalchemy import func

from app.models import (db, Student, PlatformSchoolInvoice,
                        PlatformInvoiceStudentCoverage, PlatformSchoolPayment,
                        PlatformPaymentAllocation)


ACTIVE_INVOICE_STATUSES = ('unpaid', 'partial', 'paid')


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
