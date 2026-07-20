"""Mecha-School – Fees Blueprint"""
import logging
import re
from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, session as flask_session, abort
from flask_login import login_required, current_user
from datetime import date, datetime as dt
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.models import (db, FeeRecord, FeeInstallment, FeeType, Student, AcademicYear, SchoolSettings, Revenue, RevenueCategory, Section, Grade, School)
from app.utils.decorators import (permission_required, get_current_school,
                                   get_active_year, get_view_year, historical_guard,
                                   admin_required)
from app.utils.helpers import generate_receipt_no
from app.utils.audit import log_action
from app.utils.buildings import (
    apply_building_scope_to_students, apply_building_scope_to_fees,
    user_can_access_student,
)

fees_bp = Blueprint('fees', __name__, template_folder='../../templates/fees')
_log = logging.getLogger('mecha.fees')


def _overdue_installments_query(school, *, search='', fee_type_filter='all',
                                installment_filter='all'):
    """Query yielding ONE row per *overdue* FeeInstallment.

    Overdue = due_date strictly before today AND an outstanding balance remains
    (amount - received_amount > 0). This covers both completely unpaid and
    partially paid installments, and excludes fully paid ones. An installment
    due today is NOT overdue.

    The installment is joined to its FeeRecord and Student so the shared
    fee-type / student-search / installment-number filters and the building
    scope apply identically to the record-level list. The same student appears
    once per overdue installment (no grouping/merging).

    School and academic-year isolation are enforced automatically by the central
    ORM tenant-scope event: FeeInstallment and FeeRecord are school+year scoped
    and Student is school scoped, so every joined table — including the eager-
    loaded aliases — is constrained to the current school and view year. Building
    scope is applied explicitly through the Student join.
    """
    q = (
        FeeInstallment.query
        .join(FeeRecord, FeeInstallment.fee_record_id == FeeRecord.id)
        .join(Student, FeeRecord.student_id == Student.id)
        .options(
            joinedload(FeeInstallment.fee_record).joinedload(FeeRecord.student),
            joinedload(FeeInstallment.fee_record).joinedload(FeeRecord.fee_type),
            joinedload(FeeInstallment.fee_record).joinedload(FeeRecord.academic_year),
        )
        .filter(FeeInstallment.due_date < date.today())
        .filter(
            (FeeInstallment.amount - func.coalesce(FeeInstallment.received_amount, 0)) > 0
        )
    )

    if school:
        q = q.filter(FeeRecord.school_id == school.id)

    # Building scope — restricted users only see overdue installments of their
    # buildings' students (filters on the joined Student.building_id).
    q = apply_building_scope_to_fees(q, current_user, school)

    if search:
        q = q.filter(Student.full_name.ilike(f'%{search}%') |
                     Student.student_id.ilike(f'%{search}%'))

    if fee_type_filter != 'all':
        q = q.filter(FeeRecord.fee_type_id == int(fee_type_filter))

    if installment_filter != 'all':
        q = q.filter(FeeInstallment.installment_no == int(installment_filter))

    return q.order_by(
        Student.full_name.asc(),
        FeeInstallment.due_date.asc(),
        FeeInstallment.installment_no.asc(),
    )


def _notify_fee_parents(student_id, title, body, screen, *, fee_record_id=None,
                        installment_id=None):
    """FCM push to a student's linked parents after a fee/payment change.

    Called only after the DB transaction has committed. Parents are resolved
    server-side from the student→parent link (school isolation inherited); no
    client-supplied identifier is trusted. The body carries no amounts beyond
    what the caller passes. Never raises — push must not fail a payment.
    """
    if not student_id:
        return
    data = {
        'type':   'fee',
        'screen': screen,            # 'fees'
        'route':  '/parent/fees',
        'student_id': str(student_id),
    }
    if fee_record_id is not None:
        data['fee_record_id'] = str(fee_record_id)
    if installment_id is not None:
        data['installment_id'] = str(installment_id)
    try:
        from app.services.notifications import NotificationService
        NotificationService.send_to_parents_of_student(
            student_id, title, body, ntype='fee', data=data)
    except Exception:
        _log.exception('[fees] FCM push failed for student_id=%s', student_id)


def _active_fee_student_query(school, year):
    query = (
        Student.query
        .options(joinedload(Student.section).joinedload(Section.grade))
        .filter_by(status='active')
    )
    if school:
        query = query.filter(Student.school_id == school.id)
    if year:
        query = query.filter(Student.academic_year_id == year.id)
    # Building scope — restricted users only act on their buildings' students.
    query = apply_building_scope_to_students(query, current_user, school)
    return query


def _student_payload(student):
    section = student.section
    grade = section.grade if section else None
    return {
        'id': student.id,
        'student_id': student.student_id,
        'full_name': student.full_name,
        'grade': grade.name if grade else '',
        'section': section.name if section else '',
    }


class FeeValidationError(ValueError):
    """Raised when fee form data fails validation.

    Carries a user-safe Arabic message suitable for flashing back to the form.
    """


def compute_fee_amounts(form):
    """Validate and compute (total_amount, discount, net_amount) from form data.

    Single source of truth for the fee discount rules, shared by the standalone
    `fees.create` route and the student-registration wizard so the calculation
    is never duplicated. Performs no database access and creates no records.
    Raises :class:`FeeValidationError` with an Arabic message on invalid input.
    """
    try:
        total_amount = Decimal(form.get('total_amount', '0') or '0')
    except InvalidOperation:
        raise FeeValidationError('المبلغ الإجمالي غير صالح.')

    discount_type = form.get('discount_type', 'fixed')
    try:
        discount_value = Decimal(form.get('discount_value', '0') or '0')
    except InvalidOperation:
        raise FeeValidationError('قيمة الخصم غير صالحة.')

    if discount_type == 'percentage':
        if discount_value < 0 or discount_value > 100:
            raise FeeValidationError('نسبة الخصم يجب أن تكون بين 0 و100.')
        discount = (total_amount * discount_value / Decimal('100')).quantize(Decimal('0.01'))
    else:
        discount = discount_value
        if discount < 0:
            raise FeeValidationError('الخصم لا يمكن أن يكون سالباً.')
        if discount > total_amount:
            raise FeeValidationError('الخصم لا يمكن أن يتجاوز المبلغ الإجمالي.')

    net_amount = total_amount - discount
    if net_amount < 0:
        raise FeeValidationError('الصافي لا يمكن أن يكون سالباً.')

    return total_amount, discount, net_amount


def persist_fee_record(form, *, school, student_id, fee_type_id, academic_year_id,
                       total_amount, discount, notes=None):
    """Create a FeeRecord and its FeeInstallments from already-computed amounts.

    Shared by `fees.create` and the student-registration wizard. Adds objects to
    the current ``db.session`` and flushes; the CALLER is responsible for the
    final commit and for all authorization (``manage_fees``) and ownership /
    duplicate checks. Installment splitting matches the standalone fee route
    exactly. ``notes`` lets a caller (the wizard) supply a non-colliding field
    name; when ``None`` the standard ``notes`` form field is used.
    """
    record = FeeRecord(
        student_id       = student_id,
        fee_type_id      = fee_type_id,
        academic_year_id = academic_year_id,
        school_id        = school.id if school else None,
        total_amount     = float(total_amount),
        discount         = float(discount),   # always stored as final monetary amount
        notes            = (notes if notes is not None else form.get('notes', '')).strip(),
    )
    db.session.add(record)
    db.session.flush()

    # Build installments from the server-computed net amount.
    num_inst = max(1, min(12, int(form.get('num_installments', 1) or 1)))
    net      = record.net_amount          # uses model property: total_amount - discount
    each     = round(net / num_inst, 2)
    for i in range(1, num_inst + 1):
        due_str = form.get(f'due_date_{i}')
        due     = dt.strptime(due_str, '%Y-%m-%d').date() if due_str else date.today()
        inst = FeeInstallment(
            fee_record_id    = record.id,
            school_id        = record.school_id,
            academic_year_id = record.academic_year_id,
            installment_no   = i,
            amount           = each,
            due_date         = due,
        )
        db.session.add(inst)
    return record


