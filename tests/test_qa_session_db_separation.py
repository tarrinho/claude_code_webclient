"""QA coverage for the separate-sessions-database split (registry #47).

Sessions used to live in the same SQLite file as the app's data, which meant
auth.py's synchronous sqlite3.writer and db.py's asynchronous aiosqlite writer
raced on one WAL file. The fix: sessions live in SESSION_DB_PATH (a separate
file), db.py writes DB_PATH (the combined app DB), and a one-time migration
moves any leftover rows at boot.

Covers:
* _migrate_sessions_to_own_db — copy, clear source, idempotency, no-op cases
* _ensure_session_db_file — empty file created, table created, idempotent
* Two-writer isolation — auth.py never touches DB_PATH, db.py never touches
  SESSION_DB_PATH, they can write simultaneously without locking each other
* Schema correctness — the sessions table has exactly the columns it should
* Backup/restore — restoring the main DB does not erase sessions
* Database failure recovery — sessions DB missing, sessions DB corrupted
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auth
import config
import db


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    """_migrate_sessions_to_own_db moves rows from DB_PATH to SESSION_DB_PATH."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name) / "projects"
        self.projects.mkdir()
        self.main_db = os.path.join(self.tmp.name, "main.db")
        self.sess_db = os.path.join(self.tmp.name, "sessions.db")
        self.db_patch = patch.object(config, "DB_PATH", self.main_db)
        self.sess_patch = patch.object(config, "SESSION_DB_PATH", self.sess_db)
        self.root_patch = patch.object(config, "PROJECTS_ROOT", str(self.projects))
        self.db_patch.start()
        self.sess_patch.start()
        self.root_patch.start()
        auth._sessions.clear()
        auth._persisted_last.clear()

    async def asyncTearDown(self):
        auth._sessions.clear()
        auth._persisted_last.clear()
        self.root_patch.stop()
        self.db_patch.stop()
        self.sess_patch.stop()
        self.tmp.cleanup()

    def _write_main_sessions(self, rows):
        """Insert rows into DB_PATH.sessions (the old combined file)."""
        conn = sqlite3.connect(self.main_db)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                sid_key TEXT PRIMARY KEY,
                user TEXT NOT NULL,
                role TEXT NOT NULL,
                expiry REAL NOT NULL,
                last REAL NOT NULL,
                csrf TEXT NOT NULL
            )"""
        )
        if rows:
            conn.executemany(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", rows
            )
        conn.commit()
        conn.close()

    def test_migration_copies_all_rows(self):
        self._write_main_sessions([
            ("key1", "alice", "admin", 9999999999.0, 9999999999.0, "csrf1"),
            ("key2", "bob", "user", 9999999999.0, 9999999999.0, "csrf2"),
        ])
        count = auth._migrate_sessions_to_own_db()
        self.assertEqual(count, 2)
        # Verify sessions DB has both rows
        conn = sqlite3.connect(self.sess_db)
        stored = conn.execute(
            "SELECT sid_key, user, role FROM sessions"
        ).fetchall()
        conn.close()
        self.assertEqual(len(stored), 2)
        self.assertEqual(stored[0][1], "alice")
        self.assertEqual(stored[1][1], "bob")

    def test_migration_clears_source_table(self):
        self._write_main_sessions([
            ("key1", "alice", "admin", 9999999999.0, 9999999999.0, "csrf1"),
        ])
        auth._migrate_sessions_to_own_db()
        conn = sqlite3.connect(self.main_db)
        remaining = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        conn.close()
        self.assertEqual(remaining, 0)

    def test_migration_is_idempotent(self):
        self._write_main_sessions([
            ("key1", "alice", "admin", 9999999999.0, 9999999999.0, "csrf1"),
        ])
        first = auth._migrate_sessions_to_own_db()
        self.assertEqual(first, 1)
        # Run again — source is now empty, should return 0
        second = auth._migrate_sessions_to_own_db()
        self.assertEqual(second, 0, "second migration must be a no-op")

    def test_migration_no_op_when_source_db_missing(self):
        self.assertEqual(
            auth._migrate_sessions_to_own_db(),
            0,
            "missing source DB is a no-op",
        )

    def test_migration_no_op_when_source_table_empty(self):
        # Main DB exists, sessions table exists but is empty
        conn = sqlite3.connect(self.main_db)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                sid_key TEXT PRIMARY KEY, user TEXT NOT NULL, role TEXT NOT NULL,
                expiry REAL NOT NULL, last REAL NOT NULL, csrf TEXT NOT NULL
            )"""
        )
        conn.commit()
        conn.close()
        self.assertEqual(auth._migrate_sessions_to_own_db(), 0)

    def test_migration_copies_even_when_sessions_db_has_table(self):
        """If SESSION_DB_PATH already has the table, migration still
        copies from source (INSERT OR REPLACE deduplicates on sid_key).
        This matters when a partial migration occurred on a crash."""
        self._write_main_sessions([
            ("key1", "alice", "admin", 9999999999.0, 9999999999.0, "csrf1"),
        ])
        # Pre-create the sessions DB (simulates a partial prior migration)
        auth._ensure_session_db_file()
        count = auth._migrate_sessions_to_own_db()
        self.assertEqual(count, 1)
        # Verify the row is present
        conn = sqlite3.connect(self.sess_db)
        stored = conn.execute("SELECT user FROM sessions WHERE sid_key='key1'").fetchone()[0]
        conn.close()
        self.assertEqual(stored, "alice")


