"""
Demo seed script — Arabic students + teacher employees for presentation.

Run from the project root:
    python seed_demo.py

Photos:
    Place any number of .jpg / .jpeg / .png / .webp images inside:
        seed_photos/
    The script reads them all and randomly assigns one per person.
    Reuse across multiple people is fine. Falls back to solid-colour
    placeholder PNGs if the directory is absent or empty.

Safe to run multiple times — skips existing records by name.
Remove demo records: filter by notes containing '[DEMO]'.
"""

import os
import sys
import random
import struct
import zlib
import uuid
from datetime import date, timedelta
from decimal import Decimal

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import create_app
from app.models import (
    db, School, AcademicYear, Grade, Section,
    Employee, Student, FeeRecord, FeeInstallment, FeeType,
)
from app.utils.helpers import _supabase_upload, generate_student_id, generate_employee_id

DEMO_TAG    = '[DEMO]'
PHOTOS_DIR  = os.path.join(ROOT, 'seed_photos')

_MIME = {
    'jpg':  'image/jpeg',
    'jpeg': 'image/jpeg',
    'png':  'image/png',
    'webp': 'image/webp',
    'gif':  'image/gif',
}

# ── Photo pool ────────────────────────────────────────────────────────────────

def _load_photo_pool() -> list[tuple[bytes, str, str]]:
    """
    Read every supported image from seed_photos/ and return a list of
    (raw_bytes, content_type, original_ext) tuples.
    """
    pool: list[tuple[bytes, str, str]] = []
    if not os.path.isdir(PHOTOS_DIR):
        return pool
    for fname in sorted(os.listdir(PHOTOS_DIR)):
        ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
        if ext not in _MIME:
            continue
        fpath = os.path.join(PHOTOS_DIR, fname)
        try:
            with open(fpath, 'rb') as fh:
                pool.append((fh.read(), _MIME[ext], ext))
            print(f"  loaded photo: {fname}")
        except OSError as exc:
            print(f"  warning: could not read {fname}: {exc}")
    return pool


def _upload_from_pool(
    pool: list[tuple[bytes, str, str]],
    subfolder: str,
    fallback_idx: int,
) -> str | None:
    """
    Upload a randomly chosen photo from pool to Supabase.
    Falls back to a solid-colour placeholder PNG when pool is empty.
    """
    if pool:
        img_bytes, content_type, ext = random.choice(pool)
    else:
        img_bytes, content_type, ext = _make_placeholder(fallback_idx), 'image/png', 'png'

    object_path = f"{subfolder}/{uuid.uuid4().hex}.{ext}"
    url = _supabase_upload(img_bytes, object_path, content_type)
    if url:
        print(f"    photo → {url[:72]}…")
    else:
        print(f"    photo → skipped (no Supabase config or upload failed)")
    return url


# ── Fallback: solid-colour PNG (no external deps) ─────────────────────────────

_COLORS = [
    (74,  144, 226), (80,  200, 120), (255, 149,   0), (175,  82, 222),
    (255,  59,  48), (90,  200, 250), (255, 204,   0), (52,  199,  89),
    (0,   122, 255), (255,  45,  85), (100, 100, 200), (200, 100,  50),
    (150, 200, 100), (0,   200, 200), (220, 100, 160),
]


def _make_placeholder(index: int, size: int = 80) -> bytes:
    r, g, b = _COLORS[index % len(_COLORS)]

    def chunk(name: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(name + data) & 0xFFFFFFFF
        return struct.pack('>I', len(data)) + name + data + struct.pack('>I', crc)

    ihdr = struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)
    raw  = b''.join(b'\x00' + bytes([r, g, b] * size) for _ in range(size))
    return (
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', ihdr)
        + chunk(b'IDAT', zlib.compress(raw))
        + chunk(b'IEND', b'')
    )


# ── Seed data ─────────────────────────────────────────────────────────────────

TEACHERS = [
    {'full_name': 'مصطفى خالد',  'job_title': 'مدرس رياضيات',       'gender': 'male'},
    {'full_name': 'زينب علي',    'job_title': 'مدرسة لغة عربية',    'gender': 'female'},
    {'full_name': 'حيدر فاضل',   'job_title': 'مدرس علوم',           'gender': 'male'},
    {'full_name': 'سارة محمود',  'job_title': 'مدرسة لغة إنكليزية', 'gender': 'female'},
    {'full_name': 'علي جاسم',    'job_title': 'مدرس اجتماعيات',     'gender': 'male'},
]

