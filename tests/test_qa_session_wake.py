"""QA: wc-session-wake.sh is the half where a mistake loses the thread.

wc-session-standby.sh SIGTERMs a session after writing
~/.claude/standby/<name>.json. That file is then the only pointer back: the
process is gone, and the session id lives nowhere else a person would look.
This script reads that record and launches `claude --resume` inside a detached
`screen` window so the session survives independently.

It also cleans up the record after launching (or reusing an existing screen
window), so the same name cannot be stood by twice without a fresh standby.
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

    def test_it_launches_a_screen_session_for_the_recorded_session(self):
        self._record()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        # stdout is empty (screen does the work, not this script).
        self.assertEqual(result.stdout, "")
        # stderr confirms launch.
        self.assertIn("launched in screen", result.stderr)
        self.assertIn("screen -r", result.stderr)
        # Record is deleted after launch.
        self.assertFalse(
            (self.standby / "cwebtest.json").is_file(),
            "the standby record was not cleaned up after launching",
        )

    def test_the_record_is_deleted_after_launch(self):
        """The record is transient — once launched, it cannot be used again."""
        record = self._record()
        self._run()
        self.assertFalse(
            record.is_file(),
            "the standby record was not deleted after launching",
        )

    def test_running_it_twice_fails_second_time_no_record(self):
        """The first call launches and deletes the record. The second call
        finds no record and exits with an error (the session was already launched)."""
        self._record()
        first = self._run()
        self.assertEqual(first.returncode, 0, first.stderr)

        # Second call: no record exists, script refuses to run.
        second = self._run()
        self.assertNotEqual(second.returncode, 0, second.stderr)
        self.assertIn("no standby record", second.stderr)

    def test_a_missing_record_fails_clearly(self):
        result = self._run("nosuchsession")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no standby record", result.stderr)

    def test_a_cwd_with_spaces_is_quoted_in_screen_command(self):
        """Unquoted, `cd /home/x/my project && claude ...` cds to the wrong
        directory and resumes the session against the wrong files."""
        self._record(cwd="/home/kali/my projects/thing")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        # The script echoes the launch command to stderr; paths with spaces
        # are double-quoted so the cd lands in the right directory.
        self.assertIn(r'"/home/kali/my projects/thing"', result.stderr)

    def test_a_record_with_no_cwd_still_resumes(self):
        self._record(cwd="")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("launched in screen", result.stderr)

    def test_a_record_with_no_session_id_is_refused(self):
        """`claude --resume` with an empty id is not a resume command, and
        launching one invites the user to run something that cannot work."""
        self._record(sid="")
        result = self._run()
        self.assertNotEqual(
            result.returncode, 0,
            "launched screen with no session id",
        )
        self.assertIn("no sessionId", result.stderr)

    def _fake_screen(self) -> Path:
        """A `screen` on PATH that records its argv instead of running.

        The suite asserted the message "launched in screen" and nothing else,
        so a launch that never happened passed. Measured 2026-09-14: waking
        cweb2 printed that line, exited 0, deleted the record -- and no screen
        session existed, because `screen -d -m -S name "cd X && claude ..."`
        hands screen the whole string as a *program name* to exec. There is no
        such executable, screen exits, and the script never looked.
        """
        bindir = self.home / "fakebin"
        bindir.mkdir(exist_ok=True)
        script = bindir / "screen"
        script.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$@" > {self.home}/screen-argv\n'
            "exit 0\n"
        )
        script.chmod(0o755)
        return bindir

    def test_the_command_reaches_screen_through_a_shell(self):
        """`cd X && claude ...` is shell syntax, so something must interpret
        it. Passed as screen's program argument it is just a filename that
        does not exist."""
        self._record()
        import os
        bindir = self._fake_screen()
        subprocess.run(
            ["bash", str(SCRIPT), "cwebtest"],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, HOME=str(self.home),
                     PATH=f"{bindir}:{os.environ['PATH']}"),
        )
        argv = (self.home / "screen-argv").read_text().splitlines()
        self.assertIn("-d", argv)
        self.assertIn("-m", argv)
        # The launched program must be a shell, with the command as its -c
        # argument -- not the command itself standing in for a program.
        self.assertTrue(
            any(a.endswith(("sh", "bash")) for a in argv),
            f"no shell in screen argv: {argv}",
        )
        self.assertIn("-c", argv)
        self.assertTrue(
            any("claude --resume" in a for a in argv),
            f"resume command not passed to the shell: {argv}",
        )

    def test_a_failed_launch_is_not_reported_as_success(self):
        """screen failing must not print 'launched' and must not delete the
        record -- that combination is how a session is lost: the pointer is
        gone and the operator has been told it worked."""
        record = self._record()
        import os
        bindir = self.home / "failbin"
        bindir.mkdir(exist_ok=True)
        (bindir / "screen").write_text("#!/bin/sh\nexit 1\n")
        (bindir / "screen").chmod(0o755)
        result = subprocess.run(
            ["bash", str(SCRIPT), "cwebtest"],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, HOME=str(self.home),
                     PATH=f"{bindir}:{os.environ['PATH']}"),
        )
        self.assertNotEqual(result.returncode, 0, "a failed launch exited 0")
        self.assertTrue(record.is_file(),
                        "the record was deleted after a failed launch")

    def test_the_launched_command_actually_executes(self):
        """Runs what screen was given, instead of inspecting its argv.

        test_the_command_reaches_screen_through_a_shell checks the *shape* of
        the invocation, and that is not enough: the first fix passed it while
        launching nothing, because it built `bash -c "exec cd /x && claude"`.
        `exec` in front of a compound command asks the shell to exec `cd`, a
        builtin -- bash answers "exec: cd: not found" and dies before reaching
        claude. Measured 2026-09-14 waking cweb4: the session vanished exactly
        as before the fix, for a new reason the fix introduced.

        So this fake `screen` *executes* its trailing arguments, and a fake
        `claude` on PATH records that it ran. If the command cannot run, no
        marker appears -- which is the property the argv test cannot see.
        """
        import os
        self._record(cwd=str(self.home))
        bindir = self.home / "runbin"
        bindir.mkdir(exist_ok=True)
        marker = self.home / "claude-ran"
        (bindir / "claude").write_text(
            f'#!/bin/sh\nprintf "%s" "$*" > {marker}\n')
        (bindir / "claude").chmod(0o755)
        # screen -d -m -S <name> <prog> <args...> : run prog with its args.
        (bindir / "screen").write_text(
            '#!/bin/sh\n'
            'while [ $# -gt 0 ]; do\n'
            '  case "$1" in -d|-m) shift ;; -S) shift 2 ;; *) break ;; esac\n'
            'done\n'
            'exec "$@"\n'
        )
        (bindir / "screen").chmod(0o755)

        result = subprocess.run(
            ["bash", str(SCRIPT), "cwebtest"],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, HOME=str(self.home),
                     PATH=f"{bindir}:{os.environ['PATH']}"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            marker.is_file(),
            "claude never ran -- screen was handed a command it could not "
            f"execute. stderr: {result.stderr}",
        )
        self.assertIn(SESSION_ID, marker.read_text())


if __name__ == "__main__":
    unittest.main()