def apply_installment_payment(inst, received, *, payment_method='cash',
                              paid_date=None, notes=None, collected_by=None):
    """Cap and apply a payment amount to ONE installment's fields.

    Single source of truth for "never record more than the installment's
    outstanding balance" and the resulting field/status/receipt updates.
    Shared by the standalone `fees.pay_installment` route and the student-
    registration wizard so a payment recorded from either flow is capped and
    applied identically and can never diverge between the two. Performs no
    DB-session work beyond mutating `inst`'s own attributes: it does not
    create the matching Revenue record, does not acquire any row lock, and
    does not commit — the caller owns locking, the Revenue entry, and the
    transaction boundary appropriate to its own flow.

    Returns (applied, remaining_before):
      applied          — Decimal amount actually applied to `inst`, i.e.
                          `received` capped at the installment's remaining
                          balance (0 if nothing was applied).
      remaining_before — Decimal outstanding balance before this call, so the
                          caller can tell "already fully paid" (<= 0) apart
                          from an ordinary partial/full payment.
    """
    remaining = Decimal(str(inst.amount)) - Decimal(str(inst.received_amount or 0))
    if remaining <= 0 or received <= 0:
        return Decimal('0'), remaining
    if received > remaining:
        received = remaining

    inst.received_amount = Decimal(str(inst.received_amount or 0)) + received
    inst.payment_method  = payment_method or 'cash'
    if collected_by is not None:
        inst.collected_by = collected_by
    if notes:
        inst.notes = notes
    inst.paid_date = paid_date or date.today()
    inst.recompute_status()
    if Decimal(str(inst.received_amount or 0)) > 0 and not inst.receipt_no:
        inst.receipt_no = generate_receipt_no()
    return received, remaining


def distribute_fee_payment(installments, start_installment_no, received, *,
                           payment_method='cash', paid_date=None, notes=None,
                           collected_by=None):
    """Distribute ONE entered payment across an installment's outstanding
    balance and, if it exceeds that, cascade the excess forward into the
    following unpaid/partially-paid installments of the SAME fee record, in
    ascending installment_no order — never altering any installment's
    original scheduled amount or due date.

    Single source of truth for "how much of an entered payment lands on
    which installment", shared by every payment entry point in the system:
    the standalone Fees and Installments page and the student profile page
    (both via `fees.pay_installment`), and the student-registration
    wizard's Fees step. Performs no DB queries, creates no Revenue rows, and
    never commits — the caller owns fetching/locking `installments`, the
    matching Revenue entries (one per installment actually touched, so
    existing per-installment receipt/accounting behavior is unchanged), and
    the transaction boundary appropriate to its own flow.

    `installments` must be the pre-fetched (and, where the caller needs
    concurrency safety, already row-locked) FeeInstallment rows of ONE fee
    record; only those with `installment_no >= start_installment_no`
    participate, in ascending order — payments never cascade backward into
    earlier installments.

    Raises FeeValidationError — with a message naming the maximum payable
    amount, or stating the installment is already fully paid — if `received`
    cannot be applied as requested. Nothing is mutated in that case, so the
    caller can reject the payment before saving anything.

    Returns a list of {'installment': FeeInstallment, 'applied': Decimal}
    for every installment actually touched, in the order applied.
    """
    # The selected installment itself must still have an outstanding balance —
    # checked explicitly (and separately from the cascade below) so a stale or
    # tampered request naming an already-settled installment is rejected with
    # a message about THAT installment, instead of silently starting the
    # payment on a different, later installment the caller never selected.
    start_inst = next((i for i in installments if i.installment_no == start_installment_no), None)
    if start_inst is None:
        raise FeeValidationError('القسط المحدد غير موجود.')
    start_remaining = Decimal(str(start_inst.amount)) - Decimal(str(start_inst.received_amount or 0))
    if start_remaining <= 0:
        raise FeeValidationError('هذا القسط مسدّد بالكامل بالفعل.')

    eligible = sorted(
        (inst for inst in installments if inst.installment_no >= start_installment_no),
        key=lambda i: i.installment_no,
    )
    outstanding = [
        (inst, Decimal(str(inst.amount)) - Decimal(str(inst.received_amount or 0)))
        for inst in eligible
    ]
    outstanding = [(inst, rem) for inst, rem in outstanding if rem > 0]

    total_outstanding = sum((rem for _, rem in outstanding), Decimal('0'))
    if received > total_outstanding:
        raise FeeValidationError(
            'المبلغ المدخل يتجاوز إجمالي المتبقي على هذا القسط والأقساط التالية له. '
            f'الحد الأقصى الذي يمكن دفعه حالياً هو {total_outstanding}.'
        )

    allocations = []
    leftover = received
    for inst, remaining_balance in outstanding:
        if leftover <= 0:
            break
        portion = min(remaining_balance, leftover)
        applied, _ = apply_installment_payment(
            inst, portion,
            payment_method=payment_method, paid_date=paid_date,
            notes=notes, collected_by=collected_by,
        )
        if applied > 0:
            allocations.append({'installment': inst, 'applied': applied})
            leftover -= applied
    return allocations


# Tag written into every Revenue.description of ONE accepted payment operation.
# `op_ref` is a fresh, globally-unique receipt number (same generator as the
# per-installment receipt_no), so all Revenue rows created for a single payment
# — even when the amount cascades across several installments — carry the SAME
# tag and can be summed back to the exact entered amount with no heuristics.
# Historical rows written before this tag existed simply lack it and fall back
# to per-installment behavior; they are never guessed at or merged.
_PAYMENT_TXN_RE = re.compile(r'\[TXN:([^\]]+)\]')


def _payment_txn_tag(op_ref):
    return f' [TXN:{op_ref}]'


# Shared Arabic status labels for payment notifications / receipts.
_PAY_STATUS_LABELS = {'paid': 'مكتمل', 'partial': 'دفعة جزئية',
                      'pending': 'قيد الانتظار', 'overdue': 'متأخر'}


def stage_installment_payment(installments, start_installment_no, received, *,
                              fee_category, student_name, payment_method='cash',
                              paid_date=None, notes=None, collected_by=None,
                              rev_academic_year_id=None):
    """Canonical payment recorder — the ONE pathway used by every entry point.

    Distributes ONE entered payment across installments (via the canonical
    distribute_fee_payment / apply_installment_payment) AND stages the matching
    Revenue rows in the current db.session, tagging every Revenue row of this
    single payment with one fresh, unique operation reference. That tag makes
    the payment one identifiable financial transaction even when its amount is
    spread over several installments, so a receipt can later reproduce the exact
    entered total with no timestamp/heuristic guessing.

    Adds objects to db.session but NEVER commits and acquires no lock — the
    CALLER owns the transaction boundary and (where needed) the row lock. This
    is what lets the standalone Fees route commit immediately while the
    student-registration wizard folds the same call into its single atomic
    student-creation commit. Raises FeeValidationError (from
    distribute_fee_payment) if the amount exceeds what is currently outstanding;
    nothing is staged in that case.

    Returns a list of allocation dicts, each with the captured primitives
    (inst_id, installment_no, applied, status, …) needed for post-commit side
    effects and receipts, plus the shared ``op_ref``.
    """
    paid_date = paid_date or date.today()
    allocations = distribute_fee_payment(
        installments, start_installment_no, received,
        payment_method=payment_method, paid_date=paid_date,
        notes=notes, collected_by=collected_by,
    )
    op_ref = generate_receipt_no()
    txn_tag = _payment_txn_tag(op_ref)
    staged = []
    for alloc in allocations:
        ai, aa = alloc['installment'], alloc['applied']
        rev = Revenue(
            category_id      = fee_category.id,
            school_id        = ai.school_id,
            academic_year_id = ai.academic_year_id or rev_academic_year_id,
            amount           = aa,
            description      = (
                f'دفعة رسوم للطالب {student_name}'
                f' - قسط #{ai.installment_no}'
                + (f' - {ai.receipt_no}' if ai.receipt_no else '')
                + txn_tag
            ),
            date             = paid_date,
            recorded_by      = collected_by,
        )
        db.session.add(rev)
        staged.append({
            'installment':    ai,
            'applied':        aa,
            'inst_id':        ai.id,
            'installment_no': ai.installment_no,
            'fee_record_id':  ai.fee_record_id,
            'status':         ai.status,
            'payment_method': ai.payment_method,
            'receipt_no':     ai.receipt_no,
            'op_ref':         op_ref,
            'revenue':        rev,
        })
    return staged


