"""
Mecha-School ERP — Service Layer
--------------------------------
Holds domain-service helpers that don't belong inside a single blueprint:

  * payroll.py   — Payroll ↔ Expense bridge
  * fees.py      — Partial-payment + installment logic
  * notifications.py  (Phase 3) — FCM dispatch with dev-log fallback

Blueprints import from here; services import from `app.models` but never
from blueprints (to prevent circular imports).
"""
