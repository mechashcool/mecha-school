# Mecha School — Mobile API Reference

**Generated:** 2026-06-25  
**Base URL:** `https://school.smartcoreiq.cloud/api/mobile/v1`  
**Deployment:** VPS (Docker + Nginx + Gunicorn/Flask)  
**Migration note:** Previously hosted on Render; migrated to VPS at `school.smartcoreiq.cloud`

---

## Authentication

### Method
JWT (HS256), stateless.

### How to obtain tokens
```
POST /auth/login
```
Returns an `access_token` (24 h) and `refresh_token` (30 d).

### Required header for all protected endpoints
```
Authorization: Bearer <access_token>
```

### Token refresh
```
POST /auth/refresh
Authorization: Bearer <refresh_token>
```

### Supported roles
`parent` and `teacher` only. Admin, manager, and super_admin logins are rejected with HTTP 403.

---

## Response envelope

All responses are JSON with an `ok` boolean field.

**Success:**
```json
{ "ok": true, "field1": ..., "field2": ... }
```

**Error:**
```json
{ "ok": false, "error": "error_code_or_message" }
```

**Rate-limit / lockout (login only — HTTP 429):**
```json
{
  "ok": false,
  "error": "LOGIN_LOCKED",
  "message": "تجاوزت الحد المسموح لمحاولات تسجيل الدخول. يرجى المحاولة بعد 5 دقائق.",
  "remaining_seconds": 300,
  "retry_after_seconds": 300,
  "retry_after_minutes": 5,
  "locked_until": "2026-06-29T15:30:00+00:00",
  "wait_seconds": 300
}
```
`remaining_seconds` is always an integer. `retry_after_seconds` and `wait_seconds` carry the same value and are kept for backward compatibility. `locked_until` is a UTC ISO-8601 timestamp.

---

## Media URL rules (VPS migration notes)

| URL stored in DB | What Flutter receives |
|---|---|
| Supabase public URL (`https://…supabase.co/…`) | Passed through unchanged ✅ |
| External video URL (YouTube, etc.) | Passed through unchanged ✅ |
| Local relative path + file on disk | `https://school.smartcoreiq.cloud/media/<path>` ✅ |
| Local relative path + file NOT on disk | `null` ⚠️ (old Render uploads lost during migration) |

**Important for Flutter:** Always treat media/photo/attachment fields as nullable. Show a local placeholder when the value is `null`.

### Local media is served via `/media/uploads/...` (not `/static/uploads/...`)

As of 2026-06-25, locally-stored uploads (School Board images/videos, photos,
homework/leave attachments) are returned as
`https://school.smartcoreiq.cloud/media/uploads/<path>` and served by the Flask
`media` blueprint. This guarantees the file loads regardless of how the VPS
nginx maps its `/static/` location.

- **Why not `/static/uploads/...`?** Uploads are written to the Flask package's
  `app/static/uploads/` folder. On the VPS the nginx `/static/` location did not
  serve that directory (it served the repo-root `static/` folder, which has no
  `uploads/`), so newly uploaded files returned **404**. The `/media/...` route
  is forwarded to Gunicorn and serves the file from the exact directory it was
  written to. Both URLs are HTTPS and open directly in a browser.
- Supabase/external URLs are still passed through **unchanged**.
- The route supports HTTP Range requests, so video seeking works.

**New uploads** go to Supabase Storage when `SUPABASE_URL` + `SUPABASE_SERVICE_KEY`
are set on the VPS (returning absolute Supabase URLs). When Supabase is not
configured, uploads are stored locally and returned as `/media/uploads/...` URLs.

---

## Error codes (common across all endpoints)

| HTTP | `error` field | Meaning |
|---|---|---|
| 401 | `missing_token` | No Authorization header |
| 401 | `token_expired` | Access or refresh token expired |
| 401 | `invalid_token` | Token signature invalid or malformed |
| 401 | `wrong_token_type` | Sent refresh token where access token expected |
| 401 | `user_inactive` | Account deactivated |
| 403 | `forbidden` | Role not allowed for this endpoint |
| 403 | `role_not_supported…` | Non-parent/teacher role tried to login |
| 404 | (various) | Record not found or access denied |
| 429 | `LOGIN_LOCKED` | Login throttle active — includes `remaining_seconds`, `locked_until` |

---

## 1. Authentication

### POST /auth/login
Public. No Authorization header needed.

**Request body (JSON):**
```json
{ "username": "user123", "password": "secret" }
```
`username` may be a username or email address.

**Response (200):**
```json
{
  "ok": true,
  "access_token": "<JWT>",
  "refresh_token": "<JWT>",
  "token_type": "Bearer",
  "expires_in": 86400,
  "user": {
    "id": 42,
    "name": "Ahmed Ali",
    "username": "ahmed.ali",
    "email": "ahmed@school.com",
    "phone": "+966501234567",
    "avatar": null,
    "role": "parent",
    "locale": "ar",
    "school_id": 3
  },
  "school": {
    "id": 3,
    "name": "Springfield International School",
    "name_ar": "مدرسة سبرينغفيلد الدولية",
    "logo": "https://…supabase.co/…/logo.png",
    "primary_color": "#1a73e8",
    "currency": "ر.س",
    "currency_code": "SAR",
    "phone": "+966112345678",
    "email": "info@school.com",
    "address": "Riyadh"
  },
  "children": [
    {
      "id": 101,
      "student_id": "STU-000001",
      "name": "Sara Ahmed",
      "photo": "https://…",
      "section": "A",
      "grade": "Grade 3"
    }
  ],
  "employee": null
}
```
`children` is populated for `parent` role; `employee` is populated for `teacher` role; the other is `null`.

**Errors:** `invalid_credentials` (401), `account_disabled` (401), `role_not_supported` (403).

---

### POST /auth/refresh
Requires `Authorization: Bearer <refresh_token>`.

