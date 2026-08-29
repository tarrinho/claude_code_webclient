"""CLI session listing: deduplication, liveness, and shadow-record removal.

Resuming a CLI session writes a second file to ~/.claude/sessions -- the
"shadow" record, entrypoint "webconsole" -- so the CLI can see the link. The
real CLI already keeps its own PID-named file for the same sessionId, so
listing the directory returned both and the sidebar showed every resumed
session twice. Each resume added another, permanently.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db


def _write(sessions_dir: Path, filename: str, **overrides) -> Path:
    payload = {
        "pid": os.getpid(),
        "sessionId": "aaaa1111",
        "cwd": "/tmp",
        "kind": "interactive",
        "entrypoint": "cli",
        "name": "session",
        "startedAt": 1_700_000_000_000,
        "updatedAt": 1_700_000_000_000,
        "model": "claude-sonnet-5",
    }
    payload.update(overrides)
    path = sessions_dir / filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class SessionDedupeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patcher = patch.object(db, "_CLAUDE_SESSIONS_DIR", self.dir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    async def test_cli_and_shadow_collapse_to_one_row(self):
        # A dead pid on the CLI record keeps it out of the same-process skip.
        _write(self.dir, "4242.json", pid=4242, name="cweb2", entrypoint="cli")
        _write(self.dir, "aaaa1111.json", pid=4242, name="CLI: aaaa1111...",
               entrypoint="webconsole")
        rows = await db.read_claude_sessions()
        self.assertEqual(len(rows), 1)

    async def test_cli_record_wins_so_the_real_name_survives(self):
        _write(self.dir, "4242.json", pid=4242, name="cweb2", entrypoint="cli")
        _write(self.dir, "aaaa1111.json", pid=4242, name="CLI: aaaa1111...",
               entrypoint="webconsole")
        rows = await db.read_claude_sessions()
        self.assertEqual(rows[0]["name"], "cweb2")
        self.assertEqual(rows[0]["entrypoint"], "cli")

    async def test_shadow_survives_when_no_cli_record_claims_it(self):
        """An ended session stays visible so it can be read or removed."""
        _write(self.dir, "bbbb2222.json", pid=4242, sessionId="bbbb2222",
               name="CLI: bbbb2222...", entrypoint="webconsole")
        rows = await db.read_claude_sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sessionId"], "bbbb2222")

    async def test_distinct_sessions_are_not_collapsed(self):
        _write(self.dir, "1.json", pid=4242, sessionId="aaaa1111", name="one")
        _write(self.dir, "2.json", pid=4243, sessionId="bbbb2222", name="two")
        rows = await db.read_claude_sessions()
        self.assertEqual({r["sessionId"] for r in rows}, {"aaaa1111", "bbbb2222"})

    async def test_dead_process_is_reported_not_live(self):
        _write(self.dir, "bbbb2222.json", pid=4242, sessionId="bbbb2222",
               entrypoint="webconsole", name="ended")
        rows = await db.read_claude_sessions()
        self.assertFalse(rows[0]["live"])

    async def test_running_process_is_reported_live(self):
        _write(self.dir, "9.json", pid=os.getpid(), sessionId="cccc3333",
               entrypoint="webconsole", name="alive")
        rows = await db.read_claude_sessions()
        self.assertTrue(rows[0]["live"])


class SessionFileDeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patcher = patch.object(db, "_CLAUDE_SESSIONS_DIR", self.dir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_removes_a_dead_shadow_record(self):
        path = _write(self.dir, "bbbb2222.json", pid=4242, sessionId="bbbb2222",
                      entrypoint="webconsole")
        self.assertTrue(db.delete_claude_session_file("bbbb2222"))
        self.assertFalse(path.exists())

    def test_refuses_a_record_webconsole_did_not_write(self):
        """The CLI's own files belong to Claude Code, not to this app."""
        path = _write(self.dir, "cccc3333.json", pid=4242, sessionId="cccc3333",
                      entrypoint="cli")
        with self.assertRaises(ValueError):
            db.delete_claude_session_file("cccc3333")
        self.assertTrue(path.exists())

    def test_refuses_while_the_process_is_running(self):
        path = _write(self.dir, "dddd4444.json", pid=os.getpid(),
                      sessionId="dddd4444", entrypoint="webconsole")
        with self.assertRaises(ValueError):
            db.delete_claude_session_file("dddd4444")
        self.assertTrue(path.exists())

    def test_refuses_when_a_sibling_cli_record_is_live(self):
        """Liveness comes from the CLI's own file, not the shadow's stale pid.

        A shadow record stores the pid of the WebConsole process that wrote it,
        which is dead by the time anyone looks. Judging liveness from that
        alone deleted the record of a session that was still running.
        """
        shadow = _write(self.dir, "eeee5555.json", pid=999_999,
                        sessionId="eeee5555", entrypoint="webconsole")
        _write(self.dir, "1234.json", pid=os.getpid(), sessionId="eeee5555",
               entrypoint="cli", name="cweb2")
        with self.assertRaises(ValueError):
            db.delete_claude_session_file("eeee5555")
        self.assertTrue(shadow.exists())

    def test_rejects_path_traversal(self):
        for bad in ("../escape", "a/b", "a\\b"):
            with self.assertRaises(ValueError):
                db.delete_claude_session_file(bad)

    def test_missing_file_is_not_an_error(self):
        self.assertFalse(db.delete_claude_session_file("nosuchsession"))


if __name__ == "__main__":
    unittest.main()
