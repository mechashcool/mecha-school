# -*- coding: utf-8 -*-
"""Manual attendance must never silently overwrite an approved leave as absent.

The bug: /attendance/manual renders every student with an existing attendance
row as a locked card that posts NO status_<id> field. POST
/attendance/take/<section_id> read the field with a default of 'absent', so an
approved-leave row (status='on_leave', source='leave') was converted to
status='absent', source='manual' and the parents got an absence push, merely
because a staff member saved the section.

Guaranteed here, against the real route and the real leave sync:

  * An existing row whose status_<id> field is omitted is left untouched —
    status, source, notes (incl. the leave marker), recorded_by, check_in.
  * No absence notification, feed row or outbox job for that student.
  * Genuine absences, present/late check-ins and the explicit per-student
    override of a leave row behave exactly as before.
  * take.html "الكل غائب" skips leave rows; normal rows are still marked.

Runs against the isolated local PostgreSQL test database only.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import text

from app import create_app
from app.models import (
    db, AcademicYear, Grade, LeaveRequest, MobileDeviceToken, Notification,
    NotificationOutbox, Role, School, Section, Student, StudentAttendance, User,
    parent_students,
)

OPTS = {'bypass_tenant_scope': True}
TODAY = date.today()
STUDENTS_A = ('leave', 'present', 'late', 'new1', 'new2')


class ManualLeaveGuardTest(unittest.TestCase):
    """School A: one section, five students, one parent each (one phone).
    School B: one section, one student on approved leave — only there so the
    isolation assertion has somewhere to leak to."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.ids = {'students': {}, 'parents': {}, 'users': []}
        with self.app.app_context():
            roles = {name: Role.query.filter_by(name=name).first()
                     for name in ('parent', 'school_admin')}
            for name, role in roles.items():
                self.assertIsNotNone(role, f'seed role {name} before running')

            for tag, keys in (('a', STUDENTS_A), ('b', ('leave',))):
                school = School(
                    school_name=f'LeaveGuard {tag} {self.suffix}',
                    code=f'LG{tag.upper()}{self.suffix[:6]}',
                    capacity=0, is_active=True,
                    att_late_threshold=time(7, 30),
                    att_departure_time=time(13, 0),
                    weekly_off_days=None)
                db.session.add(school)
                db.session.flush()
                year = AcademicYear(school_id=school.id,
                                    name=f'Y {tag} {self.suffix}',
                                    start_date=TODAY - timedelta(days=200),
                                    end_date=TODAY + timedelta(days=200),
                                    is_current=True)
                db.session.add(year)
                db.session.flush()
                grade = Grade(school_id=school.id, academic_year_id=year.id,
                              name=f'G{tag}{self.suffix[:4]}')
                db.session.add(grade)
                db.session.flush()
                section = Section(school_id=school.id, academic_year_id=year.id,
                                  grade_id=grade.id,
                                  name=f'S{tag}{self.suffix[:4]}', capacity=30)
                db.session.add(section)
                db.session.flush()
                self.ids.update({f'school_{tag}': school.id,
                                 f'year_{tag}': year.id,
                                 f'grade_{tag}': grade.id,
                                 f'section_{tag}': section.id})

                for name in keys:
                    key = f'{tag}_{name}'
                    student = Student(
                        student_id=f'LG-{key}-{self.suffix}',
                        full_name=f'Pupil {key} {self.suffix}',
                        date_of_birth=date(2015, 1, 1), gender='male',
                        school_id=school.id, academic_year_id=year.id,
                        section_id=section.id, status='active')
                    parent = User(
                        username=f'lg_p_{key}_{self.suffix}',
                        email=f'lg_p_{key}_{self.suffix}@example.test',
                        full_name=f'Parent {key}', role_id=roles['parent'].id,
                        school_id=school.id, is_active=True)
                    parent.set_password('Password123')
                    db.session.add_all([student, parent])
                    db.session.flush()
                    db.session.execute(parent_students.insert().values(
                        user_id=parent.id, student_id=student.id))
                    db.session.add(MobileDeviceToken(
                        user_id=parent.id, school_id=school.id,
                        fcm_token=f'tkn-lg-{key}-{self.suffix}-' + 'x' * 60,
                        platform='android', device_name='Phone',
                        is_active=True))
                    self.ids['students'][key] = student.id
                    self.ids['parents'][key] = parent.id
                    self.ids['users'].append(parent.id)

                admin = User(username=f'lg_ad_{tag}_{self.suffix}',
                             email=f'lg_ad_{tag}_{self.suffix}@example.test',
                             full_name=f'Admin {tag}',
                             role_id=roles['school_admin'].id,
                             school_id=school.id, is_active=True)
                admin.set_password('Password123')
                db.session.add(admin)
                db.session.flush()
                self.ids[f'admin_{tag}'] = admin.id
                self.ids['users'].append(admin.id)
            db.session.commit()

        self.app.config['MANUAL_ATTENDANCE_OUTBOX_ENABLED'] = False
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.config.pop('MANUAL_ATTENDANCE_OUTBOX_ENABLED', None)
        with self.app.app_context():
            sids = [self.ids['school_a'], self.ids['school_b']]
            for table in ('notification_outbox', 'push_notifications',
                          'notifications', 'student_attendance',
                          'leave_requests', 'mobile_device_tokens',
                          'audit_logs'):
                db.session.execute(
                    text(f'DELETE FROM {table} WHERE school_id = ANY(:s)'),
                    {'s': sids})
            db.session.execute(
                text('DELETE FROM parent_students WHERE student_id = ANY(:s)'),
                {'s': list(self.ids['students'].values())})
            for model, ids in (
                    (Student, list(self.ids['students'].values())),
                    (User, self.ids['users']),
                    (Section, [self.ids['section_a'], self.ids['section_b']]),
                    (Grade, [self.ids['grade_a'], self.ids['grade_b']]),
                    (AcademicYear, [self.ids['year_a'], self.ids['year_b']]),
                    (School, sids)):
                for pk in ids:
                    row = db.session.get(model, pk, execution_options=OPTS)
                    if row is not None:
                        db.session.delete(row)
                db.session.flush()
            db.session.commit()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _sid(self, key):
        return self.ids['students'][key]

    def _login(self, user_id):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True

    def _post(self, statuses=None, *, section='a', user='admin_a',
              at=time(7, 0)):
        """POST exactly what manual.html sends: _from=manual plus a
        status_<id> ONLY for the students listed (unlocked cards)."""
        self._login(self.ids[user])
        form = {'_from': 'manual'}
        for key, status in (statuses or {}).items():
            form[f'status_{self._sid(key)}'] = status
        with patch('app.blueprints.attendance.get_local_now',
                   return_value=datetime.combine(TODAY, at)):
            return self.client.post(
                f'/attendance/take/{self.ids["section_" + section]}'
                f'?date={TODAY.isoformat()}',
                data=form, follow_redirects=False)

    def _approve_leave(self, key):
        """An approved leave, materialised by the REAL sync_student_leave."""
        from app.services.leave_attendance import sync_student_leave
        tag = key.split('_', 1)[0]
        with self.app.app_context():
            leave = LeaveRequest(
                student_id=self._sid(key), school_id=self.ids[f'school_{tag}'],
                academic_year_id=self.ids[f'year_{tag}'], leave_type='sick',
                from_date=TODAY, to_date=TODAY + timedelta(days=1),
                status='approved', source='admin',
                reviewed_by=self.ids[f'admin_{tag}'],
                reviewed_at=datetime.utcnow())
            db.session.add(leave)
            db.session.commit()
            sync_student_leave(leave)
            return leave.id

    def _add_row(self, key, status, *, check_in=None, source='manual',
                 notes=None):
        tag = key.split('_', 1)[0]
        with self.app.app_context():
            db.session.add(StudentAttendance(
                student_id=self._sid(key), school_id=self.ids[f'school_{tag}'],
                academic_year_id=self.ids[f'year_{tag}'], date=TODAY,
                status=status, check_in=check_in, source=source, notes=notes))
            db.session.commit()

    def _row(self, key):
        with self.app.app_context():
            rows = (StudentAttendance.query.execution_options(**OPTS)
                    .filter_by(student_id=self._sid(key), date=TODAY).all())
            self.assertLessEqual(len(rows), 1)
            if not rows:
                return None
            r = rows[0]
            return {'status': r.status, 'source': r.source, 'notes': r.notes,
                    'check_in': r.check_in, 'recorded_by': r.recorded_by,
                    'school_id': r.school_id,
                    'academic_year_id': r.academic_year_id}

    def _feed_rows(self, tag='a'):
        with self.app.app_context():
            return [(n.target_user_id, n.ntype) for n in
                    Notification.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids[f'school_{tag}']).all()]

    def _jobs(self, tag='a'):
        with self.app.app_context():
            return [j.user_id for j in
                    NotificationOutbox.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids[f'school_{tag}']).all()]

    def _unlocked(self, status='present', **overrides):
        """What manual.html posts: a field for every school-A student with
        no row today (unlocked card), none for the locked ones."""
        form = {k: status for k in self.ids['students']
                if k.startswith('a_') and self._row(k) is None}
        form.update(overrides)
        return form

    def _legacy_mocks(self):
        """Inline (flag-off) push helpers. Absent calls are recorded by id at
        call time — the Student is detached once the request ends."""
        self.absent_ids = []

        def _record(student, *_a, **_k):
            self.absent_ids.append(student.id)
        return (patch('app.services.notifications._NotificationService'
                      '.send_to_parents_of_student'),
                patch('app.blueprints.attendance._notify_absent_parents',
                      side_effect=_record))