**Response (200):**
```json
{
  "ok": true,
  "access_token": "<new_JWT>",
  "token_type": "Bearer",
  "expires_in": 86400
}
```

---

### POST /auth/logout
Requires access token. Stateless — server does nothing; client must discard both tokens.

**Response:** `{ "ok": true, "message": "logged_out" }`

---

### POST /auth/register-device
Register or refresh an FCM device token.

**Request body (JSON):**
```json
{
  "fcm_token": "<Firebase token>",
  "platform": "android",
  "device_name": "Samsung Galaxy S24"
}
```
`platform`: `android` | `ios` | `web` (default: `android`).  
`device_name`: optional free-text label (≤200 chars).

**Response:** `{ "ok": true, "message": "device_registered", "device": { ... } }`

---

## 2. Current User

### GET /me
Returns the authenticated user's profile and school.

**Response:**
```json
{
  "ok": true,
  "user": { "id": 42, "name": "…", "role": "parent", … },
  "school": { "id": 3, "name": "…", "logo": "https://…", … },
  "children": [ … ]
}
```
`children` is populated for parent role; `null` for teacher role.

---

### POST /me/device-token
Save or update the FCM push token. Accepts both `device_token` (Flutter field) and `fcm_token`.

**Request body (JSON):**
```json
{
  "device_token": "<FCM token>",
  "platform": "android",
  "device_name": "Pixel 8",
  "locale": "ar"
}
```

**Response:** `{ "ok": true, "message": "device_token_saved" }`

---

### GET /me/badge-counts
Returns unread counts for all badge modules in one request.

**Response:**
```json
{
  "ok": true,
  "badges": {
    "notifications": 3,
    "messages": 1,
    "grades": 0,
    "homework": 2,
    "exams": 0,
    "attendance": 0,
    "fees": 1,
    "school_board": 4,
    "leave_requests": 0,
    "complaints": 0
  }
}
```
**Note:** Module counts (`grades`, `homework`, `exams`, `attendance`, `fees`) are based on changes since the last time `POST /me/mark-module-viewed/<module>` was called. On first use (before any mark-viewed call), these counts are always `0`. Flutter should call `mark-module-viewed` when the user opens each screen.

---

### POST /me/mark-module-viewed/\<module\>
Resets the badge count for a module by recording `last_viewed_at = now`.

**Allowed module names:** `grades`, `homework`, `exams`, `attendance`, `fees`, `leave_requests`, `complaints`

**Response:** `{ "ok": true, "module": "homework", "last_viewed_at": "2026-06-25T10:00:00+00:00" }`

**Error:** `{ "ok": false, "error": "unknown_module" }` (400) for invalid module names.

---

## 3. Notifications

### GET /parent/notifications
### GET /teacher/notifications
Paginated notifications visible to the authenticated user.

**Query params:**
- `limit` — default 50, max 100
- `offset` — default 0

**Response:**
```json
{
  "ok": true,
  "total": 12,
  "limit": 50,
  "offset": 0,
  "notifications": [
    {
      "id": 88,
      "title": "درجة جديدة",
      "body": "تم رصد درجة جديدة في اختبار الرياضيات.",
      "ntype": "grade",
      "is_read": false,
      "sent_at": "2026-06-25T09:00:00+00:00"
    }
  ]
}
```

---

### POST /notifications/\<notification_id\>/read
Mark a single notification as read. Available to both parent and teacher.

**Response:** `{ "ok": true, "notification_id": 88 }`  
**Error:** 404 if the notification doesn't exist, doesn't belong to this school, or isn't visible to this user.

---

### POST /notifications/read-all
Mark all visible notifications as read in a single bulk operation.

**Response:** `{ "ok": true, "marked": 5 }`

---

## 4. School Board (Videos & Announcements)

Available to both `parent` and `teacher` roles. Audience filter applied:
- `parent` → sees items with `audience = 'parents'` or `'all'`
- `teacher` → sees items with `audience = 'teachers'` or `'all'`

Only active, published (publish_at ≤ now), non-expired (expires_at > now or null) items are returned.

### GET /school/videos
Paginated video/media list. `limit` default 20, max 100. `offset` default 0.

**Response:**
```json
{
  "ok": true,
  "total": 5,
  "limit": 20,
  "offset": 0,
  "videos": [
    {
      "id": 7,
      "title": "School Opening Ceremony",
      "description": "…",
      "media_type": "video",
      "media_url": "https://youtu.be/…",
      "video_url": "https://youtu.be/…",
      "thumbnail_url": "https://…supabase.co/…/thumb.jpg",
      "audience": "all",
      "is_featured": false,
      "is_published": true,
      "is_read": false,
      "school_id": 3,
      "publish_at": "2026-06-01T00:00:00+00:00",
      "expires_at": null,
      "created_at": "2026-06-01T08:00:00+00:00"
    }
  ]
}
```
`media_type` values: `video` | `image`.  
`media_url` and `video_url` are the same field; use `media_url` as the canonical field.

---

### GET /school/videos/featured
Returns the latest featured video, or `{ "ok": true, "video": null }` if none.

---

### GET /school/videos/\<video_id\>
Single video detail. Also marks it as read automatically.

**Error:** 404 `video_not_found`.

---

### POST /school/videos/\<video_id\>/read
Explicitly mark a video as read (idempotent).

**Response:** `{ "ok": true, "message": "video_marked_read" }`

---

### GET /school/announcements
Paginated announcement list. Same pagination params as `/school/videos`.

**Response:**
```json
{
  "ok": true,
  "total": 3,
  "announcements": [
    {
      "id": 12,
      "title": "إجازة رسمية",
      "body": "تعلن المدرسة عن …",
      "description": "تعلن المدرسة عن …",
      "media_url": null,
      "media_type": "none",
      "thumbnail_url": null,
      "audience": "all",
      "is_featured": true,
      "is_published": true,
      "is_read": false,
      "school_id": 3,
      "publish_at": null,
      "expires_at": null,
      "created_at": "2026-06-24T10:00:00+00:00"
    }
  ]
}
```
`description` is an alias for `body` — both fields are returned for Flutter flexibility.  
`media_type` values: `none` | `image` | `video`.

