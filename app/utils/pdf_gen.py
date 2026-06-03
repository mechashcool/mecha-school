"""
Al-Muhandis – PDF Generator
Generates printable PDF documents using ReportLab.
Usage: from app.utils.pdf_gen import generate_fee_receipt, generate_salary_pdf
"""
from io import BytesIO
from datetime import datetime
import os


def _resolve_logo_for_pdf(logo_path: str | None) -> str | None:
    """Return a local filesystem path usable by ReportLab's Image(), or None.

    - Local relative paths (uploads/...) → resolved under static/.
    - Legacy bare filenames → also tried under static/uploads/.
    - Supabase / http(s) URLs → downloaded to a temp file; caller does NOT need
      to clean it up (the OS will reclaim it on next reboot or via tempfile GC).
    - Missing / inaccessible → returns None so the PDF just omits the logo.
    """
    if not logo_path:
        return None

    if logo_path.startswith(('http://', 'https://')):
        try:
            import requests as _req
            import tempfile
            resp = _req.get(logo_path, timeout=10)
            if resp.status_code == 200:
                ext = logo_path.rsplit('.', 1)[-1].lower() if '.' in logo_path else 'png'
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f'.{ext}')
                tmp.write(resp.content)
                tmp.flush()
                tmp.close()
                return tmp.name
        except Exception:
            pass
        return None

    from flask import current_app
    candidates = [logo_path]
    if '/' not in logo_path:
        candidates.append(f'uploads/{logo_path}')
    for candidate in candidates:
        full = os.path.join(current_app.root_path, 'static', candidate)
        if os.path.isfile(full):
            return full
    return None


