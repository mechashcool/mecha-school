# Mecha-School ERP — Phase 1 Changelog

**Scope:** Database schema overhaul + seeder rebuild.
**Branch from:** `almuhandis_final.zip` baseline.
**Engineers:** Ahmed & Abbas.

---

## 1. Files touched in this phase

| File | Change |
|---|---|
| `app/models/__init__.py` | **Overhauled.** Adds 6 new tables + 7 new columns. See §2. |
| `app/utils/seeder.py` | **Overhauled.** Fixes pre-existing bug (`seed_exam_types` / `seed_default_categories` were referenced but never defined). Adds seeders for new tables. |
| `manage.py` | Shell context expanded to expose every model. New CLI commands registered via `register_commands()`. |
| `PHASE1_CHANGELOG.md` | This file. |

Everything else is untouched. Blueprints/templates come in later phases.

---

## 2. Schema delta

### New columns on existing tables

| Table | Column | Type | Purpose |
|---|---|---|---|
| `users` | `device_token` | `String(512)` | FCM registration token for the parent mobile app. |
| `users` | `locale` | `String(10)` default `'ar'` | UI / push-notification language. |
| `students` | `rfid_tag_id` | `String(64)` unique | RFID card UID scanned at the gate. |
| `student_attendance` | `check_in` | `Time` | First scan of the day. |
| `student_attendance` | `check_out` | `Time` | Last scan of the day. |
| `student_attendance` | `source` | `String(20)` default `'manual'` | `'manual'` or `'rfid'`. |
| `student_attendance` | `device_id` | FK → `devices.id` | Which ESP32 recorded it. |
| `fee_installments` | `received_amount` | `Numeric(12,2)` default `0` | **Manual partial-payment entry** (the core fees-module ask). |
| `fee_installments` | `payment_method` | `String(20)` | cash / transfer / cheque / card. |
| `fee_installments` | `updated_at` | `DateTime` | Needed for audit. |
| `salary_records` | `expense_id` | FK → `expenses.id` | **Payroll ↔ Expenses link** so the financial report isn't double-counted. |
| `academic_years` | `updated_at` | `DateTime` | Supports rename audit. |
| `expense_categories` | `is_system` | `Boolean` | Marks auto-created ones like `"رواتب"` (Salaries). |
| `expenses` | `source` | `String(20)` default `'manual'` | `'manual'` or `'payroll'`. |
| `notifications` | `ntype` | accepts `'rfid'` | No column change, just new value. |

### New tables

| Table | Purpose |
|---|---|
| `parent_students` | M2M link `User` (parent) ↔ `Student` (child) with `relation` column. |
| `devices` | ESP32 / Arduino registry. Holds `device_id`, per-device `api_key`, `location`, `last_seen`, `firmware`. |
| `announcements` | Admin broadcasts (all parents / role / specific). Supports scheduled delivery. |
| `announcement_targets` | Targeted recipients when `audience='specific'`. |
| `push_notifications` | Per-user FCM send log: status, `fcm_message_id`, error, sent_at. |
| `school_settings` | Single-row white-label config: `school_name`, `logo_path`, currency, timezone, primary color. |

### Computed / behavioural changes

* `FeeRecord.total_paid` and `.remaining` now sum **`received_amount`** (Decimal-correct) instead of `amount`. This fixes the Fees Report accuracy ask in §1 of the brief — the report was previously summing scheduled amounts, not actual cash.
* `FeeInstallment.recompute_status()` helper drives the `pending / partial / paid / overdue` transitions from `received_amount` vs `amount` vs `due_date`. Blueprints in Phase 2 will call this on every payment write.
* `SchoolSettings.get()` class method guarantees the single settings row exists — use this in `context_processor` so templates can always read `{{ school.school_name }}`.

---

## 3. New permissions

These are seeded automatically. Admin bypasses everything; assign them to custom roles via the Users screen once Phase 2 ships.

```
manage_rfid            — RFID card assignment / deactivation
manage_devices         — Add/rotate ESP32 devices
manage_schedules       — Create/edit timetables
print_schedules        — Print timetable PDFs (Phase 4)
view_audit_log         — Read-only access to AuditLog
send_broadcast         — Compose announcements to parents
manage_white_label     — Edit school name / logo / colors
parent_view_child      — Auto-granted to Role.name='parent'
```

New role: **`parent`** (label `ولي أمر`). Parent users are linked to Students via `parent_students`. Phase 3 builds the parent portal on this.

---

## 4. How to roll the DB forward

Because you said "prioritize structural overhaul over data persistence", the cleanest path is a full reset. From the project root:

```bash
# 1) Update .env to point at your (dev) Neon DB or local PostgreSQL.
# 2) Drop & recreate everything + seed:
flask reset-db
```

This runs `db.drop_all()` → `db.create_all()` → `seed_all()`. You'll see the sample ESP32 `api_key` printed once — grab it for firmware testing.

If you want to preserve existing production data, don't run `reset-db`. Alembic migration scripts to upgrade in place can be generated next phase on request:

```bash
flask db migrate -m "phase1_schema_overhaul"
flask db upgrade
```

---

## 5. What's NOT in Phase 1 (coming next)

| Ask | Phase |
|---|---|
| Employee profile-picture save bug | 2 |
| Academic Year "Edit Name" UI | 2 |
| Fees Report accuracy fix (routes/templates) | 2 |
| Manual `received_amount` UI + transaction flow | 2 |
| General Income / Expenses / Payroll-Expenses routes + unified P&L | 2 |
| `AuditLog` decorator wrapping financial writes | 2 |
| Parent web dashboard | 3 |
| `/api/v1/parent/*` JSON endpoints | 3 |
| `/api/v1/hardware/attendance` ESP32 endpoint | 3 |
| `NotificationService` (FCM + dev-log fallback) | 3 |
| Admin broadcast composer UI | 3 |
| RFID-scan → push-parent trigger | 3 |
| White-label admin screen + logo upload | 4 |
| Sidebar reorder (Fees → primary) | 4 |
| Schedule PDF print feature | 4 |

---

## 6. Notes

* The stray `{app/` directory in the project root is leftover empty scaffolding from a broken `mkdir -p` in the original zip. It contains nothing; Python won't import it. Safe to ignore or delete.
* The `admin` account email changed from `admin@almuhandis.iq` → `admin@mecha-school.local` to match the new branding. Update any bookmarks.
* All monetary columns are `Numeric(12, 2)`. Any report that still reads floats is a bug — flag it.
