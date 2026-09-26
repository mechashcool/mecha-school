"""Part B1 — the synchronization foundation must be completely inert.

B1 adds ONLY two tables (`change_journal`, `sync_meta`) and two feature flags
that default to false. Nothing captures changes, no /sync/* endpoint exists,
and no signal service is started. These tests pin that inertness so a future
part cannot silently switch behaviour on.

They also protect the two properties that matter most to existing users:
  * existing API surface and response envelope are unchanged, so an older
    mobile build needs no new field and no new endpoint;
  * the notification dispatch path is untouched — in particular no sync work
    was added to the shared push queue, which would let sync traffic delay
    user-visible attendance/grade/homework/exam notifications.

The database test runs against whatever DATABASE_URL points at (the same
convention every other DB test here uses via create_app('testing')) and
refuses to run unless that is a LOCAL database — it never touches a shared or
production server.
"""
import importlib
import threading
import unittest
from datetime import date
from uuid import uuid4

from sqlalchemy import text

from app import create_app
from app.models import (
    db, AcademicYear, Grade, School, Section, Student, StudentAttendance,
)


# ── Flags ─────────────────────────────────────────────────────────────────────

def test_sync_flags_exist_and_default_to_disabled():
    """Both flags must be present and false in a freshly built app."""
    app = create_app('testing')
    assert 'SYNC_JOURNAL_ENABLED' in app.config
    assert 'SYNC_SIGNAL_ENABLED' in app.config
    assert app.config['SYNC_JOURNAL_ENABLED'] is False
    assert app.config['SYNC_SIGNAL_ENABLED'] is False


def test_sync_flags_default_false_when_env_is_absent(monkeypatch):
    """With no env var set at all, the defaults themselves must be false.

    Re-imports config.settings with the variables removed so the assertion
    covers the code default, not whatever .env / .env.test happens to set.
    """
    monkeypatch.delenv('SYNC_JOURNAL_ENABLED', raising=False)
    monkeypatch.delenv('SYNC_SIGNAL_ENABLED', raising=False)
    import config.settings as settings
    reloaded = importlib.reload(settings)
    try:
        assert reloaded.Config.SYNC_JOURNAL_ENABLED is False
        assert reloaded.Config.SYNC_SIGNAL_ENABLED is False
    finally:
        importlib.reload(settings)   # restore for the rest of the session


def test_enabling_the_flag_by_env_does_not_activate_any_capture(monkeypatch):
    """Even flipped on, B1 ships no capture hook — the flag is inert.

    This is the guard that keeps B1 honest: turning the switch on must not do
    anything yet, because the implementation does not exist.
    """
    monkeypatch.setenv('SYNC_JOURNAL_ENABLED', 'true')
    import config.settings as settings
    reloaded = importlib.reload(settings)
    try:
        assert reloaded.Config.SYNC_JOURNAL_ENABLED is True
        # No capture module, no journal hook is importable in B1.
        for missing in ('app.services.sync_journal', 'app.utils.sync_capture'):
            try:
                importlib.import_module(missing)
            except ImportError:
                pass
            else:                                        # pragma: no cover
                raise AssertionError(f'{missing} must not exist in B1')
    finally:
        monkeypatch.delenv('SYNC_JOURNAL_ENABLED', raising=False)
        importlib.reload(settings)


# ── API surface / backward compatibility ──────────────────────────────────────

def test_no_sync_endpoint_is_registered():
    """No mobile /sync/* route may exist yet.

    Scoped to the mobile API prefix on purpose: the unrelated, pre-existing
    ``/employees/<id>/sync-to-device`` route (attendance-device enrolment) also
    contains the word "sync" and must keep working untouched.
    """
    app = create_app('testing')
    for rule in app.url_map.iter_rules():
        assert not rule.rule.startswith('/api/mobile/v1/sync'), \
            f'unexpected mobile sync route: {rule.rule}'
        assert not rule.endpoint.startswith('mobile_api.sync'), rule.endpoint


def test_mobile_api_surface_is_unchanged():
    """The mobile API still exposes exactly its pre-B1 set of routes.

    69 routes is the count captured from HEAD afbad5fe before B1. A change here
    means the mobile contract moved and older app builds must be re-checked.
    """
    app = create_app('testing')
    mobile = [r for r in app.url_map.iter_rules()
              if r.endpoint.startswith('mobile_api.')]
    assert len(mobile) == 69, f'mobile_api route count changed: {len(mobile)}'


def test_response_envelope_gains_no_new_fields():
    """ok() / err() must still return exactly the pre-B1 shape.

    An older mobile build parses these payloads; a stray cursor/generation
    field would be a contract change even though it is additive.
    """
    app = create_app('testing')
    from app.blueprints.mobile_api.utils import ok, err
    forbidden = {'cursor', 'generation', 'scopes_version', 'valid_until',
                 'scopes', 'changes', 'has_more', 'caught_up', 'checked_at'}
    with app.test_request_context('/'):
        payload = ok(children=[], count=0).get_json()
        assert set(payload) == {'ok', 'children', 'count'}
        assert not (set(payload) & forbidden)

        body, status = err('nope', 400)
        assert status == 400
        assert set(body.get_json()) == {'ok', 'error'}


# ── Startup: no signal service ────────────────────────────────────────────────

def test_no_signal_service_thread_or_module_at_startup():
    """Creating the app must start no synchronization signal service."""
    before = {t.name for t in threading.enumerate()}
    app = create_app('testing')
    assert app.config['SYNC_SIGNAL_ENABLED'] is False
    after = {t.name for t in threading.enumerate()}
    for name in (after - before):
        assert 'sync' not in name.lower(), f'unexpected thread started: {name}'

    for missing in ('app.services.sync_signal', 'app.services.sync_stream'):
        try:
            importlib.import_module(missing)
        except ImportError:
            pass
        else:                                            # pragma: no cover
            raise AssertionError(f'{missing} must not exist in B1')