STUDENTS = [
    {'full_name': 'أحمد علي حسن',      'gender': 'male',   'dob': date(2013, 3, 15), 'sec': 'أ'},
    {'full_name': 'زهراء محمد كاظم',    'gender': 'female', 'dob': date(2013, 7, 22), 'sec': 'ب'},
    {'full_name': 'حسين عباس جبار',     'gender': 'male',   'dob': date(2013, 1, 10), 'sec': 'أ'},
    {'full_name': 'مريم سعد عبد الله',  'gender': 'female', 'dob': date(2013, 9,  5), 'sec': 'ب'},
    {'full_name': 'علي حيدر كريم',      'gender': 'male',   'dob': date(2013, 5, 18), 'sec': 'أ'},
    {'full_name': 'فاطمة قاسم مهدي',    'gender': 'female', 'dob': date(2013, 11, 3), 'sec': 'ب'},
    {'full_name': 'يوسف مصطفى ناصر',    'gender': 'male',   'dob': date(2013, 4, 25), 'sec': 'أ'},
    {'full_name': 'رقية أحمد صالح',     'gender': 'female', 'dob': date(2013, 8, 14), 'sec': 'ب'},
    {'full_name': 'حسن مرتضى جاسم',     'gender': 'male',   'dob': date(2013, 2,  7), 'sec': 'أ'},
    {'full_name': 'نور الهدى سامي',      'gender': 'female', 'dob': date(2013, 6, 30), 'sec': 'ب'},
]

TUITION      = Decimal('2000000')
NUM_INSTALLS = 4


# ── Core seed logic ───────────────────────────────────────────────────────────

