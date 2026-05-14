# Mecha-School ERP — Phase 4 Changelog

**Scope:** White-label admin page, sidebar reorder (Fees → primary), printable schedule PDF, and base-template branding.
**Depends on:** Phase 1 `SchoolSettings` model, Phase 2 `log_action` helper, Phase 3 admin structure.
**Engineers:** Ahmed & Abbas.

---

## 1. Files touched

| File | Change |
|---|---|
| `app/blueprints/admin/__init__.py` | **New route.** `GET/POST /admin/school-settings` — single-screen editor for the tenant's identity. Handles logo + favicon upload, name (AR/EN), color, currency, timezone, contact info, and receipt footer. Writes an `AuditLog` row on save. |
| `app/templates/admin/school_settings.html` | **New.** Two-column layout: identity / contact / currency / footer on the left, logo + favicon upload on the right. Live previews of current uploads. |
| `app/templates/admin/settings.html` | Added two new hub cards — *School Identity (white-label)* and *Audit Log*, keeping the original trio. |
| `app/templates/shared/sidebar.html` | **New.** Sidebar extracted from `base.html` into its own partial. Cleaner modularity — any future tenant-theme or nav change edits a single file. Reads `school` from the context processor for the header (prefers `school.school_name_ar`, falls back to EN, then to "Mecha-School"). |
| `app/templates/shared/base.html` | Inline `<nav id="sidebar">…</nav>` replaced with `{% include 'shared/sidebar.html' %}`. Page shrunk by ~210 lines. All existing CSS selectors (`#sidebar`, `.sidebar-logo`, `.sidebar-nav`, `.nav-item a.active` etc.) still live in the base `<style>` block, so the visual output is identical. |
| `app/utils/pdf_gen.py` | **New generator.** `generate_schedule_pdf(section, entries, days, school=None)` builds a landscape A4 weekly timetable. Time column on the left, Sun–Fri across the top, each cell shows `subject / teacher / room`. Header uses SchoolSettings (Arabic name + footer + phone + website). |
| `app/blueprints/schedules/__init__.py` | **New route.** `GET /schedules/<section_id>/print` → streams the PDF inline (`send_file` with `as_attachment=False`) so `target="_blank"` previews it, user can Ctrl-P from the browser. Admin-only. |
| `app/templates/schedules/index.html` | Added "طباعة الجدول (PDF)" button in the page header, only visible once a section is selected. Opens in a new tab. |

---

## 2. White-label admin screen

Single-screen, two-column layout at `/admin/school-settings`. Field map:

**Left — identity & contact:**
- `school_name` (EN, required) + `school_name_ar` (AR, optional — displayed in sidebar when set)
- `primary_color` (HTML color picker)
- `locale` (ar / en)
- `address`, `phone`, `email`, `website`
- `currency_code`, `currency_symbol`, `timezone`
- `receipt_footer` — prepended to printed documents (receipts, payslips, schedules)

**Right — uploads:**
- `logo` → saved to `app/static/uploads/logo_<ts>_<name>` and referenced as `school.logo_path`
- `favicon` → same uploads dir, written to `school.favicon_path`

Upload filenames are `secure_filename`'d and timestamped, so re-uploading the same file doesn't collide. No deletion on replace — the previous file stays on disk (harmless, can be cleaned by a future cron).

Save flow:
1. Parse all fields (falling back to current values / safe defaults).
2. Persist new upload paths only if a file was actually chosen — leaving the input blank keeps the current logo.
3. `db.session.commit()`.
4. `log_action('edit', 'school_settings', row.id, details='white-label identity updated')`.
5. Flash success + redirect back to the same page.

The model's `SchoolSettings.get()` classmethod is what every consumer uses — it lazy-creates a default row on first call, so a brand-new install already has a sane object to edit.

---

## 3. Sidebar reorder + extraction

The sidebar now lives in its own `shared/sidebar.html` partial and is pulled into the base layout with a single `{% include %}`. This is the clean-architecture move that Phase 4 was supposed to deliver on top of the nav ordering work.

Ordering is now:

```
الرئيسية
  ├─ لوحة التحكم
  └─ الرسوم والأقساط   ← Phase 4 ask: Fees is the primary money entry-point

الطلاب
  ├─ الطلاب
  ├─ الحضور والغياب
  └─ الدرجات والاختبارات

الموارد البشرية
  └─ الموظفون

المالية العامة
  ├─ الإيرادات والمصروفات
  ├─ الرواتب
  └─ تقييم الموظفين

التقارير
  └─ التقارير والإحصائيات

التواصل
  └─ إعلانات الأولياء

النظام
  ├─ الإشعارات
  ├─ الصفوف والشعب
  ├─ الجداول الدراسية
  ├─ المستخدمون والصلاحيات
  ├─ الأدوار والأذونات
  ├─ سجل العمليات
  ├─ هوية المدرسة          ← new
  └─ الإعدادات
```

Every `nav-item` is still permission-gated — a `manage_fees` permission unlocks the Fees shortcut, `view_students` unlocks the Students group, `is_admin` unlocks the System group, and so on. Parents still bypass this entire tree and land on the parent portal nav.

---

## 4. Schedule PDF print

`GET /schedules/<section_id>/print` → landscape A4 PDF via ReportLab.

Layout:
- Centered header: `school.school_name_ar` (fallback EN) + `Weekly Schedule — <grade> / <section>`.
- Table: 7 columns (`الوقت` + الأحد..الجمعة). Rows are the distinct `(start_time, end_time)` pairs sorted by start_time. Empty cells render as `—`.
- Each filled cell: subject name, teacher full name (if set), and `Room: <room>` (if set), stacked.
- Alternating row background (`#fff` / `#fafcff`) and navy header for readability when printed on mono-office paper.
- Footer line: generation timestamp + school name + phone + website, followed by `school.receipt_footer` on a second line when configured.

Behaviour when a section has zero entries: renders the header + an "لا توجد حصص مسجّلة" placeholder line instead of a table, so the PDF is still a valid 1-page document.

The route is admin-only (`@admin_required`) and the file is streamed `as_attachment=False` — clicking the button opens the PDF in a new tab, where the browser's built-in Ctrl-P dialog handles physical printing. Download filename: `schedule_<grade>_<section>.pdf`.

---

## 5. Audit trail additions

| Blueprint.route | action | resource |
|---|---|---|
| `admin.school_settings` (POST save) | edit | school_settings |

Nothing else wired in Phase 4 — the PDF print route is read-only and deliberately skipped from audit (a heavy audit of "someone printed X" would bloat the log without adding security value).

---

## 6. Verification

```
python -c "import ast; ast.parse(...)"
  → app/blueprints/admin/__init__.py         OK
  → app/blueprints/schedules/__init__.py     OK
  → app/utils/pdf_gen.py                     OK

Jinja2 Environment(...).parse(...) :
  → admin/settings.html                      OK
  → admin/school_settings.html               OK
  → shared/sidebar.html                      OK
  → shared/base.html                         OK
  → schedules/index.html                     OK

Sanity greps:
  → "include 'shared/sidebar.html'"          found in base.html
  → school.logo_path                          present in sidebar.html
  → url_for('admin.school_settings')          present in sidebar.html
  → url_for('fees.index')                     present in sidebar.html
```

No schema changes — `SchoolSettings` already shipped in Phase 1 with all the columns we touch. No migration required.

---

## 7. Status

Phases 1 → 4 of the technical overhaul are complete. What's live:

- **Phase 1:** Fresh models (RFID, Device, parent_students, Audit, Announcement, SchoolSettings, etc.) + reset-db migration.
- **Phase 2:** Core bug fixes (photo save, AY rename, fees accuracy) + manual received_amount flow + Payroll↔Expense bridge + audit wiring.
- **Phase 3:** Parent web portal + `/api/v1/parent/*` JSON API + `/api/v1/hardware/attendance` RFID endpoint + `NotificationService` (FCM or dev-log) + admin broadcast composer.
- **Phase 4:** White-label `/admin/school-settings` + sidebar reorder (Fees primary) + printable schedule PDF.

Next pass (out of scope, tracked separately) would be the scheduled-announcement cron worker and the Flutter parent app that consumes the `/api/v1/parent/*` endpoints.
