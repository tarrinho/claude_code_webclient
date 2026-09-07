"""The two SQLite writers must wait the same length of time for the lock.

Registry #47 was a `database is locked` storm caused by three connections
writing one WAL file, which permits a single writer. Two of the three were
removed or lengthened; the third -- auth.py's short-lived synchronous handle,
which exists because the session store is read from synchronous middleware --
kept the 5s budget while db.py's shared connection moved to 15s.

That asymmetry is not a style problem. Under contention the connection with the
shorter budget is always the one that gives up first, so every lock fight the
two of them had was decided in advance against the session write. A lost
session write logs somebody out for a reason they cannot see and that nothing
in the interface explains.

These tests pin the number in two directions, because either alone can pass
against a broken version:

* the constants agree -- catches drift when someone tunes one file
* the value reaches the connection -- catches a constant that is declared,
  documented, and then never applied, which is what a source-only assertion
  would happily accept
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auth
import config
import db


class BusyTimeoutParityTests(unittest.TestCase):
    def test_the_two_writers_agree(self):
        self.assertEqual(
            auth._BUSY_TIMEOUT_MS,
            db._BUSY_TIMEOUT_MS,
            "auth.py and db.py write the same WAL file; the shorter budget "
            "loses every lock fight between them (registry #47)",
        )

    def test_auth_is_not_the_shorter_of_the_two(self):
        """Stated separately from parity, and deliberately weaker.

        If a future change gives one of them a longer budget on purpose, parity
        is the assertion that should be revisited. This one states the property
        that actually matters and must hold either way: the session write is
        never the first to give up.
        """
        self.assertGreaterEqual(auth._BUSY_TIMEOUT_MS, db._BUSY_TIMEOUT_MS)


class BusyTimeoutIsAppliedTests(unittest.TestCase):
    """The constant has to reach the connection, not merely exist.

    ``sqlite3.connect(timeout=)`` and ``PRAGMA busy_timeout`` are the same
    setting reached two ways -- ``connect(timeout=15.0)`` on its own reports
    ``PRAGMA busy_timeout = 15000``. That is measured, not assumed; the first
    version of these tests asserted "the PRAGMA is set" and passed with the
    PRAGMA statement deleted, because ``timeout=`` had already set it.

    So the assertion below is about the connection's *effective* budget, which
    is the property that matters and holds however it was set. The redundancy
    is left in the source rather than removed, because two spellings that agree
    are cheap and the one that runs last wins -- which is the drift this class
    exists to catch.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sessions.db"
        sqlite3.connect(self.path).close()
        p = patch.object(config, "SESSION_DB_PATH", str(self.path))
        p.start()
        self.addCleanup(p.stop)

    def test_the_connection_waits_the_declared_time(self):
        conn = auth._session_conn()
        self.assertIsNotNone(conn, "the handle should open against a real file")
        self.addCleanup(conn.close)
        got = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(got, auth._BUSY_TIMEOUT_MS)

    def test_the_two_spellings_do_not_drift(self):
        """``connect(timeout=)`` is seconds; the PRAGMA is milliseconds.

        One setting, two units, and the PRAGMA runs after connect -- so if
        someone tunes only one of them the PRAGMA silently wins and the
        ``timeout=`` argument becomes a lie that reads as authoritative. A
        connection asked for 15 milliseconds or 15000 seconds fails in neither
        case visibly. The conversion is asserted rather than assumed.
        """
        with patch.object(auth.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            conn = auth._session_conn()
        self.addCleanup(conn.close)
        asked = connect.call_args.kwargs["timeout"]
        effective = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(asked, auth._BUSY_TIMEOUT_MS / 1000)
        self.assertEqual(asked * 1000, effective,
                         "connect(timeout=) and the PRAGMA disagree; the "
                         "PRAGMA runs second, so it is the one in force")


if __name__ == "__main__":
    unittest.main()
