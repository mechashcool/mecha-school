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


def _receipt_logo_source(logo_path: str | None):
    """Image source for the fee-receipt logo — BytesIO, a local file path, or
    None. Never raises: any failure just means "no logo" and the receipt is
    still produced.

    School logos are stored in the PRIVATE school-media Supabase bucket, so an
    unauthenticated GET of the stored URL (_resolve_logo_for_pdf) fails there
    and the logo silently disappeared. Supabase-stored logos are therefore read
    server-side through the existing authenticated _supabase_fetch (service
    key; the bucket/path come from the stored value, the host from our own
    SUPABASE_URL config — never from the stored URL). The bytes are checked to
    be a real image before use. Local static logos keep the legacy resolver.
    """
    if not logo_path:
        return None
    try:
        from app.utils.upload_access import storage_ref_of
        ref = storage_ref_of(logo_path)
    except Exception:
        ref = None
    if ref is not None:
        try:
            from app.utils.helpers import _supabase_fetch
            from PIL import Image as _PILImage
            data, _content_type = _supabase_fetch(ref[1], bucket=ref[0])
            if not data:
                return None
            with _PILImage.open(BytesIO(data)) as probe:
                probe.verify()
            return BytesIO(data)
        except Exception as exc:
            print(f'[PDF] Receipt logo unavailable ({type(exc).__name__}); '
                  'receipt generated without it.')
            return None
    try:
        return _resolve_logo_for_pdf(logo_path)
    except Exception:
        return None


def _rtl_paragraph(text, style, max_width, max_lines=None):
    """A Paragraph for logical (possibly Arabic / mixed) text, wrapped to
    `max_width` BEFORE bidi reordering so multi-line RTL text keeps its natural
    line order (ReportLab would otherwise wrap the already-reversed visual
    string and put the end of the sentence on the first line). Each line is
    shaped + bidi-ordered with _shape_arabic_text, XML-escaped, and joined with
    <br/>. Explicit newlines in `text` are kept. `max_lines` truncates."""
    from xml.sax.saxutils import escape
    from reportlab.pdfbase.pdfmetrics import stringWidth

    font, size = style.fontName, style.fontSize
    limit = max(max_width - 2, 10)
    lines = []
    for raw in ('' if text is None else str(text)).splitlines() or ['']:
        words = raw.split()
        current = []
        for word in words:
            trial = ' '.join(current + [word])
            if current and stringWidth(_shape_arabic_text(trial), font, size) > limit:
                lines.append(' '.join(current))
                current = [word]
            else:
                current.append(word)
        if current:
            lines.append(' '.join(current))
    if not lines:
        lines = ['']
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1] + ' …'
    from reportlab.platypus import Paragraph
    return Paragraph('<br/>'.join(escape(_shape_arabic_text(l)) for l in lines), style)


