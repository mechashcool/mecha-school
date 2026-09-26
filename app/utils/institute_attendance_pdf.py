"""
Institute attendance report — PDF export (institutes only).

Visually mirrors the school attendance report PDF
(app/utils/pdf_gen.generate_attendance_report_pdf): landscape A4, 1.5 cm
margins, the same Arabic font registration and shaping helpers, the same
institution-name / title / period header, header colour, zebra rows, grid,
status palette and "Core School" footer line. That module is only IMPORTED
from here, never modified, so the school PDF cannot change because of this
file.

This module does no querying and no attendance maths: it renders the dict that
app.services.institute_attendance.attendance_report() already produced for
the HTML page, so the page and the file cannot disagree. In particular the
'unrecorded' bucket is printed as its own column and is never folded into
absent.
"""
from datetime import datetime
from io import BytesIO

from app.utils.pdf_gen import (_get_rl, _register_arabic_fonts,
                               _shape_arabic_text)

# Upper bound on detail rows rendered into one file. The summary always covers
# every matching row; when the detail is longer, the PDF says so explicitly.
# Sized so a worst-case export stays well inside the web worker's timeout.
PDF_ROW_LIMIT = 10000

# ReportLab splits one long Table across pages by re-measuring the remainder,
# which grows badly with length; fixed-size chunks keep rendering linear.
_CHUNK_ROWS = 400

_STATUS_COLORS = {
    'present':  '#1aab6d',
    'late':     '#e8a020',
    'absent':   '#e03e3e',
    'excused':  '#2e7d32',
}
_MUTED = '#8b98a8'


def summary_cells(report, status_labels, unrecorded, unrecorded_label):
    """[(label, value)] for the summary table, in Arabic reading order.

    Every value is read from the service result as-is. The rate is the
    service's own figure; nothing is recomputed here.
    """
    t = report['totals']
    rate = report.get('rate')
    return [
        ('الحصص',             report['lessons']),
        ('حصص مُسجَّلة',        report['recorded_lessons']),
        ('حصص غير مُسجَّلة',    report['unrecorded_lessons']),
        (status_labels['present'], t['present']),
        (status_labels['late'],    t['late']),
        (status_labels['absent'],  t['absent']),
        (status_labels['excused'], t['excused']),
        (unrecorded_label,         t[unrecorded]),
        ('نسبة الحضور',        f'{rate}%' if rate is not None else '—'),
    ]