def _seed():
    today = date.today()

    # Load photos from seed_photos/ (or use colour placeholders)
    photo_pool = _load_photo_pool()
    if photo_pool:
        print(f"\n{len(photo_pool)} photo(s) loaded from seed_photos/ — will assign randomly.\n")
    else:
        print("\nseed_photos/ not found or empty — using solid-colour placeholder PNGs.\n")

    # ── School ────────────────────────────────────────────────────────────────
    school = School.query.filter_by(is_active=True).first()
    if not school:
        print("✗  No active school found — create one first.")
        return
    print(f"School : {school.school_name}  (id={school.id})")

    # ── Academic year ─────────────────────────────────────────────────────────
    year = AcademicYear.query.filter_by(school_id=school.id, is_current=True).first()
    if not year:
        print("✗  No current academic year found — create one first.")
        return
    print(f"Year   : {year.name}  (id={year.id})")

    # ── Grade ─────────────────────────────────────────────────────────────────
    GRADE_NAME = 'الصف السادس'
    grade = Grade.query.filter_by(
        school_id=school.id,
        academic_year_id=year.id,
        name=GRADE_NAME,
    ).first()
    if not grade:
        grade = Grade(
            name=GRADE_NAME,
            stage='الابتدائية',
            school_id=school.id,
            academic_year_id=year.id,
        )
        db.session.add(grade)
        db.session.flush()
        print(f"  + Grade: {GRADE_NAME}")
    else:
        print(f"  ~ Grade: {GRADE_NAME}  (id={grade.id})")

    # ── Sections ──────────────────────────────────────────────────────────────
    sections: dict[str, Section] = {}
    for sec_name in ('أ', 'ب'):
        sec = Section.query.filter_by(
            school_id=school.id,
            academic_year_id=year.id,
            grade_id=grade.id,
            name=sec_name,
        ).first()
        if not sec:
            sec = Section(
                name=sec_name,
                school_id=school.id,
                academic_year_id=year.id,
                grade_id=grade.id,
                capacity=30,
            )
            db.session.add(sec)
            db.session.flush()
            print(f"  + Section: {GRADE_NAME} {sec_name}")
        else:
            print(f"  ~ Section: {GRADE_NAME} {sec_name}  (id={sec.id})")
        sections[sec_name] = sec

    # ── Fee type ──────────────────────────────────────────────────────────────
    FEE_TYPE_NAME = 'رسوم الدراسة'
    fee_type = FeeType.query.filter_by(
        school_id=school.id,
        academic_year_id=year.id,
        name=FEE_TYPE_NAME,
    ).first()
    if not fee_type:
        fee_type = FeeType(
            name=FEE_TYPE_NAME,
            school_id=school.id,
            academic_year_id=year.id,
            description='الرسوم الدراسية السنوية',
        )
        db.session.add(fee_type)
        db.session.flush()
        print(f"  + FeeType: {FEE_TYPE_NAME}")
    else:
        print(f"  ~ FeeType: {FEE_TYPE_NAME}  (id={fee_type.id})")

    # ── Teachers ──────────────────────────────────────────────────────────────
    print("\n── Teachers ─────────────────────────────────────────────────────────")
    for idx, t in enumerate(TEACHERS):
        existing = Employee.query.filter_by(
            school_id=school.id,
            full_name=t['full_name'],
        ).first()
        if existing:
            print(f"  ~ {t['full_name']}  (skip — already exists)")
            continue

        last_emp = Employee.query.order_by(Employee.id.desc()).first()
        emp_id   = generate_employee_id(last_emp.id if last_emp else 0)
        photo    = _upload_from_pool(photo_pool, 'employees', idx)

        emp = Employee(
            employee_id=emp_id,
            full_name=t['full_name'],
            job_title=t['job_title'],
            gender=t['gender'],
            department='التدريس',
            school_id=school.id,
            hire_date=today,
            status='active',
            photo=photo,
            notes=DEMO_TAG,
        )
        db.session.add(emp)
        db.session.flush()
        print(f"  + {emp.full_name} — {emp.job_title}  (id={emp.id})")

    # ── Students ──────────────────────────────────────────────────────────────
    print("\n── Students ─────────────────────────────────────────────────────────")
    inst_amount = (TUITION / NUM_INSTALLS).quantize(Decimal('0.01'))

    for idx, s in enumerate(STUDENTS):
        existing = Student.query.filter_by(
            school_id=school.id,
            full_name=s['full_name'],
        ).first()
        if existing:
            print(f"  ~ {s['full_name']}  (skip — already exists)")
            continue

        last_stu = Student.query.order_by(Student.id.desc()).first()
        stu_id   = generate_student_id(last_stu.id if last_stu else 0)
        photo    = _upload_from_pool(photo_pool, 'students', idx)
        section  = sections[s['sec']]

        student = Student(
            student_id=stu_id,
            full_name=s['full_name'],
            date_of_birth=s['dob'],
            gender=s['gender'],
            nationality='عراقي',
            section_id=section.id,
            school_id=school.id,
            academic_year_id=year.id,
            enrollment_date=today,
            status='active',
            photo=photo,
            notes=DEMO_TAG,
        )
        db.session.add(student)
        db.session.flush()

        # Fee record
        fee_record = FeeRecord(
            student_id=student.id,
            fee_type_id=fee_type.id,
            academic_year_id=year.id,
            school_id=school.id,
            total_amount=TUITION,
            discount=Decimal('0'),
        )
        db.session.add(fee_record)
        db.session.flush()

        # 4 quarterly installments
        for i in range(1, NUM_INSTALLS + 1):
            db.session.add(FeeInstallment(
                fee_record_id=fee_record.id,
                school_id=school.id,
                academic_year_id=year.id,
                installment_no=i,
                amount=inst_amount,
                due_date=today + timedelta(days=90 * (i - 1)),
                status='pending',
            ))

        print(
            f"  + {student.full_name}  →  {GRADE_NAME} {s['sec']}"
            f"  |  2,000,000 IQD ÷ {NUM_INSTALLS}  (id={student.id})"
        )

    db.session.commit()

    demo_students  = Student.query.filter(Student.notes.contains(DEMO_TAG)).count()
    demo_employees = Employee.query.filter(Employee.notes.contains(DEMO_TAG)).count()
    print(f"\n✓  Done.  Students={demo_students}  Employees={demo_employees}\n")


def run():
    app = create_app()
    with app.app_context():
        _seed()


if __name__ == '__main__':
    run()