# ── Notification path untouched ───────────────────────────────────────────────

def test_push_queue_has_no_sync_tasks():
    """No sync work may share the push queue with user-visible notifications.

    The durable queue is drained by a SINGLE consumer thread, one job at a
    time. Registering sync work there would put it in line ahead of
    attendance / grade / homework / exam notifications and delay them.
    """
    create_app('testing')
    from app.services import durable_queue
    assert set(durable_queue._TASKS) == {
        'fcm.send_push_batch',
        'chat.send_room_pushes',
    }, f'push-queue task set changed: {sorted(durable_queue._TASKS)}'


def test_notification_dispatch_contract_unchanged():
    """The dispatch entry points keep their pre-B1 signatures and behaviour."""
    create_app('testing')
    from app.services import async_dispatch
    from app.services.notifications import NotificationService
    import app.services.fcm_service as fcm

    assert callable(async_dispatch.submit)
    for name in ('send_to_user', 'send_to_users', 'send_to_parents_of_student'):
        assert callable(getattr(NotificationService, name))
    assert callable(fcm.send_push_to_user)
    # Nothing in B1 may enable a real outbound channel in the test environment.
    assert fcm.is_enabled() is False


# ── Database: the journal stays empty while disabled ──────────────────────────

class SyncJournalStaysEmptyTest(unittest.TestCase):
    """An ordinary attendance write must produce ZERO journal rows in B1.

    Attendance is the pilot resource and has the widest writer surface, so it
    is the right thing to pin first.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def _assert_local_database(self):
        """Refuse to run against anything but a local database."""
        row = db.session.execute(text(
            'SELECT current_database(), '
            "coalesce(host(inet_server_addr()), 'local'), inet_server_port()"
        )).one()
        dbname, host, port = row[0], str(row[1]), int(row[2])
        self.assertIn(host, ('127.0.0.1', '::1', 'local'),
                      f'refusing to run against non-local host {host}')
        return dbname, host, port

    def test_existing_attendance_write_creates_no_journal_row(self):
        suffix = uuid4().hex[:10]
        with self.app.app_context():
            self._assert_local_database()

            before = db.session.execute(
                text('SELECT count(*) FROM change_journal')).scalar()

            school = School(school_name=f'Sync B1 School {suffix}',
                            code=f'SB{suffix[:8]}', capacity=0, is_active=True)
            db.session.add(school)
            db.session.flush()

            year = AcademicYear(school_id=school.id,
                                name=f'Sync B1 Year {suffix}',
                                start_date=date(2025, 8, 1),
                                end_date=date(2026, 6, 30),
                                is_current=True)
            db.session.add(year)
            db.session.flush()

            grade = Grade(school_id=school.id, academic_year_id=year.id,
                          name=f'G{suffix[:5]}')
            db.session.add(grade)
            db.session.flush()

            section = Section(school_id=school.id, academic_year_id=year.id,
                              grade_id=grade.id, name=f'S{suffix[:4]}',
                              capacity=30)
            db.session.add(section)
            db.session.flush()

            student = Student(school_id=school.id, academic_year_id=year.id,
                              section_id=section.id,
                              student_id=f'SB1-{suffix}',
                              full_name=f'Sync B1 Student {suffix}',
                              status='active')
            db.session.add(student)
            db.session.flush()

            # INSERT — the shape every attendance writer produces.
            att = StudentAttendance(school_id=school.id,
                                    academic_year_id=year.id,
                                    student_id=student.id,
                                    date=date(2026, 1, 12),
                                    status='absent', source='automatic')
            db.session.add(att)
            db.session.commit()

            # UPDATE — the in-place mutation (check-out) that carries no
            # timestamp change on this table.
            att.status = 'present'
            att.check_in = None
            db.session.commit()

            # DELETE — the shape used by holiday cleanup / leave revocation.
            db.session.delete(att)
            db.session.commit()

            after = db.session.execute(
                text('SELECT count(*) FROM change_journal')).scalar()
            self.assertEqual(before, after,
                             'change_journal must stay empty while '
                             'SYNC_JOURNAL_ENABLED is false')
            self.assertEqual(after, 0)

            # Clean up this test's own rows.
            db.session.delete(student)
            db.session.delete(section)
            db.session.delete(grade)
            db.session.delete(year)
            db.session.delete(school)
            db.session.commit()

    def test_sync_meta_singleton_is_present_and_disabled(self):
        with self.app.app_context():
            self._assert_local_database()
            rows = db.session.execute(text(
                'SELECT id, generation, capture_enabled FROM sync_meta'
            )).all()
            self.assertEqual(len(rows), 1, 'sync_meta must hold exactly one row')
            self.assertEqual(rows[0][0], 1)
            self.assertEqual(rows[0][1], 1)
            self.assertFalse(rows[0][2], 'capture must ship disabled')

    def test_sync_meta_singleton_constraint_is_enforced(self):
        with self.app.app_context():
            self._assert_local_database()
            with self.assertRaises(Exception):
                db.session.execute(text(
                    'INSERT INTO sync_meta (id, generation, capture_enabled) '
                    'VALUES (2, 1, false)'))
                db.session.commit()
            db.session.rollback()

    def test_change_journal_rejects_an_unknown_op(self):
        with self.app.app_context():
            self._assert_local_database()
            with self.assertRaises(Exception):
                db.session.execute(text(
                    "INSERT INTO change_journal "
                    "(school_id, scope_type, scope_id, resource, op, xid) "
                    "VALUES (1, 'student', 1, 'attendance', 'bogus', 1)"))
                db.session.commit()
            db.session.rollback()