class EnsureSessionsDbFileTests(unittest.TestCase):
    """_ensure_session_db_file creates the table if it does not exist."""

    def test_creates_table_in_new_file(self):
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "new.db")
            with patch.object(config, "SESSION_DB_PATH", path):
                auth._ensure_session_db_file()
                self.assertTrue(os.path.exists(path))
                conn = sqlite3.connect(path)
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                conn.close()
                names = {t[0] for t in tables}
                self.assertIn("sessions", names)
                conn = sqlite3.connect(path)
                cols = conn.execute(
                    "PRAGMA table_info(sessions)"
                ).fetchall()
                col_names = {c[1] for c in cols}
                conn.close()
                self.assertEqual(
                    col_names,
                    {"sid_key", "user", "role", "expiry", "last", "csrf"},
                )
        finally:
            shutil.rmtree(tmpdir)

    def test_ensures_parent_directory_exists(self):
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "deep", "nested", "sessions.db")
            with patch.object(config, "SESSION_DB_PATH", path):
                auth._ensure_session_db_file()
                self.assertTrue(os.path.exists(path))
        finally:
            shutil.rmtree(tmpdir)

    def test_idempotent_on_existing_table(self):
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "existing.db")
            with patch.object(config, "SESSION_DB_PATH", path):
                auth._ensure_session_db_file()
                auth._ensure_session_db_file()  # second call must not raise
        finally:
            shutil.rmtree(tmpdir)


class WriterIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Auth writes to SESSION_DB_PATH, db.py writes to DB_PATH. Never cross."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name) / "projects"
        self.projects.mkdir()
        self.main_db = os.path.join(self.tmp.name, "main.db")
        self.sess_db = os.path.join(self.tmp.name, "sessions.db")
        self.db_patch = patch.object(config, "DB_PATH", self.main_db)
        self.sess_patch = patch.object(config, "SESSION_DB_PATH", self.sess_db)
        self.root_patch = patch.object(config, "PROJECTS_ROOT", str(self.projects))
        self.db_patch.start()
        self.sess_patch.start()
        self.root_patch.start()
        auth._sessions.clear()
        auth._persisted_last.clear()

    async def asyncTearDown(self):
        auth._sessions.clear()
        auth._persisted_last.clear()
        self.root_patch.stop()
        self.db_patch.stop()
        self.sess_patch.stop()
        self.tmp.cleanup()

    def test_auth_never_writes_to_main_db(self):
        """Creating a session must not create or modify the sessions table
        in DB_PATH, because db.py never creates that table in the new architecture."""
        # Write something to main DB to make sure it exists
        conn = sqlite3.connect(self.main_db)
        conn.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO test VALUES (1)")
        conn.commit()
        conn.close()

        auth.session_new("alice", "admin")

        # Main DB must not have a sessions table
        conn = sqlite3.connect(self.main_db)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        conn.close()
        names = {t[0] for t in tables}
        self.assertNotIn(
            "sessions", names,
            "sessions table must not appear in the main database",
        )

    async def test_db_never_writes_to_sessions_db(self):
        """db.py init must not create a sessions table in SESSION_DB_PATH."""
        auth._ensure_session_db_file()  # create sessions DB with its table
        sess_count_before = self._sess_table_rows()

        await db.init()
        await db.close()

        sess_count_after = self._sess_table_rows()
        self.assertEqual(
            sess_count_after, sess_count_before,
            "db.py init must not touch the sessions database",
        )

    def _sess_table_rows(self):
        conn = sqlite3.connect(self.sess_db)
        rows = conn.execute(
            "SELECT COUNT(*) FROM sessions"
        ).fetchone()[0]
        conn.close()
        return rows

    def test_concurrent_writes_do_not_block_each_other(self):
        """Both writers must complete within their busy_timeout.
        With separate WAL files, there is zero contention."""
        import threading
        import time

        errors = []

        def write_sessions():
            try:
                for _ in range(50):
                    sid, _ = auth.session_new(
                        f"user-{threading.current_thread().name}", "user"
                    )
                    time.sleep(0.001)
            except Exception as exc:
                errors.append(exc)

        def write_main():
            try:
                for i in range(50):
                    conn = sqlite3.connect(self.main_db)
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS data (id INTEGER PRIMARY KEY, val TEXT)"
                    )
                    conn.execute(
                        "INSERT INTO data VALUES (?, ?)", (i, f"val-{i}")
                    )
                    conn.commit()
                    conn.close()
                    time.sleep(0.001)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=write_sessions, name="writer-sess")
        t2 = threading.Thread(target=write_main, name="writer-main")
        start = time.monotonic()
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        elapsed = time.monotonic() - start
        self.assertEqual(
            len(errors), 0,
            f"concurrent writes failed: {errors} "
            f"(elapsed={elapsed:.1f}s)",
        )
        self.assertLess(
            elapsed, 5,
            "concurrent writes must finish well within 5s "
            "since there is no cross-DB locking",
        )


class SchemaCorrectnessTests(unittest.TestCase):
    """The sessions table has exactly the right schema."""

    def test_sessions_table_has_required_columns(self):
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "sessions.db")
            with patch.object(config, "SESSION_DB_PATH", path):
                auth._ensure_session_db_file()
                conn = sqlite3.connect(path)
                cols = conn.execute(
                    "PRAGMA table_info(sessions)"
                ).fetchall()
                # sid_key must be the primary key
                pk = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
                ).fetchone()[0]
                self.assertIn("PRIMARY KEY", pk)
                conn.close()
                expected = {
                    "sid_key": "TEXT",
                    "user": "TEXT",
                    "role": "TEXT",
                    "expiry": "REAL",
                    "last": "REAL",
                    "csrf": "TEXT",
                }
                cols_dict = {c[1]: c[2] for c in cols}
                self.assertEqual(
                    cols_dict,
                    expected,
                    "sessions table must have exactly the required columns",
                )
        finally:
            shutil.rmtree(tmpdir)

    def test_sid_key_is_unique(self):
        """INSERT OR REPLACE must deduplicate on sid_key."""
        tmpdir = tempfile.mkdtemp()
        try:
            path = os.path.join(tmpdir, "sessions.db")
            with patch.object(config, "SESSION_DB_PATH", path):
                auth._ensure_session_db_file()
                conn = sqlite3.connect(path)
                conn.execute(
                    "INSERT INTO sessions VALUES ('k1', 'alice', 'admin', 1, 1, 'c1')"
                )
                conn.commit()
                conn.execute(
                    "INSERT OR REPLACE INTO sessions VALUES ('k1', 'bob', 'user', 2, 2, 'c2')"
                )
                conn.commit()
                count = conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE sid_key='k1'"
                ).fetchone()[0]
                conn.close()
                self.assertEqual(count, 1)
                # Verify the replaced row has the new values
                conn = sqlite3.connect(path)
                row = conn.execute(
                    "SELECT user, role FROM sessions WHERE sid_key='k1'"
                ).fetchone()
                conn.close()
                self.assertEqual(row[0], "bob")
                self.assertEqual(row[1], "user")
        finally:
            shutil.rmtree(tmpdir)