def finalize_payment_notifications(allocations, *, student_id, school_id):
    """Canonical POST-COMMIT side effects for ONE accepted payment operation.

    Writes one audit-log line and one parent FCM push per touched installment,
    and a single investor FCM push for the whole operation — identical whether
    the payment came from the Fees page, the Student Profile, or the Add Student
    wizard. Must be called only AFTER the payment's transaction has committed.
    Takes captured primitives (never live ORM objects) so it is safe once the
    session has expired attributes on commit. Never raises — notification or
    logging failures must not disturb an already-committed payment.
    """
    if not allocations:
        return
    total_applied = sum((Decimal(str(a['applied'])) for a in allocations), Decimal('0'))
    for a in allocations:
        log_action('payment', 'fee_installment', a['inst_id'],
                   details=f"received={a['applied']} method={a['payment_method']} "
                           f"status={a['status']}")
        if student_id is not None:
            _notify_fee_parents(
                student_id,
                'تم تسجيل دفعة',
                f"تم تسجيل دفعة بقيمة {a['applied']} لقسط الرسوم رقم {a['installment_no']} "
                f"({_PAY_STATUS_LABELS.get(a['status'], '')}).",
                screen='fees',
                fee_record_id=a['fee_record_id'],
                installment_id=a['inst_id'],
            )
    if total_applied > 0:
        try:
            from app.services.fcm_service import notify_investors
            notify_investors(
                school_id=school_id,
                title='إيراد جديد',
                body=f'تم تسجيل إيراد جديد بقيمة {float(total_applied)}',
                data={'type': 'investor_revenue', 'route': '/investor/revenues',
                      'school_id': str(school_id), 'amount': str(float(total_applied))},
            )
        except Exception:
            _log.exception('[fees] investor push failed (school_id=%s)', school_id)


def resolve_payment_amount_for_receipt(inst, op_ref=None):
    """Resolve a receipt to ONE exact payment operation and return
    ``(amount, resolved_op_ref)``.

    Two deliberately separate receipt behaviors:

      1. ``op_ref`` given — the IMMEDIATE post-payment receipt link, which
         carries the exact op_ref of the payment just recorded. Confirm this
         installment actually took part in that operation (a Revenue row
         ``... - {inst.receipt_no} [TXN:{op_ref}]`` exists in this
         installment's school), then sum ALL rows tagged with that op_ref —
         so a partial payment prints only its own accepted amount and a
         cascaded payment prints the full entered amount across all the
         installments it touched. A foreign or non-matching op_ref is ignored
         and resolution falls through — it can never surface another school's
         data.
      2. ``op_ref`` absent — the REPRINT link from an installment row's Print
         button in the Fees and Installments table. This prints the CUMULATIVE
         total received for THIS installment across all of its partial
         payments, taken from the installment's persisted ``received_amount``,
         and keeps the installment's OWN receipt number (``resolved_op_ref``
         is None, so the label stays ``inst.receipt_no``). It is deliberately
         neither the latest single Revenue row nor a full cascaded operation
         total spanning other installments — those are reserved for the
         explicit op_ref path above.
    """
    fallback = (Decimal(str(inst.received_amount or 0)), None)
    if not inst.receipt_no:
        return fallback

    def _sum_by_op(_op):
        total = (
            db.session.query(func.coalesce(func.sum(Revenue.amount), 0))
            .execution_options(include_all_years=True)
            .filter(Revenue.school_id == inst.school_id,
                    Revenue.description.like(f'%[TXN:{_op}]%'))
            .scalar()
        )
        return Decimal(str(total or 0))

    # 1) Explicit op_ref — accepted only if it belongs to THIS installment+school.
    if op_ref:
        owns = (
            Revenue.query
            .execution_options(include_all_years=True)
            .filter(Revenue.school_id == inst.school_id,
                    Revenue.description.like(f'%- {inst.receipt_no} [TXN:{op_ref}]%'))
            .first()
        )
        if owns is not None:
            return _sum_by_op(op_ref), op_ref
        # Foreign / stale op_ref → ignore and fall through to a safe default.

    # 2) Bare installment-row reprint (no valid op_ref) — show the CUMULATIVE
    # amount received for THIS installment across all of its partial payments,
    # using the installment's persisted received_amount and its own receipt
    # number (resolved_op is None → the label keeps inst.receipt_no). This is
    # deliberately not the latest single Revenue row, and not a full cascaded
    # operation total spanning other installments — both of which are reserved
    # for the explicit op_ref path above.
    return fallback


@fees_bp.route('/')
@login_required
@permission_required('manage_fees')
def index():
    page      = request.args.get('page', 1, type=int)
    search    = request.args.get('q', '')
    fee_type_filter = request.args.get('fee_type', 'all')
    payment_status = request.args.get('payment_status', 'all')
    installment_filter = request.args.get('installment', 'all')
    
    # Subquery for total paid
    total_paid_sub = db.session.query(
        FeeInstallment.fee_record_id,
        func.sum(FeeInstallment.received_amount).label('total_paid')
    ).group_by(FeeInstallment.fee_record_id).subquery()
    
    school = get_current_school()
    year   = get_view_year(school.id) if school else None

    fee_types = FeeType.query.all()
    years_q   = AcademicYear.query
    if school:
        years_q = years_q.filter_by(school_id=school.id)
    years = years_q.order_by(AcademicYear.start_date.desc()).all()

    # ── "متأخر التسديد" — one visible row per overdue installment ──────────
    # This mode intentionally does NOT group by fee record: a student with
    # several overdue installments appears once per overdue installment, each
    # row carrying that installment's own number/amount/received/remaining/
    # due date. School, building and academic-year isolation are enforced by
    # _overdue_installments_query (see its docstring).
    if payment_status == 'overdue':
        overdue_page = _overdue_installments_query(
            school,
            search=search,
            fee_type_filter=fee_type_filter,
            installment_filter=installment_filter,
        ).paginate(page=page, per_page=20, error_out=False)
        return render_template(
            'fees/index.html',
            records=overdue_page, fee_entries=[],
            overdue_mode=True, overdue_installments=overdue_page.items,
            fee_types=fee_types,
            years=years, search=search,
            fee_type_filter=fee_type_filter,
            payment_status=payment_status,
            installment_filter=installment_filter,
        )

    query = FeeRecord.query.join(Student).outerjoin(
        total_paid_sub, FeeRecord.id == total_paid_sub.c.fee_record_id
    )

    # School scoping
    if school:
        query = query.filter(FeeRecord.school_id == school.id)

    # Building scope — restricted users only see fees of their buildings' students.
    query = apply_building_scope_to_fees(query, current_user, school)

    if search:
        query = query.filter(Student.full_name.ilike(f'%{search}%') |
                             Student.student_id.ilike(f'%{search}%'))

    if fee_type_filter != 'all':
        query = query.filter(FeeRecord.fee_type_id == int(fee_type_filter))
    
    if installment_filter != 'all':
        query = query.join(FeeInstallment).filter(FeeInstallment.installment_no == int(installment_filter))
    
    # Calculate remaining: net_amount - total_paid
    net_amount_expr = FeeRecord.total_amount - func.coalesce(FeeRecord.discount, 0)
    remaining_expr = net_amount_expr - func.coalesce(total_paid_sub.c.total_paid, 0)
    
    if payment_status == 'paid':
        query = query.filter(remaining_expr <= 0)
    elif payment_status == 'unpaid':
        query = query.filter(remaining_expr > 0)

    records   = query.order_by(FeeRecord.created_at.desc())\
                     .paginate(page=page, per_page=20, error_out=False)

    # Batch-fetch installments for the current page to avoid N+1 queries.
    _page_ids = [r.id for r in records.items]
    _page_inst = []
    if _page_ids:
        _page_inst = (
            FeeInstallment.query
            .filter(FeeInstallment.fee_record_id.in_(_page_ids))
            .order_by(FeeInstallment.installment_no)
            .all()
        )
    _inst_map = {}
    for _i in _page_inst:
        _inst_map.setdefault(_i.fee_record_id, []).append(_i)
    fee_entries = [(r, _inst_map.get(r.id, [])) for r in records.items]

    return render_template('fees/index.html',
                           records=records, fee_entries=fee_entries,
                           overdue_mode=False, overdue_installments=[],
                           fee_types=fee_types,
                           years=years, search=search,
                           fee_type_filter=fee_type_filter,
                           payment_status=payment_status,
                           installment_filter=installment_filter)


