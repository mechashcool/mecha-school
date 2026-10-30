"""
Migration v3h5w7p9n1t2 — homework.parents_notified_at + ix_homework_notify_pending.

OPT-IN (HOMEWORK_MIGRATION_TEST=1): it downgrades and re-upgrades the schema of
the test database it runs against, so it never runs by accident. It refuses
any database that is not a loopback *_test database.

  * upgrade: existing rows read non-NULL (fast default, no table rewrite),
    an insert that omits the column (old application code) gets non-NULL,
    the partial index is valid with the expected definition;
  * downgrade: column and index gone, homework rows untouched;
  * re-upgrade after an interrupted run (column already present, INVALID
    index left by a failed CREATE INDEX CONCURRENTLY): recovers cleanly.

Ends at the new head.
"""
import os
import unittest
from datetime import date
from uuid import uuid4

from flask_migrate import downgrade, upgrade
from sqlalchemy import text

from app import create_app
from app.models import db, AcademicYear, School

NEW, PREV = 'v3h5w7p9n1t2', 't7h8w9i0m1g2'
INDEX = 'ix_homework_notify_pending'
MIGRATIONS = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'migrations')


@unittest.skipUnless(os.environ.get('HOMEWORK_MIGRATION_TEST') == '1',
                     'opt-in: alters the schema of the test database')
class HomeworkNotifyMigrationTest(unittest.TestCase):

    def setUp(self):
        self.app = create_app('testing')
        self.ctx = self.app.app_context()
        self.ctx.push()
        url = db.engine.url
        self.assertIn(url.host, ('127.0.0.1', 'localhost', '::1'))
        self.assertTrue((url.database or '').endswith('_test'), url.database)

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        self.ctx.pop()

    # ── helpers ──────────────────────────────────────────────────────────────

    def sql(self, statement, **params):
        with db.engine.begin() as conn:
            return conn.execute(text(statement), params)

    def autocommit(self, statement):
        with db.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
            conn.execute(text(statement))

    def version(self):
        return self.sql('SELECT version_num FROM alembic_version').scalar()

    def has_column(self):
        return self.sql(
            "SELECT count(*) FROM information_schema.columns WHERE "
            "table_name='homework' AND column_name='parents_notified_at'").scalar() == 1

    def index_state(self):
        row = self.sql(
            'SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) FROM pg_index i '
            'JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :n',
            n=INDEX).first()
        return (None, None) if row is None else (row[0], row[1])

    def relfilenode(self):
        return self.sql("SELECT relfilenode FROM pg_class WHERE relname='homework'").scalar()

    def insert_legacy_rows(self, school_id, year_id, n=2):
        """Rows exactly as OLD code writes them: no parents_notified_at."""
        return [self.sql(
            'INSERT INTO homework (school_id, academic_year_id, title, publish_date, '
            "due_date, is_active, created_at, updated_at) VALUES (:s, :y, :t, "
            "'2026-10-01', '2026-10-05', true, now(), now()) RETURNING id",
            s=school_id, y=year_id, t=f'legacy {i}').scalar() for i in range(n)]

    # ── the test ─────────────────────────────────────────────────────────────

    def test_upgrade_downgrade_and_interrupted_index_recovery(self):
        sfx = uuid4().hex[:8]
        school = School(school_name=f'MIG {sfx}', code=f'MG{sfx}', capacity=0, is_active=True)
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'Y {sfx}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 6, 30))
        db.session.add(year)
        db.session.commit()
        sid, yid = school.id, year.id
        try:
            if self.version() == NEW:
                downgrade(directory=MIGRATIONS, revision=PREV)
            self.assertEqual(self.version(), PREV)
            self.assertFalse(self.has_column())

            # ── 1. upgrade over existing rows ───────────────────────────────
            legacy = self.insert_legacy_rows(sid, yid)
            node_before = self.relfilenode()
            upgrade(directory=MIGRATIONS, revision=NEW)
            self.assertEqual(self.version(), NEW)
            self.assertEqual(self.relfilenode(), node_before, 'table was rewritten')
            self.assertTrue(self.sql(
                "SELECT atthasmissing FROM pg_attribute WHERE attrelid='homework'::regclass "
                "AND attname='parents_notified_at'").scalar(), 'default not stored as missing value')
            self.assertEqual(self.sql(
                'SELECT count(*) FROM homework WHERE parents_notified_at IS NULL').scalar(), 0)
            valid, definition = self.index_state()
            self.assertIs(valid, True)
            self.assertIn('(publish_date, id)', definition)
            self.assertIn('WHERE (parents_notified_at IS NULL)', definition)

            # ── 2. old code inserting without the column → handled, not pending
            old_insert = self.insert_legacy_rows(sid, yid, n=1)[0]
            self.assertIsNotNone(self.sql(
                'SELECT parents_notified_at FROM homework WHERE id=:i', i=old_insert).scalar())

            # ── 3a. downgrade refused while a homework is still pending ──────
            pending = self.sql(
                'INSERT INTO homework (school_id, academic_year_id, title, publish_date, '
                "due_date, is_active, parents_notified_at) VALUES (:s, :y, 'pending', "
                "'2026-12-01', '2026-12-05', true, NULL) RETURNING id", s=sid, y=yid).scalar()
            with self.assertRaises(BaseException):     # flask_migrate → SystemExit
                downgrade(directory=MIGRATIONS, revision=PREV)
            self.assertEqual(self.version(), NEW)
            self.assertTrue(self.has_column())
            self.sql('DELETE FROM homework WHERE id=:i', i=pending)

            # ── 3b. downgrade: column + index gone, rows intact ──────────────
            ids = sorted(legacy + [old_insert])
            downgrade(directory=MIGRATIONS, revision=PREV)
            self.assertEqual(self.version(), PREV)
            self.assertFalse(self.has_column())
            self.assertEqual(self.index_state(), (None, None))
            self.assertEqual(sorted(r[0] for r in self.sql(
                'SELECT id FROM homework WHERE school_id=:s', s=sid)), ids)

            # ── 4. interrupted run: step 1 committed, INVALID index left ────
            self.sql("ALTER TABLE homework ADD COLUMN parents_notified_at TIMESTAMP "
                     "WITHOUT TIME ZONE DEFAULT timezone('utc', now())")
            with self.assertRaises(Exception):     # duplicate school_id → build fails
                self.autocommit(f'CREATE UNIQUE INDEX CONCURRENTLY {INDEX} ON homework (school_id)')
            self.assertIs(self.index_state()[0], False, 'expected an INVALID leftover index')
            upgrade(directory=MIGRATIONS, revision=NEW)
            self.assertEqual(self.version(), NEW)
            valid, definition = self.index_state()
            self.assertIs(valid, True)
            self.assertNotIn('UNIQUE', definition)
            self.assertIn('WHERE (parents_notified_at IS NULL)', definition)
            self.assertEqual(self.sql(
                'SELECT count(*) FROM homework WHERE school_id=:s AND parents_notified_at '
                'IS NULL', s=sid).scalar(), 0)
        finally:
            db.session.rollback()
            self.sql('DELETE FROM homework WHERE school_id=:s', s=sid)
            self.sql('DELETE FROM academic_years WHERE school_id=:s', s=sid)
            self.sql('DELETE FROM schools WHERE id=:s', s=sid)
            if self.version() != NEW:
                upgrade(directory=MIGRATIONS, revision=NEW)


if __name__ == '__main__':
    unittest.main()
