"""
Al-Muhandis – General utility functions
"""
import os
import uuid
from datetime import datetime
from flask import current_app
from werkzeug.utils import secure_filename


ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_DOC_EXTENSIONS   = {'pdf', 'doc', 'docx', 'xls', 'xlsx'}


def allowed_file(filename, extensions=None):
    if extensions is None:
        extensions = current_app.config.get('ALLOWED_EXTENSIONS', set())
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in extensions


def save_uploaded_file(file, subfolder='misc', prefix=None):
    """Save an uploaded FileStorage object; return relative path or None."""
    if not file or file.filename == '':
        return None
    if not allowed_file(file.filename):
        return None
    ext = file.filename.rsplit('.', 1)[1].lower()
    base_name = uuid.uuid4().hex
    if prefix:
        safe_prefix = secure_filename(prefix)
        if safe_prefix:
            base_name = f"{safe_prefix}-{base_name}"
    filename = f"{base_name}.{ext}"
    upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', subfolder)
    os.makedirs(upload_dir, exist_ok=True)
    file.save(os.path.join(upload_dir, filename))
    return f"uploads/{subfolder}/{filename}"


def generate_student_id(last_id=None):
    """Generate next student ID like STU-00001."""
    next_num = (last_id or 0) + 1
    return f"STU-{next_num:05d}"


def generate_employee_id(last_id=None):
    """Generate next employee ID like EMP-00001."""
    next_num = (last_id or 0) + 1
    return f"EMP-{next_num:05d}"


def generate_receipt_no():
    """Generate unique receipt number."""
    return f"RCP-{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"


def calculate_grade_letter(marks, max_marks=100):
    pct = (float(marks) / float(max_marks)) * 100
    if pct >= 95: return 'A+'
    if pct >= 90: return 'A'
    if pct >= 85: return 'B+'
    if pct >= 80: return 'B'
    if pct >= 75: return 'C+'
    if pct >= 70: return 'C'
    if pct >= 60: return 'D'
    return 'F'