---

### GET /school/announcements/featured
Returns `{ "ok": true, "announcement": { … } }` or `{ "announcement": null }`.

---

### GET /school/announcements/\<ann_id\>
Single announcement detail + auto mark-as-read.

---

### POST /school/announcements/\<ann_id\>/read
Explicit mark-as-read (idempotent).

**Response:** `{ "ok": true, "message": "announcement_marked_read" }`

---

### GET /school/board
Combined dashboard: featured video + featured announcement + latest 5 of each.

**Response:**
```json
{
  "ok": true,
  "featured_video": { … } or null,
  "featured_announcement": { … } or null,
  "videos": [ … ],
  "announcements": [ … ]
}
```

---

## 5. Parent Endpoints

All require role `parent`.

### GET /parent/children
Lists all students linked to this parent.

**Response:**
```json
{
  "ok": true,
  "count": 2,
  "children": [
    {
      "id": 101,
      "student_id": "STU-000001",
      "name": "Sara Ahmed",
      "photo": "https://…",
      "gender": "female",
      "section": "A",
      "grade": "Grade 3",
      "stage": "primary",
      "status": "active"
    }
  ]
}
```

---

### GET /parent/children/\<student_id\>
Full child profile with 30-day attendance snapshot, latest exam result, and fee summary.

**Response:**
```json
{
  "ok": true,
  "student": {
    "id": 101,
    "student_id": "STU-000001",
    "name": "Sara Ahmed",
    "photo": "https://…",
    "gender": "female",
    "section": "A",
    "grade": "Grade 3",
    "stage": "primary",
    "status": "active",
    "date_of_birth": "2015-03-12",
    "nationality": "Saudi",
    "address": "…",
    "phone": null,
    "enrollment_date": "2023-09-01",
    "guardian_name": "Ahmed Ali",
    "guardian_phone": "+966501234567",
    "guardian_relation": "father"
  },
  "attendance_last30": {
    "total": 22, "present": 20, "absent": 1,
    "late": 1, "on_leave": 0, "excused": 0, "att_pct": 95.5
  },
  "latest_exam": {
    "name": "Midterm Math",
    "subject": "Mathematics",
    "marks": 88.0,
    "max_marks": 100.0,
    "grade": "A",
    "is_pass": true,
    "date": "2026-06-15"
  },
  "fees_summary": { "total": 5000.0, "paid": 2500.0, "remaining": 2500.0 }
}
```

---

### GET /parent/children/\<student_id\>/attendance
**Query params:** `start` (YYYY-MM-DD), `end` (YYYY-MM-DD). Default: last 30 days. Max range: 365 days.

**Response:**
```json
{
  "ok": true,
  "student_id": 101,
  "range": { "start": "2026-05-25", "end": "2026-06-25" },
  "summary": { "total": 22, "present": 20, "absent": 1, "late": 1, "on_leave": 0, "excused": 0, "att_pct": 95.5 },
  "records": [
    {
      "date": "2026-06-25",
      "status": "present",
      "check_in": "07:45",
      "check_out": "13:30",
      "source": "device",
      "notes": null
    }
  ]
}
```
`status` values: `present` | `absent` | `late` | `excused` | `on_leave`  
`on_leave` records are excluded from the attendance percentage denominator.

---

### GET /parent/children/\<student_id\>/fees
All fee records with installment detail across all academic years.

**Response:**
```json
{
  "ok": true,
  "student_id": 101,
  "summary": { "total": 5000.0, "paid": 2500.0, "remaining": 2500.0 },
  "records": [
    {
      "id": 55,
      "fee_type": "Tuition",
      "year": "2025/2026",
      "total": 5000.0,
      "discount": 0.0,
      "net": 5000.0,
      "paid": 2500.0,
      "remaining": 2500.0,
      "installments": [
        {
          "id": 201,
          "no": 1,
          "amount": 2500.0,
          "received_amount": 2500.0,
          "remaining": 0.0,
          "due_date": "2026-01-01",
          "paid_date": "2026-01-05",
          "status": "paid",
          "receipt_no": "RCP-001"
        }
      ]
    }
  ]
}
```

---

### GET /parent/children/\<student_id\>/grades
All exam results across all academic years, newest first.

**Response:**
```json
{
  "ok": true,
  "student_id": 101,
  "count": 8,
  "results": [
    {
      "id": 312,
      "exam": "Midterm Math",
      "subject": "Mathematics",
      "section": "A",
      "grade_label": "Grade 3",
      "exam_date": "2026-06-15",
      "max_marks": 100.0,
      "pass_marks": 50.0,
      "marks": 88.0,
      "grade": "A",
      "is_pass": true,
      "rank": 2,
      "notes": null,
      "year": "2025/2026"
    }
  ]
}
```

---

### GET /parent/children/\<student_id\>/exams
Exams scheduled for the child's current section.  
Default window: 30 days before today → 60 days ahead. No query params.

**Response:**
```json
{
  "ok": true,
  "student_id": 101,
  "exams": [
    {
      "id": 77,
      "name": "Final Science",
      "subject": "Science",
      "exam_date": "2026-07-01",
      "max_marks": 100.0,
      "pass_marks": 50.0,
      "is_upcoming": true
    }
  ]
}
```
**Note:** Returns `{ "exams": [] }` immediately if the child has no `section_id` assigned.

---

### GET /parent/children/\<student_id\>/homework
Homework for the child's current section (active, published, current academic year).

**Feature gate:** Returns 403 if the school's homework module is not enabled (`api_access` action). Error: `"الوصول إلى الواجبات غير مفعل لهذه المدرسة."`