@fees_bp.route('/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_fees')
def create():
    school    = get_current_school()
    year      = get_active_year(school.id) if school else None
    if not school or not year:
        flash('Select a school with an active academic year before creating fee records.', 'danger')
        return redirect(url_for('fees.index'))
    fee_types = FeeType.query.all()
    years_q   = AcademicYear.query
    if school:
        years_q = years_q.filter_by(school_id=school.id)
    years = years_q.order_by(AcademicYear.start_date.desc()).all()

    if request.method == 'POST':
        selected_student_id = request.form.get('student_id', type=int)
        selected_fee_type_id = request.form.get('fee_type_id', type=int)
        selected_year_id = request.form.get('academic_year_id', type=int) or (year.id if year else None)

        selected_student = (
            _active_fee_student_query(school, year)
            .filter(Student.id == selected_student_id)
            .first()
        ) if selected_student_id else None

        if not selected_student:
            flash('يرجى اختيار طالب من طلاب المدرسة والسنة الدراسية الحالية.', 'danger')
            return render_template('fees/form.html',
                                   fee_types=fee_types, years=years), 400

        # Section ownership validation (only when cascade filters were used, not in locked-student mode).
        submitted_section_id = request.form.get('section_id', type=int)
        if submitted_section_id:
            valid_section = Section.query.filter_by(
                id=submitted_section_id,
                school_id=school.id,
                academic_year_id=year.id,
            ).first()
            if not valid_section:
                flash('الشعبة المحددة غير صالحة أو لا تنتمي لهذه المدرسة والسنة الدراسية.', 'danger')
                return render_template('fees/form.html',
                                       fee_types=fee_types, years=years), 400
            if selected_student.section_id != submitted_section_id:
                flash('الطالب لا ينتمي إلى الشعبة المحددة.', 'danger')
                return render_template('fees/form.html',
                                       fee_types=fee_types, years=years), 400

        if FeeRecord.query.filter_by(student_id=selected_student_id,
                                     fee_type_id=selected_fee_type_id,
                                     academic_year_id=selected_year_id).first():
            flash('A fee record for this student, fee type, and academic year already exists.', 'danger')
            return render_template('fees/form.html',
                                   fee_types=fee_types, years=years,
                                   selected_student=selected_student)

        # ── Discount + record + installments (shared with the student wizard) ─
        try:
            total_amount, discount, _net = compute_fee_amounts(request.form)
            persist_fee_record(
                request.form,
                school=school,
                student_id=selected_student_id,
                fee_type_id=selected_fee_type_id,
                academic_year_id=selected_year_id,
                total_amount=total_amount,
                discount=discount,
            )
        except FeeValidationError as exc:
            flash(str(exc), 'danger')
            return render_template('fees/form.html',
                                   fee_types=fee_types, years=years,
                                   selected_student=selected_student), 400

        db.session.commit()
        # Push after the fee record + installments are committed.
        _notify_fee_parents(
            selected_student.id,
            'سجل رسوم جديد',
            f'تم إضافة سجل رسوم جديد للطالب {selected_student.full_name}.',
            screen='fees',
        )
        flash('تم إنشاء سجل الرسوم بنجاح.', 'success')
        return redirect(url_for('fees.index'))

    # Preselect a student when arriving from the student creation flow.
    # The student_id is validated server-side against school + active year.
    prefill_id = request.args.get('student_id', type=int)
    prefilled_student = None
    if prefill_id:
        prefilled_student = (
            _active_fee_student_query(school, year)
            .filter(Student.id == prefill_id)
            .first()
        )

    return render_template('fees/form.html',
                           fee_types=fee_types, years=years,
                           selected_student=prefilled_student,
                           locked_student=prefilled_student is not None)


@fees_bp.route('/students/search')
@login_required
@permission_required('manage_fees')
def search_students():
    school = get_current_school()
    year = get_active_year(school.id) if school else None
    if not school or not year:
        return jsonify({'results': []})

    term = request.args.get('q', '').strip()
    section_id = request.args.get('section_id', type=int)

    # Require at least 2 chars for open search; allow empty term only when loading
    # all students for a specific section dropdown (section_id must still be validated).
    if len(term) < 2 and not section_id:
        return jsonify({'results': []})

    query = _active_fee_student_query(school, year)

    if section_id:
        # Verify section belongs to this school + active year before applying filter.
        valid_section = Section.query.filter_by(
            id=section_id, school_id=school.id, academic_year_id=year.id
        ).first()
        if not valid_section:
            return jsonify({'results': []})
        query = query.filter(Student.section_id == section_id)

    if term:
        query = query.filter(
            Student.full_name.ilike(f'%{term}%') |
            Student.student_id.ilike(f'%{term}%')
        )

    # When loading all students in a section (no term), allow up to 200;
    # for name/code search, cap at 20 results.
    limit = 200 if (section_id and not term) else 20
    students = query.order_by(Student.full_name).limit(limit).all()
    return jsonify({'results': [_student_payload(student) for student in students]})


@fees_bp.route('/students/search-statement')
@login_required
@permission_required('manage_fees')
def search_students_statement():
    """Student search for the financial statement selector.

    Searches all students of the school regardless of academic year or active
    status, because the statement report spans multiple years and may include
    historical or transferred students. School and building isolation are still
    applied server-side.
    """
    school = get_current_school()
    if not school:
        return jsonify({'results': []})

    term = request.args.get('q', '').strip()
    if len(term) < 2:
        return jsonify({'results': []})

    query = (
        Student.query
        .execution_options(include_all_years=True)
        .options(joinedload(Student.section).joinedload(Section.grade))
        .filter(Student.school_id == school.id)
        .filter(
            Student.full_name.ilike(f'%{term}%') |
            Student.student_id.ilike(f'%{term}%')
        )
    )
    query = apply_building_scope_to_students(query, current_user, school)
    students = query.order_by(Student.full_name).limit(20).all()
    return jsonify({'results': [_student_payload(student) for student in students]})


@fees_bp.route('/api/stages')
@login_required
@permission_required('manage_fees')
def api_stages():
    """Distinct stage values for the current school and active year (for cascading dropdown)."""
    school = get_current_school()
    year = get_active_year(school.id) if school else None
    if not school or not year:
        return jsonify([])
    rows = (
        db.session.query(Grade.stage)
        .filter(
            Grade.school_id == school.id,
            Grade.academic_year_id == year.id,
            Grade.stage.isnot(None),
            Grade.stage != '',
        )
        .distinct()
        .order_by(Grade.stage)
        .all()
    )
    return jsonify([{'value': r[0], 'label': r[0]} for r in rows])


@fees_bp.route('/api/grades')
@login_required
@permission_required('manage_fees')
def api_grades():
    """Grades for a given stage, scoped to the current school and active year."""
    school = get_current_school()
    year = get_active_year(school.id) if school else None
    stage = request.args.get('stage', '').strip()
    if not school or not year or not stage:
        return jsonify([])
    grades = (
        Grade.query
        .filter_by(school_id=school.id, academic_year_id=year.id, stage=stage)
        .order_by(Grade.name)
        .all()
    )
    return jsonify([{'id': g.id, 'name': g.name} for g in grades])


@fees_bp.route('/api/sections')
@login_required
@permission_required('manage_fees')
def api_sections():
    """Sections for a given grade_id, scoped to the current school and active year."""
    school = get_current_school()
    year = get_active_year(school.id) if school else None
    grade_id = request.args.get('grade_id', type=int)
    if not school or not year or not grade_id:
        return jsonify([])
    grade = Grade.query.filter_by(id=grade_id, school_id=school.id,
                                  academic_year_id=year.id).first()
    if not grade:
        return jsonify([])
    sections = (
        Section.query
        .filter_by(grade_id=grade_id, school_id=school.id, academic_year_id=year.id)
        .order_by(Section.name)
        .all()
    )
    return jsonify([{'id': s.id, 'name': s.name} for s in sections])