def generate_institute_attendance_report_pdf(
        report, *, school=None, date_from='', date_to='', group_label='',
        student_label='', status_labels=None, unrecorded='unrecorded',
        unrecorded_label='غير مسجلة', day_names=None, local_dt=None,
        row_limit=PDF_ROW_LIMIT) -> bytes | None:
    """Render one institute attendance report. Returns bytes, or None when
    ReportLab is unavailable (the caller then flashes and redirects, exactly
    like the school export)."""
    if not _get_rl():
        return None

    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer)
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    status_labels = status_labels or {}
    day_names = day_names or []

    arabic_ok = _register_arabic_fonts(pdfmetrics, TTFont)
    fn   = 'Amiri'      if arabic_ok else 'Helvetica'
    fn_b = 'Amiri-Bold' if arabic_ok else 'Helvetica-Bold'

    # Shaping is the expensive part of a long export and most cell values
    # repeat (weekday, group, status), so each distinct string is shaped once.
    _shaped = {}

    def ar(text):
        key = '' if text is None else str(text)
        if key not in _shaped:
            _shaped[key] = _shape_arabic_text(key)
        return _shaped[key]

    HEADER_BG = HexColor('#1a3a5c')
    ALT_BG    = HexColor('#f0f4f8')
    GRID      = HexColor('#cccccc')
    WHITE     = colors.white

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=1.5*cm, rightMargin=1.5*cm,
                            topMargin=1.5*cm, bottomMargin=1.5*cm,
                            title='تقرير الحضور')

    school_s = ParagraphStyle('isch', fontName=fn_b, fontSize=16, leading=21,
                              alignment=1, textColor=HexColor('#1a3a5c'))
    title_s  = ParagraphStyle('it',  fontName=fn_b, fontSize=12, leading=17,
                              alignment=1, textColor=HexColor('#2c5578'))
    sub_s    = ParagraphStyle('is2', fontName=fn,   fontSize=9.5, leading=14,
                              alignment=1, textColor=HexColor('#6b7a8d'))
    th_s     = ParagraphStyle('ith', fontName=fn_b, fontSize=8,
                              alignment=1, textColor=WHITE)
    td_s     = ParagraphStyle('itd', fontName=fn,   fontSize=8, alignment=1)
    note_s   = ParagraphStyle('inote', fontName=fn, fontSize=8.5, leading=13,
                              alignment=1, textColor=HexColor('#b26a00'))
    def p(t, style=td_s):
        return Paragraph(ar(t if t not in (None, '') else '—'), style)

    def ph(t):
        return Paragraph(ar(t), th_s)

    def table_style(n_rows):
        cmds = [
            ('BACKGROUND',    (0, 0), (-1, 0), HEADER_BG),
            ('FONTNAME',      (0, 0), (-1, -1), fn),
            ('FONTSIZE',      (0, 1), (-1, -1), 8),
            ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
            ('GRID',          (0, 0), (-1, -1), 0.3, GRID),
            ('TOPPADDING',    (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]
        for ri in range(2, n_rows, 2):
            cmds.append(('BACKGROUND', (0, ri), (-1, ri), ALT_BG))
        return TableStyle(cmds)

    elements = []

    # ── Header — identical hierarchy to the school attendance PDF ────────────
    school_name = ''
    if school is not None:
        school_name = (getattr(school, 'school_name_ar', '')
                       or getattr(school, 'school_name', ''))
    if school_name:
        elements.append(Paragraph(ar(school_name), school_s))
        elements.append(Spacer(1, 0.18*cm))
    elements.append(Paragraph(ar('تقرير الحضور'), title_s))
    elements.append(Spacer(1, 0.1*cm))
    elements.append(Paragraph(ar(f'الفترة: {date_from}  —  {date_to}'), sub_s))
    elements.append(Paragraph(ar(f'المجموعة الدراسية: {group_label}'), sub_s))
    if student_label:
        elements.append(Paragraph(ar(f'الطالب: {student_label}'), sub_s))
    elements.append(Spacer(1, 0.4*cm))

    # ── Summary — values straight from the service result ────────────────────
    # ReportLab lays cells out left -> right, so every table here is STORED in
    # reverse of the Arabic reading order (same technique as the school PDF's
    # daily table) and therefore reads right -> left on the page.
    cells = list(reversed(summary_cells(report, status_labels, unrecorded,
                                        unrecorded_label)))
    s_tbl = Table([[ph(lbl) for lbl, _v in cells],
                   [p(v) for _lbl, v in cells]],
                  colWidths=[2.8*cm] * len(cells))
    s_tbl.setStyle(table_style(2))
    elements.append(s_tbl)
    elements.append(Spacer(1, 0.2*cm))
    elements.append(Paragraph(ar(
        f'«{unrecorded_label}» تعني حصة لم يُسجَّل فيها حضور الطالب، ولا تُحتسب غياباً. '
        'نسبة الحضور = (حاضر + متأخر) ÷ (حاضر + متأخر + غائب).'), sub_s))
    elements.append(Spacer(1, 0.4*cm))

    # ── Detail rows ──────────────────────────────────────────────────────────
    rows = report.get('rows') or []
    shown = rows[:row_limit] if row_limit else rows
    if len(shown) < len(rows):
        elements.append(Paragraph(ar(
            f'تم تضمين أول {len(shown)} سجل تفصيلي من أصل {len(rows)} في هذا الملف. '
            'الملخص أعلاه يشمل جميع السجلات؛ ضيّق الفلاتر لتصدير التفاصيل كاملة.'),
            note_s))
        elements.append(Spacer(1, 0.3*cm))

    # Reading order: # | التاريخ | اليوم | الطالب | المجموعة | التوقيت |
    #                حالة الحصة | الحالة | وقت التسجيل | ملاحظات
    head = [ph('#'), ph('التاريخ'), ph('اليوم'), ph('الطالب'), ph('المجموعة'),
            ph('التوقيت'), ph('حالة الحصة'), ph('الحالة'), ph('وقت التسجيل'),
            ph('ملاحظات')]
    widths = [1.0*cm, 2.2*cm, 1.8*cm, 5.0*cm, 4.2*cm,
              2.6*cm, 2.2*cm, 2.0*cm, 2.8*cm, 2.9*cm]
    head.reverse()
    widths.reverse()

    # Short fixed-vocabulary cells (weekday, lesson state, status) are plain
    # pre-shaped strings coloured with a TEXTCOLOR command: a Paragraph per
    # cell is what makes a long export slow. Free text that may need wrapping
    # (student, group, notes) stays a Paragraph.
    n_cols = len(head)
    col = {name: n_cols - 1 - idx for idx, name in enumerate(
        ('#', 'date', 'day', 'student', 'group', 'time', 'lesson', 'status',
         'recorded_at', 'notes'))}
    status_color = {k: HexColor(c) for k, c in _STATUS_COLORS.items()}
    muted = HexColor(_MUTED)
    group_cache = {}

    def group_cell(group):
        key = getattr(group, 'id', id(group))
        if key not in group_cache:
            txt = group.name
            if getattr(group, 'subject', None) is not None:
                txt = f'{group.name} — {group.subject.name}'
            group_cache[key] = txt
        return p(group_cache[key])

    def detail_row(i, r):
        status = r['status']
        label = status_labels.get(status, unrecorded_label
                                  if status == unrecorded else status)
        start, end = r.get('start_time'), r.get('end_time')
        rec_at = r.get('recorded_at')
        if rec_at is not None and local_dt is not None:
            rec_at = local_dt(rec_at)
        dow = r.get('day_of_week')
        notes = r.get('notes')
        cells_ = [
            str(i),
            r['date'].strftime('%Y-%m-%d'),
            ar(day_names[dow] if dow is not None and 0 <= dow < len(day_names) else '—'),
            p(r['student'].full_name),
            group_cell(r['group']),
            f'{start.strftime("%H:%M") if start else "—"} - '
            f'{end.strftime("%H:%M") if end else "—"}',
            ar('مُسجَّلة' if r.get('lesson_recorded') else 'غير مُسجَّلة'),
            ar(label),
            rec_at.strftime('%Y-%m-%d %H:%M') if rec_at else '—',
            p(notes) if notes else '—',
        ]
        cells_.reverse()
        return cells_

    if shown:
        for base in range(0, len(shown), _CHUNK_ROWS):
            chunk = shown[base:base + _CHUNK_ROWS]
            data = [head] + [detail_row(base + j + 1, r)
                             for j, r in enumerate(chunk)]
            style = table_style(len(data))
            for ri, r in enumerate(chunk, 1):
                style.add('TEXTCOLOR', (col['status'], ri), (col['status'], ri),
                          status_color.get(r['status'], muted))
                style.add('FONTNAME', (col['status'], ri), (col['status'], ri),
                          fn_b if r['status'] in status_color else fn)
                if not r.get('lesson_recorded'):
                    style.add('TEXTCOLOR', (col['lesson'], ri),
                              (col['lesson'], ri), muted)
            tbl = Table(data, colWidths=widths, repeatRows=1)
            tbl.setStyle(style)
            elements.append(tbl)
    else:
        elements.append(Paragraph(ar('لا توجد سجلات حضور مطابقة للفلاتر المحددة.'),
                                  sub_s))

    # ── Footer — same attribution line as the school attendance PDF ──────────
    elements.append(Spacer(1, 0.4*cm))
    foot_s = ParagraphStyle('if2', fontName=fn, fontSize=8, leading=12,
                            alignment=1, textColor=HexColor('#9aabb8'))
    elements.append(Paragraph(
        ar('تم إنشاء هذا التقرير بواسطة Core School — نظام إدارة المدارس'
           f'  |  تم الإنشاء: {datetime.utcnow().strftime("%Y-%m-%d %H:%M")}'),
        foot_s))

    doc.build(elements)
    return buf.getvalue()