class BackupRestoreTests(unittest.IsolatedAsyncioTestCase):
    """Restoring the main DB must not erase the sessions DB."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name) / "projects"
        self.projects.mkdir()
        self.main_db = os.path.join(self.tmp.name, "main.db")
        self.sess_db = os.path.join(self.tmp.name, "sessions.db")
        self.db_patch = patch.object(config, "DB_PATH", self.main_db)
        self.sess_patch = patch.object(config, "SESSION_DB_PATH", self.sess_db)
        self.root_patch = patch.object(config, "PROJECTS_ROOT", str(self.projects))
        self.db_patch.start()
        self.sess_patch.start()
        self.root_patch.start()
        auth._sessions.clear()
        auth._persisted_last.clear()
        auth._ensure_session_db_file()

    async def asyncTearDown(self):
        auth._sessions.clear()
        auth._persisted_last.clear()
        self.root_patch.stop()
        self.db_patch.stop()
        self.sess_patch.stop()
        self.tmp.cleanup()

    def _sessions_count(self):
        conn = sqlite3.connect(self.sess_db)
        c = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        conn.close()
        return c

    def test_restore_main_does_not_affect_sessions(self):
        # Create the main DB file so shutil.move doesn't fail
        with sqlite3.connect(self.main_db) as conn:
            conn.execute(
                "CREATE TABLE chats (id TEXT PRIMARY KEY)"
            )

        # Create a session
        auth.session_new("alice", "admin")
        sess_count_before = self._sessions_count()
        self.assertEqual(sess_count_before, 1)

        # Replace the main DB with a fresh empty one (simulate restore)
        fresh_db = os.path.join(self.tmp.name, "fresh.db")
        with sqlite3.connect(fresh_db) as conn:
            conn.execute(
                "CREATE TABLE chats (id TEXT PRIMARY KEY)"
            )
            conn.commit()

        import shutil
        shutil.move(self.main_db, self.main_db + ".bak")
        shutil.move(fresh_db, self.main_db)

        sess_count_after = self._sessions_count()
        self.assertEqual(
            sess_count_after, sess_count_before,
            "replacing the main DB must not erase sessions",
        )

    def test_session_persist_writes_to_sessions_db_only(self):
        """Verify the row lives only in SESSION_DB_PATH."""
        auth.session_new("alice", "admin")
        # Check sessions DB has the row
        conn = sqlite3.connect(self.sess_db)
        sess_rows = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        conn.close()
        self.assertEqual(sess_rows, 1)

        # Check main DB has no sessions table
        conn = sqlite3.connect(self.main_db)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        conn.close()
        names = {t[0] for t in tables}
        self.assertNotIn("sessions", names)


class DatabaseFailureRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """When the sessions DB disappears, auth must recover gracefully."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name) / "projects"
        self.projects.mkdir()
        self.main_db = os.path.join(self.tmp.name, "main.db")
        self.sess_db = os.path.join(self.tmp.name, "sessions.db")
        self.db_patch = patch.object(config, "DB_PATH", self.main_db)
        self.sess_patch = patch.object(config, "SESSION_DB_PATH", self.sess_db)
        self.root_patch = patch.object(config, "PROJECTS_ROOT", str(self.projects))
        self.db_patch.start()
        self.sess_patch.start()
        self.root_patch.start()
        auth._sessions.clear()
        auth._persisted_last.clear()

    async def asyncTearDown(self):
        auth._sessions.clear()
        auth._persisted_last.clear()
        self.root_patch.stop()
        self.db_patch.stop()
        self.sess_patch.stop()
        self.tmp.cleanup()

    def test_session_works_after_sessions_db_deleted(self):
        """If the sessions DB file is gone, _ensure_session_db_file recreates it."""
        auth._ensure_session_db_file()
        auth.session_new("alice", "admin")
        # Delete the file
        os.unlink(self.sess_db)
        self.assertFalse(os.path.exists(self.sess_db))
        # Next session creation must recreate the table
        sid, _ = auth.session_new("bob", "user")
        self.assertIsNotNone(sid)

    def test_load_sessions_with_missing_db_file(self):
        """load_sessions must return 0 when the DB file does not exist."""
        self.assertEqual(auth.load_sessions(), 0)

    def test_corrupted_sessions_db_returns_zero_on_restore(self):
        """A corrupted sessions DB must not crash the server."""
        auth._ensure_session_db_file()
        # Write garbage to the file
        with open(self.sess_db, "wb") as f:
            f.write(b"this is not sqlite")
        self.assertEqual(auth.load_sessions(), 0,
                         "corrupted sessions DB must not crash load_sessions")


if __name__ == "__main__":
    unittest.main()