@fees_bp.route('/api/students-bulk')
@login_required
@permission_required('manage_fees')
def api_students_bulk():
    """Students for bulk fee selection — filter by grade/section or name/code search.

    Scoped to the current school, active year, and building-level access.
    Returns up to 500 students when filtered by section/grade; up to 50 for open search.
    """
    school = get_current_school()
    year = get_active_year(school.id) if school else None
    if not school or not year:
        return jsonify({'results': [], 'total': 0})

    grade_id = request.args.get('grade_id', type=int)
    section_id = request.args.get('section_id', type=int)
    q = request.args.get('q', '').strip()

    if not grade_id and not section_id and len(q) < 2:
        return jsonify({'results': [], 'total': 0})

    query = _active_fee_student_query(school, year)

    if section_id:
        valid_section = Section.query.filter_by(
            id=section_id, school_id=school.id, academic_year_id=year.id
        ).first()
        if not valid_section:
            return jsonify({'results': [], 'total': 0})
        query = query.filter(Student.section_id == section_id)
    elif grade_id:
        valid_grade = Grade.query.filter_by(
            id=grade_id, school_id=school.id, academic_year_id=year.id
        ).first()
        if not valid_grade:
            return jsonify({'results': [], 'total': 0})
        # Resolve grade → section IDs to avoid a cross-join with the eager joinedload.
        section_ids = [
            row.id for row in
            Section.query
            .filter_by(grade_id=grade_id, school_id=school.id, academic_year_id=year.id)
            .with_entities(Section.id)
            .all()
        ]
        if not section_ids:
            return jsonify({'results': [], 'total': 0})
        query = query.filter(Student.section_id.in_(section_ids))

    if q:
        query = query.filter(
            Student.full_name.ilike(f'%{q}%') |
            Student.student_id.ilike(f'%{q}%')
        )

    limit = 500 if (section_id or grade_id) else 50
    students = query.order_by(Student.full_name).limit(limit).all()
    return jsonify({
        'results': [_student_payload(s) for s in students],
        'total': len(students),
    })