**Response:**
```json
{
  "ok": true,
  "student_id": 101,
  "section": "A",
  "count": 2,
  "homework": [
    {
      "id": 15,
      "homework_id": 15,
      "title": "Chapter 5 Exercises",
      "subject": "Mathematics",
      "subject_name": "Mathematics",
      "teacher_name": "Mr. Hassan",
      "grade_name": "Grade 3",
      "section_name": "A",
      "assigned_at": "2026-06-20",
      "publish_date": "2026-06-20",
      "due_date": "2026-06-27",
      "description": "Complete exercises 1–10.",
      "status": "active",
      "attachment_url": "https://…supabase.co/…/hw.pdf",
      "attachment_type": "pdf",
      "file_name": "hw.pdf",
      "file_size": null,
      "is_pdf": true,
      "submitted_status": "not_submitted"
    }
  ]
}
```
`submitted_status` is always `"not_submitted"` (student submission tracking not yet implemented server-side).  
`attachment_url` is `null` if no attachment was uploaded.  
`attachment_type`: `"pdf"` | `"image"` | `null`.

---

### GET /parent/children/\<student_id\>/schedule
Weekly class schedule for the child's section.

**Response:**
```json
{
  "ok": true,
  "student_id": 101,
  "section": "A",
  "grade": "Grade 3",
  "schedule": [
    {
      "id": 33,
      "day": 0,
      "day_name": "الأحد",
      "subject": "Mathematics",
      "subject_code": "MATH",
      "teacher": "Mr. Hassan",
      "start_time": "08:00",
      "end_time": "08:45",
      "room": "Room 5"
    }
  ]
}
```
`day` is 0-based integer: 0=Sunday, 1=Monday, …, 6=Saturday.

---

### GET /parent/children/\<student_id\>/leave-requests
List all leave requests for the child.

**Response:**
```json
{
  "ok": true,
  "count": 1,
  "requests": [
    {
      "id": 9,
      "student_id": 101,
      "student_name": "Sara Ahmed",
      "leave_type": "sick",
      "leave_type_label": "إجازة مرضية",
      "start_date": "2026-06-26",
      "end_date": "2026-06-27",
      "reason": "مراجعة طبية",
      "status": "pending",
      "status_label": "قيد الانتظار",
      "admin_note": null,
      "source": "parent",
      "created_at": "2026-06-25T09:00:00+00:00",
      "attachment": null
    }
  ]
}
```

---

### POST /parent/children/\<student_id\>/leave-requests
Create a leave request. Supports both `application/json` and `multipart/form-data`.

**`leave_type` values:** `sick` | `medical` | `family` | `travel` | `emergency` | `other`

**JSON body:**
```json
{
  "leave_type": "sick",
  "start_date": "2026-06-26",
  "end_date": "2026-06-27",
  "reason": "مراجعة طبية"
}
```

**Multipart fields:** same field names + optional `attachment` file (jpg/jpeg/png/pdf, max 15 MB).  
Attachment content is verified (magic header check) — renamed files are rejected.

**Response (201):**
```json
{ "ok": true, "message": "leave_request_created", "request": { … } }
```

**Errors:** `required_field_missing: leave_type`, `invalid_leave_type`, `end_date_before_start_date`, `no_active_academic_year_for_student`, `attachment_too_large`, `invalid_attachment_type`.

---

### GET /parent/children/\<student_id\>/leave-requests/\<request_id\>
Single leave request detail.

---

### DELETE /parent/children/\<student_id\>/leave-requests/\<request_id\>
Delete a **pending** leave request. Storage attachment is also deleted.

**Error:** `cannot_cancel_non_pending_request` if status is not `pending`.

---

### GET /parent/children/\<student_id\>/complaints
List all complaints for the child.

---

### POST /parent/children/\<student_id\>/complaints
Create a complaint. `application/json` only.

**`category` values:** `academic` | `administrative` | `financial` | `transportation` | `behavior` | `other`

**Body:**
```json
{ "category": "academic", "title": "…", "body": "…" }
```

**Response (201):** `{ "ok": true, "message": "complaint_created", "complaint": { … } }`

---

### GET /parent/children/\<student_id\>/complaints/\<complaint_id\>
Single complaint detail.

---

### DELETE /parent/children/\<student_id\>/complaints/\<complaint_id\>
Delete a complaint with status `new` only.

---

### GET /parent/children/\<student_id\>/transportation
Active transport route for the child.

**Response (assigned):**
```json
{ "ok": true, "transportation": { "driver_name": "Mohamed", "phone": "+966…", "vehicle_name": "Toyota Hiace" } }
```

**Response (not assigned):** `{ "ok": true, "transportation": null }`

---

## 6. Teacher Endpoints

All require role `teacher`.

### GET /teacher/profile
Teacher employee record + dashboard stats + assigned subjects.

**Response:**
```json
{
  "ok": true,
  "employee": {
    "id": 5,
    "employee_id": "EMP-000001",
    "user_id": 42,
    "name": "Hassan Ibrahim",
    "full_name": "Hassan Ibrahim",
    "job_title": "Math Teacher",
    "department": "Science",
    "phone": "+966…",
    "email": "hassan@school.com",
    "photo": "https://…",
    "photo_url": "https://…",
    "hire_date": "2022-09-01",
    "status": "active",
    "school_id": 3,
    "school_name": "Springfield School",
    "role": "teacher"
  },
  "stats": {
    "sections_count": 3,
    "student_count": 87,
    "upcoming_exams_14d": 2
  },
  "subjects": [
    { "id": 7, "name": "Mathematics" }
  ]
}
```

---

### GET /teacher/subjects
Distinct subjects assigned to this teacher across all sections.  
**Primary source for Flutter subject pickers** (Create Exam, Create Homework, etc.).

**Response:**
```json
{ "ok": true, "subjects": [ { "id": 7, "name": "Mathematics" } ] }
```

---

### GET /teacher/sections
All sections the teacher teaches (homeroom + subject assignments).

