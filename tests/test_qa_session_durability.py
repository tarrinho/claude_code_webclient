"""QA coverage for sessions that survive a restart.

Sessions used to live only in memory, so every restart logged everyone out --
on a phone-first console that means retyping a password each time the server is
bounced, which during a day of development was a dozen times.

They are now mirrored to SQLite and restored at boot, keyed by a hash of the
session id rather than the id itself: the id exists only in the user's cookie,
so neither the table nor a backup taken through /api/admin/export can be
replayed as a login.

Covers: round-trip across a simulated restart, that the raw id never reaches
disk, that logout and expiry are durable too, the idle-clock write budget, and
that a database failure never costs a working session.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import auth
import config
import db


class SessionDurabilityBase(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name) / "projects"
        self.projects.mkdir()
        self.db_path = os.path.join(self.tmp.name, "db")
        self.db_patch = patch.object(config, "DB_PATH", self.db_path)
        self.root_patch = patch.object(config, "PROJECTS_ROOT", str(self.projects))
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await db.close()
        auth._sessions.clear()
        auth._persisted_last.clear()

    async def asyncTearDown(self):
        auth._sessions.clear()
        auth._persisted_last.clear()
        self.root_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def restart(self) -> int:
        """Drop everything held in memory, as a process restart would."""
        auth._sessions.clear()
        auth._persisted_last.clear()
        return auth.load_sessions()


class SessionSurvivesRestartTests(SessionDurabilityBase):

    async def test_a_session_still_resolves_after_a_restart(self):
        sid, _ = auth.session_new("pedro", "admin")
        self.assertEqual(self.restart(), 1)
        session = auth.session_get(sid)
        self.assertIsNotNone(session, "the login must survive the restart")
        self.assertEqual(session["user"], "pedro")
        self.assertEqual(session["role"], "admin")

    async def test_the_csrf_token_survives_too(self):
        """Without it the first mutating request after a restart would 403."""
        sid, csrf = auth.session_new("pedro", "admin")
        self.restart()
        self.assertEqual(auth.session_get(sid)["csrf"], csrf)
        self.assertTrue(auth._csrf_valid(csrf, csrf, sid))

    async def test_the_role_is_not_silently_upgraded(self):
        sid, _ = auth.session_new("bob", "user")
        self.restart()
        self.assertEqual(auth.session_get(sid)["role"], "user")

    async def test_an_unknown_id_still_resolves_to_nothing(self):
        auth.session_new("pedro", "admin")
        self.restart()
        self.assertIsNone(auth.session_get("not-a-real-session-id"))


class SessionIdIsNeverStoredTests(SessionDurabilityBase):
    """The database is downloadable through /api/admin/export."""

    async def test_the_raw_id_never_reaches_disk(self):
        sid, _ = auth.session_new("pedro", "admin")
        blob = Path(self.db_path).read_bytes().decode("utf-8", errors="replace")
        self.assertNotIn(sid, blob, "a stored session id would be a replayable credential")

    async def test_the_stored_key_is_a_hash_of_the_id(self):
        sid, _ = auth.session_new("pedro", "admin")
        con = sqlite3.connect(self.db_path)
        stored = con.execute("SELECT sid_key FROM sessions").fetchone()[0]
        con.close()
        self.assertEqual(stored, auth._sid_key(sid))
        self.assertNotEqual(stored, sid)

    async def test_memory_is_keyed_by_the_hash_as_well(self):
        sid, _ = auth.session_new("pedro", "admin")
        self.assertIn(auth._sid_key(sid), auth._sessions)
        self.assertNotIn(sid, auth._sessions)


class LogoutAndExpiryAreDurableTests(SessionDurabilityBase):

    async def test_logout_removes_it_from_disk(self):
        sid, _ = auth.session_new("pedro", "admin")
        auth.session_drop(sid)
        self.assertEqual(self.restart(), 0, "a logged-out session must not come back")
        self.assertIsNone(auth.session_get(sid))

    async def test_an_expired_session_is_not_resurrected(self):
        sid, _ = auth.session_new("pedro", "admin")
        key = auth._sid_key(sid)
        auth._sessions[key]["expiry"] = time.time() - 1
        auth._persist(key, auth._sessions[key])
        self.assertEqual(self.restart(), 0)

    async def test_an_idle_session_is_not_resurrected(self):
        """Restarting must not reset the idle clock and revive a stale login."""
        sid, _ = auth.session_new("pedro", "admin")
        key = auth._sid_key(sid)
        auth._sessions[key]["last"] = time.time() - config.SESSION_IDLE_S - 10
        auth._persist(key, auth._sessions[key])
        self.assertEqual(self.restart(), 0)

    async def test_expiring_on_read_also_clears_the_row(self):
        sid, _ = auth.session_new("pedro", "admin")
        key = auth._sid_key(sid)
        auth._sessions[key]["expiry"] = time.time() - 1
        auth._persist(key, auth._sessions[key])
        self.assertIsNone(auth.session_get(sid))
        con = sqlite3.connect(self.db_path)
        remaining = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        con.close()
        self.assertEqual(remaining, 0)

    async def test_the_session_cap_evicts_from_disk_too(self):
        """Eviction must reach the table, not just memory.

        The cap admits one over: _sweep() runs at the top of session_new, so it
        trims to SESSION_MAX and the new session is then added, leaving
        SESSION_MAX + 1. That is pre-existing and immaterial at the real cap of
        50 -- the property worth holding is that disk never drifts from memory.
        """
        with patch.object(config, "SESSION_MAX", 3):
            for i in range(6):
                auth.session_new(f"u{i}", "user")
        con = sqlite3.connect(self.db_path)
        rows = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        con.close()
        self.assertEqual(len(auth._sessions), rows,
                         "an evicted session must not be left behind on disk")
        self.assertLessEqual(rows, 4, "the cap must still bound the store")
        self.assertEqual(self.restart(), rows, "restoring must agree with the table")


class WriteBudgetTests(SessionDurabilityBase):
    """Persisting the idle clock on every request would be a write per request."""

    async def test_reading_repeatedly_does_not_write_every_time(self):
        sid, _ = auth.session_new("pedro", "admin")
        with patch.object(auth, "_persist") as persist:
            for _ in range(20):
                auth.session_get(sid)
            self.assertEqual(persist.call_count, 0,
                             "an active session must not write on every request")

    async def test_a_drifted_idle_clock_is_written_back(self):
        sid, _ = auth.session_new("pedro", "admin")
        key = auth._sid_key(sid)
        auth._persisted_last[key] = time.time() - auth._LAST_PERSIST_S - 5
        with patch.object(auth, "_persist") as persist:
            auth.session_get(sid)
            self.assertEqual(persist.call_count, 1)


class DatabaseFailureTests(SessionDurabilityBase):
    """Durability is a convenience; losing it must never cost a login."""

    async def test_a_session_still_works_when_it_cannot_be_persisted(self):
        with patch.object(auth, "_session_conn", return_value=None):
            sid, _ = auth.session_new("pedro", "admin")
        self.assertEqual(auth.session_get(sid)["user"], "pedro")

    async def test_a_write_error_does_not_raise(self):
        broken = sqlite3.connect(self.db_path)
        broken.close()  # any use now raises ProgrammingError
        with patch.object(auth, "_session_conn", return_value=broken):
            sid, _ = auth.session_new("pedro", "admin")
        self.assertEqual(auth.session_get(sid)["user"], "pedro")

    async def test_restoring_without_a_database_returns_zero(self):
        with patch.object(auth, "_session_conn", return_value=None):
            self.assertEqual(auth.load_sessions(), 0)


if __name__ == "__main__":
    unittest.main()