def _get_rl():
    """Lazy-import ReportLab so the app doesn't crash if it's not installed."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                         Paragraph, Spacer)
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        return True
    except ImportError:
        return False


def _font_candidates():
    """Arabic-capable font locations used by ReportLab PDF output."""
    paths = []

    env_path = os.environ.get('ARABIC_PDF_FONT')
    if env_path:
        paths.append((env_path, os.environ.get('ARABIC_PDF_FONT_BOLD')))

    try:
        from flask import current_app

        app_root = current_app.root_path
        project_root = os.path.abspath(os.path.join(app_root, os.pardir))
        paths.extend([
            (
                os.path.join(app_root, 'static', 'fonts', 'Amiri-Regular.ttf'),
                os.path.join(app_root, 'static', 'fonts', 'Amiri-Bold.ttf'),
            ),
            (
                os.path.join(project_root, 'static', 'fonts', 'Amiri-Regular.ttf'),
                os.path.join(project_root, 'static', 'fonts', 'Amiri-Bold.ttf'),
            ),
        ])
    except RuntimeError:
        pass

    paths.extend([
        (r'C:\Windows\Fonts\arial.ttf', r'C:\Windows\Fonts\arialbd.ttf'),
        (r'C:\Windows\Fonts\tahoma.ttf', r'C:\Windows\Fonts\tahomabd.ttf'),
        ('/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf',
         '/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf'),
        ('/usr/share/fonts/opentype/noto/NotoNaskhArabic-Regular.ttf',
         '/usr/share/fonts/opentype/noto/NotoNaskhArabic-Bold.ttf'),
        ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
         '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
    ])
    return paths


def _register_arabic_fonts(pdfmetrics, TTFont):
    """Register Arabic-capable fonts under the names used by existing PDFs."""
    for regular_path, bold_path in _font_candidates():
        if not regular_path or not os.path.exists(regular_path):
            continue

        bold_path = bold_path if bold_path and os.path.exists(bold_path) else regular_path
        try:
            pdfmetrics.registerFont(TTFont('Amiri', regular_path))
            pdfmetrics.registerFont(TTFont('Amiri-Bold', bold_path))
            pdfmetrics.registerFont(TTFont('ArabicPDF', regular_path))
            pdfmetrics.registerFont(TTFont('ArabicPDF-Bold', bold_path))
            return True
        except Exception as exc:
            print(f"[PDF] Could not register Arabic font {regular_path}: {exc}")

    print("[PDF] No Arabic-capable TrueType font found for PDF output.")
    return False


def _shape_arabic_text(text):
    """Apply Arabic shaping and bidi display order for ReportLab."""
    if text is None:
        return ''
    text = str(text)
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


def generate_fee_receipt(installment, school_settings=None, print_date=None) -> bytes | None:
    """
    Generate a professional PDF receipt for a paid fee installment.
    Portrait orientation, top half of A4 page.
    Returns bytes or None if ReportLab unavailable.
    """
    if not _get_rl():
        return None

    from reportlab.lib.pagesizes import A4, portrait
    from reportlab.lib.units import cm, inch
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, Frame, PageTemplate
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from flask import current_app

    arabic_font_registered = _register_arabic_fonts(pdfmetrics, TTFont)

    # Set pagesize=A4 in portrait mode
    pagesize = portrait(A4)
    page_width, page_height = pagesize
    
    # Ensure all content stays within the top 400 points of the page to avoid page breaks
    # Use a single Frame and PageTemplate that doesn't overflow
    max_content_height = 400  # points - this ensures single page output
    frame = Frame(1*cm, page_height - max_content_height - 1*cm, 
                  page_width - 2*cm, max_content_height,
                  leftPadding=0, bottomPadding=0, rightPadding=0, topPadding=0)

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=pagesize, 
                           leftMargin=1*cm, rightMargin=1*cm,
                           topMargin=1*cm, bottomMargin=1*cm)
    doc.addPageTemplates([PageTemplate(frames=[frame])])

    styles = getSampleStyleSheet()
    
    # Create Arabic-aware styles with explicit font assignment
    # Only use Amiri font - no fallbacks to prevent square characters
    # Use dark grey (#444444) for professional appearance
    if arabic_font_registered:
        arabic_style = ParagraphStyle('arabic', fontName='Amiri', fontSize=10, alignment=2, textColor=colors.black)
        arabic_title = ParagraphStyle('arabic_title', fontName='Amiri', fontSize=14, alignment=1, textColor=colors.black)
        arabic_bold = ParagraphStyle('arabic_bold', fontName='Amiri-Bold', fontSize=10, alignment=2, textColor=colors.black)
        data_style = ParagraphStyle('data_cell', fontName='Amiri', fontSize=8, textColor=colors.black, alignment=0)
    else:
        # If Amiri font is not available, use default fonts but warn about Arabic issues
        print("WARNING: Amiri font not loaded. Arabic text will display as squares.")
        arabic_style = ParagraphStyle('arabic', fontSize=10, alignment=2, textColor=colors.black)
        arabic_title = ParagraphStyle('arabic_title', fontSize=14, alignment=1, textColor=colors.black)
        arabic_bold = ParagraphStyle('arabic_bold', fontSize=10, alignment=2, textColor=colors.black)
        data_style = ParagraphStyle('data_cell', fontSize=8, textColor=colors.black, alignment=0)

    elements = []

    # Header with school info and logo in side-by-side layout
    school_name_ar = school_settings.school_name_ar if school_settings and school_settings.school_name_ar else "المدرسة"
    school_name_en = school_settings.school_name if school_settings else "School"
    
    # Create header table with 3 columns: Arabic name (right), Logo (center), English name (left)
    header_data = []
    
    # Prepare the three elements for the header row
    arabic_name_element = None
    logo_element = None  
    english_name_element = None
    
    # Arabic school name (right side)
    arabic_name_element = Paragraph(_shape_arabic_text(school_name_ar), arabic_title)
    
    # Logo (center) — supports both local paths and Supabase/CDN URLs
    if school_settings and school_settings.logo_path:
        logo_path = _resolve_logo_for_pdf(school_settings.logo_path)
        if logo_path:
            try:
                logo_element = Image(logo_path, width=1.5*cm, height=1.5*cm)
                logo_element.hAlign = 'CENTER'
            except Exception as e:
                print(f"Error loading logo: {e}")
                logo_element = Paragraph("", arabic_title)  # Empty placeholder
    
    # English school name (left side) - use Amiri font for consistency
    if arabic_font_registered:
        english_style = ParagraphStyle('english_header', fontName='Amiri', fontSize=14, alignment=0, textColor=colors.black)
    else:
        english_style = ParagraphStyle('english_header', fontSize=14, alignment=0, textColor=colors.black)
    english_name_element = Paragraph(school_name_en, english_style)
    
    # If no logo, create empty placeholder
    if logo_element is None:
        logo_element = Paragraph("", arabic_title)
    
    # Create header table row
    header_data = [[arabic_name_element, logo_element, english_name_element]]
    
    # Create header table with equal column widths
    header_table = Table(header_data, colWidths=[5*cm, 2*cm, 5*cm])  # Arabic, Logo, English
    header_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (0, 0), 'RIGHT'),   # Arabic name right-aligned
        ('ALIGN', (1, 0), (1, 0), 'CENTER'),  # Logo centered
        ('ALIGN', (2, 0), (2, 0), 'LEFT'),    # English name left-aligned
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),  # All vertically centered
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    
    elements.append(header_table)
    elements.append(Spacer(1, 0.1*cm))  # Reduced top spacing after header

    # Combined receipt title as single centered paragraph
    receipt_title_combined = "إيصال رسوم دراسية - Fee Receipt"
    elements.append(Paragraph(_shape_arabic_text(receipt_title_combined), arabic_title))

    elements.append(Spacer(1, 0.4*cm))  # Reduced space after title before table

    # Receipt details table
    issue_date = print_date if print_date is not None else datetime.now().date()
    student = installment.fee_record.student
    fee_record = installment.fee_record
    
    # Calculate remaining balance after this payment
    total_paid = sum(float(i.received_amount or 0) for i in fee_record.installments)
    remaining = float(fee_record.net_amount) - total_paid
    
    def process_arabic_text(text):
        return _shape_arabic_text(text)
    
    def create_arabic_paragraph(text, style):
        """Create a Paragraph with Amiri font for all text (headers and labels)."""
        processed_text = process_arabic_text(text)
        if arabic_font_registered:
            # Always use Amiri font with black text
            arabic_style_with_font = ParagraphStyle('arabic_cell', 
                                                  fontName='Amiri', 
                                                  fontSize=style.fontSize if hasattr(style, 'fontSize') else 8,
                                                  alignment=style.alignment if hasattr(style, 'alignment') else 0,
                                                  textColor=colors.black)
            return Paragraph(processed_text, arabic_style_with_font)
        else:
            # Fallback style with black text
            return Paragraph(processed_text, style)
    
    def create_data_paragraph(text):
        """Create a data cell paragraph with Amiri font and black text."""
        processed_text = process_arabic_text(text)
        if arabic_font_registered:
            return Paragraph(processed_text, data_style)
        else:
            return Paragraph(processed_text, ParagraphStyle('data_cell', fontSize=8, textColor=colors.black, alignment=0))
    
    data = [
        [create_arabic_paragraph('رقم الإيصال / Receipt No', arabic_bold), create_data_paragraph(installment.receipt_no or '—')],
        [create_arabic_paragraph('اسم الطالب / Student Name', arabic_bold), create_data_paragraph(student.full_name)],
        [create_arabic_paragraph('رقم الطالب / Student ID', arabic_bold), create_data_paragraph(student.student_id)],
        [create_arabic_paragraph('نوع الرسم / Fee Type', arabic_bold), create_data_paragraph(fee_record.fee_type.name)],
        [create_arabic_paragraph('القسط / Installment', arabic_bold), create_data_paragraph(f"#{installment.installment_no}")],
        [create_arabic_paragraph('المبلغ المدفوع / Amount Paid', arabic_bold), create_data_paragraph(f"{float(installment.received_amount):,.2f} {school_settings.currency_symbol if school_settings else 'د.ع'}")],
        [create_arabic_paragraph('المبلغ المتبقي / Remaining Balance', arabic_bold), create_data_paragraph(f"{remaining:,.2f} {school_settings.currency_symbol if school_settings else 'د.ع'}")],
        [create_arabic_paragraph('تاريخ الاستحقاق / Due Date', arabic_bold), create_data_paragraph(installment.due_date.strftime('%Y-%m-%d') if installment.due_date else '—')],
        [create_arabic_paragraph('تاريخ إصدار الوصل / Receipt Issue Date', arabic_bold), create_data_paragraph(issue_date.strftime('%Y-%m-%d'))],
        [create_arabic_paragraph('طريقة الدفع / Payment Method', arabic_bold), create_data_paragraph({
            'cash': 'نقداً / Cash',
            'transfer': 'تحويل بنكي / Bank Transfer',
            'cheque': 'شيك / Cheque',
            'card': 'بطاقة / Card'
        }.get(installment.payment_method, installment.payment_method or '—'))],
    ]

    tbl = Table(data, colWidths=[7*cm, 8*cm])
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), HexColor("#c0c0c0")),  # Dark grey header
        ('BACKGROUND', (1, 0), (-1, -1), HexColor("#c0c0c0")),  # Clean white data cells
        ('GRID', (0, 0), (-1, -1), 1, HexColor("#000000")),  # Clean grey grid lines
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    elements.append(tbl)
    elements.append(Spacer(1, 0.3*cm))  # Reduced spacer

    # Signature section
    elements.append(Spacer(1, 0.5*cm))  # Reduced spacer
    signature_data = [
        [create_arabic_paragraph('توقيع المستلم / Received By:', arabic_style), Paragraph('________________________', ParagraphStyle('sig_line', fontSize=8, textColor=colors.black)), create_arabic_paragraph('ختم المدرسة / School Stamp:', arabic_style), Paragraph('________________________', ParagraphStyle('sig_line', fontSize=8, textColor=colors.black))]
    ]
    signature_table = Table(signature_data, colWidths=[2.5*cm, 4*cm, 2.5*cm, 4*cm])  # Adjusted widths
    signature_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 0), (-1, -1), 8),  # Smaller font
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 15),  # Reduced padding
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),  # Black text
    ]))
    elements.append(signature_table)

    # Footer
    footer_text = school_settings.receipt_footer if school_settings and school_settings.receipt_footer else f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC | {school_name_en} System"
    
    # Create footer paragraph with Amiri font
    if arabic_font_registered:
        footer_style_arabic = ParagraphStyle('footer_arabic', fontName='Amiri', fontSize=7, textColor=HexColor('#666666'), alignment=1)
        elements.append(Spacer(1, 0.2*cm))
        elements.append(Paragraph(process_arabic_text(footer_text), footer_style_arabic))
    else:
        footer_style = ParagraphStyle('footer', fontSize=7, textColor=HexColor('#666666'), alignment=1)
        elements.append(Spacer(1, 0.2*cm))
        elements.append(Paragraph(footer_text, footer_style))

    doc.build(elements)
    return buf.getvalue()


def generate_schedule_pdf(section, entries, days, school=None) -> bytes | None:
    """
    Phase 4 — printable weekly schedule for a section.

    Args:
        section : Section model row (with .name and .grade.name)
        entries : flat list of Schedule rows for this section
        days    : list of day labels, indexed 0..6 (0 = Sunday)
        school  : optional SchoolSettings row for header white-label
    Returns:
        PDF bytes, or None if ReportLab is not installed.
    """
    if not _get_rl():
        return None

    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    arabic_font_registered = _register_arabic_fonts(pdfmetrics, TTFont)

    if not arabic_font_registered:
        return generate_error_pdf(
            "خطأ في تحميل الخط العربي",
            "يرجى التحقق من وجود ملفات الخط العربي في مجلد static/fonts/"
        )

    def process_arabic_text(text):
        """Process Arabic text for proper display in PDF."""
        return _shape_arabic_text(text)

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                             rightMargin=1.0*cm, leftMargin=1.0*cm,
                             topMargin=0.4*cm, bottomMargin=1.0*cm)

    elements = []

    school_name = (school.school_name if school and school.school_name else 'Mecha-School')
    school_name_ar = (school.school_name_ar if school and school.school_name_ar else '')

    # ════════════════════════════════════════════════════════════════════════
    # PARAGRAPH STYLE ENFORCEMENT: Use same approach as fee receipts
    # ════════════════════════════════════════════════════════════════════════
    # Use Amiri for all Arabic text, never fall back to Helvetica for Arabic content
    if arabic_font_registered:
        title_s = ParagraphStyle(
            'title',
            fontSize=17,
            fontName='Amiri',
            alignment=1,
            spaceBefore=0,
            spaceAfter=12,
            textColor=colors.HexColor("#b1c1d1")
        )
        
        sub_s = ParagraphStyle(
            'sub',
            fontSize=11,
            fontName='Amiri' if arabic_font_registered else 'Helvetica',
            alignment=1,
            spaceBefore=0,
            spaceAfter=18,
            textColor=colors.HexColor('#6b7a8d')
        )

        # ENFORCED: Use Amiri for all Arabic content
        empty_style = ParagraphStyle(
            'empty',
            fontSize=13,
            alignment=1,
            textColor=colors.HexColor('#9aabb8'),
            fontName='Amiri'
        )
        
        header_cell_style = ParagraphStyle(
            'hdr_cell',
            fontName='Amiri-Bold',
            alignment=1,
            fontSize=10,
            textColor=colors.white
        )
        
        cell_style = ParagraphStyle(
            'cell',
            fontSize=9,
            alignment=1,
            fontName='Amiri'
        )
        
        time_style = ParagraphStyle(
            'time',
            fontSize=9,
            alignment=1,
            fontName='Helvetica-Bold'
        )
        
        footer_s = ParagraphStyle(
            'footer',
            fontSize=9,
            alignment=1,
            textColor=colors.HexColor('#9aabb8'),
            fontName='Amiri'
        )
    else:
        # Fallback styles if Amiri font is not available
        print("WARNING: Amiri font not loaded. Arabic text will display as squares.")
        title_s = ParagraphStyle('title', fontSize=17, alignment=1, spaceAfter=4, textColor=colors.HexColor("#b1c1d1"))
        sub_s = ParagraphStyle('sub', fontSize=11, alignment=1, spaceAfter=14, textColor=colors.HexColor('#6b7a8d'))
        empty_style = ParagraphStyle('empty', fontSize=13, alignment=1, textColor=colors.HexColor('#9aabb8'))
        header_cell_style = ParagraphStyle('hdr_cell', alignment=1, fontSize=10, textColor=colors.white)
        cell_style = ParagraphStyle('cell', fontSize=9, alignment=1)
        time_style = ParagraphStyle('time', fontSize=9, alignment=1, fontName='Helvetica-Bold')
        footer_s = ParagraphStyle('footer', fontSize=9, alignment=1, textColor=colors.HexColor('#9aabb8'))
        footer_s = ParagraphStyle('footer', fontSize=9, alignment=1, textColor=colors.HexColor('#9aabb8'))

    header_line = process_arabic_text(school_name_ar) if school_name_ar else school_name
    elements.append(Paragraph(header_line, title_s))
    elements.append(Paragraph(process_arabic_text('الجدول الأسبوعي'), sub_s))
    elements.append(Spacer(1, 0.4*cm))

    # ── Build time-slot axis (rows = distinct start_times) ──────────────────
    # Always 5 days: Sun-Thu
    day_indices = list(range(5))  # 0=Sun, 1=Mon, 2=Tue, 3=Wed, 4=Thu
    day_labels = [process_arabic_text(day) for day in days]

    # Unique start_times sorted
    slot_keys = sorted({(e.start_time, e.end_time) for e in entries},
                        key=lambda x: x[0])

    if not slot_keys:
        empty_text = process_arabic_text("لا توجد حصص مسجّلة لهذه الشعبة.")
        elements.append(Paragraph(empty_text, empty_style))
    else:
        # Header row: time col + 5 day cols
        header = [Paragraph(process_arabic_text('الوقت'), header_cell_style)]
        for day in day_labels:
            header.append(Paragraph(day, header_cell_style))
        data = [header]

        for (st, et) in slot_keys:
            label = f"{st.strftime('%H:%M')} — {et.strftime('%H:%M')}"
            row = [Paragraph(label, time_style)]
            for d in day_indices:
                cell_entries = [e for e in entries
                                if e.day_of_week == d
                                and e.start_time == st
                                and e.end_time   == et]
                if cell_entries:
                    e = cell_entries[0]
                    parts = [process_arabic_text(e.subject.name)]
                    if e.teacher:
                        parts.append(process_arabic_text(e.teacher.full_name))
                    if e.room:
                        parts.append(f"Room: {e.room}")
                    cell_text = '<br/>'.join(parts)
                    row.append(Paragraph(cell_text, cell_style))
                else:
                    row.append(Paragraph('—', cell_style))
            data.append(row)

        # Column widths: 3cm for time, rest split evenly among 5 days
        avail_w = 25.0  # cm in landscape A4 minus margins
        day_w   = (avail_w - 3.0) / 5.0
        col_widths = [3.0*cm] + [day_w*cm] * 5

        tbl = Table(data, colWidths=col_widths, repeatRows=1)
        tbl.setStyle(TableStyle([
            # Header
            ('BACKGROUND',  (0, 0), (-1, 0), colors.HexColor("#1e3a5c")),
            ('TEXTCOLOR',   (0, 0), (-1, 0), colors.white),
            ('FONTNAME',    (0, 0), (-1, 0), 'Amiri-Bold' if arabic_font_registered else 'Helvetica-Bold'),
            ('FONTSIZE',    (0, 0), (-1, 0), 11),
            ('ALIGN',       (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN',      (0, 0), (-1, -1), 'MIDDLE'),
            # Body
            ('BACKGROUND',  (0, 1), (0, -1), colors.HexColor("#f1f5f9")),
            ('ROWBACKGROUNDS', (1, 1), (-1, -1),
                                [colors.white, colors.HexColor("#f8fafc")]),
            ('GRID',        (0, 0), (-1, -1), 0.4, colors.HexColor('#d1d9e6')),
            ('TOPPADDING',  (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING',  (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ]))
        elements.append(tbl)

    elements.append(Spacer(1, 0.8*cm))

    # ── Footer ──────────────────────────────────────────────────────────────
    footer_bits = [
        f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
        process_arabic_text(school_name) if school_name else "Mecha-School",
    ]
    if school and school.phone:
        footer_bits.append(process_arabic_text(school.phone))
    if school and school.website:
        footer_bits.append(school.website)
    footer_text = '  |  '.join(footer_bits)

    elements.append(Paragraph(footer_text, footer_s))

    if school and school.receipt_footer:
        footer_receipt_s = ParagraphStyle(
            'footer_receipt',
            fontSize=9,
            alignment=1,
            textColor=colors.HexColor('#9aabb8'),
            fontName='Amiri'  # ENFORCED: Always use Amiri for Arabic
        )
        elements.append(Paragraph(process_arabic_text(school.receipt_footer), footer_receipt_s))

    doc.build(elements)
    return buf.getvalue()


def generate_salary_pdf(record) -> bytes | None:
    """
    Generate a PDF salary slip.
    Returns bytes or None if ReportLab unavailable.
    """
    if not _get_rl():
        return None

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    ARABIC_MONTHS = ['', 'January', 'February', 'March', 'April', 'May', 'June',
                     'July', 'August', 'September', 'October', 'November', 'December']

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                             rightMargin=2*cm, leftMargin=2*cm,
                             topMargin=2*cm, bottomMargin=2*cm)

    styles   = getSampleStyleSheet()
    elements = []

    title_s = ParagraphStyle('title', fontSize=16, fontName='Helvetica-Bold',
                               spaceAfter=4, alignment=1)
    sub_s   = ParagraphStyle('sub',   fontSize=11, spaceAfter=16, alignment=1,
                               textColor=colors.HexColor('#6b7a8d'))

    elements.append(Paragraph("Al-Muhandis School — Salary Slip", title_s))
    elements.append(Paragraph(
        f"{ARABIC_MONTHS[record.month]} {record.year}", sub_s))

    # Employee info
    emp = record.employee
    emp_data = [
        ['Employee',   emp.full_name],
        ['Employee ID', emp.employee_id],
        ['Job Title',  emp.job_title],
        ['Department', emp.department or '—'],
    ]
    emp_tbl = Table(emp_data, colWidths=[5*cm, 10*cm])
    emp_tbl.setStyle(TableStyle([
        ('BACKGROUND',  (0,0),(0,-1), colors.HexColor('#0f2540')),
        ('TEXTCOLOR',   (0,0),(0,-1), colors.white),
        ('FONTNAME',    (0,0),(-1,-1), 'Helvetica'),
        ('FONTSIZE',    (0,0),(-1,-1), 10),
        ('FONTNAME',    (0,0),(0,-1), 'Helvetica-Bold'),
        ('ROWBACKGROUNDS',(1,0),(-1,-1),[colors.white, colors.HexColor('#f8fbff')]),
        ('GRID',        (0,0),(-1,-1), 0.5, colors.HexColor('#dde3ec')),
        ('TOPPADDING',  (0,0),(-1,-1), 7),
        ('BOTTOMPADDING',(0,0),(-1,-1), 7),
        ('LEFTPADDING', (0,0),(-1,-1), 10),
    ]))
    elements.append(emp_tbl)
    elements.append(Spacer(1, 0.6*cm))

    # Salary breakdown
    sal_data = [
        ['Component', 'Amount (IQD)'],
        ['Base Salary',  f"{float(record.base_salary):,.2f}"],
        ['Allowances',  f"+{float(record.allowances):,.2f}"],
        ['Deductions',  f"-{float(record.deductions):,.2f}"],
        ['NET SALARY',  f"{float(record.net_salary):,.2f}"],
    ]
    sal_tbl = Table(sal_data, colWidths=[8*cm, 7*cm])
    sal_tbl.setStyle(TableStyle([
        ('BACKGROUND',  (0,0),(-1,0), colors.HexColor("#aebcca")),
        ('TEXTCOLOR',   (0,0),(-1,0), colors.white),
        ('FONTNAME',    (0,0),(-1,0), 'Helvetica-Bold'),
        ('FONTNAME',    (0,1),(-1,-2), 'Helvetica'),
        ('FONTNAME',    (0,-1),(-1,-1), 'Helvetica-Bold'),
        ('FONTSIZE',    (0,0),(-1,-1), 11),
        ('FONTSIZE',    (0,-1),(-1,-1), 13),
        ('BACKGROUND',  (0,-1),(-1,-1), colors.HexColor('#e8f8f0')),
        ('TEXTCOLOR',   (0,-1),(-1,-1), colors.HexColor('#1aab6d')),
        ('ROWBACKGROUNDS',(0,1),(-1,-2),[colors.white, colors.HexColor('#f8fbff')]),
        ('GRID',        (0,0),(-1,-1), 0.5, colors.HexColor('#dde3ec')),
        ('ALIGN',       (1,0),(-1,-1), 'RIGHT'),
        ('TOPPADDING',  (0,0),(-1,-1), 9),
        ('BOTTOMPADDING',(0,0),(-1,-1), 9),
        ('LEFTPADDING', (0,0),(-1,-1), 12),
        ('RIGHTPADDING',(0,0),(-1,-1), 12),
    ]))
    elements.append(sal_tbl)
    elements.append(Spacer(1, 0.8*cm))

    # Status + date
    status_s = ParagraphStyle('status', fontSize=10, alignment=1,
                               textColor=colors.HexColor('#9aabb8'))
    paid_txt = f"Status: {'PAID' if record.status=='paid' else 'PENDING'}"
    if record.paid_date:
        paid_txt += f"  |  Paid: {record.paid_date.strftime('%Y-%m-%d')}"
    elements.append(Paragraph(paid_txt, status_s))
    elements.append(Paragraph(
        f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC | Al-Muhandis System",
        status_s))

    doc.build(elements)
    return buf.getvalue()


def generate_error_pdf(title, message) -> bytes | None:
    """
    Generate a simple error PDF when font loading fails.
    Returns bytes or None if ReportLab unavailable.
    """
    if not _get_rl():
        return None

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                             rightMargin=2*cm, leftMargin=2*cm,
                             topMargin=2*cm, bottomMargin=2*cm)

    styles = getSampleStyleSheet()
    elements = []

    # Error title
    title_s = ParagraphStyle('error_title', fontSize=18, fontName='Helvetica-Bold',
                              spaceAfter=12, alignment=1, textColor=colors.red)
    elements.append(Paragraph(title, title_s))
    elements.append(Spacer(1, 1*cm))

    # Error message
    message_s = ParagraphStyle('error_message', fontSize=12, alignment=1,
                                textColor=colors.HexColor('#6b7a8d'))
    elements.append(Paragraph(message, message_s))
    elements.append(Spacer(1, 1*cm))

    # Footer
    footer_s = ParagraphStyle('footer', fontSize=9, alignment=1,
                               textColor=colors.HexColor('#9aabb8'))
    elements.append(Paragraph(
        f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC | Al-Muhandis System",
        footer_s))

    doc.build(elements)
    return buf.getvalue()


# ─── EMPLOYEE ATTENDANCE PDF ──────────────────────────────────────────────────

def generate_employee_attendance_pdf(rows, date_from: str, date_to: str,
                                     school=None) -> bytes | None:
    """
    Generate a professional Arabic RTL PDF summary for employee attendance.
    rows: list of stat dicts from get_employees_attendance_summary().
    Returns bytes or None if ReportLab is unavailable.
    """
    if not _get_rl():
        return None

    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    arabic_font_registered = _register_arabic_fonts(pdfmetrics, TTFont)
    fn = 'Amiri' if arabic_font_registered else 'Helvetica'
    fn_b = 'Amiri-Bold' if arabic_font_registered else 'Helvetica-Bold'

    ar = _shape_arabic_text
    HEADER_BG = HexColor('#1a3a5c')
    ALT_BG = HexColor('#f0f4f8')
    WHITE = colors.white

    buf_pdf = BytesIO()
    doc = SimpleDocTemplate(buf_pdf, pagesize=landscape(A4),
                            leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                            topMargin=1.5 * cm, bottomMargin=1.5 * cm)

    title_s = ParagraphStyle('t2', fontName=fn_b, fontSize=14,
                              alignment=1, textColor=HexColor('#1a3a5c'))
    sub_s = ParagraphStyle('s2', fontName=fn, fontSize=10,
                            alignment=1, textColor=HexColor('#555555'))

    elements = []
    school_name = (school.school_name_ar or school.school_name) if school else ''
    if school_name:
        elements.append(Paragraph(ar(school_name), title_s))
        elements.append(Spacer(1, 0.2 * cm))
    elements.append(Paragraph(ar('تقرير حضور الموظفين'), title_s))
    elements.append(Paragraph(ar(f'{date_from}  —  {date_to}'), sub_s))
    elements.append(Spacer(1, 0.6 * cm))

    col_headers = ['نسبة الحضور', 'انصراف', 'غائب', 'متأخر', 'حاضر',
                   'أيام العمل', 'المسمى الوظيفي', 'القسم', 'اسم الموظف', '#']
    col_widths = [2.8*cm, 2*cm, 1.8*cm, 1.8*cm, 1.8*cm,
                  2.2*cm, 3.5*cm, 3*cm, 4.5*cm, 1*cm]

    th_s = ParagraphStyle('th2', fontName=fn_b, fontSize=8, alignment=1, textColor=WHITE)
    td_s = ParagraphStyle('td2', fontName=fn, fontSize=8, alignment=1)

    table_data = [[Paragraph(ar(h), th_s) for h in col_headers]]
    row_alt = []
    for i, row in enumerate(rows, 1):
        emp = row['employee']
        table_data.append([
            Paragraph(ar(f"{row['rate']}%"), td_s),
            Paragraph(str(row['checked_out']), td_s),
            Paragraph(str(row['absent']), td_s),
            Paragraph(str(row['late']), td_s),
            Paragraph(str(row['present']), td_s),
            Paragraph(str(row['working_days']), td_s),
            Paragraph(ar(emp.job_title or '—'), td_s),
            Paragraph(ar(emp.department or '—'), td_s),
            Paragraph(ar(emp.full_name), td_s),
            Paragraph(str(i), td_s),
        ])
        if i % 2 == 0:
            row_alt.append(i)

    style_cmds = [
        ('BACKGROUND', (0, 0), (-1, 0), HEADER_BG),
        ('FONTNAME', (0, 0), (-1, -1), fn),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.3, HexColor('#cccccc')),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]
    for ri in row_alt:
        style_cmds.append(('BACKGROUND', (0, ri), (-1, ri), ALT_BG))

    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle(style_cmds))
    elements.append(tbl)

    elements.append(Spacer(1, 0.4 * cm))
    footer_s2 = ParagraphStyle('f2', fontName=fn, fontSize=8,
                                alignment=1, textColor=HexColor('#9aabb8'))
    elements.append(Paragraph(
        ar(f'تم الإنشاء: {datetime.utcnow().strftime("%Y-%m-%d %H:%M")} | نظام المهندس'),
        footer_s2))

    doc.build(elements)
    return buf_pdf.getvalue()


def generate_single_employee_attendance_pdf(emp_row, date_from: str, date_to: str,
                                             school=None) -> bytes | None:
    """
    Generate a detailed PDF for one employee (day-by-day breakdown).
    emp_row: stat dict from calculate_employee_stats().
    """
    if not _get_rl():
        return None

    from reportlab.lib.pagesizes import A4, portrait
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    arabic_font_registered = _register_arabic_fonts(pdfmetrics, TTFont)
    fn = 'Amiri' if arabic_font_registered else 'Helvetica'
    fn_b = 'Amiri-Bold' if arabic_font_registered else 'Helvetica-Bold'
    ar = _shape_arabic_text
    emp = emp_row['employee']

    HEADER_BG = HexColor('#1a3a5c')
    ALT_BG = HexColor('#f0f4f8')
    ABSENT_BG = HexColor('#ffe0e0')
    LATE_BG = HexColor('#fff3cd')
    WHITE = colors.white

    buf_emp = BytesIO()
    doc = SimpleDocTemplate(buf_emp, pagesize=portrait(A4),
                            leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                            topMargin=1.5 * cm, bottomMargin=1.5 * cm)

    title_s = ParagraphStyle('te', fontName=fn_b, fontSize=13, alignment=1,
                              textColor=HexColor('#1a3a5c'))
    sub_s = ParagraphStyle('se', fontName=fn, fontSize=10, alignment=1,
                            textColor=HexColor('#555555'))
    th_s = ParagraphStyle('the', fontName=fn_b, fontSize=8, alignment=1, textColor=WHITE)
    td_s = ParagraphStyle('tde', fontName=fn, fontSize=8, alignment=1)

    elements = []
    school_name = (school.school_name_ar or school.school_name) if school else ''
    if school_name:
        elements.append(Paragraph(ar(school_name), title_s))
        elements.append(Spacer(1, 0.2 * cm))
    elements.append(Paragraph(ar(emp.full_name), title_s))
    elements.append(Paragraph(ar(f"{emp.department or '—'} | {emp.job_title or '—'}"), sub_s))
    elements.append(Paragraph(ar(f'{date_from}  —  {date_to}'), sub_s))
    elements.append(Spacer(1, 0.5 * cm))

    # Summary row
    summary_data = [
        [Paragraph(ar(h), ParagraphStyle('ssh', fontName=fn_b, fontSize=9,
                                          alignment=1, textColor=WHITE))
         for h in ['أيام العمل', 'حاضر', 'متأخر', 'غائب', 'انصراف', 'نسبة الحضور']],
        [Paragraph(ar(str(v)), ParagraphStyle('ssv', fontName=fn_b, fontSize=11,
                                               alignment=1, textColor=HexColor('#1a3a5c')))
         for v in [emp_row['working_days'], emp_row['present'], emp_row['late'],
                   emp_row['absent'], emp_row['checked_out'], f"{emp_row['rate']}%"]],
    ]
    s_tbl = Table(summary_data, colWidths=[2.8*cm]*6)
    s_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), HEADER_BG),
        ('FONTNAME', (0, 0), (-1, -1), fn),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.3, HexColor('#cccccc')),
    ]))
    elements.append(s_tbl)
    elements.append(Spacer(1, 0.5 * cm))

    STATUS_AR = {'present': 'حاضر', 'absent': 'غائب', 'late': 'متأخر'}
    d_headers = ['ملاحظات', 'الجهاز', 'المصدر', 'الحالة',
                 'وقت الانصراف', 'وقت الحضور', 'التاريخ', '#']
    d_widths = [3*cm, 2.5*cm, 2*cm, 2*cm, 2.5*cm, 2.5*cm, 2.8*cm, 1*cm]

    detail_data = [[Paragraph(ar(h), th_s) for h in d_headers]]
    row_bgs = []
    for i, day in enumerate(emp_row.get('daily', []), 1):
        status = day.get('status', '')
        dev = day.get('device')
        detail_data.append([
            Paragraph(ar(day.get('notes') or '—'), td_s),
            Paragraph(ar(dev.name if dev else '—'), td_s),
            Paragraph(ar(day.get('source') or '—'), td_s),
            Paragraph(ar(STATUS_AR.get(status, status)), td_s),
            Paragraph(day['check_out'].strftime('%H:%M') if day.get('check_out') else '—', td_s),
            Paragraph(day['check_in'].strftime('%H:%M') if day.get('check_in') else '—', td_s),
            Paragraph(day['date'].strftime('%Y-%m-%d') if day.get('date') else '', td_s),
            Paragraph(str(i), td_s),
        ])
        if status == 'absent':
            row_bgs.append((i, ABSENT_BG))
        elif status == 'late':
            row_bgs.append((i, LATE_BG))
        elif i % 2 == 0:
            row_bgs.append((i, ALT_BG))

    sc = [
        ('BACKGROUND', (0, 0), (-1, 0), HEADER_BG),
        ('FONTNAME', (0, 0), (-1, -1), fn),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.3, HexColor('#cccccc')),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]
    for ri, bg in row_bgs:
        sc.append(('BACKGROUND', (0, ri), (-1, ri), bg))

    d_tbl = Table(detail_data, colWidths=d_widths, repeatRows=1)
    d_tbl.setStyle(TableStyle(sc))
    elements.append(d_tbl)

    doc.build(elements)
    return buf_emp.getvalue()


# ─── STUDENT REGISTRATION RECORD (سجل قيد الطالب) — A3 Landscape ────────────

def generate_registration_record_pdf(record, school=None) -> bytes | None:
    """
    Generate an official A3-landscape student registration record PDF.
    record : StudentRegistrationRecord model instance.
    school : School model instance (for logo / name override).
    Returns bytes or None if ReportLab is unavailable.
    """
    if not _get_rl():
        return None

    from reportlab.lib.pagesizes import A3, landscape
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                     Paragraph, Spacer, Image, HRFlowable)
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    arabic_font_registered = _register_arabic_fonts(pdfmetrics, TTFont)
    fn   = 'Amiri'      if arabic_font_registered else 'Helvetica'
    fn_b = 'Amiri-Bold' if arabic_font_registered else 'Helvetica-Bold'
    ar   = _shape_arabic_text

    NAVY   = HexColor('#1a3a5c')
    LTBLUE = HexColor('#e8f0f8')
    GREY   = HexColor('#f5f5f5')
    WHITE  = colors.white
    BLACK  = colors.black
    BORDER = HexColor('#b0b8c4')

    buf = BytesIO()
    page_size = landscape(A3)
    pw, _  = page_size  # ~1190 × 842 pt

    doc = SimpleDocTemplate(
        buf, pagesize=page_size,
        leftMargin=1.2*cm, rightMargin=1.2*cm,
        topMargin=1.2*cm, bottomMargin=1.2*cm,
    )

    # ── Styles ──────────────────────────────────────────────────────────────
    def style(name, **kw):
        return ParagraphStyle(name, fontName=kw.pop('fontName', fn),
                               fontSize=kw.pop('fontSize', 9), **kw)

    title_s   = style('title',   fontName=fn_b, fontSize=18, alignment=1, textColor=NAVY)
    sub_s     = style('sub',     fontName=fn,   fontSize=10, alignment=1,
                      textColor=HexColor('#555555'))
    sec_s     = style('sec',     fontName=fn_b, fontSize=11, textColor=WHITE, alignment=1)
    lbl_s     = style('lbl',     fontName=fn_b, fontSize=8,  textColor=HexColor('#444444'),
                      alignment=2)
    val_s     = style('val',     fontName=fn,   fontSize=9,  textColor=BLACK, alignment=2)
    hdr_s     = style('hdr',     fontName=fn_b, fontSize=8,  textColor=WHITE, alignment=1)
    cell_s    = style('cell',    fontName=fn,   fontSize=8,  textColor=BLACK, alignment=1)
    foot_s    = style('foot',    fontName=fn,   fontSize=7,  textColor=HexColor('#888888'),
                      alignment=1)


    def p(text, s=val_s):
        return Paragraph(ar(str(text or '—')), s)

    avail_w = pw - 2.4*cm  # total usable width

    elements = []

    # ── 1. HEADER ────────────────────────────────────────────────────────────
    school_ar = ar((school.school_name_ar if school and school.school_name_ar
                    else (record.snap_school_name_ar or record.snap_school_name or 'المدرسة')))
    school_en = school.school_name if school else record.snap_school_name or ''

    logo_el = None
    if school and school.logo_path:
        lp = _resolve_logo_for_pdf(school.logo_path)
        if lp:
            try:
                logo_el = Image(lp, width=2.2*cm, height=2.2*cm)
                logo_el.hAlign = 'CENTER'
            except Exception:
                pass

    header_data = [[
        Paragraph(school_ar, style('ha', fontName=fn_b, fontSize=14,
                                   alignment=2, textColor=NAVY)),
        logo_el or Paragraph('', sub_s),
        Paragraph(school_en, style('he', fontName=fn_b, fontSize=14,
                                   alignment=0, textColor=NAVY)),
    ]]
    header_tbl = Table(header_data, colWidths=[avail_w*0.42, avail_w*0.16, avail_w*0.42])
    header_tbl.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('ALIGN',  (0,0), (0,0),  'RIGHT'),
        ('ALIGN',  (1,0), (1,0),  'CENTER'),
        ('ALIGN',  (2,0), (2,0),  'LEFT'),
        ('TOPPADDING',    (0,0), (-1,-1), 0),
        ('BOTTOMPADDING', (0,0), (-1,-1), 0),
    ]))
    elements.append(header_tbl)
    elements.append(Spacer(1, 0.25*cm))
    elements.append(Paragraph(ar('سجل قيد الطالب'), title_s))
    elements.append(Spacer(1, 0.15*cm))
    elements.append(HRFlowable(width='100%', thickness=2, color=NAVY, spaceAfter=0.2*cm))

    # ── Helper: section heading ───────────────────────────────────────────────
    def section_heading(arabic_text):
        tbl = Table([[p(arabic_text, sec_s)]],
                    colWidths=[avail_w])
        tbl.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), NAVY),
            ('TOPPADDING', (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('LEFTPADDING', (0,0), (-1,-1), 6),
        ]))
        elements.append(tbl)
        elements.append(Spacer(1, 0.1*cm))

    # ── Helper: info grid (pairs) ────────────────────────────────────────────
    def info_row(pairs, col_widths=None):
        """pairs = [(label, value), ...] — max 4 per row."""
        n = len(pairs)
        cws = col_widths or ([avail_w / (n * 2)] * (n * 2))
        row_lbl = [p(lbl, lbl_s) for lbl, _ in pairs]
        row_val = [p(val, val_s) for _, val in pairs]
        tbl = Table([row_lbl, row_val], colWidths=cws)
        tbl.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), LTBLUE),
            ('GRID', (0,0), (-1,-1), 0.4, BORDER),
            ('ALIGN', (0,0), (-1,-1), 'RIGHT'),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('TOPPADDING', (0,0), (-1,-1), 3),
            ('BOTTOMPADDING', (0,0), (-1,-1), 3),
            ('LEFTPADDING', (0,0), (-1,-1), 5),
            ('RIGHTPADDING', (0,0), (-1,-1), 5),
        ]))
        elements.append(tbl)
        elements.append(Spacer(1, 0.1*cm))

    # ── 2. STUDENT DATA ──────────────────────────────────────────────────────
    section_heading('بيانات الطالب')

    gender_ar = {'male': 'ذكر', 'female': 'أنثى'}.get(record.snap_gender or '', record.snap_gender or '')
    status_ar = {'active': 'نشط', 'archived': 'محفوظ', 'inactive': 'غير نشط'}.get(
        record.snap_status or '', record.snap_status or '')

    info_row([('اسم الطالب',  record.snap_full_name),
              ('رقم الطالب',  record.snap_student_number),
              ('الجنس',        gender_ar),
              ('تاريخ الميلاد', record.snap_date_of_birth)])
    info_row([('الجنسية',      record.snap_nationality),
              ('هاتف الطالب',  record.snap_phone),
              ('الحالة',       status_ar),
              ('تاريخ التسجيل', record.snap_enrollment_date)])
    info_row([('العنوان',     record.snap_address or ''),
              ('',            '')],
             col_widths=[avail_w*0.12, avail_w*0.38,
                         avail_w*0.12, avail_w*0.38])

    # ── 3. GUARDIAN DATA ─────────────────────────────────────────────────────
    section_heading('بيانات ولي الأمر')
    info_row([('اسم ولي الأمر',  record.snap_guardian_name),
              ('صلة القرابة',   record.snap_guardian_relation),
              ('الهاتف',         record.snap_guardian_phone),
              ('البريد',         record.snap_guardian_email)])
    info_row([('عنوان ولي الأمر', record.snap_guardian_address or ''),
              ('',               '')],
             col_widths=[avail_w*0.12, avail_w*0.38,
                         avail_w*0.12, avail_w*0.38])

    # ── 4. ADMISSION DATA ────────────────────────────────────────────────────
    section_heading('بيانات القبول والتسجيل')
    info_row([('المدرسة',      record.snap_school_name_ar or record.snap_school_name),
              ('العام الدراسي', record.snap_year_name),
              ('المرحلة',      record.snap_stage),
              ('الصف / الشعبة',
               f"{record.snap_grade_name or ''} / {record.snap_section_name or ''}")])
    info_row([('تاريخ القبول',  record.admission_date),
              ('رقم الوثيقة',  record.document_number),
              ('المدرسة السابقة', record.previous_school),
              ('سبب التحويل',  record.transfer_reason)])

    # ── 5. DOCUMENTS CHECKLIST ───────────────────────────────────────────────
    section_heading('الوثائق المطلوبة')

    def chk(flag, label):
        mark = '✔' if flag else '✘'
        clr  = HexColor('#1a7a1a') if flag else HexColor('#cc0000')
        return Paragraph(f'{ar(label)}  {mark}',
                         style('ck', fontName=fn, fontSize=10,
                                textColor=clr, alignment=1))

    docs_row = [[chk(record.has_birth_cert,       'شهادة الميلاد'),
                 chk(record.has_id_card,           'بطاقة الهوية'),
                 chk(record.has_prev_certificate,  'شهادة المدرسة السابقة'),
                 chk(record.has_photo,             'صورة شخصية')]]
    docs_tbl = Table(docs_row, colWidths=[avail_w/4]*4)
    docs_tbl.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 0.4, BORDER),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('BACKGROUND', (0,0), (-1,-1), GREY),
    ]))
    elements.append(docs_tbl)
    elements.append(Spacer(1, 0.1*cm))

    if record.document_notes:
        info_row([('ملاحظات الوثائق', record.document_notes), ('', '')],
                 col_widths=[avail_w*0.12, avail_w*0.38,
                             avail_w*0.12, avail_w*0.38])

    # ── 6. ACADEMIC HISTORY TABLE ────────────────────────────────────────────
    section_heading('سجل السنوات الدراسية')

    hist = record.academic_history
    ah_headers = ['العام الدراسي', 'الصف', 'الشعبة', 'النتيجة',
                  'المعدل', 'الدور', 'الحالة', 'الملاحظات']
    ah_cws = [avail_w * f for f in [0.15, 0.12, 0.10, 0.12,
                                     0.10, 0.10, 0.12, 0.19]]

    ah_data = [[p(h, hdr_s) for h in ah_headers]]
    if hist:
        for row in hist:
            ah_data.append([
                p(row.get('year', '')),
                p(row.get('grade', '')),
                p(row.get('section', '')),
                p(row.get('result', '')),
                p(row.get('gpa', '')),
                p(row.get('round', '')),
                p(row.get('status', '')),
                p(row.get('notes', '')),
            ])
    else:
        # Empty rows for manual filling
        for _ in range(4):
            ah_data.append([Paragraph('', cell_s)] * 8)

    ah_tbl = Table(ah_data, colWidths=ah_cws, repeatRows=1)
    ah_style = [
        ('BACKGROUND', (0,0), (-1,0), NAVY),
        ('TEXTCOLOR',  (0,0), (-1,0), WHITE),
        ('GRID', (0,0), (-1,-1), 0.4, BORDER),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ]
    for i in range(1, len(ah_data)):
        if i % 2 == 0:
            ah_style.append(('BACKGROUND', (0,i), (-1,i), GREY))
    ah_tbl.setStyle(TableStyle(ah_style))
    elements.append(ah_tbl)
    elements.append(Spacer(1, 0.15*cm))

    # ── 7. NOTES & SIGNATURES ────────────────────────────────────────────────
    section_heading('الملاحظات والتواقيع')
    if record.general_notes:
        info_row([('الملاحظات', record.general_notes), ('', '')],
                 col_widths=[avail_w*0.12, avail_w*0.38,
                             avail_w*0.12, avail_w*0.38])

    sig_data = [[
        p('توقيع المدير', lbl_s),
        p(record.signature_admin or '_' * 30, val_s),
        p('توقيع ولي الأمر', lbl_s),
        p(record.signature_parent or '_' * 30, val_s),
        p('التاريخ', lbl_s),
        p(datetime.utcnow().strftime('%Y-%m-%d'), val_s),
    ]]
    sig_tbl = Table(sig_data, colWidths=[avail_w*f for f in
                                          [0.10, 0.23, 0.13, 0.23, 0.08, 0.23]])
    sig_tbl.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 0.4, BORDER),
        ('BACKGROUND', (0,0), (0,0), LTBLUE),
        ('BACKGROUND', (2,0), (2,0), LTBLUE),
        ('BACKGROUND', (4,0), (4,0), LTBLUE),
        ('ALIGN', (0,0), (-1,-1), 'RIGHT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LEFTPADDING', (0,0), (-1,-1), 5),
        ('RIGHTPADDING', (0,0), (-1,-1), 5),
    ]))
    elements.append(sig_tbl)
    elements.append(Spacer(1, 0.2*cm))

    # ── 8. FOOTER ────────────────────────────────────────────────────────────
    elements.append(HRFlowable(width='100%', thickness=1,
                               color=HexColor('#cccccc'), spaceAfter=0.1*cm))
    school_display = school.school_name_ar or school.school_name if school else ''
    foot_text = ar(
        f'تم إنشاء هذا السجل بواسطة نظام المهندس المدرسي  |  '
        f'{school_display}  |  '
        f'{datetime.utcnow().strftime("%Y-%m-%d %H:%M")} UTC'
    )
    elements.append(Paragraph(foot_text, foot_s))

    doc.build(elements)
    return buf.getvalue()