**Response:**
```json
{
  "ok": true,
  "sections": [
    {
      "id": 12,
      "name": "A",
      "grade_name": "Grade 3",
      "grade": "Grade 3",
      "stage": "primary",
      "display_name": "Grade 3 - شعبة A",
      "capacity": 30,
      "student_count": 28,
      "is_homeroom": true,
      "subjects": [ { "id": 7, "name": "Mathematics" } ]
    }
  ]
}
```
**Returns empty array if the teacher has no section assignments (homeroom or subject).**  
Admin must configure section/subject assignments via the web admin panel.

---

### GET /teacher/sections/\<section_id\>/students
Active students in one of the teacher's sections.

**Query param:** `q=<name fragment>` for name search (optional).

**Response:**
```json
{
  "ok": true,
  "section": { "id": 12, "name": "A", "grade": "Grade 3" },
  "count": 28,
  "students": [
    {
      "id": 101,
      "student_id": "STU-000001",
      "name": "Sara Ahmed",
      "gender": "female",
      "photo": "https://…",
      "status": "active"
    }
  ]
}
```

---

### GET /teacher/students/\<student_id\>
Profile for a student in one of the teacher's sections.  
Includes 30-day attendance snapshot and last 10 exam results for the teacher's sections.

---

### GET /teacher/schedule
The teacher's weekly timetable for the current academic year.

**Response:**
```json
{
  "ok": true,
  "schedule": [
    {
      "id": 44,
      "day": "sunday",
      "day_int": 0,
      "day_en": "sunday",
      "day_label": "الأحد",
      "day_name": "الأحد",
      "start_time": "08:00",
      "end_time": "08:45",
      "grade_id": 8,
      "grade_name": "Grade 3",
      "grade": "Grade 3",
      "section_id": 12,
      "section_name": "A",
      "section": "A",
      "subject_id": 7,
      "subject_name": "Mathematics",
      "subject": "Mathematics",
      "subject_code": "MATH",
      "room": "Room 5"
    }
  ]
}
```
**Note:** Returns empty array if no active academic year is configured for the school.  
Includes both section-based and grade-based schedule rows.  
Unassigned rows (`teacher_id IS NULL`) are included when the section/grade belongs to this teacher.

---

### GET /teacher/my-attendance
The teacher's own employee attendance records for the current academic year.

**Response:**
```json
{
  "ok": true,
  "records": [
    {
      "id": 501,
      "date": "2026-06-25",
      "check_in": "07:55",
      "check_out": "13:45",
      "status": "present",
      "notes": ""
    }
  ],
  "summary": {
    "total_days": 120,
    "present_days": 115,
    "late_days": 3,
    "absent_days": 2
  }
}
```
`status` values: `present` | `late` | `absent`  
Notes from AiFace device records are hidden (returned as empty string).

---

### GET /teacher/exams
Exams for all of the teacher's sections.

**Query params:**
- `upcoming=1` — only future exams
- `past=1` — only past exams
- `limit` — default 50, max 100
- `offset` — default 0

**Response:**
```json
{
  "ok": true,
  "count": 3,
  "exams": [
    {
      "id": 77,
      "title": "Midterm Math",
      "name": "Midterm Math",
      "subject_id": 7,
      "subject_name": "Mathematics",
      "subject": "Mathematics",
      "section_id": 12,
      "section_name": "A",
      "section": "A",
      "grade_name": "Grade 3",
      "grade": "Grade 3",
      "exam_date": "2026-07-01",
      "exam_time": "09:00",
      "duration_minutes": 60,
      "max_score": 100.0,
      "max_marks": 100.0,
      "pass_marks": 50.0,
      "notes": null,
      "is_upcoming": true,
      "result_count": 0,
      "created_at": "2026-06-20T08:00:00"
    }
  ]
}
```

---

### GET /teacher/exams/check-conflict
Read-only: check whether a proposed exam slot conflicts with existing exams.

**Query params (all required):**
- `section_id` — int
- `exam_date` — YYYY-MM-DD
- `exam_time` — HH:MM
- `duration_minutes` — positive int
- `exclude_exam_id` — int (optional, for edit support)

**Response (no conflict):** `{ "ok": true, "has_conflict": false, "available": true }`

**Response (conflict):**
```json
{
  "ok": true,
  "has_conflict": true,
  "available": false,
  "message": "There is another exam for this section at the same time",
  "conflict": {
    "exam_id": 75,
    "title": "Science Quiz",
    "subject_name": "Science",
    "exam_date": "2026-07-01",
    "exam_time": "08:30",
    "duration_minutes": 45
  }
}
```

---

### GET /teacher/exams/check-day
Soft check: return all exams for a section on a given date (informational only).

**Query params:** `section_id`, `exam_date` (YYYY-MM-DD)

**Response:**
```json
{
  "ok": true,
  "has_exams_same_day": true,
  "same_day_exams": [
    { "exam_id": 75, "title": "…", "subject_name": "…", "exam_date": "…", "exam_time": "09:00", "duration_minutes": 45 }
  ]
}
```

---

### POST /teacher/exams
Create an exam. The teacher must be assigned to both the section and the subject.

**Request body (JSON):**
```json
{
  "title": "Midterm Math",
  "section_id": 12,
  "subject_id": 7,
  "exam_date": "2026-07-01",
  "exam_time": "09:00",
  "duration_minutes": 60,
  "max_score": 100,
  "pass_marks": 50,
  "exam_type_id": null
}
```
`title` required. `exam_time` and `duration_minutes` optional (no conflict check if omitted).  
`max_score` also accepted as `max_marks`.

**Response (201):**
```json
{ "ok": true, "message": "exam_created", "exam": { … } }
```

**Errors:**  
`required_field_missing: title|section_id|subject_id|exam_date`  
`forbidden — section not assigned to you` (403)  
`forbidden — subject not assigned to you` (403)  
`no_active_academic_year` (400)  
`exam_time_conflict` (409) — when time+duration conflict with existing exam

