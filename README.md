# نظام المهندس — Al-Muhandis Private School Management System

**شركة المهندس** | Production-ready school ERP built with Flask + PostgreSQL

---

## Quick Start (5 minutes)

```bash
# 1. Clone / extract project
cd almuhandis

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env — set DATABASE_URL and SECRET_KEY

# 5. Create PostgreSQL database
createdb almuhandis_db             # or use pgAdmin / DBeaver

# 6. Initialize and seed database
flask create-db
flask seed

# 7. Run
python run.py
```

Open **http://localhost:5000**  
Default credentials: `admin` / `Admin@1234` — **change immediately after first login**

---

## Docker Deployment (Recommended for Production)

```bash
# Edit docker-compose.yml — change POSTGRES_PASSWORD and SECRET_KEY
docker compose up -d

# First-time setup
docker compose exec web flask create-db
docker compose exec web flask seed
```

Nginx will serve on port 80.

---

## Project Structure

```
almuhandis/
├── run.py                    Development server entry point
├── wsgi.py                   Production (Gunicorn) entry point
├── Dockerfile
├── docker-compose.yml
├── nginx.conf
├── requirements.txt
├── config/
│   └── settings.py           Development / Production / Testing configs
└── app/
    ├── __init__.py           Application factory (create_app)
    ├── models/__init__.py    24 SQLAlchemy models with all relationships
    ├── blueprints/           15 Blueprint route modules
    │   ├── auth/             Login, logout, profile
    │   ├── admin/            Dashboard, users, roles, permissions, academic years
    │   ├── students/         Student CRUD + profile view
    │   ├── employees/        Employee CRUD + documents
    │   ├── fees/             Fee records, installments, payment collection
    │   ├── finances/         Revenue & expense CRUD, categories, monthly charts
    │   ├── salaries/         Monthly salary processing, pay slips, bulk pay
    │   ├── attendance/       Daily student attendance by section
    │   ├── grades/           Exam creation, result entry, auto-ranking
    │   ├── sections/         Grade → Section → Subject hierarchy
    │   ├── schedules/        Weekly timetable builder
    │   ├── evaluations/      Employee performance evaluations
    │   ├── reports/          6 comprehensive report pages with charts
    │   ├── notifications/    Announcements and alerts
    │   └── audit/            Full action audit log
    ├── templates/            56 Arabic RTL Jinja2 templates
    └── utils/
        ├── decorators.py     @permission_required, @admin_required
        ├── helpers.py        File upload, ID generation, grade letters
        ├── seeder.py         flask seed / create-db / reset-db commands
        └── audit.py          log_action() helper
```

---

## User Roles & Permissions

| Role | Arabic | Default Permissions |
|------|--------|---------------------|
| `admin` | مسؤول النظام | **Full access** — bypasses all permission checks |
| `accountant` | محاسب | Fees, revenues, expenses, reports |
| `teacher` | معلم | Attendance (own sections), grades |
| `hr` | موارد بشرية | Employees, salaries, reports |
| `reception` | استقبال | Students (view/add/edit), notifications |

Admin can also grant **extra per-user permissions** on top of any role.

### All Permissions

| Permission | Arabic | Category |
|-----------|--------|----------|
| `view_students` | عرض الطلاب | الطلاب |
| `add_student` | إضافة طالب | الطلاب |
| `edit_student` | تعديل طالب | الطلاب |
| `delete_student` | حذف طالب | الطلاب |
| `manage_fees` | إدارة الرسوم | المالية |
| `record_payments` | تسجيل المدفوعات | المالية |
| `manage_revenues` | إدارة الإيرادات | المالية |
| `manage_expenses` | إدارة المصروفات | المالية |
| `manage_employees` | إدارة الموظفين | الموارد البشرية |
| `manage_salaries` | إدارة الرواتب | الموارد البشرية |
| `take_attendance` | تسجيل الحضور | الأكاديمي |
| `enter_grades` | إدخال الدرجات | الأكاديمي |
| `view_reports` | عرض التقارير | التقارير |
| `manage_notifications` | إدارة الإشعارات | النظام |
| `manage_settings` | إدارة الإعدادات | النظام |
| `manage_users` | إدارة المستخدمين | النظام |

---

## Database — 24 Models

| Model | Description |
|-------|-------------|
| User | System accounts with role FK |
| Role | Role definitions (is_admin flag) |
| Permission | Fine-grained permissions |
| role_permissions | M2M Role↔Permission |
| user_permissions | M2M User extra permissions |
| AcademicYear | School years with is_current flag |
| Grade | Grade levels per academic year |
| Section | Class sections with homeroom teacher |
| Subject | Academic subjects with codes |
| Student | Full profiles, guardian info, status |
| Employee | Staff profiles, salary, contract |
| EmployeeDocument | Staff document uploads |
| FeeType | Configurable fee categories |
| FeeRecord | Student fee assignments |
| FeeInstallment | Installments with receipt numbers |
| RevenueCategory | Revenue classification |
| Revenue | Revenue entries |
| ExpenseCategory | Expense classification |
| Expense | Expense entries with receipts |
| SalaryRecord | Monthly salary slips (unique per employee/month/year) |
| StudentAttendance | Daily attendance (unique per student/date) |
| EmployeeAttendance | Staff daily attendance |
| ExamType | Monthly / Midterm / Final |
| Exam | Exam definitions per section/subject |
| ExamResult | Student results with grade letter and rank |
| EmployeeEvaluation | Performance scoring |
| Notification | System-wide notifications |
| NotificationRead | Per-user read tracking |
| Schedule | Weekly timetable entries |
| AuditLog | Full action audit trail |

---

## CLI Commands

```bash
flask create-db    # Create all database tables
flask seed         # Seed roles, permissions, admin, exam types, categories
flask reset-db     # DROP + recreate + seed (asks confirmation)
flask db migrate   # Generate Flask-Migrate migration
flask db upgrade   # Apply migration
```

---

## Reports Available

1. **Main Dashboard** — KPIs, monthly finance chart, student donut, attendance today
2. **Financial Report** — Revenue & expense breakdown by category with monthly chart
3. **Students Report** — Status distribution, per-section capacity utilization
4. **Fees Report** — Collection rate progress bar, overdue installments list
5. **Attendance Report** — Per-student rates with color-coded progress bars
6. **Salary Report** — Annual salary totals per employee

---

## Technology Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python 3.11, Flask 3.0, SQLAlchemy |
| Database | PostgreSQL 16 |
| Authentication | Flask-Login + Flask-Bcrypt |
| Migrations | Flask-Migrate (Alembic) |
| Frontend | Bootstrap 5.3 RTL, Cairo font, Chart.js 4 |
| Icons | Bootstrap Icons 1.11 |
| File uploads | Werkzeug (local disk) |
| Production | Gunicorn + Nginx + Docker |

---

## Security Features

- Password hashing with bcrypt
- CSRF-protected forms (Flask-WTF ready)
- Route-level permission enforcement (decorator-based)
- Admin-only routes separated via `@admin_required`
- Audit log for login, logout, and key operations
- File upload type validation
- Session expiry (configurable)

---

© 2025 شركة المهندس — Al-Muhandis Company. All rights reserved.
