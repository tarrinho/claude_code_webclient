"""QA: the CLI session registry gets collected, and collects only what it may.

`~/.claude/sessions` had no reaper. `_write_agent_name` writes a shadow record
per session id a turn runs under, `chat_set_session` repoints the chat the
moment the CLI hands back a new id -- orphaning the previous record -- and
`delete_claude_session_file` had exactly one caller, a user-initiated route. So
the directory was write-only in practice. Measured 2026-09-15: 42 of 50 records
named a webconsole process that no longer existed, up from the 34 recorded in
`_session_is_live`'s own comment a day earlier.

It is not only untidiness: `_session_is_live` and the standby endpoint walk this
directory, and standby was answering HTTP 500 ("no running session found
matching ...") against a registry mostly composed of the dead.

The tests that matter here are the refusals. A reaper that deletes too much
destroys a live session's record or a real CLI session's, and both are worse
than the leak it fixes.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
from routes import db_sessions


def _write_record(directory: Path, session_id: str, *, entrypoint: str, pid: int) -> Path:
    path = directory / f"{session_id}.json"
    path.write_text(json.dumps({
        "sessionId": session_id,
        "pid": pid,
        "name": f"record {session_id}",
        "cwd": "/tmp",
        "entrypoint": entrypoint,
    }), encoding="utf-8")
    return path


# A pid that cannot be running. Chosen rather than invented: os.getpid() is
# certainly alive, and a hardcoded large number can collide on a busy box.
def _dead_pid() -> int:
    pid = os.getpid()
    for candidate in range(pid + 1, pid + 5000):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue
    raise unittest.SkipTest("no free pid found to stand in for a dead process")


class SessionReaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        # conftest's autouse fixture already points this at a temp dir; narrowing
        # it to this test's own directory keeps the cases independent.
        patcher = patch.object(db, "_CLAUDE_SESSIONS_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_dead_webconsole_record_is_removed(self):
        _write_record(self.dir, "stale", entrypoint="webconsole", pid=_dead_pid())
        self.assertEqual(db_sessions.reap_stale_session_files(), 1)
        self.assertFalse((self.dir / "stale.json").exists())

    def test_a_live_webconsole_record_survives(self):
        """The current server's own records carry its live pid. Eating those
        would make the reaper destroy the work of the process running it."""
        _write_record(self.dir, "mine", entrypoint="webconsole", pid=os.getpid())
        self.assertEqual(db_sessions.reap_stale_session_files(), 0)
        self.assertTrue((self.dir / "mine.json").exists())

    def test_a_real_cli_session_is_never_touched(self):
        """Even with a dead pid. A CLI record is not WebConsole's to delete, and
        `delete_claude_session_file` refuses it -- this asserts the reaper does
        not route around that refusal."""
        _write_record(self.dir, "cli-sess", entrypoint="cli", pid=_dead_pid())
        self.assertEqual(db_sessions.reap_stale_session_files(), 0)
        self.assertTrue((self.dir / "cli-sess.json").exists())

    def test_it_collects_only_the_collectable_from_a_mixed_directory(self):
        """The real shape of the directory: all three kinds at once."""
        dead = _dead_pid()
        _write_record(self.dir, "dead-a", entrypoint="webconsole", pid=dead)
        _write_record(self.dir, "dead-b", entrypoint="webconsole", pid=dead)
        _write_record(self.dir, "live", entrypoint="webconsole", pid=os.getpid())
        _write_record(self.dir, "cli", entrypoint="cli", pid=dead)

        self.assertEqual(db_sessions.reap_stale_session_files(), 2)

        survivors = sorted(p.stem for p in self.dir.glob("*.json"))
        self.assertEqual(survivors, ["cli", "live"])

    def test_a_malformed_record_does_not_stop_the_sweep(self):
        """One unreadable file must not strand every collectable record behind
        it -- the failure mode that turns a leak into an unbounded one."""
        (self.dir / "broken.json").write_text("{not json", encoding="utf-8")
        _write_record(self.dir, "stale", entrypoint="webconsole", pid=_dead_pid())
        self.assertEqual(db_sessions.reap_stale_session_files(), 1)
        self.assertTrue((self.dir / "broken.json").exists())

    def test_a_missing_directory_is_not_an_error(self):
        """Startup must not fail because nothing has written a session yet."""
        with patch.object(db, "_CLAUDE_SESSIONS_DIR", self.dir / "nope"):
            self.assertEqual(db_sessions.reap_stale_session_files(), 0)

    def test_an_empty_directory_reaps_nothing(self):
        self.assertEqual(db_sessions.reap_stale_session_files(), 0)