# ═════════════════════════════════════════════════════════════════════════════
#  1 / 9 — Approved leave survives a manual section save
# ═════════════════════════════════════════════════════════════════════════════

class LeaveRowPreservedTest(ManualLeaveGuardTest):

    def test_leave_row_unchanged_and_nobody_notified_flag_off(self):
        leave_id = self._approve_leave('a_leave')
        before = self._row('a_leave')
        self.assertEqual((before['status'], before['source']), ('on_leave', 'leave'))
        self.assertEqual(before['notes'], f'إجازة مجازة #{leave_id}')

        form = self._unlocked()            # leave card locked → no field
        self.assertNotIn('a_leave', form)
        inline_p, absent_p = self._legacy_mocks()
        with inline_p as inline, absent_p as absent:
            resp = self._post(form)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._row('a_leave'), before)
        self.assertEqual(absent.call_count, 0)
        notified = [c.args[0] for c in inline.call_args_list]
        self.assertEqual(sorted(notified),
                         sorted(self._sid(k) for k in form))
        self.assertNotIn(self._sid('a_leave'), notified)
        self.assertEqual(self._feed_rows('a'), [])

    def test_leave_row_unchanged_and_nothing_staged_flag_on(self):
        self._approve_leave('a_leave')
        before = self._row('a_leave')
        self.app.config['MANUAL_ATTENDANCE_OUTBOX_ENABLED'] = True
        resp = self._post(self._unlocked())
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._row('a_leave'), before)
        self.assertNotIn(self.ids['parents']['a_leave'], self._jobs('a'))
        self.assertTrue(self._jobs('a'))      # the present check-ins were staged
        self.assertEqual(self._feed_rows('a'), [])

    def test_converted_absence_keeps_its_restore_marker(self):
        # Auto-absence first, leave approved later → converted with a marker.
        self._add_row('a_leave', 'absent', source='automatic')
        leave_id = self._approve_leave('a_leave')
        before = self._row('a_leave')
        self.assertEqual(before['status'], 'on_leave')
        self.assertTrue(before['notes'].startswith(
            f'[leave:{leave_id}|was:absent:automatic]'))
        self._post({})
        self.assertEqual(self._row('a_leave'), before)

    def test_manual_on_leave_row_is_also_preserved(self):
        self._add_row('a_leave', 'on_leave', source='manual', notes='note')
        before = self._row('a_leave')
        self._post({})
        self.assertEqual(self._row('a_leave'), before)

    def test_mixed_save_notifies_only_the_genuine_absence(self):
        self._approve_leave('a_leave')
        before = self._row('a_leave')
        inline_p, absent_p = self._legacy_mocks()
        with inline_p, absent_p as absent:
            resp = self._post(self._unlocked(a_new1='absent'))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._row('a_leave'), before)
        self.assertEqual(self.absent_ids, [self._sid('a_new1')])
        self.assertEqual(self._row('a_new1')['status'], 'absent')

    def test_mixed_save_stages_only_the_genuine_students_flag_on(self):
        self._approve_leave('a_leave')
        self.app.config['MANUAL_ATTENDANCE_OUTBOX_ENABLED'] = True
        form = self._unlocked(a_new1='absent')
        self._post(form)
        self.assertNotIn(self.ids['parents']['a_leave'], self._jobs('a'))
        self.assertEqual(sorted(set(self._jobs('a'))),
                         sorted(self.ids['parents'][k] for k in form))
        self.assertEqual([u for u, _t in self._feed_rows('a')],
                         [self.ids['parents']['a_new1']])

    def test_other_school_leave_row_is_untouched(self):
        self._approve_leave('b_leave')
        before = self._row('b_leave')
        self._post({'b_leave': 'absent'}, section='b', user='admin_a')
        self.assertEqual(self._row('b_leave'), before)
        self.assertEqual(self._feed_rows('b'), [])