---

### GET /teacher/exams/\<exam_id\>
Exam detail + entered results + list of students missing a result.

**Response:**
```json
{
  "ok": true,
  "exam": {
    "id": 77, "title": "Midterm Math",
    "subject_id": 7, "subject_name": "Mathematics", "subject": "Mathematics",
    "section_id": 12, "section_name": "A", "section": "A",
    "grade_name": "Grade 3", "grade": "Grade 3",
    "exam_date": "2026-07-01", "exam_time": "09:00", "duration_minutes": 60,
    "max_score": 100.0, "max_marks": 100.0, "pass_marks": 50.0,
    "total_students": 28, "results_entered": 25, "results_missing": 3,
    "created_at": "2026-06-20T08:00:00"
  },
  "results": [
    {
      "student_id": 101,
      "student_name": "Sara Ahmed",
      "marks": 88.0,
      "grade_letter": "A",
      "grade": "A",
      "is_pass": true,
      "rank": 2,
      "notes": null
    }
  ],
  "missing_students": [
    { "id": 105, "student_id": "STU-000005", "name": "Yousef Ali" }
  ]
}
```

---

### POST /teacher/exams/\<exam_id\>/results
Bulk upsert grade entries. Triggers FCM notifications to parents of affected students.

**Request body (JSON):**
```json
{
  "results": [
    { "student_id": 101, "score": 88.5, "note": "Well done" },
    { "student_id": 102, "score": 72.0, "notes": "" }
  ]
}
```
`score` or `marks` accepted (Flutter uses `score`).  
`note` (singular) or `notes` (plural) accepted.  
`grade_letter` is always calculated server-side — never trust client value.  
`student_id` is coerced from float to int (safe for Dart JSON numbers).

**Response (200):**
```json
{
  "ok": true,
  "saved": 2,
  "created": 1,
  "updated": 1,
  "unchanged": 0,
  "errors": [],
  "results": [ … ]
}
```
`errors` array contains per-entry validation failures (e.g., `marks_out_of_range`, `not_in_section`).

---

### GET /teacher/homework
List homework created by this teacher for the current academic year.

**Feature gate:** Returns 403 if homework module is not enabled.  
**Query params:** `limit` (default 50, max 100), `offset` (default 0).

**Response:**
```json
{
  "ok": true,
  "total": 10,
  "limit": 50,
  "offset": 0,
  "homework": [
    {
      "id": 15,
      "title": "Chapter 5 Exercises",
      "subject_id": 7,
      "subject_name": "Mathematics",
      "subject": "Mathematics",
      "section_id": 12,
      "section_name": "A",
      "section": "A",
      "grade_name": "Grade 3",
      "grade": "Grade 3",
      "display_name": "Grade 3 - شعبة A",
      "publish_date": "2026-06-20",
      "due_date": "2026-06-27",
      "description": "Complete exercises 1–10.",
      "attachment_url": "https://…",
      "attachment_type": "pdf",
      "created_at": "2026-06-20T08:00:00"
    }
  ]
}
```

---

### POST /teacher/homework
Create a homework assignment. Triggers FCM notifications to parents in the section.

**Feature gates:** `homework.api_access` AND `homework.create` must both be enabled.

Supports `application/json` or `multipart/form-data`.

**JSON fields:**
```json
{
  "title": "Chapter 5 Exercises",
  "section_id": 12,
  "subject_id": 7,
  "due_date": "2026-06-27",
  "publish_date": "2026-06-20",
  "description": "Complete exercises 1–10."
}
```
`publish_date` defaults to today if omitted.  
**Multipart:** same fields + optional `attachment` file (jpg/jpeg/png/webp/pdf).

**Response (201):**
```json
{
  "ok": true,
  "message": "تم إضافة الواجب بنجاح.",
  "homework": {
    "id": 15,
    "title": "…",
    "description": "…",
    "subject_id": 7,
    "subject_name": "Mathematics",
    "section_id": 12,
    "section_name": "A",
    "grade_name": "Grade 3",
    "publish_date": "2026-06-20",
    "due_date": "2026-06-27",
    "attachment_url": null,
    "attachment_name": null,
    "attachment_type": null
  }
}
```

---

### PUT /teacher/homework/\<homework_id\>
### PATCH /teacher/homework/\<homework_id\>
Update an existing homework assignment. Teacher can only update their own homework.  
Supports JSON or multipart. Same field requirements as POST except `publish_date` is not editable.

**Response:** `{ "ok": true, "homework": { … } }`

---

### DELETE /teacher/homework/\<homework_id\>
Soft-delete (sets `is_active=False`). Teacher can only delete their own homework.

**Response:** `{ "ok": true, "message": "homework_deleted" }`

---

## 7. Teacher Leave Requests

### GET /teacher/leave-requests
List the teacher's own leave requests.

**Response:**
```json
{
  "ok": true,
  "leave_requests": [
    {
      "id": 3,
      "leave_type": "sick",
      "start_date": "2026-06-26",
      "end_date": "2026-06-27",
      "reason": "مراجعة طبية",
      "details": null,
      "status": "pending",
      "admin_response": null,
      "rejection_reason": null,
      "reviewed_at": null,
      "created_at": "2026-06-25T09:00:00+00:00",
      "attachment_url": null,
      "can_delete": true
    }
  ]
}
```

---

### POST /teacher/leave-requests
Create a leave request. Supports both `application/json` and `multipart/form-data`.

**JSON body:**
```json
{
  "leave_type": "sick",
  "start_date": "2026-06-26",
  "end_date": "2026-06-27",
  "reason": "مراجعة طبية",
  "details": "تفاصيل اختيارية"
}
```
**Multipart:** same fields + optional `attachment` file (jpg/jpeg/png/pdf, max 15 MB).

`leave_type` values: `sick` | `medical` | `family` | `travel` | `emergency` | `other`

**Response (201):** `{ "ok": true, "leave_request": { … } }`

