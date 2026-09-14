"""QA: a shadow record's pid belongs to the writer, not to the session.

`db.write_claude_session_file` seeds `~/.claude/sessions/<session_id>.json`
with a name so the CLI can pick it up, and records `pid: os.getpid()` -- the
WebConsole server's pid. `prompts.py` already documents this in
`_self_and_ancestors`: "db.write_claude_session_file records os.getpid() --
the WebConsole server -- for sessions created through the web."

Nothing else knew. Two readers treat that pid as if it belonged to the session:

* `_read_claude_sessions_sync` sets `live` from `_pid_is_running(pid)`, so
  while the server is up every shadow record reports a live CLI session that
  does not exist.
* `_session_is_live` returns True for any record with a running pid, and
  `delete_claude_session_file` refuses to remove a live session -- so a shadow
  record written by the running server cannot be deleted through
  `DELETE /api/sessions/{id}`. The cleanup path is blocked by the same defect
  it exists to clean.

Measured 2026-09-14: 34 shadow records in the registry, all with pids from
long-dead server instances (266057, 260770, 1142969, ...). They read as dead
only because the server has restarted since; any written by the current server
would read as live.

The fix is in the readers rather than the writer: it needs no migration, and it
corrects the 34 records already on disk. The pid stays in the file because
`prompts.py` and the `same_process` carve-out in `_read_claude_sessions_sync`
both reason about it deliberately.
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


def _other_live_pid(case) -> int:
    """A live pid that is not this process.

    _read_claude_sessions_sync skips a record whose pid equals the current
    process unless its entrypoint is "webconsole" -- the same_process carve-out
    -- so os.getpid() cannot stand in for a real CLI session here. Orphaned so
    init reaps it rather than leaving a zombie, which answers kill -0 and would
    read as alive after it died.
    """
    out = subprocess.run(["bash", "-c", "sleep 30 >/dev/null 2>&1 & echo $!"],
                         capture_output=True, text=True, timeout=10)
    pid = int(out.stdout.strip())
    case.addCleanup(lambda: (os.kill(pid, 9) if os.path.exists(f"/proc/{pid}") else None))
    return pid


class ShadowRecordLivenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self._patch = patch.object(db_sessions, "_resolve_sessions_dir",
                                   lambda: self.dir)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _write(self, *, session_id: str, entrypoint: str, pid: int,
               name: str = "seeded") -> Path:
        path = self.dir / f"{session_id}.json"
        path.write_text(json.dumps({
            "sessionId": session_id,
            "pid": pid,
            "name": name,
            "kind": "interactive",
            "entrypoint": entrypoint,
            "cwd": "/tmp",
            "status": "idle",
        }))
        return path

    def test_a_shadow_record_is_not_live_just_because_its_writer_is(self):
        """The defect. os.getpid() here stands in for the running server."""
        self._write(session_id="sid-shadow", entrypoint="webconsole",
                    pid=os.getpid())
        rows = db_sessions._read_claude_sessions_sync()
        shadow = [r for r in rows if r["sessionId"] == "sid-shadow"]
        self.assertTrue(shadow, "the shadow record must still be listed")
        self.assertIs(shadow[0]["live"], False,
                      "a webconsole shadow record reported a live session "
                      "because its pid is the server's")

    def test_a_real_cli_record_with_a_live_pid_is_still_live(self):
        """The property being protected. Only webconsole-written records carry
        someone else's pid."""
        self._write(session_id="sid-cli", entrypoint="cli", pid=_other_live_pid(self))
        rows = db_sessions._read_claude_sessions_sync()
        cli = [r for r in rows if r["sessionId"] == "sid-cli"]
        self.assertTrue(cli)
        self.assertIs(cli[0]["live"], True)

    def test_session_is_live_ignores_a_shadow_records_pid(self):
        self._write(session_id="sid-shadow2", entrypoint="webconsole",
                    pid=os.getpid())
        self.assertFalse(db_sessions._session_is_live("sid-shadow2"))

    def test_session_is_live_still_sees_a_real_process(self):
        self._write(session_id="sid-cli2", entrypoint="cli", pid=_other_live_pid(self))
        self.assertTrue(db_sessions._session_is_live("sid-cli2"))

    def test_a_shadow_record_can_be_deleted_while_the_server_runs(self):
        """The consequence that blocked cleanup: delete refuses a live
        session, and the record claimed the server's own pid, so the 34
        records on disk could never be removed through the endpoint."""
        path = self._write(session_id="sid-shadow3", entrypoint="webconsole",
                           pid=os.getpid())
        self.assertTrue(db_sessions.delete_claude_session_file("sid-shadow3"))
        self.assertFalse(path.exists())

    def test_a_live_cli_session_is_still_protected_from_deletion(self):
        """The guard that must survive: a running CLI session must not be
        clearable out of the sidebar by accident.

        Refusal is a ValueError, not a False return -- routes/misc.py catches
        it and logs session_delete_refused. The first version of this test
        asserted a False return and failed against correct behaviour.
        """
        self._write(session_id="sid-cli3", entrypoint="cli", pid=_other_live_pid(self))
        with self.assertRaises(ValueError):
            db_sessions.delete_claude_session_file("sid-cli3")


if __name__ == "__main__":
    unittest.main()