# ═════════════════════════════════════════════════════════════════════════════
#  2–6 — Normal manual attendance is unchanged
# ═════════════════════════════════════════════════════════════════════════════

class NormalBehaviourTest(ManualLeaveGuardTest):

    def test_locked_present_row_stays_present(self):
        self._add_row('a_present', 'present', check_in=time(7, 5))
        before = self._row('a_present')
        self._post({})
        self.assertEqual(self._row('a_present'), before)

    def test_locked_late_row_stays_late(self):
        self._add_row('a_late', 'late', check_in=time(7, 45))
        before = self._row('a_late')
        self._post({})
        self.assertEqual(self._row('a_late'), before)

    def test_explicit_absent_for_a_new_student_is_created_and_notified(self):
        inline_p, absent_p = self._legacy_mocks()
        with inline_p, absent_p:
            self._post(self._unlocked(a_new1='absent'))
        row = self._row('a_new1')
        self.assertEqual((row['status'], row['check_in']), ('absent', None))
        self.assertEqual(row['recorded_by'], self.ids['admin_a'])
        self.assertEqual(self.absent_ids, [self._sid('a_new1')])

    def test_explicit_present_for_a_new_student_checks_in(self):
        inline_p, absent_p = self._legacy_mocks()
        form = self._unlocked()
        with inline_p as inline, absent_p as absent:
            self._post(form)
        row = self._row('a_new1')
        self.assertEqual((row['status'], row['check_in']), ('present', time(7, 0)))
        self.assertEqual(inline.call_count, len(form))
        self.assertEqual(absent.call_count, 0)

    def test_new_student_without_a_field_keeps_the_legacy_default(self):
        """Unchanged: a student with NO row and no field is still absent."""
        self._post({})
        self.assertEqual(self._row('a_new1')['status'], 'absent')

    def test_explicit_on_leave_choice_keeps_the_leave_row(self):
        self._approve_leave('a_leave')
        before = self._row('a_leave')
        self._post({'a_leave': 'on_leave'})
        self.assertEqual(self._row('a_leave'), before)

    def test_explicit_present_override_of_leave_still_works(self):
        self._approve_leave('a_leave')
        self._post({'a_leave': 'present'})
        row = self._row('a_leave')
        self.assertEqual((row['status'], row['source'], row['check_in']),
                         ('present', 'manual', time(7, 0)))

    def test_explicit_absent_override_of_leave_still_works(self):
        """The existing explicit per-student override is kept as it was."""
        self._approve_leave('a_leave')
        inline_p, absent_p = self._legacy_mocks()
        with inline_p, absent_p:
            self._post(self._unlocked(a_leave='absent'))
        row = self._row('a_leave')
        self.assertEqual((row['status'], row['source']), ('absent', 'manual'))
        self.assertEqual(self.absent_ids, [self._sid('a_leave')])


