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

from app import create_app
from app.utils.seeder import register_commands

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
