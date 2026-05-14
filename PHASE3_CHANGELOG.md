# Mecha-School ERP — Phase 3 Changelog

**Scope:** Parent web portal, mobile/parent JSON API, RFID/ESP32 hardware endpoint, FCM NotificationService, admin broadcast composer.
**Depends on:** Phase 1 (parent_students, Device, Announcement, PushNotification, SchoolSettings) + Phase 2 (`received_amount` surface + audit helper).
**Engineers:** Ahmed & Abbas.

---

## 1. New modules

| Path | Purpose |
|---|---|
| `app/services/notifications.py` | **NotificationService** — pluggable FCM dispatcher with dev-log fallback. Writes one `PushNotification` row per attempt. |
| `app/blueprints/parent/` | Web parent portal (dashboard, per-child view, announcements feed). |
| `app/templates/parent/dashboard.html` | Overview card grid: one card per child (attendance rate, fees, latest exam). |
| `app/templates/parent/child.html` | Tabbed per-child detail (Attendance / Fees / Grades). |
| `app/templates/parent/announcements.html` | Parent-facing announcements reader. |
| `app/blueprints/api/` | `/api/v1/parent/*` JSON endpoints for the mobile app. |
| `app/blueprints/hardware/` | `/api/v1/hardware/*` — ESP32/Arduino RFID attendance + ping endpoints. |
| `app/blueprints/broadcast/` | Admin broadcast composer (index + compose + send_now + delete). |
| `app/templates/broadcast/index.html`, `compose.html` | Admin-side broadcast UI. |

`app/__init__.py` was updated to register the 4 new blueprints. A root-redirect tweak sends logged-in parent users straight to `/parent/dashboard` while all other roles still land on the admin dashboard.

---

## 2. NotificationService — pluggable FCM

The service is a module-level singleton exported as `NotificationService` from `app.services.notifications`. It's imported by routes that need to push to parents:

```python
from app.services.notifications import NotificationService

NotificationService.send_to_parents_of_student(
    student_id, title, body, ntype='rfid', data={...}
)
NotificationService.broadcast(announcement_id)
NotificationService.send_to_users([ids], title, body, ntype='general')
```

Back-end selection at boot time:
- If `FCM_SERVICE_ACCOUNT_JSON` env var points at an existing service-account JSON file → `FCMBackend` is used (lazy-imports `firebase_admin`).
- Otherwise → `DevLogBackend` is used. Every send logs via the stdlib `logging` module AND persists a `PushNotification` row with `status='sent'` and a pseudo message ID. This means local dev and CI never crash on missing FCM creds, and the audit trail still exists.

Every send call returns a list of `PushNotification` rows so the caller can report "delivered to N users" in a flash message.

Public contract of the service — stable across phases:
1. `send_to_user(user_id, title, body, ntype='general', data=None)`
2. `send_to_users(user_ids, title, body, ntype='general', data=None)`
3. `send_to_parents_of_student(student_id, title, body, ntype='general', data=None)`
4. `broadcast(announcement_id)` — reads audience/target_role/specific targets off the Announcement row.

---

## 3. Hardware (ESP32) endpoint

`POST /api/v1/hardware/attendance` with header `X-Device-Key: <api_key>`:

```json
{
  "rfid_tag": "04A1B2C3D4",
  "timestamp": "2026-04-23T07:32:10Z",
  "scan_type": "check_in"
}
```

Behaviour on each scan:

1. Authenticate via the `X-Device-Key` header → looks up a row in `devices` where `is_active=True` and `api_key` matches. Updates `device.last_seen`.
2. Find Student by `rfid_tag_id`. Unknown tags return `404` and write `log_action('rfid_unknown', ...)`.
3. If no `StudentAttendance` exists for today → insert one with `status='present'`, `check_in=<scan_time>`, `source='rfid'`, `device_id=<device.id>`.
4. If a row already exists → set `check_out` (unless the client explicitly says `scan_type='check_in'` and check_in is null, in which case fill check_in).
5. Fire `NotificationService.send_to_parents_of_student` with a localized title/body describing the check-in or check-out time.
6. Write `log_action('rfid_scan', 'student', ...)`.

`GET /api/v1/hardware/ping` just validates the `X-Device-Key` and returns server time + device location. Useful for ESP32 boot-time health checks.

---

## 4. Parent JSON API

All endpoints require an authenticated parent session (Flask-Login cookie). The Flutter app will call `/auth/login` first, then hit the endpoints below.

| Endpoint | Purpose |
|---|---|
| `GET  /api/v1/parent/me` | Profile + linked children list + school white-label config. |
| `POST /api/v1/parent/register-device` | Save FCM device token + locale for this user. |
| `GET  /api/v1/parent/children/<id>/attendance?start=&end=` | 30-day default window. Includes `check_in`/`check_out` + `source`. |
| `GET  /api/v1/parent/children/<id>/fees` | Every `FeeRecord` + `FeeInstallment`, including `received_amount` and `remaining`. |
| `GET  /api/v1/parent/children/<id>/grades` | Exam results. |
| `GET  /api/v1/parent/announcements` | Last 50 sent announcements (read-only). |

The `_assert_parent_of(student_id)` helper raises 404 if the caller isn't actually linked to the student via `parent_students`. This means parents can't enumerate other children by iterating IDs.

---

## 5. Admin broadcast composer

`/broadcast/` (permission `send_broadcast`, seeded in Phase 1):

- **Compose form** with audience options: *all parents* / *by role* / *specific users*.
- Send-now button (dispatches through NotificationService + flips announcement status to `sent`).
- Schedule button (saves with `scheduled_at` + `status='scheduled'`; a future cron can pick these up).
- Index list with manual re-send + delete actions.
- Every create / send / delete writes an AuditLog row.

---

## 6. Model tweaks that came with this phase

Discovered while building — added to `app/models/__init__.py`:

- `Expense.payment_method` (String(20), default `'cash'`)
- `Expense.reference_no` (String(64), nullable) — used for `SAL-<id>` payroll refs
- `Expense.created_by` (FK → users.id) + `Expense.updated_at`
- `SalaryRecord.payment_method` (String(20), nullable) — set when paying

These are additive. A fresh `flask reset-db` picks them up without drama. An Alembic migration for the phase would be:

```bash
flask db migrate -m "phase3_expense_payment_fields"
flask db upgrade
```

---

## 7. Verification

```
python -c "import ast; …for each changed .py file"
→ 15 files, all OK.
```

Next: Phase 4 — white-label admin page + sidebar reorder + schedule PDF print.