---

### GET /teacher/leave-requests/\<request_id\>
Single leave request detail.

---

### DELETE /teacher/leave-requests/\<request_id\>
Delete a **pending** leave request only.

**Response:** `{ "ok": true, "message": "Leave request deleted successfully" }`

---

## 8. Chat / Messaging

Chat module must be enabled for the school (`chat` module + `chat.api_access` feature).  
Available to both `parent` and `teacher` roles.

### GET /chat/rooms
List all chat rooms the user is a member of.

**Query params:**
- `limit` — default 50, max 100
- `offset` — default 0
- `type` — optional filter: `private` | `group` | `announcement`

**Response:**
```json
{
  "ok": true,
  "total": 3,
  "rooms": [
    {
      "id": 1,
      "name": "Grade 3 - Math",
      "type": "group",
      "scope": "section",
      "is_closed": false,
      "is_announcement_only": false,
      "can_send": true,
      "my_role": "member",
      "is_blocked": false,
      "unread_count": 2,
      "last_message": {
        "body": "Homework due tomorrow",
        "sender_name": "Mr. Hassan",
        "created_at": "2026-06-25T10:00:00+00:00"
      }
    }
  ]
}
```

---

### GET /chat/rooms/\<room_id\>
Room detail including members and send schedule.

---

### GET /chat/rooms/\<room_id\>/messages
Paginated message history (newest first, reversed before return so oldest is first in array).

**Query params:** `limit` (default 50, max 100), `before` (message ID for cursor pagination).

**Response:**
```json
{
  "ok": true,
  "room_id": 1,
  "count": 10,
  "messages": [
    {
      "id": 500,
      "sender_id": 42,
      "sender_name": "Mr. Hassan",
      "sender_role": "teacher",
      "body": "Homework due tomorrow",
      "message_type": "text",
      "attachment_url": null,
      "created_at": "2026-06-25T10:00:00+00:00",
      "is_mine": false,
      "is_deleted": false
    }
  ]
}
```

---

### POST /chat/rooms/\<room_id\>/messages
Send a text message. Feature gate: `chat.send_message` must be enabled.

**Request body (JSON):** `{ "body": "…" }`  
Max message length is configurable per school (default 2000 chars).

**Response (201):** `{ "ok": true, "message": "تم إرسال الرسالة بنجاح.", "data": { … } }`

**Errors:** `لست عضواً في هذه المحادثة.` (403), `هذه المحادثة مغلقة` (403), schedule-window messages (403).

---

### POST /chat/rooms/\<room_id\>/read
Mark all unread messages in the room as read.

**Response:** `{ "ok": true, "marked": 5 }`

---

### GET /chat/contacts
Available contacts for starting private chats.  
- Parent → teachers of children's sections + school admins  
- Teacher → parents of students in teacher's sections + school admins

**Response:**
```json
{
  "ok": true,
  "count": 8,
  "contacts": [
    {
      "user_id": 42,
      "name": "Hassan Ibrahim",
      "role": "teacher",
      "job_title": "Math Teacher",
      "photo": "https://…"
    }
  ]
}
```

---

## 9. Endpoint Summary Table

| Method | Path | Roles | Feature Gate |
|---|---|---|---|
| POST | /auth/login | — | — |
| POST | /auth/refresh | — | — |
| POST | /auth/logout | any | — |
| POST | /auth/register-device | any | — |
| GET | /me | parent, teacher | — |
| POST | /me/device-token | parent, teacher | — |
| GET | /me/badge-counts | parent, teacher | — |
| POST | /me/mark-module-viewed/\<module\> | parent, teacher | — |
| POST | /notifications/\<id\>/read | parent, teacher | — |
| POST | /notifications/read-all | parent, teacher | — |
| GET | /school/videos | parent, teacher | — |
| GET | /school/videos/featured | parent, teacher | — |
| GET | /school/videos/\<id\> | parent, teacher | — |
| POST | /school/videos/\<id\>/read | parent, teacher | — |
| GET | /school/announcements | parent, teacher | — |
| GET | /school/announcements/featured | parent, teacher | — |
| GET | /school/announcements/\<id\> | parent, teacher | — |
| POST | /school/announcements/\<id\>/read | parent, teacher | — |
| GET | /school/board | parent, teacher | — |
| GET | /parent/children | parent | — |
| GET | /parent/children/\<id\> | parent | — |
| GET | /parent/children/\<id\>/attendance | parent | — |
| GET | /parent/children/\<id\>/fees | parent | — |
| GET | /parent/children/\<id\>/grades | parent | — |
| GET | /parent/children/\<id\>/exams | parent | — |
| GET | /parent/children/\<id\>/homework | parent | homework.api_access |
| GET | /parent/children/\<id\>/schedule | parent | — |
| GET | /parent/notifications | parent | — |
| GET | /parent/children/\<id\>/leave-requests | parent | — |
| POST | /parent/children/\<id\>/leave-requests | parent | — |
| GET | /parent/children/\<id\>/leave-requests/\<rid\> | parent | — |
| DELETE | /parent/children/\<id\>/leave-requests/\<rid\> | parent | — |
| GET | /parent/children/\<id\>/complaints | parent | — |
| POST | /parent/children/\<id\>/complaints | parent | — |
| GET | /parent/children/\<id\>/complaints/\<cid\> | parent | — |
| DELETE | /parent/children/\<id\>/complaints/\<cid\> | parent | — |
| GET | /parent/children/\<id\>/transportation | parent | — |
| GET | /teacher/profile | teacher | — |
| GET | /teacher/subjects | teacher | — |
| GET | /teacher/sections | teacher | — |
| GET | /teacher/sections/\<id\>/students | teacher | — |
| GET | /teacher/students/\<id\> | teacher | — |
| GET | /teacher/schedule | teacher | — |
| GET | /teacher/my-attendance | teacher | — |
| GET | /teacher/exams | teacher | — |
| GET | /teacher/exams/check-conflict | teacher | — |
| GET | /teacher/exams/check-day | teacher | — |
| POST | /teacher/exams | teacher | — |
| GET | /teacher/exams/\<id\> | teacher | — |
| POST | /teacher/exams/\<id\>/results | teacher | — |
| GET | /teacher/notifications | teacher | — |
| GET | /teacher/homework | teacher | homework.api_access |
| POST | /teacher/homework | teacher | homework.api_access + homework.create |
| PUT | /teacher/homework/\<id\> | teacher | homework.api_access |
| PATCH | /teacher/homework/\<id\> | teacher | homework.api_access |
| DELETE | /teacher/homework/\<id\> | teacher | homework.api_access |
| GET | /teacher/leave-requests | teacher | — |
| POST | /teacher/leave-requests | teacher | — |
| GET | /teacher/leave-requests/\<id\> | teacher | — |
| DELETE | /teacher/leave-requests/\<id\> | teacher | — |
| GET | /chat/rooms | parent, teacher | chat + chat.api_access |
| GET | /chat/rooms/\<id\> | parent, teacher | chat + chat.api_access |
| GET | /chat/rooms/\<id\>/messages | parent, teacher | chat + chat.api_access |
| POST | /chat/rooms/\<id\>/messages | parent, teacher | chat + chat.api_access + chat.send_message |
| POST | /chat/rooms/\<id\>/read | parent, teacher | chat + chat.api_access |
| GET | /chat/contacts | parent, teacher | chat + chat.api_access |