def generate_fee_receipt(installment, school_settings=None, print_date=None,
                         actual_paid=None, receipt_no_override=None,
                         refund_status=None) -> bytes | None:
    """
    Fee receipt as a real PDF (ReportLab) in the approved Core School layout:
    one A4 portrait page — navy school header (dynamic logo + Arabic school
    name + "إيصال استلام رسوم دراسية", optional refund stamp), the compact
    information strip with the orange separator, the amount summary, the
    receipt-details table, the enlarged stamp/signature area, the school's own
    fee-receipt footer (School.fee_receipt_footer, omitted when empty) and the
    fixed Core School line, which is always the last line.

    Branding (name / logo / currency / footer) comes from `school_settings`,
    which the caller resolves from the installment's OWN school.
    School.receipt_footer (class-schedule PDF) is never read here.

    `actual_paid`: amount of the specific payment operation this receipt
    represents (resolved by the caller from persisted Revenue rows); falls back
    to installment.received_amount. `receipt_no_override`: that operation's
    reference, shown as the receipt number; falls back to
    installment.receipt_no. Arabic uses the project's registered Arabic font +
    arabic_reshaper / python-bidi (_shape_arabic_text).

    Returns bytes or None if ReportLab is unavailable.
    """
    if not _get_rl():
        return None

    from reportlab.lib.pagesizes import A4, portrait
    from reportlab.lib.units import mm
    from reportlab.lib.colors import HexColor, white
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer, Image, HRFlowable)
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from xml.sax.saxutils import escape

    from app.utils.arabic_numbers import amount_to_words_iqd

    has_ar = _register_arabic_fonts(pdfmetrics, TTFont)
    F = 'Amiri' if has_ar else 'Helvetica'
    FB = 'Amiri-Bold' if has_ar else 'Helvetica-Bold'

    # ── Palette (same values as the approved receipt design) ────────────────
    NAVY       = HexColor('#1a3a5c')
    NAVY_HEAD  = HexColor('#14304e')   # solid stand-in for the header gradient
    LOGO_BG    = HexColor('#3a5573')   # rgba(255,255,255,.15) over the navy
    TITLE_SOFT = HexColor('#d1d9e2')   # rgba(255,255,255,.82) over the navy
    INK        = HexColor('#1e2b3a')
    MUTED      = HexColor('#9aabb8')
    LINE       = HexColor('#dde8f4')
    LINE_SOFT  = HexColor('#f0f4f8')
    STRIP_BG   = HexColor('#f8fbff')
    TH_BG      = HexColor('#f0f5fc')
    ORANGE     = HexColor('#e8a020')
    GREEN      = HexColor('#1aab6d')
    RED        = HexColor('#e03e3e')
    SIGN_LINE  = HexColor('#b0bec5')
    FOOT_LINE  = HexColor('#e0e8f0')
    FOOT_TEXT  = HexColor('#64748b')
    STAMP_RED  = HexColor('#c0392b')
    STAMP_BG   = HexColor('#fef0f0')

    def ps(name, font=F, size=10, color=INK, align=TA_RIGHT, leading=None):
        return ParagraphStyle(name, fontName=font, fontSize=size, textColor=color,
                              alignment=align, leading=leading or size * 1.35)

    # ── Page geometry: A4 portrait, 8 mm margins (print stylesheet) ─────────
    pagesize = portrait(A4)
    margin = 8 * mm
    W = pagesize[0] - 2 * margin          # full-bleed width (header, strip)
    INSET = 18                            # 24 CSS px section inset
    SW = W - 2 * INSET                    # section width

    # ── Data — identical sources to the previous receipt ────────────────────
    s = school_settings
    school_name = (getattr(s, 'school_name_ar', None) or getattr(s, 'school_name', None)
                   or 'المدرسة')
    currency = (getattr(s, 'currency_symbol', None) or 'د.ع')
    school_footer = (getattr(s, 'fee_receipt_footer', None) or '').strip() or None

    fee_record = installment.fee_record
    student = fee_record.student
    issue_date = print_date if print_date is not None else datetime.now().date()
    total_paid = sum(
        float(i.received_amount or 0)
        for i in fee_record.installments.execution_options(include_all_years=True)
    )
    total_due = float(fee_record.net_amount)
    remaining = total_due - total_paid
    paid_amount = (float(actual_paid) if actual_paid is not None
                   else float(installment.received_amount or 0))
    _words = amount_to_words_iqd(int(paid_amount))
    amount_words = (_words + ' فقط لا غير') if _words else '—'
    receipt_no = receipt_no_override or installment.receipt_no or '—'
    student_code = student.student_id or '—'
    inst_label = f'#{installment.installment_no}'
    method_label = {
        'cash':     'نقداً / Cash',
        'transfer': 'تحويل بنكي / Bank Transfer',
        'cheque':   'شيك / Cheque',
        'card':     'بطاقة / Card',
    }.get(installment.payment_method, installment.payment_method or '—')
    due_label = installment.due_date.strftime('%Y-%m-%d') if installment.due_date else '—'

    def money(value, style):
        """'<currency> <number>' with the currency visually on the LEFT of the
        number (approved order). Built as an explicit visual string so bidi can
        never swap the two parts."""
        return Paragraph(f'{escape(_shape_arabic_text(currency))} {value:,.2f}', style)

    elements = []

    # ── 1. Navy header: logo on the right, name + title, refund stamp left ──
    logo = None
    logo_src = _receipt_logo_source(getattr(s, 'logo_path', None)) if s else None
    if logo_src is not None:
        try:
            img = Image(logo_src, width=38, height=38, kind='proportional')
            logo = Table([[img]], colWidths=[46], rowHeights=[46])
            logo.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), LOGO_BG),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                ('ROUNDEDCORNERS', [6, 6, 6, 6]),
            ]))
        except Exception as exc:
            print(f'[PDF] Could not load receipt logo: {exc}')
            logo = None

    stamp = None
    if refund_status in ('refunded', 'partial'):
        _label = 'مسترجع بالكامل' if refund_status == 'refunded' else 'مسترجع جزئياً'
        stamp = Table([[Paragraph(escape(_shape_arabic_text(f'★ {_label} ★')),
                                  ps('stamp', FB, 11, STAMP_RED, TA_CENTER))]],
                      colWidths=[104])
        stamp.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), STAMP_BG),
            ('BOX', (0, 0), (-1, -1), 1.5, STAMP_RED),
            ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('ROUNDEDCORNERS', [6, 6, 6, 6]),
        ]))

    logo_w = 46 + 14 if logo is not None else 0
    stamp_w = 104 + 14 if stamp is not None else 0
    info_w = W - logo_w - stamp_w - 36          # 18 pt side padding each side
    info = [
        # info_w - 14: the narrowest content width this cell can get after
        # padding, so ReportLab never re-wraps the already-ordered lines.
        _rtl_paragraph(school_name, ps('school', FB, 17, white, leading=22), info_w - 14,
                       max_lines=2),
        Spacer(1, 2),
        Paragraph(escape(_shape_arabic_text('إيصال استلام رسوم دراسية')),
                  ps('title', FB, 12, TITLE_SOFT)),
    ]
    head_row, head_widths = [], []
    if stamp is not None:
        head_row.append(stamp); head_widths.append(stamp_w)
    head_row.append(info); head_widths.append(info_w)
    if logo is not None:
        head_row.append(logo); head_widths.append(logo_w)
    head_widths[0] += 18
    head_widths[-1] += 18
    header = Table([head_row], colWidths=head_widths)
    header.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), NAVY_HEAD),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (-1, 0), (-1, 0), 'RIGHT'),
        ('TOPPADDING', (0, 0), (-1, -1), 11), ('BOTTOMPADDING', (0, 0), (-1, -1), 11),
        ('LEFTPADDING', (0, 0), (-1, -1), 7), ('RIGHTPADDING', (0, 0), (-1, -1), 7),
        ('LEFTPADDING', (0, 0), (0, 0), 18), ('RIGHTPADDING', (-1, 0), (-1, 0), 18),
    ]))
    elements.append(header)

    # ── 2. Compact information strip + orange separator (RTL order) ─────────
    strip_items = [                                # rightmost first (RTL)
        ('رقم الإيصال', receipt_no, True),
        ('اسم الطالب', student.full_name, False),
        ('رقم الطالب', student_code, False),
        ('نوع الرسم', fee_record.fee_type.name, False),
        ('القسط', inst_label, False),
    ]
    cell_w = W / len(strip_items)
    lab_style = ps('is_label', FB, 9, MUTED, TA_CENTER)
    val_style = ps('is_val', FB, 12, INK, TA_CENTER, leading=15)
    mono_style = ps('is_mono', 'Courier-Bold', 10.5, INK, TA_CENTER, leading=14)
    strip_cells = []
    for label, value, mono in reversed(strip_items):   # LTR column order
        strip_cells.append([
            Paragraph(escape(_shape_arabic_text(label)), lab_style),
            (_rtl_paragraph(value, mono_style, cell_w - 16, max_lines=2) if mono
             else _rtl_paragraph(value, val_style, cell_w - 16, max_lines=2)),
        ])
    strip = Table([strip_cells], colWidths=[cell_w] * len(strip_items))
    strip.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), STRIP_BG),
        ('LINEBELOW', (0, 0), (-1, -1), 3, ORANGE),
        ('LINEBEFORE', (1, 0), (-1, -1), 1, LINE),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 7), ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(strip)

    section_title_style = ps('sec_title', FB, 12.5, white)

    # ── 3. Amount summary (title bar + 3 cells, RTL: paid · remaining · due) ─
    ag_label = ps('ag_label', FB, 9, MUTED, TA_CENTER)
    ag_val = lambda name, color: ps(name, FB, 15, color, TA_CENTER, leading=19)
    summary_cells = [                               # LTR column order
        [Paragraph(escape(_shape_arabic_text('إجمالي المستحق')), ag_label),
         money(total_due, ag_val('ag_due', INK))],
        [Paragraph(escape(_shape_arabic_text('الرصيد المتبقي')), ag_label),
         money(remaining, ag_val('ag_rem', GREEN if remaining <= 0 else RED))],
        [Paragraph(escape(_shape_arabic_text('المبلغ المدفوع')), ag_label),
         money(paid_amount, ag_val('ag_paid', GREEN))],
    ]
    summary = Table(
        [[Paragraph(escape(_shape_arabic_text('ملخص المبلغ')), section_title_style), '', ''],
         summary_cells],
        colWidths=[SW / 3] * 3)
    summary.setStyle(TableStyle([
        ('SPAN', (0, 0), (-1, 0)),
        ('BACKGROUND', (0, 0), (-1, 0), NAVY),
        ('TOPPADDING', (0, 0), (-1, 0), 5), ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('LEFTPADDING', (0, 0), (-1, 0), 14), ('RIGHTPADDING', (0, 0), (-1, 0), 14),
        ('BOX', (0, 1), (-1, -1), 1, LINE),
        ('LINEBEFORE', (1, 1), (-1, -1), 1, LINE),
        ('VALIGN', (0, 1), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 1), (-1, -1), 7), ('BOTTOMPADDING', (0, 1), (-1, -1), 8),
        ('ROUNDEDCORNERS', [6, 6, 6, 6]),
    ]))
    summary.hAlign = 'CENTER'
    elements.append(Spacer(1, 8))
    elements.append(summary)

    # ── 4. Receipt-details table (label column on the right, 34 %) ──────────
    th_w, td_w = SW * 0.34, SW * 0.66
    th_style = ps('th', FB, 11, NAVY)
    td_style = ps('td', FB, 11.5, INK, leading=15)
    td_mono = ps('td_mono', 'Courier-Bold', 10.5, INK, leading=14)
    td_money = ps('td_money', FB, 11.5, INK, leading=15)
    detail_rows = [
        ('رقم الإيصال',         _rtl_paragraph(receipt_no, td_mono, td_w - 24)),
        ('اسم الطالب',          _rtl_paragraph(student.full_name, td_style, td_w - 24)),
        ('رقم الطالب',          _rtl_paragraph(student_code, td_style, td_w - 24)),
        ('نوع الرسم',           _rtl_paragraph(fee_record.fee_type.name, td_style, td_w - 24)),
        ('القسط',               _rtl_paragraph(inst_label, td_style, td_w - 24)),
        ('المبلغ المدفوع',      money(paid_amount, td_money)),
        ('المبلغ كتابةً',       _rtl_paragraph(amount_words, td_style, td_w - 24, max_lines=3)),
        ('الرصيد المتبقي',      money(remaining, td_money)),
        ('تاريخ الاستحقاق',     _rtl_paragraph(due_label, td_style, td_w - 24)),
        ('تاريخ إصدار الإيصال', _rtl_paragraph(issue_date.strftime('%Y-%m-%d'), td_style, td_w - 24)),
        ('طريقة الدفع',         _rtl_paragraph(method_label, td_style, td_w - 24)),
    ]
    detail_data = [[Paragraph(escape(_shape_arabic_text('تفاصيل الإيصال')), section_title_style), '']]
    for label, value_para in detail_rows:
        detail_data.append([value_para, Paragraph(escape(_shape_arabic_text(label)), th_style)])
    detail = Table(detail_data, colWidths=[td_w, th_w])
    detail_style = [
        ('SPAN', (0, 0), (-1, 0)),
        ('BACKGROUND', (0, 0), (-1, 0), NAVY),
        ('TOPPADDING', (0, 0), (-1, 0), 5), ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('LEFTPADDING', (0, 0), (-1, 0), 14), ('RIGHTPADDING', (0, 0), (-1, 0), 14),
        ('BACKGROUND', (1, 1), (1, -1), TH_BG),
        ('BOX', (0, 1), (-1, -1), 1, LINE),
        ('LINEBELOW', (1, 1), (1, -2), 1, LINE),
        ('LINEBELOW', (0, 1), (0, -2), 1, LINE_SOFT),
        ('VALIGN', (0, 1), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 1), (-1, -1), 3.5), ('BOTTOMPADDING', (0, 1), (-1, -1), 4.5),
        ('LEFTPADDING', (0, 1), (-1, -1), 12), ('RIGHTPADDING', (0, 1), (-1, -1), 12),
        ('ROUNDEDCORNERS', [6, 6, 0, 0]),
    ]
    for r in range(2, len(detail_data), 2):        # even data rows (nth-child(even))
        detail_style.append(('BACKGROUND', (0, r), (0, r), STRIP_BG))
    detail.setStyle(TableStyle(detail_style))
    detail.hAlign = 'CENTER'
    elements.append(Spacer(1, 8))
    elements.append(detail)

    # ── 5. Enlarged stamp / signature area (RTL: stamp right, signature left)
    sign_label = ps('sign_label', FB, 11, NAVY, TA_CENTER)

    def sign_box(label):
        return [Paragraph(escape(_shape_arabic_text(label)), sign_label),
                Spacer(1, 30),
                HRFlowable(width='72%', thickness=1, color=SIGN_LINE, hAlign='CENTER',
                           spaceBefore=0, spaceAfter=0)]

    sign = Table([[sign_box('توقيع المستلم'), sign_box('ختم المدرسة')]],
                 colWidths=[SW / 2] * 2)
    sign.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 1, LINE),
        ('LINEBEFORE', (1, 0), (1, 0), 1, LINE),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 9), ('BOTTOMPADDING', (0, 0), (-1, -1), 11),
        ('ROUNDEDCORNERS', [8, 8, 8, 8]),
    ]))
    sign.hAlign = 'CENTER'
    elements.append(Spacer(1, 14))
    elements.append(sign)

    # ── 6. Footers: school fee-receipt footer (if any), then the fixed line ──
    elements.append(Spacer(1, 8))
    elements.append(HRFlowable(width='100%', thickness=1, color=FOOT_LINE,
                               spaceBefore=0, spaceAfter=6))
    if school_footer:
        elements.append(_rtl_paragraph(school_footer,
                                       ps('school_footer', FB, 10.5, FOOT_TEXT, TA_CENTER, leading=14),
                                       W - 2 * INSET, max_lines=3))
        elements.append(Spacer(1, 3))
    elements.append(Paragraph(
        escape(_shape_arabic_text('تم اصدار هذا الوصل الكترونيا بواسطة نظام Core School')),
        ps('core_footer', FB, 10, SIGN_LINE, TA_CENTER)))

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=pagesize, leftMargin=margin, rightMargin=margin,
                            topMargin=margin, bottomMargin=margin,
                            title=f'إيصال استلام رسوم دراسية — {receipt_no}',
                            author='Core School')
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
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # Arabic-capable font only for the branding lines; tables keep Helvetica.
    arabic_font_registered = _register_arabic_fonts(pdfmetrics, TTFont)
    fn_ar = 'Amiri' if arabic_font_registered else 'Helvetica'
    ar = _shape_arabic_text

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
    brand_s = ParagraphStyle('brand', fontName=fn_ar, fontSize=10, spaceAfter=2,
                               alignment=1, textColor=colors.HexColor('#6b7a8d'))

    elements.append(Paragraph("Core School", title_s))
    elements.append(Paragraph(ar('نظام إدارة المدارس'), brand_s))
    elements.append(Paragraph(
        f"Salary Slip — {ARABIC_MONTHS[record.month]} {record.year}", sub_s))

    # Employee info (prefer snapshots so historical slips stay correct)
    emp = record.employee
    emp_data = [
        ['Employee',    record.employee_name],
        ['Employee ID', emp.employee_id if emp else '—'],
        ['Job Title',   record.job_title or '—'],
        ['Department',  record.department or '—'],
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

    # Salary breakdown — itemized from PayrollItem lines, with cached totals.
    sal_data = [['Component', 'Amount (IQD)']]
    sal_data.append(['Base Salary', f"{float(record.base_salary):,.2f}"])
    try:
        items = list(record.items)
    except Exception:
        items = []
    for it in items:
        sign = '+' if it.item_type == 'addition' else '-'
        sal_data.append([it.name, f"{sign}{float(it.amount):,.2f}"])
    # If no line items (legacy records), fall back to aggregate columns.
    if not items:
        if record.allowances:
            sal_data.append(['Allowances', f"+{float(record.allowances):,.2f}"])
        if record.deductions:
            sal_data.append(['Deductions', f"-{float(record.deductions):,.2f}"])
    sal_data.append(['NET SALARY', f"{float(record.net_salary):,.2f}"])

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
    _status_en = {'draft': 'DRAFT', 'pending': 'DRAFT', 'approved': 'APPROVED',
                  'paid': 'PAID', 'cancelled': 'CANCELLED'}
    paid_txt = f"Status: {_status_en.get(record.status, record.status.upper())}"
    if record.paid_date:
        paid_txt += f"  |  Paid: {record.paid_date.strftime('%Y-%m-%d')}"
    elements.append(Paragraph(paid_txt, status_s))
    elements.append(Paragraph(
        f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
        status_s))
    attribution_s = ParagraphStyle('attr', fontName=fn_ar, fontSize=8, alignment=1,
                                    textColor=colors.HexColor('#9aabb8'))
    elements.append(Paragraph(
        ar('تم إنشاء هذا الوصل إلكترونياً باستخدام نظام Core School لإدارة المدارس'),
        attribution_s))

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

    col_headers = ['نسبة الحضور', 'انصراف', 'مجاز', 'غائب', 'متأخر', 'حاضر',
                   'أيام العمل', 'المسمى الوظيفي', 'القسم', 'اسم الموظف', '#']
    col_widths = [2.6*cm, 1.8*cm, 1.8*cm, 1.8*cm, 1.8*cm, 1.8*cm,
                  2*cm, 3.2*cm, 2.8*cm, 4*cm, 1*cm]

    th_s = ParagraphStyle('th2', fontName=fn_b, fontSize=8, alignment=1, textColor=WHITE)
    td_s = ParagraphStyle('td2', fontName=fn, fontSize=8, alignment=1)

    table_data = [[Paragraph(ar(h), th_s) for h in col_headers]]
    row_alt = []
    for i, row in enumerate(rows, 1):
        emp = row['employee']
        table_data.append([
            Paragraph(ar(f"{row['rate']}%"), td_s),
            Paragraph(str(row['checked_out']), td_s),
            Paragraph(str(row.get('on_leave', 0)), td_s),
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
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer, Image, HRFlowable)
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    arabic_font_registered = _register_arabic_fonts(pdfmetrics, TTFont)
    fn = 'Amiri' if arabic_font_registered else 'Helvetica'
    fn_b = 'Amiri-Bold' if arabic_font_registered else 'Helvetica-Bold'
    ar = _shape_arabic_text
    emp = emp_row['employee']

    # ── Core School payroll-print palette (navy / white / light-gray) ─────────
    NAVY       = HexColor('#0f2540')   # headers, title, accent borders
    NAVY_SOFT  = HexColor('#1a3a5c')   # secondary navy
    LABEL_NAVY = HexColor('#5b6b7d')   # subtle field labels
    CARD_BG    = HexColor('#eef3f9')   # light-blue label / card background
    STRIP_BG   = HexColor('#f8fafc')   # summary value background
    ZEBRA_BG   = HexColor('#f4f7fb')   # alternating table rows
    BORDER     = HexColor('#d5dde8')   # thin professional borders
    GREEN      = HexColor('#157347')   # present / positive
    TEAL       = HexColor('#2e7d32')   # on-leave
    AMBER      = HexColor('#b26a00')   # late
    RED        = HexColor('#b02a37')   # absent
    DARK       = HexColor('#1e2b3a')   # strong value text
    WHITE      = colors.white

    # ── Margins ~1.3 cm → usable width ≈ 18.4 cm (≈ 88% of the A4 width) ──────
    L_MARGIN = 1.3 * cm
    doc = SimpleDocTemplate(buf_emp := BytesIO(), pagesize=portrait(A4),
                            leftMargin=L_MARGIN, rightMargin=L_MARGIN,
                            topMargin=1.2 * cm, bottomMargin=1.35 * cm)
    USABLE_W = A4[0] - 2 * L_MARGIN

    # Explicit `leading` on every style keeps Arabic lines from touching.
    sch_s   = ParagraphStyle('sch', fontName=fn_b, fontSize=15, alignment=2,
                             leading=18, textColor=NAVY)            # right
    title_s = ParagraphStyle('ttl', fontName=fn_b, fontSize=16, alignment=1,
                             leading=20, textColor=NAVY)            # center
    period_s = ParagraphStyle('per', fontName=fn, fontSize=9, alignment=1,
                              leading=12, textColor=LABEL_NAVY)     # center
    brand_s  = ParagraphStyle('brand', fontName=fn_b, fontSize=10, alignment=0,
                              leading=13, textColor=NAVY)           # left
    brand_sub = ParagraphStyle('brandsub', fontName=fn, fontSize=7.5, alignment=0,
                               leading=10, textColor=LABEL_NAVY)    # left
    th_s = ParagraphStyle('the', fontName=fn_b, fontSize=8.5, alignment=1,
                          leading=11, textColor=WHITE)
    td_s = ParagraphStyle('tde', fontName=fn, fontSize=8, alignment=1, leading=10.5,
                          textColor=DARK)

    elements = []

    # ── Three-part header table: [Core School | title | logo+school] ──────────
    _logo_flow = None
    if school and getattr(school, 'logo_path', None):
        _logo_path = _resolve_logo_for_pdf(school.logo_path)
        if _logo_path:
            try:
                _logo_flow = Image(_logo_path, width=1.6 * cm, height=1.6 * cm)
                _logo_flow.hAlign = 'RIGHT'
            except Exception:
                _logo_flow = None

    school_name = (school.school_name_ar or school.school_name) if school else ''
    _gen_date = datetime.now().strftime('%Y-%m-%d')

    # Right cell (read first in RTL): logo stacked above school name.
    right_cell = []
    if _logo_flow is not None:
        right_cell.append(_logo_flow)
        right_cell.append(Spacer(1, 0.08 * cm))
    if school_name:
        right_cell.append(Paragraph(ar(school_name), sch_s))
    if not right_cell:
        right_cell = [Paragraph(ar('المدرسة'), sch_s)]

    center_cell = [
        Paragraph(ar('سجل حضور الموظف'), title_s),
        Spacer(1, 0.06 * cm),
        Paragraph(ar(f'{date_from}  —  {date_to}'), period_s),
    ]
    left_cell = [
        Paragraph('Core School', brand_s),
        Paragraph(ar('نظام إدارة المدارس'), brand_sub),
        Spacer(1, 0.05 * cm),
        Paragraph(ar(f'تاريخ الإصدار: {_gen_date}'), brand_sub),
    ]

    header_tbl = Table([[left_cell, center_cell, right_cell]],
                       colWidths=[USABLE_W * 0.28, USABLE_W * 0.44, USABLE_W * 0.28])
    header_tbl.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 2),
        ('RIGHTPADDING', (0, 0), (-1, -1), 2),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(header_tbl)
    elements.append(Spacer(1, 0.15 * cm))
    elements.append(HRFlowable(width='100%', thickness=1.4, color=NAVY,
                               spaceBefore=0, spaceAfter=10))

    # ── Employee information card (labels subtle navy, values strong dark) ────
    lbl_s = ParagraphStyle('lbl', fontName=fn, fontSize=8.5, alignment=2,
                           leading=12, textColor=LABEL_NAVY)
    val_s = ParagraphStyle('val', fontName=fn_b, fontSize=9.5, alignment=2,
                           leading=12.5, textColor=DARK)

    def _L(t):
        return Paragraph(ar(t), lbl_s)

    def _V(t):
        return Paragraph(ar(str(t)), val_s)

    _emp_code = getattr(emp, 'employee_id', None) or '—'
    # 4 columns per row read right→left as: [label, value]  |  [label, value]
    info_rows = [
        [_V(_emp_code), _L('الرقم الوظيفي'), _V(emp.full_name), _L('اسم الموظف')],
        [_V(emp.department or '—'), _L('القسم / الاختصاص'), _V(emp.job_title or '—'), _L('المسمى الوظيفي')],
        [_V(_gen_date), _L('تاريخ الإصدار'), _V(f'{date_from}  —  {date_to}'), _L('الفترة')],
    ]
    _cw = [USABLE_W * 0.20, USABLE_W * 0.16, USABLE_W * 0.48, USABLE_W * 0.16]
    info_tbl = Table(info_rows, colWidths=_cw)
    info_tbl.setStyle(TableStyle([
        ('BACKGROUND', (1, 0), (1, -1), CARD_BG),   # label columns tinted
        ('BACKGROUND', (3, 0), (3, -1), CARD_BG),
        ('BOX', (0, 0), (-1, -1), 0.6, BORDER),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, BORDER),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 7),
        ('RIGHTPADDING', (0, 0), (-1, -1), 7),
    ]))
    elements.append(info_tbl)
    elements.append(Spacer(1, 0.4 * cm))

    # ── Attendance summary strip (label above, value below, color-coded) ──────
    _on_leave_count = emp_row.get('on_leave', 0)
    summary_headers = ['أيام العمل', 'حاضر', 'متأخر', 'غائب', 'مجاز', 'انصراف', 'نسبة الحضور']
    summary_values  = [emp_row['working_days'], emp_row['present'], emp_row['late'],
                       emp_row['absent'], _on_leave_count, emp_row['checked_out'],
                       f"{emp_row['rate']}%"]
    val_colors = [NAVY, GREEN, AMBER, RED, TEAL, NAVY_SOFT, NAVY]
    val_sizes  = [12, 12, 12, 12, 12, 12, 14]
    lbl_row = [Paragraph(ar(h), ParagraphStyle('sh', fontName=fn_b, fontSize=8,
                                               alignment=1, leading=11, textColor=NAVY))
               for h in summary_headers]
    val_row = [Paragraph(ar(str(v)), ParagraphStyle(f'sv{i}', fontName=fn_b,
               fontSize=val_sizes[i], alignment=1, leading=val_sizes[i] + 3,
               textColor=val_colors[i])) for i, v in enumerate(summary_values)]
    s_tbl = Table([lbl_row, val_row], colWidths=[USABLE_W / 7.0] * 7)
    s_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), CARD_BG),      # label row light-blue
        ('BACKGROUND', (0, 1), (-1, 1), STRIP_BG),     # value row near-white
        ('LINEABOVE', (0, 0), (-1, 0), 1.4, NAVY),     # navy accent on top
        ('BOX', (0, 0), (-1, -1), 0.6, BORDER),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, BORDER),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    elements.append(s_tbl)
    elements.append(Spacer(1, 0.45 * cm))

    # ── Daily attendance table (full width; subtle status-cell tint) ──────────
    # 'not_recorded' is a working-day slot with no persisted attendance row.
    # It must never be exported as 'غائب'.
    STATUS_AR = {'present': 'حاضر', 'absent': 'غائب', 'late': 'متأخر',
                 'on_leave': 'مجاز', 'not_recorded': 'غير مسجل بعد'}
    # Status cell text styles (bold, color-coded) + subtle per-status cell tint.
    st_text = {
        'present':  ParagraphStyle('stp', fontName=fn_b, fontSize=8, alignment=1, leading=10.5, textColor=GREEN),
        'late':     ParagraphStyle('stl', fontName=fn_b, fontSize=8, alignment=1, leading=10.5, textColor=AMBER),
        'absent':   ParagraphStyle('sta', fontName=fn_b, fontSize=8, alignment=1, leading=10.5, textColor=RED),
        'on_leave': ParagraphStyle('sto', fontName=fn_b, fontSize=8, alignment=1, leading=10.5, textColor=TEAL),
    }
    st_bg = {
        'present':  HexColor('#eef7f0'),
        'late':     HexColor('#fdf6e3'),
        'absent':   HexColor('#fdecec'),
        'on_leave': HexColor('#eef6ef'),
    }

    d_headers = ['ملاحظات', 'الحالة', 'وقت الانصراف', 'وقت الحضور', 'التاريخ', '#']
    # Redistributed for portrait A4 after dropping المصدر/الجهاز — extra width to
    # notes/status/date/times, tight row-number; sums to the full usable width.
    _frac = [0.300, 0.150, 0.135, 0.135, 0.220, 0.060]
    d_widths = [USABLE_W * f for f in _frac]

    STATUS_CELL = 1   # 0-based index of the الحالة column (matches d_headers)
    detail_data = [[Paragraph(ar(h), th_s) for h in d_headers]]
    status_by_row = []
    for i, day in enumerate(emp_row.get('daily', []), 1):
        status = day.get('status', '')
        st_style = st_text.get(status, td_s)
        detail_data.append([
            Paragraph(ar(day.get('notes') or '—'), td_s),
            Paragraph(ar(STATUS_AR.get(status, status)), st_style),
            Paragraph(day['check_out'].strftime('%H:%M') if day.get('check_out') else '—', td_s),
            Paragraph(day['check_in'].strftime('%H:%M') if day.get('check_in') else '—', td_s),
            Paragraph(day['date'].strftime('%Y-%m-%d') if day.get('date') else '', td_s),
            Paragraph(str(i), td_s),
        ])
        status_by_row.append((i, status))

    sc = [
        ('BACKGROUND', (0, 0), (-1, 0), NAVY),
        ('FONTNAME', (0, 0), (-1, -1), fn),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LINEBELOW', (0, 0), (-1, 0), 0.8, NAVY),
        ('GRID', (0, 0), (-1, -1), 0.4, BORDER),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]
    # Subtle zebra first, then a light tint only on the status cell so the row
    # stays clean while the status column remains easy to scan.
    for ri, status in status_by_row:
        if ri % 2 == 0:
            sc.append(('BACKGROUND', (0, ri), (-1, ri), ZEBRA_BG))
        if status in st_bg:
            sc.append(('BACKGROUND', (STATUS_CELL, ri), (STATUS_CELL, ri), st_bg[status]))

    d_tbl = Table(detail_data, colWidths=d_widths, repeatRows=1)
    d_tbl.setStyle(TableStyle(sc))
    elements.append(d_tbl)

    # ── Footer: thin separator + Core School attribution + date + page number ─
    def _draw_footer(canvas, doc_):
        canvas.saveState()
        page_w = doc_.pagesize[0]
        y = 1.02 * cm
        canvas.setStrokeColor(BORDER)
        canvas.setLineWidth(0.5)
        canvas.line(L_MARGIN, y + 0.32 * cm, page_w - L_MARGIN, y + 0.32 * cm)
        canvas.setFillColor(HexColor('#7a8794'))
        canvas.setFont(fn, 7.5)
        canvas.drawCentredString(page_w / 2.0, y, ar('تم إنشاء هذا التقرير بواسطة Core School'))
        canvas.setFont(fn, 7)
        canvas.setFillColor(HexColor('#9aa7b3'))
        canvas.drawRightString(page_w - L_MARGIN, y, str(canvas.getPageNumber()))
        canvas.drawString(L_MARGIN, y, datetime.now().strftime('%Y-%m-%d %H:%M'))
        canvas.restoreState()

    doc.build(elements, onFirstPage=_draw_footer, onLaterPages=_draw_footer)
    return buf_emp.getvalue()


