"""
Mobile API Blueprint  —  /api/mobile/v1/
=========================================
JWT-authenticated REST API for the Mecha-School mobile application.

Supported roles
  parent  — children, attendance, fees, grades, exams, schedule, notifications
  teacher — profile, sections, students, schedule, exams, grade-entry, notifications

Authentication
  POST /api/mobile/v1/auth/login    → access token (24 h) + refresh token (30 d)
  POST /api/mobile/v1/auth/refresh  → new access token
  POST /api/mobile/v1/auth/logout   → client-side only (stateless)

All other endpoints require:
  Authorization: Bearer <access_token>

All responses are JSON.  The web session / Flask-Login flow is not affected.
"""
from flask import Blueprint

mobile_api_bp = Blueprint('mobile_api', __name__)

# Import route modules after creating the blueprint to avoid circular imports.
from . import auth, common, parent, teacher, teacher_leave, chat, school_board  # noqa: E402, F401