@fees_bp.route('/bulk-create', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_fees')
def bulk_create():
    """Bulk fee assignment — create the same fee record for multiple students at once.

    Two-step flow:
      Step 1 (GET / POST action=preview): select students + configure fee → show review.
      Step 2 (POST action=confirm): re-validate → create records atomically → redirect.

    Security: all student IDs are re-validated against the authenticated school + active
    year + building scope on every POST. Duplicates are detected and surfaced to the user
    before any record is created. The entire creation is wrapped in a single DB transaction
    that rolls back completely on any unexpected failure.
    """
    school = get_current_school()
    year = get_active_year(school.id) if school else None
    if not school or not year:
        flash('يرجى اختيار مدرسة ذات سنة دراسية نشطة قبل إنشاء سجلات الرسوم.', 'danger')
        return redirect(url_for('fees.index'))

    fee_types = FeeType.query.all()
    years_q = AcademicYear.query
    if school:
        years_q = years_q.filter_by(school_id=school.id)
    years = years_q.order_by(AcademicYear.start_date.desc()).all()

    if request.method == 'GET':
        return render_template('fees/bulk_form.html', fee_types=fee_types, years=years)

    # ── POST (preview or confirm) ─────────────────────────────────────────────
    action = request.form.get('action', 'preview')

    # ── Parse and deduplicate submitted student IDs ───────────────────────────
    raw_ids = request.form.get('student_ids', '').strip()
    try:
        student_id_list = list(dict.fromkeys(
            int(x) for x in raw_ids.split(',') if x.strip().isdigit()
        ))
    except Exception:
        student_id_list = []

    if not student_id_list:
        flash('يرجى اختيار طالب واحد على الأقل.', 'danger')
        return render_template('fees/bulk_form.html', fee_types=fee_types, years=years), 400

    # ── Validate fee type (school-scoped) ─────────────────────────────────────
    selected_fee_type_id = request.form.get('fee_type_id', type=int)
    selected_year_id = request.form.get('academic_year_id', type=int) or year.id

    fee_type = FeeType.query.filter_by(
        id=selected_fee_type_id, school_id=school.id
    ).first()
    if not fee_type:
        flash('نوع الرسم المحدد غير صالح.', 'danger')
        return render_template('fees/bulk_form.html', fee_types=fee_types, years=years), 400

    # ── Validate amounts ──────────────────────────────────────────────────────
    try:
        total_amount, discount, net_amount = compute_fee_amounts(request.form)
    except FeeValidationError as exc:
        flash(str(exc), 'danger')
        return render_template('fees/bulk_form.html', fee_types=fee_types, years=years), 400

    if total_amount <= 0:
        flash('يجب أن يكون المبلغ الإجمالي أكبر من صفر.', 'danger')
        return render_template('fees/bulk_form.html', fee_types=fee_types, years=years), 400

    # ── Validate installments ─────────────────────────────────────────────────
    num_inst = max(1, min(12, int(request.form.get('num_installments', 1) or 1)))
    installment_dates = []
    for i in range(1, num_inst + 1):
        due_str = request.form.get(f'due_date_{i}')
        try:
            due = dt.strptime(due_str, '%Y-%m-%d').date() if due_str else date.today()
        except ValueError:
            due = date.today()
        installment_dates.append(due)

    # ── Load and validate selected students ───────────────────────────────────
    # School + year + building scope enforced by _active_fee_student_query.
    valid_students = (
        _active_fee_student_query(school, year)
        .filter(Student.id.in_(student_id_list))
        .all()
    )
    valid_ids = {s.id for s in valid_students}
    invalid_count = len(student_id_list) - len(valid_ids)

    if invalid_count:
        flash(
            f'تم استبعاد {invalid_count} طالب غير صالح '
            '(لا ينتمي لهذه المدرسة أو السنة الدراسية أو غير نشط).',
            'warning',
        )

    if not valid_students:
        flash('لا يوجد طلاب صالحون من بين المحددين.', 'danger')
        return render_template('fees/bulk_form.html', fee_types=fee_types, years=years), 400

    # ── Detect duplicates: students who already have this fee for this year ───
    existing_student_ids = set(
        row.student_id for row in
        FeeRecord.query
        .filter(
            FeeRecord.student_id.in_(valid_ids),
            FeeRecord.fee_type_id == selected_fee_type_id,
            FeeRecord.academic_year_id == selected_year_id,
        )
        .with_entities(FeeRecord.student_id)
        .all()
    )

    # Preserve original selection order.
    order_map = {sid: idx for idx, sid in enumerate(student_id_list)}
    duplicate_students = sorted(
        [s for s in valid_students if s.id in existing_student_ids],
        key=lambda s: order_map.get(s.id, 9999),
    )
    eligible_students = sorted(
        [s for s in valid_students if s.id not in existing_student_ids],
        key=lambda s: order_map.get(s.id, 9999),
    )

    # ── Build passthrough fields for the confirm form ─────────────────────────
    # Only trusted server-computed or explicitly validated values are forwarded.
    passthrough = {
        'academic_year_id': str(selected_year_id),
        'fee_type_id':      str(selected_fee_type_id),
        'total_amount':     str(total_amount),
        'discount_type':    request.form.get('discount_type', 'fixed'),
        'discount_value':   str(request.form.get('discount_value', '0') or '0'),
        'num_installments': str(num_inst),
        'notes':            request.form.get('notes', ''),
    }
    for i, d in enumerate(installment_dates, 1):
        passthrough[f'due_date_{i}'] = d.isoformat()

    # ── PREVIEW step ──────────────────────────────────────────────────────────
    if action == 'preview':
        return render_template(
            'fees/bulk_review.html',
            school=school,
            year=year,
            fee_type=fee_type,
            eligible_students=eligible_students,
            duplicate_students=duplicate_students,
            total_amount=float(total_amount),
            discount=float(discount),
            net_amount=float(net_amount),
            num_inst=num_inst,
            installment_dates=installment_dates,
            total_financial_value=float(net_amount) * len(eligible_students),
            passthrough=passthrough,
            eligible_ids=','.join(str(s.id) for s in eligible_students),
        )

    # ── CONFIRM step ──────────────────────────────────────────────────────────
    if action != 'confirm':
        flash('طلب غير صالح.', 'danger')
        return redirect(url_for('fees.bulk_create'))

    if not eligible_students:
        flash(
            'لا يوجد طلاب مؤهلون لإنشاء سجلات رسوم لهم '
            '(جميع المحددين لديهم رسوم مماثلة).',
            'warning',
        )
        return redirect(url_for('fees.index'))

    # Capture student names before commit to avoid post-commit N+1 lazy-loads.
    student_names = {s.id: s.full_name for s in eligible_students}

    created_count = 0
    concurrent_skip = 0
    created_ids = []

    try:
        for student in eligible_students:
            # Guard against race: another request may have created the fee between
            # the preview step and this confirm step.
            if FeeRecord.query.filter_by(
                student_id=student.id,
                fee_type_id=selected_fee_type_id,
                academic_year_id=selected_year_id,
            ).first():
                concurrent_skip += 1
                continue

            persist_fee_record(
                request.form,
                school=school,
                student_id=student.id,
                fee_type_id=selected_fee_type_id,
                academic_year_id=selected_year_id,
                total_amount=total_amount,
                discount=discount,
            )
            created_ids.append(student.id)
            created_count += 1

        # Single atomic commit — if any persist_fee_record flush failed earlier,
        # the exception is caught below and the whole batch rolls back.
        db.session.commit()

    except Exception:
        db.session.rollback()
        _log.exception('[fees] Bulk creation failed school_id=%s fee_type_id=%s',
                       school.id, selected_fee_type_id)
        flash(
            'حدث خطأ أثناء إنشاء سجلات الرسوم. '
            'تم التراجع عن جميع التغييرات. يرجى المحاولة مرة أخرى.',
            'danger',
        )
        return redirect(url_for('fees.bulk_create'))

    # Post-commit: push FCM notifications to parents (best-effort — must not block result).
    for sid in created_ids:
        name = student_names.get(sid, '')
        _notify_fee_parents(
            sid,
            'سجل رسوم جديد',
            f'تم إضافة سجل رسوم جديد للطالب {name}.',
            screen='fees',
        )

    skipped_total = len(duplicate_students) + concurrent_skip
    log_action(
        'bulk_create', 'fee_record', None,
        details=(
            f'bulk_fee created={created_count} skipped={skipped_total} '
            f'school_id={school.id} fee_type_id={selected_fee_type_id} '
            f'year_id={selected_year_id}'
        ),
    )

    parts = []
    if created_count:
        parts.append(f'تم إنشاء {created_count} سجل رسوم بنجاح.')
    if skipped_total:
        parts.append(f'تم تخطي {skipped_total} طالب لوجود رسوم مماثلة.')
    if not parts:
        parts.append('لم يتم إنشاء أي سجل رسوم.')

    flash(' '.join(parts), 'success' if created_count else 'warning')
    return redirect(url_for('fees.index'))


@fees_bp.route('/pay/<int:inst_id>', methods=['POST'])
@login_required
@historical_guard
@permission_required('record_payments')
def pay_installment(inst_id):
    """
    Record a payment starting at ONE installment.

    If the entered amount exceeds that installment's own outstanding balance,
    the excess automatically cascades into the following unpaid/partially-paid
    installments of the same fee record, in order (see distribute_fee_payment).
    If the amount exceeds the total outstanding across the selected installment
    and everything after it, the whole payment is rejected up front with the
    maximum amount currently payable — nothing is saved.

    All mutations — every touched installment's fields and receipt number, and
    one matching Revenue row per installment — are written in a single atomic
    transaction. Either everything commits or everything rolls back. There is
    no silent swallowing of Revenue failures.

    Form fields:
      received_amount  — Decimal (required). May exceed the selected
                          installment's own remaining balance; the excess is
                          distributed to the installments after it.
      payment_method   — cash | transfer | cheque | card  (default cash)
      paid_date        — YYYY-MM-DD (optional, defaults to today)
      notes            — free text
    """
    try:
        # include_all_years lets staff pay installments from prior academic years
        # that are still visible on the student fee profile. The ORM school filter
        # still applies; only year filtering is relaxed.
        inst = (
            FeeInstallment.query
            .execution_options(include_all_years=True)
            .options(
                joinedload(FeeInstallment.fee_record)
                .joinedload(FeeRecord.student)
            )
            .filter(FeeInstallment.id == inst_id)
            .first_or_404()
        )

        # Building scope — restricted users cannot pay for students outside their buildings.
        _student = inst.fee_record.student if inst.fee_record else None
        if not user_can_access_student(current_user, get_current_school(), _student):
            return jsonify({'status': 'error',
                            'message': 'ليس لديك صلاحية الوصول إلى بيانات هذه البناية'}), 403

        # Capture the student name NOW, while the eagerly-loaded relationship is
        # still live. After db.session.commit() SQLAlchemy expires all attributes;
        # a subsequent lazy-reload of fee_record goes through the year-scoped ORM
        # filter and may return None for prior-year installments, making
        # inst.fee_record.student.full_name raise AttributeError.
        student_name = _student.full_name if _student else '—'

        raw_amount = request.form.get('received_amount', '').strip()
        if not raw_amount:
            return jsonify({'status': 'error', 'message': 'يرجى إدخال المبلغ المستلم.'}), 400

        try:
            received = Decimal(raw_amount)
        except (InvalidOperation, ValueError):
            return jsonify({'status': 'error', 'message': 'المبلغ غير صالح.'}), 400

        if received <= 0:
            return jsonify({'status': 'error', 'message': 'يجب أن يكون المبلغ المستلم أكبر من صفر.'}), 400

        # ── Resolve the Revenue category before acquiring the row lock ────────
        # This must happen before the FOR UPDATE lock: on first use it issues its
        # own db.session.commit(), which would release any lock acquired before it.
        fee_category = (
            RevenueCategory.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(name='رسوم دراسية', school_id=inst.school_id)
            .first()
        )
        if not fee_category:
            try:
                fee_category = RevenueCategory(name='رسوم دراسية', school_id=inst.school_id)
                db.session.add(fee_category)
                db.session.flush()
                db.session.commit()
            except IntegrityError:
                # Race condition: another concurrent request created the category.
                db.session.rollback()
                fee_category = (
                    RevenueCategory.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(name='رسوم دراسية', school_id=inst.school_id)
                    .first()
                )
                if not fee_category:
                    raise  # should never happen

        # ── Parse the payment date ────────────────────────────────────────────
        paid_str = request.form.get('paid_date')
        if paid_str:
            try:
                paid_date = dt.strptime(paid_str, '%Y-%m-%d').date()
            except ValueError:
                paid_date = date.today()
        else:
            paid_date = date.today()

        # ── Determine academic_year_id for the Revenue record ─────────────────
        # Use the installment's own academic year. Revenue queries in the finances
        # module already use include_all_years=True and filter by Revenue.date
        # (calendar year), so the record will appear correctly regardless.
        # Fall back to the active year only if the installment somehow has no year.
        rev_academic_year_id = inst.academic_year_id
        if not rev_academic_year_id:
            school = get_current_school()
            active_year = get_active_year(school.id) if school else None
            rev_academic_year_id = active_year.id if active_year else None

        # ── Acquire row lock on the selected installment AND every installment
        # after it in the same fee record (race-condition guard for the whole
        # cascade, not just the one the user clicked). SELECT FOR UPDATE
        # serializes concurrent payment attempts; populate_existing() refreshes
        # each installment's scalar attributes from the latest committed DB
        # state so any payment already committed by a concurrent request is
        # reflected before distribution. The lock is held until commit below.
        locked_installments = (
            db.session.query(FeeInstallment)
            .execution_options(include_all_years=True)
            .with_for_update()
            .filter(FeeInstallment.fee_record_id == inst.fee_record_id,
                    FeeInstallment.installment_no >= inst.installment_no)
            .populate_existing()
            .all()
        )

        # ── Record the payment through the ONE canonical pathway ──────────────
        # stage_installment_payment settles the selected installment, cascades
        # any excess into the following installments (via distribute_fee_payment
        # / apply_installment_payment), and stages one tagged Revenue row per
        # touched installment — the exact same code the Add Student wizard now
        # calls, so both entry points produce byte-for-byte identical financial
        # results. Raises FeeValidationError (with the maximum payable amount)
        # when the amount exceeds what is currently outstanding; nothing is
        # staged, so the request is rejected before anything is saved.
        _sel_inst_id = inst.id            # captured pre-commit for the receipt URL
        _sel_school_id = inst.school_id   # captured pre-commit for post-commit notify
        _notify_student_id = _student.id if _student is not None else None
        allocations = stage_installment_payment(
            locked_installments, inst.installment_no, received,
            fee_category=fee_category, student_name=student_name,
            payment_method=request.form.get('payment_method', 'cash'),
            paid_date=paid_date,
            notes=request.form.get('notes', '').strip(),
            collected_by=current_user.id,
            rev_academic_year_id=rev_academic_year_id,
        )
        _sel_receipt_no = next(
            (a['receipt_no'] for a in allocations if a['installment_no'] == inst.installment_no),
            None,
        )
        # Every row staged by ONE stage_installment_payment call shares one
        # op_ref — this payment's exact operation reference. The receipt link
        # below carries it so the printed/reprinted receipt is tied directly to
        # THIS payment, never resolved by earliest/latest row selection.
        _op_ref = allocations[0]['op_ref'] if allocations else None
        db.session.commit()  # single commit — installments + revenue are atomic

        # ── Post-commit side effects through the same canonical helper ────────
        finalize_payment_notifications(
            allocations,
            student_id=_notify_student_id,
            school_id=_sel_school_id,
        )

        total_applied = sum(a['applied'] for a in allocations)
        if len(allocations) > 1:
            _touched_nos = '، '.join(str(a['installment_no']) for a in allocations)
            message = (f'تم تسجيل دفعة بقيمة {total_applied}، وُزّعت على الأقساط: {_touched_nos}.')
        else:
            _a = allocations[0]
            status_label = _PAY_STATUS_LABELS.get(_a['status'], '')
            message = (f'تم تسجيل دفعة {total_applied} ({status_label}). '
                      f"رقم الإيصال: {_a['receipt_no'] or '—'}")

        receipt_url = (url_for('fees.generate_receipt', inst_id=_sel_inst_id, op=_op_ref)
                       if _sel_receipt_no else None)
        return jsonify({
            'status':      'success',
            'message':     message,
            'receipt_url': receipt_url,
        })

    except FeeValidationError as _fee_exc:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(_fee_exc)}), 400
    except Exception as e:
        db.session.rollback()
        _log.exception('[fees] Payment processing error for inst_id=%s', inst_id)
        return jsonify({'status': 'error',
                        'message': 'حدث خطأ في معالجة الدفع. يرجى المحاولة مرة أخرى.'}), 500