# ═════════════════════════════════════════════════════════════════════════════
#  7 / 8 — take.html "الكل غائب" (real rendered page, fake DOM in Node)
# ═════════════════════════════════════════════════════════════════════════════

_JS_HARNESS = r"""
'use strict';
const ROWS = __ROWS__;
const rows = ROWS.map(r => ({style: {opacity: r.opacity}, dataset: r.dataset,
                             sid: r.sid, radios: []}));
const radios = [];
rows.forEach((row, i) => ROWS[i].radios.forEach(spec => {
  const radio = {value: spec.value, name: spec.name, disabled: false,
                 _checked: spec.checked, closest: () => row};
  Object.defineProperty(radio, 'checked', {
    get() { return this._checked; },
    set(v) {
      if (v) radios.filter(o => o.name === this.name).forEach(o => { o._checked = false; });
      this._checked = !!v;
    }
  });
  row.radios.push(radio); radios.push(radio);
}));
const document = {
  querySelectorAll(sel) {
    const m = /^input\[type=radio\]\[value=(\w+)\]:not\(:disabled\)$/.exec(sel);
    if (m) return radios.filter(r => r.value === m[1] && !r.disabled);
    throw new Error('unexpected selector ' + sel);
  }
};
function updateCounts() {}
__MARKALL__
function snapshot() {
  const out = {};
  rows.forEach(row => {
    const c = row.radios.find(r => r.checked);
    out[row.sid] = row.radios.length ? (c ? c.value : null) : 'locked';
  });
  return out;
}
const result = {};
markAll('absent');  result.afterAbsent = snapshot();
markAll('present'); result.afterPresent = snapshot();
console.log(JSON.stringify(result));
"""


