# Mecha-School ERP — Phase 2 Changelog

**Scope:** Core bug fixes + advanced financial logic + audit-trail wiring.
**Depends on:** Phase 1 schema (received_amount, expense_id, source, is_system).
**Engineers:** Ahmed & Abbas.

---

## 1. Files touched

| File | Change |
|---|---|
| `app/blueprints/employees/__init__.py` | **Bug fix.** `edit()` now saves `photo` on re-upload + also updates gender/nationality/hire_date which were silently dropped before. |
| `app/blueprints/admin/__init__.py` | Added `edit_academic_year()` and `delete_academic_year()` routes. Both write AuditLog entries. |
| `app/templates/admin/academic_years.html` | Rebuilt table with per-row Edit modal + Delete button. |
| `app/services/__init__.py` | **New.** Service-layer package scaffold. |
| `app/services/payroll.py` | **New.** `ensure_salaries_category()` + `post_salary_expense()` + `unpost_salary_expense()` — keeps the Payroll↔Expense bridge DRY. |
| `app/blueprints/salaries/__init__.py` | `mark_paid()` and `pay_all()` now auto-create linked Expense rows. New `unpay()` route reverses the link. All three write AuditLog entries. |
| `app/blueprints/fees/__init__.py` | **Major rewrite.** `pay_installment()` accepts `received_amount`, `payment_method`, `paid_date`, and `notes`. Cumulative partial payments supported. Uses `inst.recompute_status()` from Phase 1. New `installment_info()` JSON endpoint for the pay modal. |
| `app/templates/fees/index.html` | Rebuilt installments table to show received/remaining columns + partial badge. New pay modal with received_amount + payment method + "use remaining" helper. |
| `app/blueprints/reports/__init__.py` | **Accuracy fix.** `fees_report()` now sums `received_amount` (actual cash) instead of `amount` filter-by-paid, correctly accounting for partial payments. Dashboard `total_fee_paid` also fixed. Overdue detection now catches partial rows past due date. |
| `app/templates/reports/fees.html` | Removed inline pay form (which couldn't work with new required `received_amount`); overdue rows now link to fees page pre-filtered on student. |
| `app/blueprints/finances/__init__.py` | Added AuditLog calls on create/edit/delete of Revenue and Expense. Payroll-sourced Expenses are now read-only from this blueprint (you have to unpay the salary). Expense creation now honours `payment_method`, `reference_no`, and correctly sets `source='manual'` + `created_by`. |

---

## 2. The 3 core bug fixes

### 2.1 Employee profile-picture save bug

Before: the create form wrote `photo` correctly, but the edit form never called `save_uploaded_file`, so re-uploading a photo silently did nothing — the old path remained in the DB.

Fix: mirrored the `request.files` block from `create()` into `edit()`, gated on `request.files['photo'].filename` so leaving the field blank keeps the existing picture. Also patched three fields that the edit handler had been dropping: `gender`, `nationality`, `hire_date`.

### 2.2 Academic-year rename

Before: only an Add form existed. Admins couldn't correct a typo in "2024-2025" or shift a date.

Fix: new `POST /admin/academic-years/<int>/edit` route + per-row Bootstrap modal in `academic_years.html`. The `is_current` flag is guarded — switching a year to current automatically un-sets all others in a single `UPDATE`. Every edit writes a `log_action('edit', 'academic_year', …)` row.

Bonus: `POST /admin/academic-years/<int>/delete` route added, with protection against deleting the currently-active year.

### 2.3 Fees Report accuracy

Before: the report summed `FeeInstallment.amount` filtered by `status='paid'`. Two problems:
1. A partial payment (e.g. half of a 500k installment) didn't count at all, because the row was still `pending`/`partial`, not `paid`.
2. Any installment paid early at a lesser amount (discount) was over-counted at its scheduled amount instead of what was actually received.

Fix: all money totals in `fees_report()` now aggregate from `FeeInstallment.received_amount` (actual cash in) versus `FeeInstallment.amount` (scheduled). This matches how the pay modal writes data and uses Phase-1's Decimal-exact column. The Admin dashboard `fee_rate` indicator was fixed the same way.

Overdue detection widened: a row is now overdue when `status` is pending/partial/overdue AND `received_amount < amount` AND `due_date < today` — so a half-paid installment still flags if it's past due.

---

## 3. Manual-payment flow (the big Fees ask)

`pay_installment(inst_id)` now expects:

```
received_amount   Decimal, required    — this cash collection
payment_method    string               — cash | transfer | cheque | card
paid_date         YYYY-MM-DD           — optional, defaults to today
notes             string               — optional
```

Behaviour:

1. Adds `received_amount` (cumulative) to `inst.received_amount`. Caps at remaining balance so over-pays are silently trimmed.
2. Calls `FeeInstallment.recompute_status()` → transitions `pending → partial → paid` or flags overdue if past due and not full.
3. Generates a fresh `receipt_no` only on the transaction that *completes* the installment. Partial payments go through but don't consume a receipt number.
4. Writes an `AuditLog` row with receiver name, method and resulting status.

The UI surface:

* `fees/index.html` installments table now shows **المبلغ / المستلم / المتبقي** as three separate columns.
* Status badge added for the new `partial` state.
* Pay button opens a modal with number input + method dropdown + date + "use remaining" button that fills in whatever is left to owe.
* Data-*remaining* is injected server-side, so the modal prevents you from entering more than is actually owed.

---

## 4. Payroll ↔ Expense bridge

To produce a unified P&L without double-counting, every salary payment now also writes an `Expense` row:

```
category       = "رواتب"     (auto-created, is_system=True)
source         = 'payroll'
amount         = record.net_salary
date           = record.paid_date
description    = "راتب <name> — MM/YYYY"
reference_no   = "SAL-<record_id>"
```

Then `salary_record.expense_id` is set to that expense's id. The helper lives in `app/services/payroll.py` (not inside the blueprint) so Phase 3's REST mobile-pay-salary endpoint can reuse it.

Key idempotency: `post_salary_expense` short-circuits if `expense_id` is already set, so double-clicking the Pay button won't create duplicate expenses.

Reversal: the new `POST /salaries/<id>/unpay` route calls `unpost_salary_expense`, which deletes the linked Expense row (guarded by `source='payroll'` so a hand-entered row can never be wiped by accident). The SalaryRecord goes back to `status='pending'`.

Guard in `finances`: `edit_expense()` and `delete_expense()` now refuse to touch any Expense with `source='payroll'` — the only way to modify it is via the payroll module. This keeps the salary/expense rows perpetually in sync.

---

## 5. Audit trail

`app/utils/audit.py:log_action(action, resource, resource_id, details=None)` is now called from:

| Blueprint.route | action | resource |
|---|---|---|
| `admin.edit_academic_year` | edit | academic_year |
| `admin.delete_academic_year` | delete | academic_year |
| `fees.pay_installment` | payment | fee_installment |
| `salaries.mark_paid` | pay | salary |
| `salaries.pay_all` | pay_all | salary |
| `salaries.unpay` | unpay | salary |
| `salaries.*` (via `post_salary_expense`) | create | expense |
| `salaries.unpay` (via `unpost_salary_expense`) | delete | expense |
| `finances.create_revenue / edit / delete` | create / edit / delete | revenue |
| `finances.create_expense / edit / delete` | create / edit / delete | expense |

The `view_audit_log` permission introduced in Phase 1 is what will gate the upcoming AuditLog browser UI (still Phase 2 deliverable — comes with the Phase 2 blueprint routes wrap-up in Phase 3 if needed).

---

## 6. Verification

```
python -c "import ast; ...for each changed .py file..."
→ all OK
```

All 8 modified Python files pass AST syntax checks.

Nothing in Phase 2 depends on running migrations — the columns we use (`received_amount`, `expense_id`, `source`, `is_system`, `payment_method`) all already exist from Phase 1's schema overhaul. A fresh `flask reset-db` is still the smoothest way to have the "رواتب" system category present on first boot.

---

## 7. What's still pending

→ Phase 3: Parent Portal + Mobile API (`/api/v1/parent/*`) + hardware (`/api/v1/hardware/attendance`) + FCM `NotificationService` + broadcast composer + RFID-scan push trigger.

→ Phase 4: White-label admin screen, sidebar reorder (Fees → primary), schedule PDF print.
