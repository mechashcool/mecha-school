"""School inventory / warehouse module."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload

from app.models import (
    db, InventoryCategory, InventoryCount, InventoryItem, InventoryMovement,
)
from app.utils.decorators import admin_required, get_active_year, get_current_school, historical_guard


inventory_bp = Blueprint('inventory', __name__, template_folder='../../templates/inventory')


CATEGORY_NAMES = ['قرطاسية', 'كتب', 'زي مدرسي', 'ملابس', 'مواد تنظيف', 'أجهزة', 'أثاث', 'أخرى']
MOVEMENT_TYPES = {'in': 'إدخال', 'out': 'إخراج'}
MOVEMENT_REASONS = [
    'شراء جديد', 'تبرع', 'مرتجع', 'تسليم للطلاب', 'تسليم للمدرسين',
    'صرف للإدارة', 'تلف', 'فقدان', 'استخدام داخلي',
]
COUNT_STATUS = {'match': 'مطابق', 'shortage': 'نقص', 'surplus': 'زيادة'}

# Categories that require a size field (clothing / uniform items).
CLOTHING_CATEGORIES = {'زي مدرسي', 'ملابس', 'uniforms', 'clothing', 'school uniform'}
CLOTHING_SIZES = ['XS', 'S', 'M', 'L', 'XL', 'XXL', 'XXXL',
                  '4', '6', '8', '10', '12', '14', '16']


def _school_year_or_redirect():
    school = get_current_school()
    year = get_active_year(school.id) if school else None
    if not school or not year:
        flash('يرجى اختيار مدرسة وسنة دراسية فعالة قبل استخدام المخازن.', 'danger')
        return None, None, redirect(url_for('admin.dashboard'))
    return school, year, None


def _decimal_value(name, default='0'):
    raw = (request.form.get(name) or default).strip()
    try:
        return Decimal(raw or default)
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _parse_date(name, default=None):
    raw = (request.form.get(name) or '').strip()
    if not raw:
        return default or date.today()
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return default or date.today()


def _ensure_default_categories(school, year):
    existing = {
        c.name for c in InventoryCategory.query
        .filter_by(school_id=school.id, academic_year_id=year.id)
        .all()
    }
    missing = [name for name in CATEGORY_NAMES if name not in existing]
    if missing:
        for name in missing:
            db.session.add(InventoryCategory(
                school_id=school.id,
                academic_year_id=year.id,
                name=name,
            ))
        db.session.commit()


def _categories(school, year):
    return (InventoryCategory.query
            .filter_by(school_id=school.id, academic_year_id=year.id)
            .order_by(InventoryCategory.name)
            .all())


def _clothing_category_ids(categories):
    """Return IDs of categories whose names match the clothing set."""
    lower_names = {n.lower() for n in CLOTHING_CATEGORIES}
    return [c.id for c in categories if c.name.strip().lower() in lower_names]


def _items_query(school, year):
    return (InventoryItem.query
            .options(joinedload(InventoryItem.category))
            .filter_by(school_id=school.id, academic_year_id=year.id))


def _item_or_404(item_id, school, year):
    return _items_query(school, year).filter_by(id=item_id).first_or_404()


@inventory_bp.route('/')
@login_required
@admin_required
def index():
    school, year, response = _school_year_or_redirect()
    if response:
        return response
    _ensure_default_categories(school, year)

    q = request.args.get('q', '').strip()
    category_id = request.args.get('category_id', type=int)
    status = request.args.get('status', 'all')
    query = _items_query(school, year)
    if q:
        like = f'%{q}%'
        query = query.filter((InventoryItem.name.ilike(like)) | (InventoryItem.item_code.ilike(like)))
    if category_id:
        query = query.filter(InventoryItem.category_id == category_id)
    if status == 'low':
        query = query.filter(InventoryItem.current_quantity <= InventoryItem.minimum_quantity)

    items = query.order_by(InventoryItem.name).all()
    low_count = sum(1 for item in items if item.is_low_stock)
    return render_template('inventory/index.html',
                           items=items,
                           categories=_categories(school, year),
                           q=q,
                           category_id=category_id,
                           status=status,
                           low_count=low_count)


@inventory_bp.route('/items/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@admin_required
def create_item():
    school, year, response = _school_year_or_redirect()
    if response:
        return response
    _ensure_default_categories(school, year)
    categories = _categories(school, year)

    if request.method == 'POST':
        item = InventoryItem(
            school_id=school.id,
            academic_year_id=year.id,
        )
        _populate_item(item)
        db.session.add(item)
        db.session.commit()
        flash('تمت إضافة المادة بنجاح.', 'success')
        return redirect(url_for('inventory.index'))

    clothing_ids = _clothing_category_ids(categories)
    return render_template('inventory/item_form.html', item=None, categories=categories,
                           clothing_category_ids=clothing_ids,
                           clothing_sizes=CLOTHING_SIZES)


@inventory_bp.route('/items/<int:item_id>/edit', methods=['GET', 'POST'])
@login_required
@historical_guard
@admin_required
def edit_item(item_id):
    school, year, response = _school_year_or_redirect()
    if response:
        return response
    item = _item_or_404(item_id, school, year)
    categories = _categories(school, year)

    if request.method == 'POST':
        _populate_item(item)
        db.session.commit()
        flash('تم تحديث المادة بنجاح.', 'success')
        return redirect(url_for('inventory.index'))

    clothing_ids = _clothing_category_ids(categories)
    return render_template('inventory/item_form.html', item=item, categories=categories,
                           clothing_category_ids=clothing_ids,
                           clothing_sizes=CLOTHING_SIZES)


def _populate_item(item):
    item.name = request.form.get('name', '').strip()
    item.category_id = request.form.get('category_id', type=int)
    item.item_code = request.form.get('item_code', '').strip() or None
    item.unit = request.form.get('unit', '').strip() or 'قطعة'
    item.size = request.form.get('size', '').strip() or None
    item.current_quantity = _decimal_value('current_quantity')
    item.minimum_quantity = _decimal_value('minimum_quantity')
    item.purchase_price = _decimal_value('purchase_price') if request.form.get('purchase_price') else None
    item.supplier = request.form.get('supplier', '').strip() or None
    item.notes = request.form.get('notes', '').strip() or None
    item.is_active = bool(request.form.get('is_active', '1'))


@inventory_bp.route('/categories', methods=['GET', 'POST'])
@login_required
@historical_guard
@admin_required
def categories():
    school, year, response = _school_year_or_redirect()
    if response:
        return response
    _ensure_default_categories(school, year)

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip() or None
        if not name:
            flash('اسم التصنيف مطلوب.', 'danger')
        elif InventoryCategory.query.filter_by(school_id=school.id, academic_year_id=year.id, name=name).first():
            flash('هذا التصنيف موجود بالفعل.', 'warning')
        else:
            db.session.add(InventoryCategory(
                school_id=school.id,
                academic_year_id=year.id,
                name=name,
                description=description,
            ))
            db.session.commit()
            flash('تمت إضافة التصنيف بنجاح.', 'success')
        return redirect(url_for('inventory.categories'))

    return render_template('inventory/categories.html', categories=_categories(school, year))


@inventory_bp.route('/categories/<int:category_id>/delete', methods=['POST'])
@login_required
@historical_guard
@admin_required
def delete_category(category_id):
    school, year, response = _school_year_or_redirect()
    if response:
        return response
    category = InventoryCategory.query.filter_by(
        id=category_id,
        school_id=school.id,
        academic_year_id=year.id,
    ).first()
    if not category:
        flash('التصنيف غير موجود أو تم حذفه مسبقاً.', 'warning')
        return redirect(url_for('inventory.categories'))

    if category.items.count():
        flash('لا يمكن حذف هذا التصنيف لأنه يحتوي على مواد مخزنية.', 'danger')
        return redirect(url_for('inventory.categories'))

    db.session.delete(category)
    db.session.commit()
    flash('تم حذف التصنيف بنجاح.', 'success')
    return redirect(url_for('inventory.categories'))


@inventory_bp.route('/movements')
@login_required
@admin_required
def movements():
    school, year, response = _school_year_or_redirect()
    if response:
        return response
    query = (InventoryMovement.query
             .options(joinedload(InventoryMovement.item))
             .filter_by(school_id=school.id, academic_year_id=year.id))
    item_id = request.args.get('item_id', type=int)
    if item_id:
        query = query.filter(InventoryMovement.item_id == item_id)
    movements = query.order_by(InventoryMovement.movement_date.desc(), InventoryMovement.id.desc()).all()
    return render_template('inventory/movements.html',
                           movements=movements,
                           movement_types=MOVEMENT_TYPES,
                           items=_items_query(school, year).order_by(InventoryItem.name).all(),
                           selected_item_id=item_id)


@inventory_bp.route('/movements/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@admin_required
def create_movement():
    school, year, response = _school_year_or_redirect()
    if response:
        return response
    items = _items_query(school, year).filter_by(is_active=True).order_by(InventoryItem.name).all()
    selected_item_id = request.args.get('item_id', type=int)

    if request.method == 'POST':
        item = _item_or_404(request.form.get('item_id', type=int), school, year)
        movement_type = request.form.get('movement_type')
        reason = request.form.get('reason')
        quantity = _decimal_value('quantity')
        if movement_type not in MOVEMENT_TYPES or reason not in MOVEMENT_REASONS or quantity <= 0:
            flash('يرجى إدخال بيانات حركة صحيحة.', 'danger')
            return render_template('inventory/movement_form.html',
                                   items=items,
                                   movement_types=MOVEMENT_TYPES,
                                   reasons=MOVEMENT_REASONS,
                                   selected_item_id=selected_item_id), 400
        if movement_type == 'out' and item.current_quantity < quantity:
            flash('الكمية المطلوبة أكبر من الكمية المتوفرة.', 'danger')
            return render_template('inventory/movement_form.html',
                                   items=items,
                                   movement_types=MOVEMENT_TYPES,
                                   reasons=MOVEMENT_REASONS,
                                   selected_item_id=selected_item_id), 400

        movement = InventoryMovement(
            school_id=school.id,
            academic_year_id=year.id,
            item_id=item.id,
            movement_type=movement_type,
            reason=reason,
            quantity=quantity,
            movement_date=_parse_date('movement_date'),
            recipient=request.form.get('recipient', '').strip() or None,
            notes=request.form.get('notes', '').strip() or None,
            created_by=current_user.id,
        )
        item.current_quantity = item.current_quantity + quantity if movement_type == 'in' else item.current_quantity - quantity
        db.session.add(movement)
        db.session.commit()
        flash('تم تسجيل حركة المخزن بنجاح.', 'success')
        return redirect(url_for('inventory.movements'))

    return render_template('inventory/movement_form.html',
                           items=items,
                           movement_types=MOVEMENT_TYPES,
                           reasons=MOVEMENT_REASONS,
                           selected_item_id=selected_item_id)


@inventory_bp.route('/counts')
@login_required
@admin_required
def counts():
    school, year, response = _school_year_or_redirect()
    if response:
        return response
    rows = (InventoryCount.query
            .options(joinedload(InventoryCount.item), joinedload(InventoryCount.counter))
            .filter_by(school_id=school.id, academic_year_id=year.id)
            .order_by(InventoryCount.count_date.desc(), InventoryCount.id.desc())
            .all())
    return render_template('inventory/counts.html', counts=rows, status_labels=COUNT_STATUS)


@inventory_bp.route('/counts/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@admin_required
def create_count():
    school, year, response = _school_year_or_redirect()
    if response:
        return response
    items = _items_query(school, year).filter_by(is_active=True).order_by(InventoryItem.name).all()

    if request.method == 'POST':
        try:
            item = _item_or_404(request.form.get('item_id', type=int), school, year)
            actual = _decimal_value('actual_quantity')
            diff = actual - item.current_quantity
            status = 'match' if diff == 0 else ('surplus' if diff > 0 else 'shortage')
            row = InventoryCount(
                school_id=school.id,
                academic_year_id=year.id,
                item_id=item.id,
                system_quantity=item.current_quantity,
                actual_quantity=actual,
                difference=diff,
                status=status,
                reason=request.form.get('reason', '').strip() or None,
                notes=request.form.get('notes', '').strip() or None,
                counted_by=current_user.id,
                count_date=_parse_date('count_date'),
            )
            db.session.add(row)
            db.session.commit()
            flash('تم تسجيل الجرد السنوي بنجاح.', 'success')
            return redirect(url_for('inventory.counts'))
        except SQLAlchemyError:
            db.session.rollback()
            flash('تعذر حفظ سجل الجرد بسبب خطأ في قاعدة البيانات. يرجى المحاولة مرة أخرى.', 'danger')
        except Exception:
            db.session.rollback()
            flash('تعذر حفظ سجل الجرد. يرجى مراجعة البيانات والمحاولة مرة أخرى.', 'danger')
        return render_template('inventory/count_form.html', items=items), 400

    return render_template('inventory/count_form.html', items=items)


@inventory_bp.route('/reports')
@login_required
@admin_required
def reports():
    school, year, response = _school_year_or_redirect()
    if response:
        return response
    items = _items_query(school, year).order_by(InventoryItem.name).all()
    low_items = [item for item in items if item.is_low_stock]
    category_totals = (
        db.session.query(InventoryCategory.name, func.count(InventoryItem.id), func.sum(InventoryItem.current_quantity))
        .join(InventoryItem, InventoryItem.category_id == InventoryCategory.id)
        .filter(InventoryItem.school_id == school.id, InventoryItem.academic_year_id == year.id)
        .group_by(InventoryCategory.name)
        .order_by(InventoryCategory.name)
        .all()
    )
    from_date = request.args.get('from_date')
    to_date = request.args.get('to_date')
    movement_query = InventoryMovement.query.filter_by(school_id=school.id, academic_year_id=year.id)
    if from_date:
        movement_query = movement_query.filter(InventoryMovement.movement_date >= datetime.strptime(from_date, '%Y-%m-%d').date())
    if to_date:
        movement_query = movement_query.filter(InventoryMovement.movement_date <= datetime.strptime(to_date, '%Y-%m-%d').date())
    movements_report = movement_query.order_by(InventoryMovement.movement_date.desc()).limit(100).all()
    counts_report = InventoryCount.query.filter_by(school_id=school.id, academic_year_id=year.id).order_by(InventoryCount.count_date.desc()).all()
    return render_template('inventory/reports.html',
                           items=items,
                           low_items=low_items,
                           category_totals=category_totals,
                           movements=movements_report,
                           counts=counts_report,
                           movement_types=MOVEMENT_TYPES,
                           count_status=COUNT_STATUS,
                           from_date=from_date,
                           to_date=to_date)


@inventory_bp.route('/reports/export.xlsx')
@login_required
@admin_required
def export_excel():
    school, year, response = _school_year_or_redirect()
    if response:
        return response
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except Exception:
        flash('مكتبة Excel غير متاحة.', 'warning')
        return redirect(url_for('inventory.reports'))

    wb = Workbook()
    ws = wb.active
    ws.title = 'المخزون الحالي'
    headers = ['المادة', 'التصنيف', 'الرمز', 'الوحدة', 'الكمية الحالية', 'الحد الأدنى', 'تنبيه']
    ws.append(headers)
    ar = lambda text, style=None: text
    header_style = None
    rows = [[
        ar('اسم المادة', header_style),
        ar('التصنيف', header_style),
        ar('الكمية', header_style),
        ar('الحد الأدنى', header_style),
        ar('القيمة', header_style),
    ]]
    rows = [[
        ar('اسم المادة', header_style),
        ar('التصنيف', header_style),
        ar('الكمية', header_style),
        ar('الحد الأدنى', header_style),
        ar('القيمة', header_style),
    ]]
    rows = [[
        ar('\u0627\u0633\u0645 \u0627\u0644\u0645\u0627\u062f\u0629', header_style),
        ar('\u0627\u0644\u062a\u0635\u0646\u064a\u0641', header_style),
        ar('\u0627\u0644\u0643\u0645\u064a\u0629', header_style),
        ar('\u0627\u0644\u062d\u062f \u0627\u0644\u0623\u062f\u0646\u0649', header_style),
        ar('\u0627\u0644\u0642\u064a\u0645\u0629', header_style),
    ]]
    for item in _items_query(school, year).order_by(InventoryItem.name).all():
        ws.append([
            item.name,
            item.category.name if item.category else '',
            item.item_code or '',
            item.unit,
            float(item.current_quantity or 0),
            float(item.minimum_quantity or 0),
            'منخفض' if item.is_low_stock else '',
        ])
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='1A3A5C')
        cell.alignment = Alignment(horizontal='center')
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename=inventory.xlsx'},
    )


@inventory_bp.route('/reports/export.pdf')
@login_required
@admin_required
def export_pdf():
    school, year, response = _school_year_or_redirect()
    if response:
        return response
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from app.utils.pdf_gen import _register_arabic_fonts, _shape_arabic_text, generate_error_pdf
    except Exception:
        flash('مكتبة PDF غير متاحة.', 'warning')
        return redirect(url_for('inventory.reports'))

    arabic_font_registered = _register_arabic_fonts(pdfmetrics, TTFont)
    if not arabic_font_registered:
        pdf_bytes = generate_error_pdf(
            'خطأ في تحميل الخط العربي',
            'يرجى التحقق من وجود ملفات الخط العربي في مجلد static/fonts/'
        )
        if not pdf_bytes:
            flash('تعذر تحميل الخط العربي لتوليد PDF.', 'danger')
            return redirect(url_for('inventory.reports'))
        return Response(pdf_bytes, mimetype='application/pdf')

    title_style = ParagraphStyle(
        'inventory_title',
        fontName='Amiri-Bold',
        fontSize=16,
        alignment=1,
        textColor=colors.HexColor('#1a3a5c'),
        spaceAfter=12,
    )
    header_style = ParagraphStyle(
        'inventory_header',
        fontName='Amiri-Bold',
        fontSize=10,
        alignment=1,
        textColor=colors.white,
    )
    cell_style = ParagraphStyle(
        'inventory_cell',
        fontName='Amiri',
        fontSize=9,
        alignment=1,
        leading=12,
    )

    def ar(text, style=cell_style):
        return Paragraph(_shape_arabic_text(text), style)

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    rows = [[
        ar('اسم المادة', header_style),
        ar('التصنيف', header_style),
        ar('الكمية', header_style),
        ar('الحد الأدنى', header_style),
        ar('القيمة', header_style),
    ]]
    rows = [['المادة', 'التصنيف', 'الكمية', 'الحد الأدنى']]
    for item in _items_query(school, year).order_by(InventoryItem.name).all():
        quantity = item.current_quantity or 0
        price = item.purchase_price or 0
        value = quantity * price
        rows.append([
            ar(item.name),
            ar(item.category.name if item.category else '-'),
            ar(f'{quantity} {item.unit or ""}'),
            ar(str(item.minimum_quantity or 0)),
            ar(str(value)),
        ])
    rows[0] = [
        ar('\u0627\u0633\u0645 \u0627\u0644\u0645\u0627\u062f\u0629', header_style),
        ar('\u0627\u0644\u062a\u0635\u0646\u064a\u0641', header_style),
        ar('\u0627\u0644\u0643\u0645\u064a\u0629', header_style),
        ar('\u0627\u0644\u062d\u062f \u0627\u0644\u0623\u062f\u0646\u0649', header_style),
        ar('\u0627\u0644\u0642\u064a\u0645\u0629', header_style),
    ]
    table = Table(rows, repeatRows=1, colWidths=[120, 95, 80, 80, 80])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a3a5c')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Amiri-Bold'),
        ('FONTNAME', (0, 1), (-1, -1), 'Amiri'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7f9fb')]),
    ]))
    doc.build([ar('\u062a\u0642\u0631\u064a\u0631 \u0627\u0644\u0645\u062e\u0632\u0648\u0646', title_style), Spacer(1, 12), table])
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name='inventory.pdf', mimetype='application/pdf')
    doc.build([ar('تقرير المخزون', title_style), Spacer(1, 12), table])
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name='inventory.pdf', mimetype='application/pdf')
    doc.build([Paragraph('تقرير المخزون الحالي', styles['Title']), Spacer(1, 12), table])
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name='inventory.pdf', mimetype='application/pdf')


@inventory_bp.route('/reports/export_annual.xlsx')
@login_required
@admin_required
def export_annual_excel():
    school, year, response = _school_year_or_redirect()
    if response:
        return response
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except Exception:
        flash('مكتبة Excel غير متاحة.', 'warning')
        return redirect(url_for('inventory.reports'))

    counts = (InventoryCount.query
              .options(joinedload(InventoryCount.item).joinedload(InventoryItem.category))
              .filter_by(school_id=school.id, academic_year_id=year.id)
              .order_by(InventoryCount.count_date.asc())
              .all())

    wb = Workbook()
    ws = wb.active
    ws.title = 'الجرد السنوي'
    ws.sheet_view.rightToLeft = True

    ws.append([school.school_name if school else '', '', f'السنة: {year.name if year else ""}'])
    ws.append(['الجرد السنوي', '', f'تاريخ التقرير: {date.today().strftime("%Y-%m-%d")}'])
    ws.append([])

    headers = ['التاريخ', 'المادة', 'التصنيف', 'الحجم', 'كمية النظام', 'الكمية الفعلية', 'الفرق', 'الحالة']
    ws.append(headers)

    status_map = {'match': 'مطابق', 'shortage': 'نقص', 'surplus': 'زيادة'}
    for row in counts:
        item = row.item
        ws.append([
            row.count_date.strftime('%Y-%m-%d') if row.count_date else '',
            item.name if item else '',
            item.category.name if item and item.category else '',
            item.size or '' if item else '',
            float(row.system_quantity or 0),
            float(row.actual_quantity or 0),
            float(row.difference or 0),
            status_map.get(row.status, row.status or ''),
        ])

    for cell in ws[4]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='1A3A5C')
        cell.alignment = Alignment(horizontal='center')

    for col in ws.columns:
        max_len = max((len(str(cell.value or '')) for cell in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename=annual_inventory.xlsx'},
    )


@inventory_bp.route('/reports/export_annual.pdf')
@login_required
@admin_required
def export_annual_pdf():
    school, year, response = _school_year_or_redirect()
    if response:
        return response
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from app.utils.pdf_gen import _register_arabic_fonts, _shape_arabic_text, generate_error_pdf
    except Exception:
        flash('مكتبة PDF غير متاحة.', 'warning')
        return redirect(url_for('inventory.reports'))

    arabic_font_registered = _register_arabic_fonts(pdfmetrics, TTFont)
    if not arabic_font_registered:
        pdf_bytes = generate_error_pdf(
            'خطأ في تحميل الخط العربي',
            'يرجى التحقق من وجود ملفات الخط العربي في مجلد static/fonts/'
        )
        if not pdf_bytes:
            flash('تعذر تحميل الخط العربي لتوليد PDF.', 'danger')
            return redirect(url_for('inventory.reports'))
        return Response(pdf_bytes, mimetype='application/pdf')

    title_style = ParagraphStyle('annual_title', fontName='Amiri-Bold', fontSize=16,
                                 alignment=1, textColor=colors.HexColor('#1a3a5c'), spaceAfter=6)
    sub_style = ParagraphStyle('annual_sub', fontName='Amiri', fontSize=10,
                               alignment=1, textColor=colors.HexColor('#555555'), spaceAfter=12)
    header_style = ParagraphStyle('annual_hdr', fontName='Amiri-Bold', fontSize=9,
                                  alignment=1, textColor=colors.white)
    cell_style = ParagraphStyle('annual_cell', fontName='Amiri', fontSize=8,
                                alignment=1, leading=11)

    def ar(text, style=cell_style):
        return Paragraph(_shape_arabic_text(str(text)), style)

    counts = (InventoryCount.query
              .options(joinedload(InventoryCount.item).joinedload(InventoryItem.category))
              .filter_by(school_id=school.id, academic_year_id=year.id)
              .order_by(InventoryCount.count_date.asc())
              .all())

    status_map = {'match': 'مطابق', 'shortage': 'نقص', 'surplus': 'زيادة'}
    table_data = [[
        ar('التاريخ', header_style), ar('المادة', header_style),
        ar('التصنيف', header_style), ar('الحجم', header_style),
        ar('كمية النظام', header_style), ar('الكمية الفعلية', header_style),
        ar('الفرق', header_style), ar('الحالة', header_style),
    ]]
    for row in counts:
        item = row.item
        table_data.append([
            ar(row.count_date.strftime('%Y-%m-%d') if row.count_date else '-'),
            ar(item.name if item else '-'),
            ar(item.category.name if item and item.category else '-'),
            ar(item.size if item and item.size else '-'),
            ar(str(row.system_quantity or 0)),
            ar(str(row.actual_quantity or 0)),
            ar(str(row.difference or 0)),
            ar(status_map.get(row.status, row.status or '-')),
        ])

    col_widths = [65, 100, 80, 45, 65, 65, 45, 55]
    table = Table(table_data, repeatRows=1, colWidths=col_widths)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a3a5c')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7f9fb')]),
    ]))

    school_name = school.school_name if school else ''
    year_name = year.name if year else ''
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4),
                            rightMargin=20, leftMargin=20, topMargin=25, bottomMargin=25)
    doc.build([
        ar(school_name, title_style),
        ar(f'الجرد السنوي — {year_name}', title_style),
        Spacer(1, 4),
        ar(f'تاريخ التقرير: {date.today().strftime("%Y-%m-%d")}', sub_style),
        table,
    ])
    buffer.seek(0)
    return send_file(buffer, as_attachment=True,
                     download_name='annual_inventory.pdf', mimetype='application/pdf')
