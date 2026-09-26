# Mecha School Project Instructions

This is a production multi-school, multi-user, multi-role, and multi-academic-year school management system built with Flask and PostgreSQL.

The global engineering, security, and isolation requirements in the user-level `CLAUDE.md` are mandatory and must be applied to every task in this project.

## Project-Specific Requirements

For every modification:

* Preserve strict `school_id` isolation across all database reads, writes, updates, deletes, reports, exports, searches, notifications, files, caches, scheduled jobs, and background operations.
* Preserve `academic_year_id` isolation for all academic data where applicable.
* Verify server-side authorization for every role and object-level operation.
* Never trust client-provided ownership fields such as `school_id`, `academic_year_id`, `user_id`, `student_id`, `parent_id`, `teacher_id`, `section_id`, or similar identifiers without validating them against the authenticated server-side context.
* Prevent cross-school, cross-user, cross-role, and cross-academic-year data access or data mixing.
* Treat any cross-school or cross-user data exposure as a critical security vulnerability.
* Verify that related records belong to the same school before creating or updating relationships.
* Ensure parents can access only their explicitly linked children.
* Ensure teachers can access only their permitted and assigned students, sections, subjects, exams, homework, attendance, results, and communications.
* Ensure school managers and school administrators remain restricted to their assigned school.
* Ensure super-admin cross-school actions are explicit, authorized, narrowly scoped, and intentional.
* Include `school_id`, `user_id`, role, `academic_year_id`, and any other necessary ownership dimensions in cache keys.
* Never allow request-level memoization, shared state, global variables, or caches to leak context between users or schools.
* Verify that reports, dashboards, counts, badge totals, searches, exports, files, notifications, and background jobs apply the same isolation rules as detailed database queries.
* Preserve compatibility with the existing web application, mobile API, Flutter application, PostgreSQL database, Supabase Storage integration, and production deployment behavior where relevant.
* Do not modify production data, run destructive commands, apply unsafe migrations, or perform broad data repairs without explicit approval.
* Prefer the smallest safe change that fully solves the requested issue.
* Avoid unrelated refactoring and unnecessary dependency changes.
* After every task, report the files changed, security and isolation checks performed, tests actually run, results, and anything that remains unverified.
