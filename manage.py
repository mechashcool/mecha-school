"""
Mecha-School ERP – Management Script
====================================
Provides a rich shell context and extra management commands.

Common usage:
    python manage.py shell            # interactive shell with all models
    python manage.py routes           # list all registered routes
    flask create-db                   # create tables
    flask seed                        # seed roles / perms / categories / settings
    flask reset-db                    # drop + create + seed  (destructive)
    flask rotate-device-key <id>      # regenerate an RFID device api_key
"""
import os

# Declared BEFORE create_app(). This module is what `flask --app manage ...`
# imports, so every migration and management command constructs the
# application under the CLI role and starts NO background services.
#
# The previous argv heuristic missed `python -m flask --app manage db current`
# entirely — argv[1] is '--app', not 'db' — so a read-only migration query
# started the auto-attendance scheduler. Declaring the role removes the guess.
from app.lifecycle import ROLE_CLI, set_role

set_role(ROLE_CLI)

from app import create_app  # noqa: E402  — must follow set_role()
from app.utils.seeder import register_commands  # noqa: E402

app = create_app(os.environ.get('FLASK_ENV', 'development'))
register_commands(app)


@app.shell_context_processor
def make_shell_context():
    """Expose every model in flask shell for quick debugging."""
    from app.models import (
        db,
        # Identity
        User, Role, Permission,
        # Academic
        AcademicYear, Grade, Section, Subject,
        # People
        Student, Employee, EmployeeDocument,
        # Fees & Finance
        FeeType, FeeRecord, FeeInstallment,
        RevenueCategory, Revenue,
        ExpenseCategory, Expense,
        SalaryRecord,
        # Attendance & Hardware
        StudentAttendance, EmployeeAttendance, Device,
        # Grades
        ExamType, Exam, ExamResult,
        # HR
        EmployeeEvaluation,
        # Comms
        Notification, NotificationRead,
        Announcement, AnnouncementTarget, PushNotification,
        # Schedules / Audit / White-label
        Schedule, AuditLog, SchoolSettings,
    )
    return dict(
        db=db,
        User=User, Role=Role, Permission=Permission,
        AcademicYear=AcademicYear, Grade=Grade, Section=Section, Subject=Subject,
        Student=Student, Employee=Employee, EmployeeDocument=EmployeeDocument,
        FeeType=FeeType, FeeRecord=FeeRecord, FeeInstallment=FeeInstallment,
        RevenueCategory=RevenueCategory, Revenue=Revenue,
        ExpenseCategory=ExpenseCategory, Expense=Expense,
        SalaryRecord=SalaryRecord,
        StudentAttendance=StudentAttendance, EmployeeAttendance=EmployeeAttendance,
        Device=Device,
        ExamType=ExamType, Exam=Exam, ExamResult=ExamResult,
        EmployeeEvaluation=EmployeeEvaluation,
        Notification=Notification, NotificationRead=NotificationRead,
        Announcement=Announcement, AnnouncementTarget=AnnouncementTarget,
        PushNotification=PushNotification,
        Schedule=Schedule, AuditLog=AuditLog, SchoolSettings=SchoolSettings,
    )


if __name__ == '__main__':
    app.run()