@fees_bp.route('/installment/<int:inst_id>/receipt')
@login_required
@permission_required('record_payments')
def generate_receipt(inst_id):
    """Generate and serve PDF receipt for a paid installment."""
    from app.utils.pdf_gen import generate_fee_receipt
    from app.models import SchoolSettings
    from flask import send_file, abort
    
    inst = (
        FeeInstallment.query
        .execution_options(include_all_years=True)
        .options(
            joinedload(FeeInstallment.fee_record)
            .joinedload(FeeRecord.student)
        )
        .filter(FeeInstallment.id == inst_id)
        .first_or_404()
    )
    if not inst.receipt_no:
        abort(404, "No receipt available for this installment")

    # Building scope — restricted users cannot view receipts outside their buildings.
    _student = inst.fee_record.student if inst.fee_record else None
    if not user_can_access_student(current_user, get_current_school(), _student):
        abort(403)

    # Tie the receipt DIRECTLY to one payment operation via its exact op_ref
    # (passed by the pay action and every reprint link). resolve_* sums the
    # Revenue rows for that exact operation — a partial payment prints its own
    # amount, a cascaded payment prints its full entered total, and two separate
    # payments on the same installment resolve to two distinct operations — with
    # no earliest/latest row guessing. A missing/foreign op_ref falls back
    # safely (completing operation, then historical received_amount).
    _op_ref = (request.args.get('op') or '').strip() or None
    _actual_paid, _resolved_op = resolve_payment_amount_for_receipt(inst, _op_ref)

    school_settings = get_current_school() or SchoolSettings.get()
    # Show the operation reference as the receipt number when resolved, so each
    # distinct payment operation prints as a distinct, reproducible receipt;
    # historical untagged payments keep the installment's own receipt number.
    _receipt_label = _resolved_op or inst.receipt_no
    pdf_bytes = generate_fee_receipt(inst, school_settings, print_date=date.today(),
                                     actual_paid=_actual_paid,
                                     receipt_no_override=_resolved_op)

    if not pdf_bytes:
        abort(500, "PDF generation failed")

    from io import BytesIO
    buf = BytesIO(pdf_bytes)
    buf.seek(0)

    filename = f"receipt_{_receipt_label}.pdf"
    return send_file(buf, as_attachment=False, download_name=filename, mimetype='application/pdf')


@fees_bp.route('/export/excel')
@login_required
@permission_required('manage_fees')
def export_excel():
    from flask import Response
    from app.utils.excel_export import export_fees, export_overdue_installments

    search            = request.args.get('q', '')
    fee_type_filter   = request.args.get('fee_type', 'all')
    payment_status    = request.args.get('payment_status', 'all')
    installment_filter = request.args.get('installment', 'all')

    # "متأخر التسديد" — export one row per overdue installment (no grouping),
    # mirroring the on-screen overdue meaning. Same shared filters + isolation.
    if payment_status == 'overdue':
        overdue_insts = _overdue_installments_query(
            get_current_school(),
            search=search,
            fee_type_filter=fee_type_filter,
            installment_filter=installment_filter,
        ).all()
        data = export_overdue_installments(overdue_insts)
        if not data:
            flash('مكتبة Excel غير متاحة.', 'warning')
            return redirect(url_for('fees.index', payment_status='overdue'))
        return Response(
            data,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': 'attachment; filename=fees_overdue.xlsx'}
        )

    total_paid_sub = db.session.query(
        FeeInstallment.fee_record_id,
        func.sum(FeeInstallment.received_amount).label('total_paid')
    ).group_by(FeeInstallment.fee_record_id).subquery()

    query = FeeRecord.query.join(Student).outerjoin(
        total_paid_sub, FeeRecord.id == total_paid_sub.c.fee_record_id
    )

    # Building scope — restricted users only export their buildings' fees.
    query = apply_building_scope_to_fees(query, current_user, get_current_school())

    if search:
        query = query.filter(Student.full_name.ilike(f'%{search}%') |
                             Student.student_id.ilike(f'%{search}%'))

    if fee_type_filter != 'all':
        query = query.filter(FeeRecord.fee_type_id == int(fee_type_filter))

    if installment_filter != 'all':
        query = query.join(FeeInstallment).filter(FeeInstallment.installment_no == int(installment_filter))

    net_amount_expr = FeeRecord.total_amount - func.coalesce(FeeRecord.discount, 0)
    remaining_expr = net_amount_expr - func.coalesce(total_paid_sub.c.total_paid, 0)
    if payment_status == 'paid':
        query = query.filter(remaining_expr <= 0)
    elif payment_status == 'unpaid':
        query = query.filter(remaining_expr > 0)

    records = query.order_by(FeeRecord.created_at.desc()).all()
    data = export_fees(records)
    if not data:
        flash('مكتبة Excel غير متاحة.', 'warning')
        return redirect(url_for('fees.index'))

    return Response(
        data,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename=fees.xlsx'}
    )


