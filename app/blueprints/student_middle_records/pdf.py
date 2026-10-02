"""Fixed A4 PDF layout for official middle student records."""
from html import escape
from io import BytesIO

from app.utils.pdf_gen import (
    _get_rl,
    _register_arabic_fonts,
    _shape_arabic_text,
)


def generate_middle_record_pdf(record, subjects, grade_columns):
    """Render one saved middle-record snapshot as a one-page A4 portrait PDF."""
    if not _get_rl():
        return None

    try:
        import arabic_reshaper  # noqa: F401
        from bidi.algorithm import get_display  # noqa: F401
    except ImportError:
        return None

    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    if not _register_arabic_fonts(pdfmetrics, TTFont):
        return None

    font = 'Amiri'
    bold_font = 'Amiri-Bold'
    black = '#111111'
    border = '#222222'
    margin = 7 * mm
    usable_width = A4[0] - 2 * margin

    def style(name, font_name=font, size=7, leading=None, alignment=TA_CENTER):
        return ParagraphStyle(
            name,
            fontName=font_name,
            fontSize=size,
            leading=leading or size + 1.5,
            alignment=alignment,
            textColor=black,
            spaceBefore=0,
            spaceAfter=0,
            splitLongWords=1,
        )

    title_style = style('MiddleRecordTitle', bold_font, 13, 16)
    section_style = style('MiddleRecordSection', bold_font, 8, 10)
    header_style = style('MiddleRecordHeader', bold_font, 6.4, 8)
    cell_style = style('MiddleRecordCell', font, 7, 9)
    subject_style = style('MiddleRecordSubject', bold_font, 7, 9, TA_RIGHT)
    result_style = style('MiddleRecordResult', font, 8, 10, TA_RIGHT)
    footer_style = style('MiddleRecordFooter', bold_font, 8, 10, TA_LEFT)

    def paragraph(value, paragraph_style):
        text = '' if value is None else str(value)
        if not text.strip():
            return Paragraph('&nbsp;', paragraph_style)
        lines = [escape(_shape_arabic_text(line)) for line in text.splitlines()]
        return Paragraph('<br/>'.join(lines), paragraph_style)

    def cell(text, paragraph_style=cell_style):
        return paragraph(text, paragraph_style)

    story = []
    page_number = record.page_number or ''
    year_name = record.snap_year_name or ''
    top = Table(
        [[
            cell(f'رقم الصفحة\n({page_number})', header_style),
            cell('سجل الدرجات\nللمدارس المتوسطة', title_style),
            cell(f'السنة الدراسية\n{year_name}', header_style),
        ]],
        colWidths=[150, 255, 150],
        rowHeights=[42],
    )
    top.setStyle(TableStyle([
        ('BOX', (0, 0), (0, 0), 0.9, border),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('LEFTPADDING', (0, 0), (-1, -1), 3),
        ('RIGHTPADDING', (0, 0), (-1, -1), 3),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    story.extend([top, Spacer(1, 4)])

    school_name = record.school_name_ar or record.school_name or ''
    school_box = Table([[cell(school_name, section_style)]],
                       colWidths=[usable_width], rowHeights=[25])
    school_box.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 1.0, border),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    story.extend([school_box, Spacer(1, 5)])

    student_fields = [
        ('سنوات الرسوب', record.years_failed),
        ('المواليد', record.snap_date_of_birth.strftime('%Y-%m-%d')
         if record.snap_date_of_birth else ''),
        ('رقم القيد', record.record_number or record.snap_student_number or ''),
        ('الصف والشعبة', ' / '.join(filter(None, [
            record.snap_grade_name, record.snap_section_name,
        ]))),
        ('اسم أب الجد', record.great_grandfather_name),
        ('اسم الجد', record.grandfather_name),
        ('اسم الأب', record.father_name),
        ('اسم الطالب', record.snap_full_name),
    ]
    student_widths = [60, 75, 70, 90, 55, 55, 55, 95]
    student_rows = [
        [cell(label, header_style) for label, _value in student_fields],
        [cell(value) for _label, value in student_fields],
    ]
    student_table = Table(student_rows, colWidths=student_widths,
                          rowHeights=[23, 29])
    student_table.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.7, border),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('LEFTPADDING', (0, 0), (-1, -1), 2),
        ('RIGHTPADDING', (0, 0), (-1, -1), 2),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    story.extend([student_table, Spacer(1, 6)])

    header_lines = {
        'first_term_average': 'معدل الفصل\nالأول',
        'mid_year': 'نصف\nالسنة',
        'second_term_average': 'معدل الفصل\nالثاني',
        'annual_effort': 'درجة السعي\nالسنوي',
        'final_exam': 'درجة الامتحان\nالنهائي',
        'final_grade': 'الدرجة\nالنهائية',
        'completion_grade': 'درجة\nالإكمال',
        'final_after_completion': 'الدرجة النهائية\nبعد الإكمال',
        'notes': 'الملاحظات',
    }
    visual_columns = list(reversed(grade_columns))
    reversed_subjects = list(reversed(subjects))
    grid_widths = [105, 62, 44, 44, 48, 47, 45, 45, 45, 70]
    grid_data = [[
        cell(header_lines.get(key, label), header_style)
        for key, label in visual_columns
    ] + [cell('المادة', header_style)]]

    subject_grades = record.subject_grades
    if not isinstance(subject_grades, dict):
        subject_grades = {}

    for subject_key, subject_label in reversed_subjects:
        values = subject_grades.get(subject_key, {})
        if not isinstance(values, dict):
            values = {}
        grid_data.append([
            cell(values.get(key), cell_style) for key, _label in visual_columns
        ] + [cell(subject_label, subject_style)])

    grid_data.append([
        cell(record.total_score, cell_style),
        *[cell('', cell_style) for _ in range(8)],
        cell('المجموع', subject_style),
    ])
    grid_table = Table(
        grid_data,
        colWidths=grid_widths,
        rowHeights=[58] + [25] * len(subjects) + [25],
        repeatRows=1,
    )
    grid_table.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.65, border),
        ('SPAN', (0, -1), (8, -1)),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('LEFTPADDING', (0, 0), (-1, -1), 2),
        ('RIGHTPADDING', (0, 0), (-1, -1), 2),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    story.extend([grid_table, Spacer(1, 6)])

    result_rows = [
        [cell(record.first_round_result, result_style),
         cell('نتيجة الدور الأول', header_style)],
        [cell(record.second_round_result, result_style),
         cell('نتيجة الدور الثاني', header_style)],
        [cell(record.result_notes, result_style),
         cell('الملاحظات', header_style)],
    ]
    results_table = Table(result_rows, colWidths=[usable_width - 125, 125],
                          rowHeights=[29, 29, 38])
    results_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.9, border),
        ('INNERGRID', (0, 0), (-1, -1), 0.65, border),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    story.extend([results_table, Spacer(1, 9)])

    footer = Table([[cell('مدير المدرسة', footer_style), cell('')]],
                   colWidths=[usable_width / 2, usable_width / 2], rowHeights=[20])
    footer.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 3),
        ('RIGHTPADDING', (0, 0), (-1, -1), 3),
    ]))
    story.append(footer)

    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
        title='السجل الوسطي',
    )
    document.build(story)
    return output.getvalue()