---

## 10. Known missing APIs / Flutter-side adjustments

| Area | Status |
|---|---|
| Student homework submission tracking | Not implemented server-side. `submitted_status` always returns `"not_submitted"`. |
| File/image upload for chat messages | Not implemented. Text messages only. |
| Push notification badge count without `mark-module-viewed` | Module counts (`grades`, `homework`, etc.) return 0 until the module is viewed once. Flutter must call `POST /me/mark-module-viewed/<module>` on screen entry to activate badge tracking. |
| Exam result edit (single entry) | Not a separate endpoint — use `POST /teacher/exams/<id>/results` with a single-item array; existing result is updated. |

---

## 11. Verification checklist (VPS)

### Check API health
```bash
curl -s https://school.smartcoreiq.cloud/api/mobile/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"testparent","password":"wrongpass"}' | python3 -m json.tool
# Expected: {"ok": false, "error": "invalid_credentials"}
```

### Verify login and extract token
```bash
TOKEN=$(curl -s https://school.smartcoreiq.cloud/api/mobile/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"PARENT_USER","password":"PARENT_PASS"}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('access_token',''))")
echo "Token: $TOKEN"
```

### Test protected endpoint
```bash
curl -s https://school.smartcoreiq.cloud/api/mobile/v1/me \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

### Test parent children
```bash
curl -s https://school.smartcoreiq.cloud/api/mobile/v1/parent/children \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

### Test school board
```bash
curl -s https://school.smartcoreiq.cloud/api/mobile/v1/school/board \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
# Verify: video.media_url and announcement.media_url are absolute HTTPS URLs or null
```

### Verify media file URL format
```bash
# Pick any photo/media URL from /me, /parent/children, or /school/board and verify 200.
# Local uploads are served by the Flask media blueprint at /media/uploads/...
curl -I "https://school.smartcoreiq.cloud/media/uploads/students/example.jpg"
# Expected: HTTP/2 200 with Cache-Control header

# School Board video example (Range support — video seeking)
curl -I "https://school.smartcoreiq.cloud/media/uploads/schools/3/board/media/<uuid>.mp4"
# Expected: HTTP/2 200, Accept-Ranges: bytes
```

### Check Nginx logs for mobile requests on VPS
```bash
# On VPS server:
docker logs <nginx_container_name> --tail 100 | grep "api/mobile"
# Or if using journald:
journalctl -u nginx --since "1 hour ago" | grep "api/mobile"
```

### Check Flask/Gunicorn logs for errors
```bash
docker logs <web_container_name> --tail 200 | grep -E "ERROR|mecha.mobile"
```

### Verify Supabase storage is working (new uploads go to Supabase)
```bash
# After uploading a homework attachment from the teacher app, check the response:
# attachment_url should start with https://<supabase-project>.supabase.co
# If it starts with https://school.smartcoreiq.cloud/static/, Supabase is NOT configured
```

---

## 12. Migration notes from Render to VPS

| Issue | Detail |
|---|---|
| **Old media paths** | Student/employee photos uploaded before migration that used local `uploads/…` paths will return `null` in the API. Files are not on the new VPS disk. |
| **Supabase requirement** | Set `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, and `SUPABASE_BUCKET` on VPS. Without these, uploads fall back to the VPS container's local disk and will be lost on container restart. |
| **HTTPS URLs for media files** | Fixed (2026-06-25): `photo_url()` forces the `PREFERRED_URL_SCHEME` ('https' in production) on generated URLs, so media URLs are HTTPS even though Flask receives plain HTTP from nginx. |
| **School Board local media 404** | Fixed (2026-06-25): newly uploaded School Board images/videos returned `/static/uploads/...` URLs that the VPS nginx did not serve (uploads live in `app/static/uploads/`, but nginx `/static/` served the repo-root `static/` folder). Local uploads are now served by the Flask `media` blueprint at `/media/uploads/...`, which is proxy-independent. Supabase/external URLs pass through unchanged. |
| **Teacher leave JSON** | Fixed (2026-06-25): `POST /teacher/leave-requests` now accepts both `application/json` and `multipart/form-data`, consistent with the parent leave endpoint. |
| **Nginx SSL** | HTTPS is terminated by the upstream proxy. The `/media/...` route is served by Gunicorn (no nginx static alias required). Optional optimization: add an nginx `location /media/uploads/ { alias /var/www/mecha-school/app/static/uploads/; }` to offload media serving from Gunicorn. |