@fees_bp.route('/fee-types', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_fees')
def fee_types():
    school = get_current_school()
    year = get_active_year(school.id) if school else None
    if request.method == 'POST':
        if not school or not year:
            flash('Select a school with an active academic year before adding fee types.', 'danger')
            return redirect(url_for('fees.fee_types'))
        name = request.form.get('name', '').strip()
        if name:
            if FeeType.query.filter_by(name=name, school_id=school.id,
                                       academic_year_id=year.id).first():
                flash('Fee type already exists for this school/year.', 'danger')
                return redirect(url_for('fees.fee_types'))
            ft = FeeType(name=name,
                         school_id=school.id,
                         academic_year_id=year.id,
                         description=request.form.get('description', '').strip())
            db.session.add(ft)
            db.session.commit()
            flash('تم إضافة نوع الرسوم.', 'success')
        return redirect(url_for('fees.fee_types'))
    types = FeeType.query.all()
    return render_template('fees/fee_types.html', types=types)


@fees_bp.route('/print')
@login_required
@permission_required('manage_fees')
def print_list():
    """Print view of current fee records — same filters as index, no pagination."""
    search             = request.args.get('q', '')
    fee_type_filter    = request.args.get('fee_type', 'all')
    payment_status     = request.args.get('payment_status', 'all')
    installment_filter = request.args.get('installment', 'all')

    total_paid_sub = db.session.query(
        FeeInstallment.fee_record_id,
        func.sum(FeeInstallment.received_amount).label('total_paid')
    ).group_by(FeeInstallment.fee_record_id).subquery()

    school = get_current_school()
    year   = get_view_year(school.id) if school else None

    logo_url = None
    if school and getattr(school, 'logo_path', None):
        from app.utils.helpers import resolve_photo_url
        logo_url = resolve_photo_url(school.logo_path)

    fee_types     = FeeType.query.all()
    fee_type_name = next(
        (ft.name for ft in fee_types if str(ft.id) == fee_type_filter), None)

    # "متأخر التسديد" — print one row per overdue installment (no grouping),
    # mirroring the on-screen overdue meaning. Same shared filters + isolation.
    if payment_status == 'overdue':
        overdue_insts = _overdue_installments_query(
            school,
            search=search,
            fee_type_filter=fee_type_filter,
            installment_filter=installment_filter,
        ).all()

        grand_amount = sum(float(i.amount or 0) for i in overdue_insts)
        grand_paid   = sum(float(i.received_amount or 0) for i in overdue_insts)
        grand_rem    = grand_amount - grand_paid

        return render_template(
            'fees/print_list.html',
            fee_entries=[],
            overdue_mode=True,
            overdue_installments=overdue_insts,
            row_count=len(overdue_insts),
            school=school,
            year=year,
            search=search,
            fee_type_filter=fee_type_filter,
            fee_type_name=fee_type_name,
            payment_status=payment_status,
            installment_filter=installment_filter,
            grand_amount=grand_amount,
            grand_paid=grand_paid,
            grand_rem=grand_rem,
            print_date=date.today(),
            logo_url=logo_url,
        )

    query = (
        FeeRecord.query
        .join(Student)
        .outerjoin(total_paid_sub, FeeRecord.id == total_paid_sub.c.fee_record_id)
    )

    if school:
        query = query.filter(FeeRecord.school_id == school.id)

    query = apply_building_scope_to_fees(query, current_user, school)

    if search:
        query = query.filter(
            Student.full_name.ilike(f'%{search}%') |
            Student.student_id.ilike(f'%{search}%')
        )

    if fee_type_filter != 'all':
        query = query.filter(FeeRecord.fee_type_id == int(fee_type_filter))

    if installment_filter != 'all':
        query = query.join(FeeInstallment).filter(
            FeeInstallment.installment_no == int(installment_filter))

    net_amount_expr = FeeRecord.total_amount - func.coalesce(FeeRecord.discount, 0)
    remaining_expr  = net_amount_expr - func.coalesce(total_paid_sub.c.total_paid, 0)

    if payment_status == 'paid':
        query = query.filter(remaining_expr <= 0)
    elif payment_status == 'unpaid':
        query = query.filter(remaining_expr > 0)

    records = query.order_by(FeeRecord.created_at.desc()).all()

    _inst_map = {}
    if records:
        _ids = [r.id for r in records]
        _insts = (
            FeeInstallment.query
            .filter(FeeInstallment.fee_record_id.in_(_ids))
            .order_by(FeeInstallment.installment_no)
            .all()
        )
        for _i in _insts:
            _inst_map.setdefault(_i.fee_record_id, []).append(_i)

    fee_entries = [(r, _inst_map.get(r.id, [])) for r in records]

    grand_total = sum(float(r.total_amount) for r in records)
    grand_disc  = sum(float(r.discount or 0) for r in records)
    grand_paid  = sum(
        sum(float(i.received_amount or 0) for i in _inst_map.get(r.id, []))
        for r in records
    )
    grand_rem   = grand_total - grand_disc - grand_paid

    return render_template(
        'fees/print_list.html',
        fee_entries=fee_entries,
        overdue_mode=False,
        overdue_installments=[],
        row_count=len(fee_entries),
        school=school,
        year=year,
        search=search,
        fee_type_filter=fee_type_filter,
        fee_type_name=fee_type_name,
        payment_status=payment_status,
        installment_filter=installment_filter,
        grand_total=grand_total,
        grand_disc=grand_disc,
        grand_paid=grand_paid,
        grand_rem=grand_rem,
        print_date=date.today(),
        logo_url=logo_url,
    )


@fees_bp.route('/student/<int:student_id>/statement')
@login_required
@permission_required('manage_fees')
def student_statement(student_id):
    """Complete financial statement for a student — all fee types, all academic years."""
    school = get_current_school()

    student = (
        Student.query
        .execution_options(include_all_years=True)
        .options(
            joinedload(Student.section).joinedload(Section.grade)
        )
        .filter(Student.id == student_id)
        .first_or_404()
    )

    # School isolation: student must belong to the authenticated user's school.
    if school and student.school_id and student.school_id != school.id:
        abort(403)

    # Building scope: restricted users cannot view students outside their buildings.
    if not user_can_access_student(current_user, school, student):
        abort(403)

    # Load all fee records across all academic years, scoped to school.
    fee_records = (
        FeeRecord.query
        .execution_options(include_all_years=True)
        .filter_by(student_id=student.id, school_id=student.school_id)
        .options(
            joinedload(FeeRecord.fee_type),
            joinedload(FeeRecord.academic_year),
        )
        .order_by(FeeRecord.academic_year_id.asc(), FeeRecord.created_at.asc())
        .all()
    )

    fee_entries = []
    if fee_records:
        _ids = [r.id for r in fee_records]
        _insts = (
            FeeInstallment.query
            .execution_options(include_all_years=True)
            .filter(
                FeeInstallment.fee_record_id.in_(_ids),
                FeeInstallment.school_id == student.school_id,
            )
            .options(joinedload(FeeInstallment.collector))
            .order_by(FeeInstallment.fee_record_id, FeeInstallment.installment_no)
            .all()
        )
        _inst_map = {}
        for _i in _insts:
            _inst_map.setdefault(_i.fee_record_id, []).append(_i)
        fee_entries = [(r, _inst_map.get(r.id, [])) for r in fee_records]

    grand_total = sum(float(r.total_amount) for r, _ in fee_entries)
    grand_disc  = sum(float(r.discount or 0) for r, _ in fee_entries)
    grand_net   = grand_total - grand_disc
    grand_paid  = sum(
        sum(float(i.received_amount or 0) for i in insts)
        for _, insts in fee_entries
    )
    grand_rem   = grand_net - grand_paid

    logo_url = None
    if school and getattr(school, 'logo_path', None):
        from app.utils.helpers import resolve_photo_url
        logo_url = resolve_photo_url(school.logo_path)

    return render_template(
        'fees/student_statement.html',
        student=student,
        school=school,
        fee_entries=fee_entries,
        grand_total=grand_total,
        grand_disc=grand_disc,
        grand_net=grand_net,
        grand_paid=grand_paid,
        grand_rem=grand_rem,
        print_date=date.today(),
        logo_url=logo_url,
    )


@fees_bp.route('/reminder-settings', methods=['POST'])
@login_required
@permission_required('manage_fees')
def reminder_settings():
    # Resolve school_id explicitly — do NOT rely on get_current_school() because
    # it can return a SchoolSettings fallback object for super_admin without an
    # active school in session, which is not a School row and cannot be saved.
    if current_user.is_super_admin:
        school_id = flask_session.get('active_school_id')
    else:
        school_id = current_user.school_id

    _log.warning(
        '[fees-reminder-settings] called user_id=%s is_super_admin=%s school_id=%s form=%s',
        current_user.id, current_user.is_super_admin, school_id, dict(request.form),
    )

    if not school_id:
        flash('الرجاء اختيار مدرسة أولاً.', 'warning')
        return redirect(url_for('fees.index'))

    school = (
        School.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(id=school_id)
        .first()
    )
    if not school:
        flash('المدرسة غير موجودة.', 'warning')
        return redirect(url_for('fees.index'))

    enabled = request.form.get('fee_reminder_enabled') == '1'

    try:
        days_before = max(1, min(int(request.form.get('fee_reminder_days_before', '3').strip()), 30))
    except (ValueError, TypeError):
        days_before = 3

    try:
        per_day = max(1, min(int(request.form.get('fee_reminder_per_day', '1').strip()), 6))
    except (ValueError, TypeError):
        per_day = 1

    school.fee_reminder_enabled     = enabled
    school.fee_reminder_days_before = days_before
    school.fee_reminder_per_day     = per_day

    _log.warning(
        '[fees-reminder-settings] saving school_id=%s enabled=%s days_before=%s per_day=%s',
        school.id, enabled, days_before, per_day,
    )

    db.session.commit()

    _log.warning(
        '[fees-reminder-settings] saved school_id=%s enabled=%s days_before=%s per_day=%s',
        school.id, enabled, days_before, per_day,
    )

    log_action('edit', 'school', school.id, details='fee reminder settings updated')
    flash('تم حفظ إعدادات تذكير الأقساط بنجاح.', 'success')
    return redirect(url_for('fees.index'))