class TakePageMarkAllTest(ManualLeaveGuardTest):

    def _rendered(self):
        self._login(self.ids['admin_a'])
        with patch('app.blueprints.attendance.get_local_now',
                   return_value=datetime.combine(TODAY, time(7, 0))):
            resp = self.client.get(
                f'/attendance/take/{self.ids["section_a"]}?date={TODAY.isoformat()}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    @staticmethod
    def _rows(html):
        body = html.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
        rows = []
        for attrs, inner in re.findall(r'<tr\b([^>]*)>(.*?)</tr>', body, re.S):
            radios = []
            for tag in re.findall(r'<input[^>]*type="radio"[^>]*>', inner, re.S):
                name = re.search(r'name="status_(\d+)"', tag).group(1)
                radios.append({'name': name,
                               'value': re.search(r'value="(\w+)"', tag).group(1),
                               'checked': bool(re.search(r'\schecked\b', tag))})
            sid = (radios[0]['name'] if radios else
                   re.search(r'<code>([^<]+)</code>', inner).group(1))
            rows.append({'sid': sid,
                         'opacity': '.55' if 'opacity' in attrs else '',
                         'dataset': ({'onLeave': '1'}
                                     if 'data-on-leave="1"' in attrs else {}),
                         'radios': radios})
        return rows

    def test_leave_marker_is_rendered_only_on_the_leave_row(self):
        self._approve_leave('a_leave')
        rows = self._rows(self._rendered())
        flagged = [r['sid'] for r in rows if r['dataset']]
        self.assertEqual(flagged, [str(self._sid('a_leave'))])

    @unittest.skipUnless(shutil.which('node'), 'node not available')
    def test_mark_all_absent_skips_leave_rows(self):
        self._approve_leave('a_leave')
        self._add_row('a_present', 'present', check_in=time(7, 5))
        html = self._rendered()
        rows = self._rows(html)
        mark_all = re.search(r'function markAll\(status\) \{.*?\n\}\n', html, re.S)
        self.assertIsNotNone(mark_all)
        script = (_JS_HARNESS.replace('__ROWS__', json.dumps(rows))
                  .replace('__MARKALL__', mark_all.group(0)))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'take_markall_test.js')
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(script)
            proc = subprocess.run(['node', path], capture_output=True, text=True,
                                  encoding='utf-8', timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        r = json.loads(proc.stdout.strip().splitlines()[-1])

        leave = str(self._sid('a_leave'))
        normal = [str(self._sid(k)) for k in ('a_late', 'a_new1', 'a_new2')]
        locked_code = f'LG-a_present-{self.suffix}'

        # 7: the leave student keeps مجاز; 8: everyone eligible becomes absent.
        self.assertEqual(r['afterAbsent'][leave], 'on_leave')
        for sid in normal:
            self.assertEqual(r['afterAbsent'][sid], 'absent')
        self.assertEqual(r['afterAbsent'][locked_code], 'locked')
        # "الكل حاضر" is unchanged by this fix (actual attendance may override).
        self.assertEqual(r['afterPresent'][leave], 'present')
        for sid in normal:
            self.assertEqual(r['afterPresent'][sid], 'present')


if __name__ == '__main__':
    unittest.main()
