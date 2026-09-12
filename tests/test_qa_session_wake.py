"""QA: wc-session-wake.sh is the half where a mistake loses the thread.

wc-session-standby.sh SIGTERMs a session after writing
~/.claude/standby/<name>.json. That file is then the only pointer back: the
process is gone, and the session id lives nowhere else a person would look.
So this script printing the wrong command, or losing the record, costs the
conversation it was meant to preserve. It had no tests.

The defect these pin: the script `rm -f`s the record immediately after
*printing* the resume command. It prints rather than execs on purpose -- its
own header says a resumed `claude` needs an interactive terminal someone is
attached to, "the user's call, not this script's" -- and then deletes the
record as though the call had already been made. Close the terminal without
running the line, or lose it in scrollback, and the pointer is gone.

Keeping the record is the safer failure. A stale record prints a resume
command that does not resume -- visible, recoverable, and overwritten the next
time that name is stood by. A deleted record cannot be recovered at all.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "wc-session-wake.sh"

SESSION_ID = "11111111-2222-3333-4444-555555555555"


class SessionWakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.standby = self.home / ".claude" / "standby"
        self.standby.mkdir(parents=True)

    def _record(self, name="cwebtest", *, cwd="/home/kali/projects", sid=SESSION_ID):
        (self.standby / f"{name}.json").write_text(json.dumps({
            "sessionId": sid,
            "cwd": cwd,
            "name": name,
            "standbyAt": "2026-09-12T22:00:00Z",
        }))
        return self.standby / f"{name}.json"

    def _run(self, name="cwebtest"):
        import os
        return subprocess.run(
            ["bash", str(SCRIPT), name],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, HOME=str(self.home)),
        )

    def test_it_prints_a_resume_command_for_the_recorded_session(self):
        self._record()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"claude --resume {SESSION_ID}", result.stdout)
        self.assertIn("cd /home/kali/projects", result.stdout)

    def test_the_record_survives_being_printed(self):
        """The defect. Printing is not resuming, and this script cannot tell
        whether the command it printed was ever run."""
        record = self._record()
        self._run()
        self.assertTrue(
            record.is_file(),
            "the standby record was deleted after merely printing the command; "
            "if the user never ran it, the session has no pointer left",
        )

    def test_running_it_twice_gives_the_same_answer(self):
        """Follows from the record surviving, and is the property a person
        actually relies on -- scrollback is lost, terminals get closed."""
        self._record()
        first = self._run()
        second = self._run()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)

    def test_a_missing_record_fails_clearly(self):
        result = self._run("nosuchsession")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no standby record", result.stderr)

    def test_a_cwd_with_spaces_is_quoted(self):
        """Unquoted, `cd /home/x/my project && claude ...` cds to the wrong
        directory and resumes the session against the wrong files."""
        self._record(cwd="/home/kali/my projects/thing")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        # printf %q escapes the space rather than wrapping the path in quotes,
        # so the shape to assert is `my\ projects`, not a leading quote. The
        # first version of this test looked for a quote character and failed
        # against a correctly-quoted path.
        self.assertNotIn("cd /home/kali/my projects/thing &&", result.stdout)
        self.assertIn(r"my\ projects", result.stdout)

    def test_a_record_with_no_cwd_still_resumes(self):
        self._record(cwd="")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"claude --resume {SESSION_ID}", result.stdout)
        self.assertNotIn("cd ", result.stdout)

    def test_a_record_with_no_session_id_is_refused(self):
        """`claude --resume` with an empty id is not a resume command, and
        printing one invites the user to run something that cannot work."""
        self._record(sid="")
        result = self._run()
        self.assertNotEqual(
            result.returncode, 0,
            "printed a resume command with no session id: " + result.stdout,
        )


if __name__ == "__main__":
    unittest.main()