# ─── STUDENT REGISTRATION RECORD (سجل القيد العام) — A3 Landscape ──────────

_SUBJECTS = [
    'التربية الإسلامية',
    'اللغة العربية',
    'اللغة الإنكليزية',
    'الرياضيات',
    'العلوم',
    'الاجتماعيات',
    'التربية الفنية والنشيد',
    'التربية الرياضية',
]


# ─── STUDENT ATTENDANCE REPORT PDF ───────────────────────────────────────────

def generate_attendance_report_pdf(rows, report_type='detail', date_from='', date_to='',
                                   school=None, grade_map=None,
                                   daily_rows=None) -> bytes | None:
    """
    Generate an Arabic RTL attendance report PDF.

    rows: list of dicts with keys: student, present, absent, late, checkout, details
    daily_rows: optional day-by-day rows for a single-student report (dicts with
        date/check_in/check_out/status_ar/source/notes as built by the attendance
        blueprint). When given, a "تفاصيل الحضور اليومي" table is appended after
        the existing summary table; everything above it is unchanged.
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

    arabic_ok = _register_arabic_fonts(pdfmetrics, TTFont)
    fn   = 'Amiri'      if arabic_ok else 'Helvetica'
    fn_b = 'Amiri-Bold' if arabic_ok else 'Helvetica-Bold'
    ar   = _shape_arabic_text

    HEADER_BG = HexColor('#1a3a5c')
    ALT_BG    = HexColor('#f0f4f8')
    WHITE     = colors.white

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=1.5*cm, rightMargin=1.5*cm,
                            topMargin=1.5*cm, bottomMargin=1.5*cm)

    # Header hierarchy: school name (strongest) → report title → period.
    # Explicit `leading` on every Arabic style so the three lines never collide.
    school_s = ParagraphStyle('asch', fontName=fn_b, fontSize=16, leading=21,
                              alignment=1, textColor=HexColor('#1a3a5c'))
    title_s = ParagraphStyle('at',  fontName=fn_b, fontSize=12, leading=17,
                              alignment=1, textColor=HexColor('#2c5578'))
    sub_s   = ParagraphStyle('as2', fontName=fn,   fontSize=9.5, leading=14,
                              alignment=1, textColor=HexColor('#6b7a8d'))
    th_s    = ParagraphStyle('ath', fontName=fn_b, fontSize=8,
                              alignment=1, textColor=WHITE)
    td_s    = ParagraphStyle('atd', fontName=fn,   fontSize=8, alignment=1)

    elements = []

    # School identity — read from the School object the caller already resolved
    # from the authenticated context. Never hard-coded, never a system name.
    school_name = ''
    if school:
        school_name = getattr(school, 'school_name_ar', '') or getattr(school, 'school_name', '')
    if school_name:
        elements.append(Paragraph(ar(school_name), school_s))
        elements.append(Spacer(1, 0.18*cm))

    type_labels = {
        'detail':  'تقرير تفصيلي عام',
        'grade':   'تقرير حسب الصف',
        'section': 'تقرير حسب الشعبة',
        'student': 'تقرير حسب الطالب',
        'shift':   'تقرير حسب الشفت',
    }
    elements.append(Paragraph(ar(type_labels.get(report_type, 'تقرير الحضور')), title_s))
    elements.append(Spacer(1, 0.1*cm))
    elements.append(Paragraph(ar(f'الفترة: {date_from}  —  {date_to}'), sub_s))
    elements.append(Spacer(1, 0.4*cm))

    def p(t):
        return Paragraph(ar(str(t or '—')), td_s)

    def ph(t):
        return Paragraph(ar(str(t or '')), th_s)

    # Summary row
    total_present  = sum(r['present']          for r in rows)
    total_absent   = sum(r['absent']           for r in rows)
    total_late     = sum(r['late']             for r in rows)
    total_on_leave = sum(r.get('on_leave', 0) for r in rows)
    grand_total    = total_present + total_absent + total_late + total_on_leave
    billable       = total_present + total_absent + total_late
    grand_pct      = round((total_present + total_late) / billable * 100, 1) if billable > 0 else 0

    sum_data = [
        [ph('عدد الطلاب'), ph('حاضر'), ph('متأخر'), ph('غائب'), ph('مجاز'), ph('إجمالي'), ph('نسبة الحضور')],
        [p(len(rows)), p(total_present), p(total_late), p(total_absent), p(total_on_leave), p(grand_total), p(f'{grand_pct}%')],
    ]
    s_tbl = Table(sum_data, colWidths=[2.6*cm]*7)
    s_tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, 0), HEADER_BG),
        ('FONTNAME',      (0, 0), (-1, -1), fn),
        ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('GRID',          (0, 0), (-1, -1), 0.3, HexColor('#cccccc')),
    ]))
    elements.append(s_tbl)
    elements.append(Spacer(1, 0.5*cm))

    # Main detail table
    col_h = [ph('#'), ph('اسم الطالب'), ph('الرقم'), ph('الصف'), ph('الشعبة'),
             ph('حاضر'), ph('متأخر'), ph('غائب'), ph('مجاز'), ph('إجمالي'), ph('نسبة الحضور')]
    col_w = [0.8*cm, 4*cm, 2*cm, 3*cm, 1.8*cm,
             1.4*cm, 1.4*cm, 1.4*cm, 1.4*cm, 1.8*cm, 2.4*cm]

    table_data = [col_h]
    row_bgs    = []
    for i, row in enumerate(rows, 1):
        s         = row['student']
        grade_n   = s.section.grade.name if s.section and s.section.grade else '—'
        sec_n     = s.section.name       if s.section else '—'
        ol        = row.get('on_leave', 0)
        billable  = row['present'] + row['absent'] + row['late']
        total     = billable + ol
        pct_val   = round((row['present'] + row['late']) / billable * 100, 1) if billable else 0
        table_data.append([
            p(i), p(s.full_name), p(s.student_id),
            p(grade_n), p(sec_n),
            p(row['present']), p(row['late']), p(row['absent']),
            p(ol), p(total), p(f'{pct_val}%'),
        ])
        if i % 2 == 0:
            row_bgs.append(i)

    style_cmds = [
        ('BACKGROUND',    (0, 0), (-1, 0), HEADER_BG),
        ('FONTNAME',      (0, 0), (-1, -1), fn),
        ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID',          (0, 0), (-1, -1), 0.3, HexColor('#cccccc')),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]
    for ri in row_bgs:
        style_cmds.append(('BACKGROUND', (0, ri), (-1, ri), ALT_BG))

    tbl = Table(table_data, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle(style_cmds))
    elements.append(tbl)

    # ── Day-by-day details (single-student reports only) ──────────────────────
    if daily_rows:
        elements.append(Spacer(1, 0.6*cm))
        elements.append(Paragraph(ar('تفاصيل الحضور اليومي'), title_s))
        elements.append(Spacer(1, 0.3*cm))

        # RTL layout: ReportLab always lays cells out left→right, so the columns
        # are STORED in reverse of the Arabic reading order. Read right→left the
        # table is:  التاريخ | وقت الحضور | وقت الانصراف | الحالة
        d_head   = [ph('الحالة'), ph('وقت الانصراف'), ph('وقت الحضور'), ph('التاريخ')]
        d_widths = [5*cm, 4.8*cm, 4.8*cm, 5.4*cm]   # same right→left order

        # Status colours — identical palette to the on-screen daily table. A
        # Paragraph carries its own colour, so this is done with per-status
        # styles rather than a TEXTCOLOR table command.
        _status_colors = {
            'حاضر':  HexColor('#1aab6d'),
            'متأخر': HexColor('#e8a020'),
            'غائب':  HexColor('#e03e3e'),
            'مجاز':  HexColor('#2e7d32'),
        }
        _status_styles = {
            lbl: ParagraphStyle(f'atd_st{si}', parent=td_s,
                                fontName=fn_b, textColor=col)
            for si, (lbl, col) in enumerate(_status_colors.items())
        }
        _muted_style = ParagraphStyle('atd_stm', parent=td_s,
                                      textColor=HexColor('#8b98a8'))

        def p_status(label):
            return Paragraph(ar(str(label)),
                             _status_styles.get(label, _muted_style))

        d_data = [d_head]
        d_bgs  = []
        for i, d in enumerate(daily_rows, 1):
            d_data.append([
                p_status(d.get('status_ar') or '—'),
                p(d['check_out'].strftime('%H:%M') if d.get('check_out') else '—'),
                p(d['check_in'].strftime('%H:%M')  if d.get('check_in')  else '—'),
                p(d['date'].strftime('%Y-%m-%d')   if d.get('date')      else '—'),
            ])
            if i % 2 == 0:
                d_bgs.append(i)

        d_style = [
            ('BACKGROUND',    (0, 0), (-1, 0), HEADER_BG),
            ('FONTNAME',      (0, 0), (-1, -1), fn),
            ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID',          (0, 0), (-1, -1), 0.3, HexColor('#cccccc')),
            ('TOPPADDING',    (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]
        for ri in d_bgs:
            d_style.append(('BACKGROUND', (0, ri), (-1, ri), ALT_BG))

        d_tbl = Table(d_data, colWidths=d_widths, repeatRows=1, hAlign='CENTER')
        d_tbl.setStyle(TableStyle(d_style))
        elements.append(d_tbl)

    elements.append(Spacer(1, 0.4*cm))
    foot_s = ParagraphStyle('af2', fontName=fn, fontSize=8, leading=12,
                             alignment=1, textColor=HexColor('#9aabb8'))
    # System attribution — Core School is the platform identity; the school's own
    # name stays in the header above. Generation time and footer position kept.
    elements.append(Paragraph(
        ar('تم إنشاء هذا التقرير بواسطة Core School — نظام إدارة المدارس'
           f'  |  تم الإنشاء: {datetime.utcnow().strftime("%Y-%m-%d %H:%M")}'),
        foot_s))

    doc.build(elements)
    return buf.getvalue()


def _build_registration_flowables(record, school=None, paper='a3'):
    """Build the ReportLab flowables for a single سجل القيد العام record.

    Returns ``(elements, page_size, MARG)``. Registering the Arabic fonts is a
    side effect. Callers must guarantee ReportLab is available (guard with
    ``_get_rl()``). Shared by the single-record and bulk export helpers so the
    per-record layout is byte-for-byte identical in both.
    """
    from reportlab.lib.pagesizes import A3, A4, landscape
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                     Paragraph, Spacer, Image)
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from app.utils.arabic_numbers import num_to_arabic_words

    arabic_ok = _register_arabic_fonts(pdfmetrics, TTFont)
    fn   = 'Amiri'      if arabic_ok else 'Helvetica'
    fn_b = 'Amiri-Bold' if arabic_ok else 'Helvetica-Bold'
    ar   = _shape_arabic_text

    BLACK  = colors.black
    WHITE  = colors.white
    BORDER = HexColor('#333333')
    NAVY   = HexColor('#1a3a5c')
    LTGREY = HexColor('#f0f0f0')

    # ── Paper size selection ──────────────────────────────────────────────────
    paper = (paper or 'a3').lower()
    if paper == 'a4':
        page_size = landscape(A4)
        MARG = 0.4 * cm   # tighter margins on A4 to maximise usable width
    else:
        page_size = landscape(A3)
        MARG = 0.8 * cm

    pw, ph = page_size

    AW = pw - 2 * MARG   # available content width in points

    # Scale factor: 1.0 for A3, ~0.72 for A4 (A4-landscape AW / A3-landscape AW).
    # Used to proportionally reduce font sizes, padding, row heights, and column
    # widths so the full table fits without horizontal clipping on A4.
    _A3_AW = 1190.55 - 2 * (0.8 * cm)   # ≈ 1145 pt — A3 reference
    scale  = AW / _A3_AW

    # ── Scaled helpers ────────────────────────────────────────────────────────
    _pad = max(1, round(2 * scale))

    def ps(name, font=fn, size=8, align=1, color=BLACK, **kw):
        return ParagraphStyle(name, fontName=font, fontSize=size,
                               alignment=align, textColor=color, **kw)

    title_s = ps('T',  fn_b, max(8,  round(14 * scale)), 1, NAVY)
    hdr_s   = ps('H',  fn_b, max(5,  round(7  * scale)), 1, BLACK)
    shdr_s  = ps('SH', fn_b, max(4,  round(6  * scale)), 1, BLACK)
    cell_s  = ps('C',  fn,   max(5,  round(7  * scale)), 1, BLACK)
    subj_s  = ps('SU', fn_b, max(5,  round(7  * scale)), 2, BLACK)
    foot_s  = ps('F',  fn,   max(4,  round(6  * scale)), 1, HexColor('#666666'))

    def p(text, s=cell_s):
        return Paragraph(ar(str(text or '')), s)

    def ph_(text, s=hdr_s):
        return Paragraph(ar(str(text or '')), s)

    PAD  = [('TOPPADDING',    (0,0), (-1,-1), _pad),
            ('BOTTOMPADDING', (0,0), (-1,-1), _pad),
            ('LEFTPADDING',   (0,0), (-1,-1), _pad),
            ('RIGHTPADDING',  (0,0), (-1,-1), _pad)]
    GRID = [('GRID', (0,0), (-1,-1), 0.5, BORDER),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('ALIGN',  (0,0), (-1,-1), 'CENTER')]

    elements = []

    # ── Collect extra fields ──────────────────────────────────────────────────
    ef  = record.extra_fields   # dict
    gh  = record.academic_history
    sav = gh.get('years', [])
    while len(sav) < 9:
        sav.append({})

    gender_ar = {'male': 'ذكر', 'female': 'أنثى'}.get(
        record.snap_gender or '', record.snap_gender or '')

    # ── 1. TITLE ROW ─────────────────────────────────────────────────────────
    rec_num = ef.get('record_number', '')
    gen_reg = ef.get('general_registry', '')

    title_row = [[
        Paragraph(ar(f'رقم الصحيفة  :  {rec_num}'),
                  ps('TL', fn,   max(6, round(10 * scale)), 0, BLACK)),
        Paragraph(ar(f'سجل القيد العام  :  {gen_reg}'),
                  ps('TR', fn_b, max(8, round(13 * scale)), 2, NAVY)),
    ]]
    title_tbl = Table(title_row, colWidths=[AW * 0.40, AW * 0.60])
    title_tbl.setStyle(TableStyle([
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(title_tbl)
    elements.append(Spacer(1, max(1, round(3 * scale))))

    # ── 2. TOP STUDENT INFO TABLE ─────────────────────────────────────────────
    # 19 physical columns (left=0 → right=18), scaled proportionally to AW.
    _BASE_TOP_CWS = [95, 55, 80, 55, 55, 45, 40, 55, 60, 65,
                     40, 40, 80, 65, 40, 40, 80, 95, 55]
    _base_sum = sum(_BASE_TOP_CWS)   # 1140
    TOP_CWS = [int(w * AW / _base_sum) for w in _BASE_TOP_CWS]
    TOP_CWS[0] += int(AW) - sum(TOP_CWS)   # absorb integer-division remainder

    # Row 0: main headers
    R0 = [
        ph_('سبب المغادرة والملاحظات'),
        ph_('تاريخ المغادرة'),
        ph_('آخر مدرسة كان فيها'),
        ph_('الصف الذي قبل فيه'),
        ph_('تاريخ دخول المدرسة'),
        ph_('ديانته'),
        ph_('جنسيته'),
        ph_('تاريخ ولادته'),
        ph_('مسقط رأسه'),
        ph_('رقم دفتر نفوس الطالب'),
        ph_('مسكن ولي أمره'),   # colspan 2
        '',
        ph_('اسم ولي أمر الطالب'),
        ph_('صنعة الأب وعنوانه'),
        ph_('مسكن الأب'),        # colspan 2
        '',
        ph_('اسم أبيه وشهرته'),
        ph_('الطالب'),            # colspan 2
        '',
    ]

    # Row 1: sub-headers for spanned groups
    R1 = [
        '', '', '', '', '', '', '', '', '', '',
        ph_('المحلة',    shdr_s),
        ph_('رقم الدار', shdr_s),
        '', '',
        ph_('المحلة',    shdr_s),
        ph_('رقم الدار', shdr_s),
        '',
        ph_('اسمه',      shdr_s),
        ph_('رقم قيده',  shdr_s),
    ]

    # Row 2: data values
    dob_str = str(record.snap_date_of_birth)   if record.snap_date_of_birth   else ''
    enr_str = str(record.snap_enrollment_date) if record.snap_enrollment_date else ''
    adm_str = str(record.admission_date)       if record.admission_date       else ''
    R2 = [
        p(ef.get('departure_reason', '')),
        p(ef.get('departure_date', '')),
        p(record.previous_school or ''),
        p(record.snap_grade_name or ''),
        p(enr_str or adm_str),
        p(ef.get('religion', '')),
        p(gender_ar),
        p(dob_str),
        p(ef.get('birth_place', '')),
        p(ef.get('civil_registry_num', '')),
        p(ef.get('guardian_mahalla', '')),
        p(ef.get('guardian_house_num', '')),
        p(record.snap_guardian_name or ''),
        p(ef.get('father_occupation', '')),
        p(ef.get('father_mahalla', '')),
        p(ef.get('father_house_num', '')),
        p(ef.get('father_name', '')),
        p(record.snap_full_name or ''),
        p(record.snap_student_number or ''),
    ]

    top_data  = [R0, R1, R2]
    top_table = Table(top_data, colWidths=TOP_CWS)

    rowspan_cols = [0,1,2,3,4,5,6,7,8,9,12,13,16]
    spans = []
    for c in rowspan_cols:
        spans.append(('SPAN', (c, 0), (c, 1)))
    spans += [
        ('SPAN', (10, 0), (11, 0)),
        ('SPAN', (14, 0), (15, 0)),
        ('SPAN', (17, 0), (18, 0)),
    ]

    _fs = max(5, round(7 * scale))
    top_style = TableStyle(
        GRID + PAD + spans + [
            ('BACKGROUND', (0,0), (-1,1), LTGREY),
            ('FONTNAME',   (0,0), (-1,1), fn_b),
            ('FONTSIZE',   (0,0), (-1,-1), _fs),
            ('ROWHEIGHT',  (0,0), (0,0), max(12, round(22 * scale))),
            ('ROWHEIGHT',  (0,1), (0,1), max(8,  round(14 * scale))),
            ('ROWHEIGHT',  (0,2), (0,2), max(12, round(20 * scale))),
        ]
    )
    top_table.setStyle(top_style)
    elements.append(top_table)
    elements.append(Spacer(1, max(2, round(4 * scale))))

    # ── 3. GRADE GRID ────────────────────────────────────────────────────────
    # 20 physical columns:
    #   col 0:    الملاحظات
    #   cols 1-18: 9 years × 2 sub-cols each (n=رقماً, t=كتابة)
    #   col 19:   مواد الدراسة

    NOTES_W = max(30, round(80  * scale))
    SUBJ_W  = max(50, round(110 * scale))
    YCOL_W  = (int(AW) - NOTES_W - SUBJ_W) // 18

    remainder = int(AW) - NOTES_W - SUBJ_W - YCOL_W * 18
    NOTES_W  += remainder   # absorb difference

    GC   = 20
    GCWS = [NOTES_W] + [YCOL_W] * 18 + [SUBJ_W]

    def ync(yi):
        return 17 - 2 * yi   # n-col (number/رقماً)

    def ytc(yi):
        return 18 - 2 * yi   # t-col (text/كتابة)

    YEAR_LABELS = ['الأول', 'الثاني', 'الثالث', 'الرابع', 'الخامس',
                   'السادس', 'السابع', 'الثامن', 'التاسع']

    def blank_row():
        return [''] * GC

    GHR0 = blank_row()
    GHR1 = blank_row()
    GHR2 = blank_row()
    GHR3 = blank_row()

    GHR0[0]  = ph_('الملاحظات')
    GHR0[19] = ph_('مواد\nالدراسة')

    for yi in range(9):
        yr   = sav[yi]
        clas = yr.get('class', '')
        year = yr.get('year', '')
        nc   = ync(yi)
        tc   = ytc(yi)
        GHR0[nc] = ph_(clas or YEAR_LABELS[yi])
        GHR1[nc] = ph_(f'السنة\n{year}' if year else 'السنة')
        GHR2[nc] = ph_('الدرجة')
        GHR3[tc] = ph_('رقماً', shdr_s)
        GHR3[nc] = ph_('كتابة', shdr_s)

    g_data = [GHR0, GHR1, GHR2, GHR3]

    def subj_row(subj_label, key_n, key_t):
        row = blank_row()
        row[19] = Paragraph(ar(subj_label), subj_s)
        for yi in range(9):
            yr = sav[yi]
            n_val = yr.get(key_n, '')
            t_val = yr.get(key_t, '') or num_to_arabic_words(n_val)
            row[ytc(yi)] = p(n_val)
            row[ync(yi)] = p(t_val)
        return row

    for j, subj in enumerate(_SUBJECTS):
        g_data.append(subj_row(subj, f's{j}_n', f's{j}_t'))

    for k in range(3):
        row = blank_row()
        for yi in range(9):
            yr     = sav[yi]
            extras = yr.get('extra', [])
            ex     = extras[k] if k < len(extras) else {}
            name   = ex.get('name', '')
            en_val = ex.get('n', '')
            et_val = ex.get('t', '') or num_to_arabic_words(en_val)
            row[19] = Paragraph(ar(name or ''), subj_s) if name else p('')
            row[ytc(yi)] = p(en_val)
            row[ync(yi)] = p(et_val)
        g_data.append(row)

    def bottom_row(label, key):
        row = blank_row()
        row[19] = Paragraph(ar(label), subj_s)
        for yi in range(9):
            yr = sav[yi]
            row[ync(yi)] = p(yr.get(key, ''))
        return row

    def bottom_row2(label, key_n, key_t):
        row = blank_row()
        row[19] = Paragraph(ar(label), subj_s)
        for yi in range(9):
            yr    = sav[yi]
            n_val = yr.get(key_n, '')
            t_val = yr.get(key_t, '') or num_to_arabic_words(n_val)
            row[ytc(yi)] = p(n_val)
            row[ync(yi)] = p(t_val)
        return row

    total_row = blank_row()
    total_row[19] = Paragraph(ar('المجموع'), subj_s)
    for yi in range(9):
        yr = sav[yi]
        auto_total = 0
        has_num = False
        for j in range(len(_SUBJECTS)):
            v = yr.get(f's{j}_n', '')
            if v:
                try:
                    auto_total += int(v)
                    has_num = True
                except (ValueError, TypeError):
                    pass
        total_n_val = str(auto_total) if has_num else yr.get('total_n', '')
        total_t_val = yr.get('total_t', '') or num_to_arabic_words(total_n_val)
        total_row[ytc(yi)] = p(total_n_val)
        total_row[ync(yi)] = p(total_t_val)
    g_data.append(total_row)

    g_data.append(bottom_row('السلوك', 'behavior'))
    g_data.append(bottom_row('النتيجة', 'result'))
    g_data.append(bottom_row('ملاحظات عن نتائج الدروس المكمل فيها', 'notes_results'))
    g_data.append(bottom_row2('النتيجة النهائية', 'final_result', 'final_result_t'))
    g_data.append(bottom_row('توقيع مدير المدرسة', 'principal_sig'))

    NROWS = len(g_data)

    grid_spans = [
        ('SPAN', (0,  0), (0,  3)),
        ('SPAN', (19, 0), (19, 3)),
    ]
    for yi in range(9):
        nc = ync(yi)
        tc = ytc(yi)
        grid_spans += [
            ('SPAN', (nc, 0), (tc, 0)),
            ('SPAN', (nc, 1), (tc, 1)),
            ('SPAN', (nc, 2), (tc, 2)),
        ]
        for roff in [NROWS-5, NROWS-4, NROWS-3, NROWS-1]:
            grid_spans.append(('SPAN', (nc, roff), (tc, roff)))

    HDR_ROWS = 4
    g_style = TableStyle(
        GRID + PAD + grid_spans + [
            ('BACKGROUND',  (0, 0),  (-1, HDR_ROWS-1), LTGREY),
            ('FONTNAME',    (0, 0),  (-1, HDR_ROWS-1), fn_b),
            ('FONTSIZE',    (0, 0),  (-1, -1),          _fs),
            ('BACKGROUND',  (0, 0),  (0,  -1),          HexColor('#e8f0f8')),
            ('BACKGROUND',  (19, 0), (19, -1),          LTGREY),
            ('FONTNAME',    (19, 0), (19, -1),           fn_b),
            ('ROWHEIGHT',   (0, 0),  (0, 0),  max(10, round(18 * scale))),
            ('ROWHEIGHT',   (0, 1),  (0, 1),  max(8,  round(14 * scale))),
            ('ROWHEIGHT',   (0, 2),  (0, 2),  max(6,  round(12 * scale))),
            ('ROWHEIGHT',   (0, 3),  (0, 3),  max(5,  round(10 * scale))),
        ]
    )
    for r in range(HDR_ROWS, NROWS):
        if (r - HDR_ROWS) % 2 == 0:
            g_style.add('BACKGROUND', (0, r), (18, r), WHITE)
        else:
            g_style.add('BACKGROUND', (0, r), (18, r), HexColor('#f9f9f9'))

    grade_tbl = Table(g_data, colWidths=GCWS)
    grade_tbl.setStyle(g_style)
    elements.append(grade_tbl)
    elements.append(Spacer(1, max(3, round(6 * scale))))

    # ── 4. BOTTOM NOTE ───────────────────────────────────────────────────────
    note_text = ar(
        'ملاحظة: الدرجة الكبرى في الصفوف الأربعة الأولى(10) '
        'الدرجة الصغرى (5) وللصفين الخامس والسادس(100)و(50)'
    )
    elements.append(Paragraph(note_text, foot_s))

    return elements, page_size, MARG


def generate_registration_record_pdf(record, school=None, paper='a3') -> bytes | None:
    """Generate the official سجل القيد العام PDF for a single record.

    paper='a3'  Landscape A3 — default; best quality, matches the physical form.
    paper='a4'  Landscape A4 — all column widths and font sizes scaled
                proportionally to fit without clipping.

    Returns bytes or None if ReportLab is unavailable.
    """
    if not _get_rl():
        return None
    from reportlab.platypus import SimpleDocTemplate

    elements, page_size, MARG = _build_registration_flowables(record, school, paper)
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=page_size,
        leftMargin=MARG, rightMargin=MARG,
        topMargin=MARG, bottomMargin=MARG,
    )
    doc.build(elements)
    return buf.getvalue()


def generate_registration_records_bulk_pdf(records, school=None, paper='a3') -> bytes | None:
    """Generate one PDF containing every record in ``records`` — one record per
    page — reusing the exact single-record layout via
    ``_build_registration_flowables``.

    paper='a3' | 'a4'  Landscape A3/A4, same paper handling as the single-record
    export. ``records`` must already be scoped/ordered by the caller.

    Returns bytes, or None if ReportLab is unavailable or ``records`` is empty.
    """
    if not _get_rl():
        return None
    from reportlab.platypus import SimpleDocTemplate, PageBreak

    all_elements = []
    page_size = MARG = None
    for idx, record in enumerate(records):
        elements, page_size, MARG = _build_registration_flowables(record, school, paper)
        if idx:
            all_elements.append(PageBreak())   # force a new page before each record
        all_elements.extend(elements)

    if not all_elements:
        return None

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=page_size,
        leftMargin=MARG, rightMargin=MARG,
        topMargin=MARG, bottomMargin=MARG,
    )
    doc.build(all_elements)
    return buf.getvalue()


def generate_payroll_report_pdf(records, totals, filters_label='',
                                school=None, arabic_months=None,
                                status_labels=None) -> bytes | None:
    """
    Arabic RTL payroll ledger report PDF (landscape) for a filtered record set.

    records : list of SalaryRecord rows (already filtered/ordered by the caller)
    totals  : dict from the blueprint's _report_totals
    Returns bytes or None when ReportLab is unavailable.
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

    arabic_ok = _register_arabic_fonts(pdfmetrics, TTFont)
    fn   = 'Amiri'      if arabic_ok else 'Helvetica'
    fn_b = 'Amiri-Bold' if arabic_ok else 'Helvetica-Bold'
    ar   = _shape_arabic_text
    arabic_months = arabic_months or ['']*13
    status_labels = status_labels or {}

    HEADER_BG = HexColor('#1a3a5c')
    ALT_BG    = HexColor('#f0f4f8')
    CANCEL_BG = HexColor('#fbe9e9')
    WHITE     = colors.white

    def money(v):
        try:
            return f"{float(v or 0):,.0f}"
        except (TypeError, ValueError):
            return '0'

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=1.2*cm, rightMargin=1.2*cm,
                            topMargin=1.4*cm, bottomMargin=1.4*cm)

    title_s = ParagraphStyle('pt',  fontName=fn_b, fontSize=14, alignment=1,
                             textColor=HexColor('#1a3a5c'))
    sub_s   = ParagraphStyle('ps',  fontName=fn,   fontSize=9,  alignment=1,
                             textColor=HexColor('#555555'))
    th_s    = ParagraphStyle('pth', fontName=fn_b, fontSize=8,  alignment=1, textColor=WHITE)
    td_s    = ParagraphStyle('ptd', fontName=fn,   fontSize=8,  alignment=1)

    elements = []
    school_name = ''
    if school:
        school_name = getattr(school, 'school_name_ar', '') or getattr(school, 'school_name', '')
    if school_name:
        elements.append(Paragraph(ar(school_name), title_s))
        elements.append(Spacer(1, 0.15*cm))
    elements.append(Paragraph(ar('تقرير الرواتب'), title_s))
    if filters_label:
        elements.append(Paragraph(ar(filters_label), sub_s))
    elements.append(Spacer(1, 0.35*cm))

    def p(t):
        return Paragraph(ar(str(t if t not in (None, '') else '—')), td_s)

    def ph(t):
        return Paragraph(ar(str(t or '')), th_s)

    # ── Summary block ─────────────────────────────────────────────────────────
    sum_data = [
        [ph('عدد السجلات'), ph('إجمالي الأساسي'), ph('إجمالي البدلات'),
         ph('إجمالي الخصومات'), ph('إجمالي الصافي'), ph('إجمالي المصروف')],
        [p(totals.get('count', 0)), p(money(totals.get('total_base'))),
         p(money(totals.get('total_allow'))), p(money(totals.get('total_deduct'))),
         p(money(totals.get('total_net'))), p(money(totals.get('total_paid')))],
    ]
    s_tbl = Table(sum_data, colWidths=[3.8*cm]*6)
    s_tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, 0), HEADER_BG),
        ('FONTNAME',      (0, 0), (-1, -1), fn),
        ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('GRID',          (0, 0), (-1, -1), 0.3, HexColor('#cccccc')),
    ]))
    elements.append(s_tbl)
    elements.append(Spacer(1, 0.45*cm))

    # ── Detail table ──────────────────────────────────────────────────────────
    col_h = [ph('#'), ph('الموظف'), ph('الرقم'), ph('القسم'), ph('الشهر/السنة'),
             ph('الأساسي'), ph('البدلات'), ph('الخصومات'), ph('الصافي'),
             ph('الحالة'), ph('تاريخ الصرف')]
    col_w = [0.8*cm, 4.3*cm, 2*cm, 3*cm, 2.6*cm,
             2.3*cm, 2.1*cm, 2.1*cm, 2.4*cm, 1.8*cm, 2.3*cm]

    table_data = [col_h]
    row_meta = []  # (index, is_cancelled)
    for i, r in enumerate(records, 1):
        emp_code = r.employee.employee_id if r.employee else ''
        period   = f"{arabic_months[r.month] if r.month < len(arabic_months) else r.month} {r.year}"
        table_data.append([
            p(i), p(r.employee_name), p(emp_code), p(r.department or '—'), p(period),
            p(money(r.base_salary)), p(money(r.allowances)), p(money(r.deductions)),
            p(money(r.net_salary)), p(status_labels.get(r.status, r.status)),
            p(r.paid_date.strftime('%Y-%m-%d') if r.paid_date else '—'),
        ])
        row_meta.append((i, r.status == 'cancelled'))

    style_cmds = [
        ('BACKGROUND',    (0, 0), (-1, 0), HEADER_BG),
        ('FONTNAME',      (0, 0), (-1, -1), fn),
        ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID',          (0, 0), (-1, -1), 0.3, HexColor('#cccccc')),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]
    for idx, is_cancel in row_meta:
        if is_cancel:
            style_cmds.append(('BACKGROUND', (0, idx), (-1, idx), CANCEL_BG))
        elif idx % 2 == 0:
            style_cmds.append(('BACKGROUND', (0, idx), (-1, idx), ALT_BG))

    tbl = Table(table_data, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle(style_cmds))
    elements.append(tbl)

    elements.append(Spacer(1, 0.35*cm))
    foot_s = ParagraphStyle('pf', fontName=fn, fontSize=8, alignment=1,
                            textColor=HexColor('#9aabb8'))
    elements.append(Paragraph(
        ar(f'تم الإنشاء: {datetime.utcnow().strftime("%Y-%m-%d %H:%M")} | نظام المهندس'),
        foot_s))

    doc.build(elements)
    return buf.getvalue()


def generate_employee_statement_pdf(employee, statement, school=None,
                                    year=None, arabic_months=None) -> bytes | None:
    """
    Arabic RTL salary account statement (ledger) PDF for one employee.
    Returns bytes or None when ReportLab is unavailable.
    """
    if not _get_rl():
        return None

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    arabic_ok = _register_arabic_fonts(pdfmetrics, TTFont)
    fn   = 'Amiri'      if arabic_ok else 'Helvetica'
    fn_b = 'Amiri-Bold' if arabic_ok else 'Helvetica-Bold'
    ar   = _shape_arabic_text

    HEADER_BG = HexColor('#1a3a5c')
    ALT_BG    = HexColor('#f0f4f8')
    WHITE     = colors.white

    def money(v):
        try:
            return f"{float(v or 0):,.0f}"
        except (TypeError, ValueError):
            return '0'

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=1.5*cm, rightMargin=1.5*cm,
                            topMargin=1.5*cm, bottomMargin=1.5*cm)

    title_s = ParagraphStyle('st',  fontName=fn_b, fontSize=15, alignment=1,
                             textColor=HexColor('#1a3a5c'))
    sub_s   = ParagraphStyle('ss',  fontName=fn,   fontSize=10, alignment=1,
                             textColor=HexColor('#555555'))
    th_s    = ParagraphStyle('sth', fontName=fn_b, fontSize=9,  alignment=1, textColor=WHITE)
    td_s    = ParagraphStyle('std', fontName=fn,   fontSize=9,  alignment=1)

    elements = []
    school_name = ''
    if school:
        school_name = getattr(school, 'school_name_ar', '') or getattr(school, 'school_name', '')
    if school_name:
        elements.append(Paragraph(ar(school_name), title_s))
        elements.append(Spacer(1, 0.15*cm))
    elements.append(Paragraph(ar('كشف حساب الراتب'), title_s))

    info_bits = [employee.full_name]
    if employee.job_title:
        info_bits.append(employee.job_title)
    if employee.department:
        info_bits.append(employee.department)
    if year:
        info_bits.append(f'السنة: {year}')
    elements.append(Paragraph(ar('  ·  '.join(info_bits)), sub_s))
    elements.append(Spacer(1, 0.35*cm))

    def p(t, style=td_s):
        return Paragraph(ar(str(t if t not in (None, '') else '—')), style)

    def ph(t):
        return Paragraph(ar(str(t or '')), th_s)

    # Summary
    sum_data = [
        [ph('إجمالي المستحق'), ph('إجمالي المصروف'), ph('الرصيد')],
        [p(money(statement.get('total_credit'))),
         p(money(statement.get('total_debit'))),
         p(money(statement.get('balance')))],
    ]
    s_tbl = Table(sum_data, colWidths=[5.5*cm]*3)
    s_tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, 0), HEADER_BG),
        ('FONTNAME',      (0, 0), (-1, -1), fn),
        ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('GRID',          (0, 0), (-1, -1), 0.3, HexColor('#cccccc')),
    ]))
    elements.append(s_tbl)
    elements.append(Spacer(1, 0.45*cm))

    col_h = [ph('التاريخ'), ph('النوع'), ph('الوصف'),
             ph('مستحق'), ph('مصروف'), ph('الرصيد'), ph('المرجع')]
    col_w = [2.3*cm, 2.6*cm, 4.6*cm, 2.3*cm, 2.3*cm, 2.3*cm, 2.2*cm]

    table_data = [col_h]
    rows = statement.get('rows', [])
    for i, rrow in enumerate(rows, 1):
        table_data.append([
            p(rrow['date'].strftime('%Y-%m-%d') if rrow.get('date') else '—'),
            p(rrow.get('type', '')),
            p(rrow.get('description', '')),
            p(money(rrow.get('credit')) if rrow.get('credit') else '—'),
            p(money(rrow.get('debit')) if rrow.get('debit') else '—'),
            p(money(rrow.get('balance'))),
            p(rrow.get('ref', '')),
        ])

    style_cmds = [
        ('BACKGROUND',    (0, 0), (-1, 0), HEADER_BG),
        ('FONTNAME',      (0, 0), (-1, -1), fn),
        ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID',          (0, 0), (-1, -1), 0.3, HexColor('#cccccc')),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]
    for i in range(1, len(rows) + 1):
        if i % 2 == 0:
            style_cmds.append(('BACKGROUND', (0, i), (-1, i), ALT_BG))

    if rows:
        tbl = Table(table_data, colWidths=col_w, repeatRows=1)
        tbl.setStyle(TableStyle(style_cmds))
        elements.append(tbl)
    else:
        elements.append(Paragraph(ar('لا توجد حركات مالية لهذا الموظف.'), sub_s))

    elements.append(Spacer(1, 0.4*cm))
    foot_s = ParagraphStyle('sf', fontName=fn, fontSize=8, alignment=1,
                            textColor=HexColor('#9aabb8'))
    elements.append(Paragraph(
        ar(f'تم الإنشاء: {datetime.utcnow().strftime("%Y-%m-%d %H:%M")} | نظام المهندس'),
        foot_s))

    doc.build(elements)
    return buf.getvalue()
