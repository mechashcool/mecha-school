"""Fixed A4 layout for the official middle student record PDF."""
from io import BytesIO

from app.utils.pdf_gen import (
    _get_rl,
    _register_arabic_fonts,
    _shape_arabic_text,
)

def y_from_top(top_mm, height_mm=0):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm

    return A4[1] - (top_mm + height_mm) * mm


PAGE_NUMBER = dict(x=10, top=8, w=48, h=14, radius=3)
HEADER_CENTER = dict(x=61, top=5.5, w=88, h=19)
SCHOOL_BOX = dict(x=24, top=27, w=162, h=14, radius=3)

STUDENT_TABLE = dict(x=8, top=45, w=194, header_h=11, value_h=10)
STUDENT_COLUMNS = [
    ('اسم الطالب', 25),
    ('اسم الأب', 24),
    ('اسم الجد', 22),
    ('اسم أب الجد', 23),
    ('الصف والشعبة', 29),
    ('رقم القيد', 23),
    ('المواليد', 24),
    ('سنوات الرسوب', 24),
]

GRADE_TABLE = dict(x=8, top=71, w=194, header_h=29, row_h=10.3, total_h=9)
GRADE_COLUMNS = [
    ('المادة', 30),
    ('معدل الفصل الأول', 17),
    ('نصف السنة', 15),
    ('معدل الفصل الثاني', 17),
    ('درجة السعي السنوي', 18),
    ('درجة الامتحان النهائي', 20),
    ('الدرجة النهائية', 16),
    ('درجة الإكمال', 16),
    ('الدرجة النهائية بعد الإكمال', 22),
    ('الملاحظات', 23),
]

TOTAL_KEYS = [
    'first_term_average',
    'mid_year',
    'second_term_average',
    'annual_effort',
    'final_exam',
    'final_grade',
    'completion_grade',
    'final_after_completion',
]

SUBJECTS_TOP_TO_BOTTOM = [
    ('islamic_education', 'التربية الإسلامية'),
    ('arabic_language', 'اللغة العربية'),
    ('english_language', 'اللغة الإنكليزية'),
    ('mathematics', 'الرياضيات'),
    ('physics', 'الفيزياء'),
    ('chemistry', 'الكيمياء'),
    ('biology', 'الأحياء'),
    ('social_studies', 'الاجتماعيات'),
    ('computer', 'الحاسوب'),
    ('moral_education', 'التربية الأخلاقية'),
    ('physical_education', 'التربية الرياضية'),
    ('art_education', 'التربية الفنية'),
]

RESULT_1 = dict(x=10, top=246, w=190, h=11, radius=3, label_w=42)
RESULT_2 = dict(x=10, top=260, w=190, h=11, radius=3, label_w=42)
MANAGER = dict(x=18, top=276)

OUTER_BORDER = 0.45
INNER_BORDER = 0.22
STUDENT_BORDER = 0.30


def blank_safe_number(value):
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_column_totals(subject_grades):
    totals = []
    subject_grades = subject_grades or {}
    for key in TOTAL_KEYS:
        total = 0.0
        seen = False
        for subject_key, _subject_label in SUBJECTS_TOP_TO_BOTTOM:
            row = subject_grades.get(subject_key, {}) or {}
            number = blank_safe_number(row.get(key))
            if number is None:
                continue
            total += number
            seen = True
        if not seen:
            totals.append('')
        elif total.is_integer():
            totals.append(str(int(total)))
        else:
            totals.append(str(total))
    return totals


