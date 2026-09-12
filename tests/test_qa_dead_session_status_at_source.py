"""QA: the "a dead process has no status" rule lives where `live` is computed.

d489c75 fixed one consumer, `classification._cli_maps`. Then a sweep of every
caller of `read_claude_sessions` found two more reading the same stale value:

  * routes/chats.py:258 builds the set of "busy" session ids for the chat list
    from `item["status"]` with no liveness test, so a session that died while
    busy keeps its linked chat showing as working.
  * routes/db_supervisor_map.py's `_cli_session_nodes` hands the raw entry to
    `classification._classify_cli_session`, which reads `cli["status"]`
    directly -- so it never saw the map that d489c75 corrected.

Three consumers of one wrong value is the shape CLAUDE.md keeps warning about,
and the same file says why the fix does not belong in each of them: "this rule
used to exist three times". So it is enforced in `_read_claude_sessions_sync`,
on the line after `live` is computed from the same pid -- one place, and the
two callers above are fixed without being touched.

What is deliberately NOT blanked is `status_updated_at`. It is a timestamp, and
it stays true after the process exits; chat-list.js:548 uses it to grey a stale
row, which is exactly what an ended session should look like.

The rule cannot cover remote sessions -- `_read_remote_sessions_sync` cannot
run `_pid_is_running` against another host, so those entries carry no `live`
key at all. `_cli_maps` keeps its own guard for that reason, and these tests
pin the local guarantee it relies on.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from routes import db_sessions


class DeadSessionStatusAtSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _write(self, *, pid: int, status: str, name: str = "s1") -> None:
        (self.dir / f"{pid}.json").write_text(json.dumps({
            "pid": pid,
            "name": name,
            "kind": "interactive",
            "entrypoint": "cli",
            "status": status,
            "statusUpdatedAt": 1789000000000,
            "sessionId": f"sid-{name}",
            "cwd": "/tmp",
        }))

    def _read(self) -> list[dict]:
        with patch.object(db_sessions, "_resolve_sessions_dir",
                          lambda: self.dir):
            return db_sessions._read_claude_sessions_sync()

    def _live_pid(self) -> int:
        """Not os.getpid(): the reader skips its own pid deliberately (the
        `same_process` guard), so a record naming it is never returned at all.
        Orphaned so init reaps it on cleanup rather than leaving a zombie.
        """
        out = subprocess.run(
            ["bash", "-c", "sleep 30 >/dev/null 2>&1 & echo $!"],
            capture_output=True, text=True, timeout=10,
        )
        pid = int(out.stdout.strip())
        self.addCleanup(lambda: self._kill(pid))
        return pid

    @staticmethod
    def _kill(pid: int) -> None:
        try:
            os.kill(pid, 9)
        except OSError:
            pass

    @staticmethod
    def _dead_pid() -> int:
        """A pid that is reaped, so it is genuinely gone rather than a zombie
        (a zombie answers `kill -0`, which cost a test in 598e7c4)."""
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def test_a_dead_session_reports_no_status(self):
        self._write(pid=self._dead_pid(), status="waiting")
        entry = self._read()[0]
        self.assertIs(entry["live"], False)
        self.assertEqual(entry["status"], "",
                         "the last status a dead process wrote was kept")

    def test_a_dead_busy_session_reports_no_status_either(self):
        """routes/chats.py keys on exactly this value to mark a chat working."""
        self._write(pid=self._dead_pid(), status="busy")
        self.assertEqual(self._read()[0]["status"], "")

    def test_a_live_session_keeps_its_status(self):
        self._write(pid=self._live_pid(), status="waiting")
        entry = self._read()[0]
        self.assertIs(entry["live"], True)
        self.assertEqual(entry["status"], "waiting")

    def test_the_status_timestamp_survives_death(self):
        """It is still true, and the sidebar greys a stale row with it."""
        self._write(pid=self._dead_pid(), status="waiting")
        self.assertTrue(self._read()[0]["status_updated_at"],
                        "a timestamp does not stop being true")


if __name__ == "__main__":
    unittest.main()