def generate_middle_record_pdf(record, subjects, grade_columns):
    """Render one saved middle-record snapshot as a one-page A4 portrait PDF."""
    if not _get_rl():
        return None

    try:
        import arabic_reshaper  # noqa: F401
        from bidi.algorithm import get_display  # noqa: F401
    except ImportError:
        return None

    from reportlab.lib.colors import black
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    if not _register_arabic_fonts(pdfmetrics, TTFont):
        return None

    font = 'Amiri'
    bold_font = 'Amiri-Bold'
    page_height = A4[1]
    output = BytesIO()
    page = canvas.Canvas(output, pagesize=A4, pageCompression=1)
    page.setTitle('السجل الوسطي')
    page.setAuthor('Core School')
    page.setFillColor(black)
    page.setStrokeColor(black)

    def x_pt(value):
        return value * mm

    def box_y(top, height):
        return page_height - (top + height) * mm

    def shaped(value):
        if value is None:
            return ''
        return _shape_arabic_text(str(value))

    def draw_lines(value, x, y, width, height, size, bold=False,
                   align='center', leading=None, padding=0.8):
        text = '' if value is None else str(value)
        raw_lines = text.splitlines() or ['']
        leading = leading or size * 1.12
        max_width = x_pt(width - 2 * padding)
        lines = []
        for raw_line in raw_lines:
            words = raw_line.split()
            if not words:
                lines.append('')
                continue
            current = words[0]
            for word in words[1:]:
                candidate = f'{current} {word}'
                if pdfmetrics.stringWidth(shaped(candidate), bold_font if bold else font, size) <= max_width:
                    current = candidate
                else:
                    lines.append(current)
                    current = word
            lines.append(current)

        max_lines = max(1, int((x_pt(height) - x_pt(1)) / (leading * 0.85)))
        lines = lines[:max_lines]
        center_y = y + x_pt(height) / 2
        first_baseline = center_y + ((len(lines) - 1) * leading / 2) - size * 0.35
        page.setFont(bold_font if bold else font, size)
        for index, line in enumerate(lines):
            display_text = shaped(line)
            baseline = first_baseline - index * leading
            if align == 'right':
                page.drawRightString(x + x_pt(width - padding), baseline, display_text)
            elif align == 'left':
                page.drawString(x + x_pt(padding), baseline, display_text)
            else:
                page.drawCentredString(x + x_pt(width / 2), baseline, display_text)

    def draw_cell(x, top, width, height, line_width, value, size,
                  bold=False, align='center', leading=None, padding=0.8):
        y = box_y(top, height)
        page.setLineWidth(line_width * mm)
        page.rect(x_pt(x), y, x_pt(width), x_pt(height), stroke=1, fill=0)
        draw_lines(value, x_pt(x), y, width, height, size, bold=bold,
                   align=align, leading=leading, padding=padding)

    def draw_round_box(spec, text, size, bold=False):
        y = box_y(spec['top'], spec['h'])
        page.setLineWidth(OUTER_BORDER * mm)
        page.roundRect(x_pt(spec['x']), y, x_pt(spec['w']), x_pt(spec['h']),
                       x_pt(spec['radius']), stroke=1, fill=0)
        draw_lines(text, x_pt(spec['x']), y, spec['w'], spec['h'], size,
                   bold=bold, leading=size * 1.1)

    def draw_result_box(spec, label, value):
        x = spec['x']
        y = box_y(spec['top'], spec['h'])
        width = spec['w']
        height = spec['h']
        label_x = x + width - spec['label_w']
        page.setLineWidth(OUTER_BORDER * mm)
        page.roundRect(x_pt(x), y, x_pt(width), x_pt(height),
                       x_pt(spec['radius']), stroke=1, fill=0)
        page.setLineWidth(STUDENT_BORDER * mm)
        page.line(x_pt(label_x), y, x_pt(label_x), y + x_pt(height))
        draw_lines(value, x_pt(x), y, width - spec['label_w'], height,
                   13, align='right', padding=3)
        draw_lines(label, x_pt(label_x), y, spec['label_w'], height,
                   12, bold=True, padding=1)

    page_number = record.page_number or ''
    year_name = record.snap_year_name or ''
    draw_round_box(PAGE_NUMBER, f'رقم الصفحة ( {page_number} )', 14, bold=True)

    header_x = x_pt(HEADER_CENTER['x'])
    header_y = box_y(HEADER_CENTER['top'], HEADER_CENTER['h'])
    header_center_x = header_x + x_pt(HEADER_CENTER['w'] / 2)
    page.setFont(bold_font, 17)
    page.drawCentredString(header_center_x, header_y + x_pt(14), shaped('سجل الدرجات'))
    page.setFont(bold_font, 14)
    page.drawCentredString(header_center_x, header_y + x_pt(8.2), shaped('للمدارس المتوسطة'))
    page.setFont(bold_font, 12)
    page.drawCentredString(
        header_center_x,
        header_y + x_pt(2.1),
        shaped(f'للسنة الدراسية  {year_name}'),
    )

    school_name = record.school_name_ar or record.school_name or ''
    draw_round_box(SCHOOL_BOX, school_name, 16, bold=True)

    student_values = [
        record.snap_full_name or '',
        record.father_name or '',
        record.grandfather_name or '',
        record.great_grandfather_name or '',
        ' / '.join(filter(None, [record.snap_grade_name, record.snap_section_name])),
        record.record_number or record.snap_student_number or '',
        record.snap_date_of_birth.strftime('%Y-%m-%d') if record.snap_date_of_birth else '',
        record.years_failed or '',
    ]
    right = STUDENT_TABLE['x'] + STUDENT_TABLE['w']
    header_top = STUDENT_TABLE['top']
    value_top = header_top + STUDENT_TABLE['header_h']
    for (label, width), value in zip(STUDENT_COLUMNS, student_values):
        left = right - width
        draw_cell(left, header_top, width, STUDENT_TABLE['header_h'], STUDENT_BORDER,
                  label, 10.5, bold=True, leading=11, padding=0.6)
        draw_cell(left, value_top, width, STUDENT_TABLE['value_h'], STUDENT_BORDER,
                  value, 10.5, leading=11, padding=0.6)
        right = left

    saved_grades = record.subject_grades
    if not isinstance(saved_grades, dict):
        saved_grades = {}
    totals = dict(zip(TOTAL_KEYS, compute_column_totals(saved_grades)))
    total_by_key = {key: totals.get(key, '') for key in TOTAL_KEYS}

    columns_by_label = {label: key for key, label in grade_columns}
    displayed_columns = []
    for label, width in GRADE_COLUMNS:
        key = columns_by_label.get(label)
        displayed_columns.append((key, label, width))

    table_left = GRADE_TABLE['x']
    header_top = GRADE_TABLE['top']
    header_height = GRADE_TABLE['header_h']
    header_data = [('subject', 'المادة', 30)] + [
        (key, label, width) for key, label, width in displayed_columns
    ]
    visual_header = list(reversed(header_data))
    current_x = table_left
    for key, label, width in visual_header:
        header_text = label
        if label == 'معدل الفصل الأول':
            header_text = 'معدل الفصل\nالأول'
        elif label == 'نصف السنة':
            header_text = 'نصف\nالسنة'
        elif label == 'معدل الفصل الثاني':
            header_text = 'معدل الفصل\nالثاني'
        elif label == 'درجة السعي السنوي':
            header_text = 'درجة السعي\nالسنوي'
        elif label == 'درجة الامتحان النهائي':
            header_text = 'درجة الامتحان\nالنهائي'
        elif label == 'الدرجة النهائية':
            header_text = 'الدرجة\nالنهائية'
        elif label == 'درجة الإكمال':
            header_text = 'درجة\nالإكمال'
        elif label == 'الدرجة النهائية بعد الإكمال':
            header_text = 'الدرجة النهائية\nبعد الإكمال'
        draw_cell(current_x, header_top, width, header_height, INNER_BORDER,
                  header_text, 9.2, bold=True, leading=10.2, padding=0.7)
        current_x += width

    def draw_grade_row(top, height, subject_label, values, total=False):
        row_x = table_left
        for key, label, width in visual_header:
            if key == 'subject':
                value = subject_label
                size = 9.5 if not total else 9.2
                bold = True
                align = 'right'
                padding = 1.2
            else:
                value = values.get(key, '')
                size = 8.8
                bold = total
                align = 'center'
                padding = 0.6
            draw_cell(row_x, top, width, height, INNER_BORDER, value, size,
                      bold=bold, align=align, padding=padding)
            row_x += width

    row_top = header_top + header_height
    for subject_key, subject_label in SUBJECTS_TOP_TO_BOTTOM:
        row = saved_grades.get(subject_key, {}) or {}
        if not isinstance(row, dict):
            row = {}
        draw_grade_row(row_top, GRADE_TABLE['row_h'], subject_label, row)
        row_top += GRADE_TABLE['row_h']

    total_values = dict(total_by_key)
    total_values['notes'] = ''
    draw_grade_row(row_top, GRADE_TABLE['total_h'], 'المجموع', total_values, total=True)
    table_bottom = row_top + GRADE_TABLE['total_h']
    page.setLineWidth(OUTER_BORDER * mm)
    page.rect(x_pt(table_left), box_y(table_bottom, 0),
              x_pt(GRADE_TABLE['w']), x_pt(table_bottom - GRADE_TABLE['top']),
              stroke=1, fill=0)

    draw_result_box(RESULT_1, 'نتيجة الدور الأول:', record.first_round_result or '')
    draw_result_box(RESULT_2, 'نتيجة الدور الثاني:', record.second_round_result or '')

    manager_y = y_from_top(MANAGER['top']) - pdfmetrics.getAscent(bold_font, 11.5)
    page.setFont(bold_font, 11.5)
    page.drawString(x_pt(MANAGER['x']), manager_y, shaped('مدير المدرسة'))

    page.showPage()
    page.save()
    return output.getvalue()