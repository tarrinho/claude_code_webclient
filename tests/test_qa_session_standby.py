"""QA: which sessions bin/wc-session-standby.sh is willing to suspend.

Standby sends SIGTERM. Getting the gate wrong does not fail loudly -- it ends
someone's session, and the damage is only visible later, when the thread they
were in the middle of is gone. So the gate is asserted here rather than left to
the one line of shell that implements it.

The original gate refused a single value, `status = busy`. That left `waiting`
passing, which is the worst case of the three: a session in that state is
blocked asking its user a question, so standby kills it at the exact moment a
person is needed. It also allowed a session that went idle four seconds ago,
which is almost certainly still being worked in.

The rule these assert is therefore an allowlist plus an age:

  * `status` must be exactly `idle` -- busy, waiting, empty, and any value that
    does not exist yet are all refused. This is the same reasoning
    classification.py:294 already applies to this field, and for the same
    reason: an unknown value must fall on the safe side.
  * it must have been idle for at least WC_STANDBY_MIN_IDLE_S (default 3600),
    measured from the record's own `statusUpdatedAt`.
  * a record with no `statusUpdatedAt` cannot show it has been idle for an
    hour, so it is refused rather than assumed old enough.

The script reads ~/.claude/sessions, so each case runs it against a throwaway
HOME. The accept case uses a real `sleep` process as the target, which means it
exercises the SIGTERM path for real instead of stopping at the gate.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "wc-session-standby.sh"

HOUR_MS = 3600 * 1000


class SessionStandbyGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.sessions = self.home / ".claude" / "sessions"
        self.sessions.mkdir(parents=True)
        self.children: list[int] = []
        self.addCleanup(self._reap)

    def _reap(self):
        for pid in self.children:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def _live_pid(self) -> int:
        """A real process the script may legitimately signal.

        Deliberately orphaned rather than held as a Popen child. The script
        waits for the pid to disappear with `kill -0`, and `kill -0` succeeds
        on a zombie -- so a child of this test process stays "alive" by that
        test until pytest reaps it, which it has no reason to do mid-test. The
        first version of this fixture did exactly that and the accept case
        failed with "did not exit within 5s after SIGTERM" against a `sleep`
        that had already died.

        Orphaning it makes init the parent, which reaps immediately, so the pid
        really does vanish. That also matches the real case: these sessions are
        children of screen, not of the script that suspends them.
        """
        out = subprocess.run(
            ["bash", "-c", "sleep 30 >/dev/null 2>&1 & echo $!"],
            capture_output=True, text=True, timeout=10,
        )
        pid = int(out.stdout.strip())
        self.children.append(pid)
        return pid

    def _record(self, *, status: str, idle_ms: int | None, pid: int | None = None,
                name: str = "cwebtest") -> int:
        """Write a session file the way the CLI writes one."""
        pid = self._live_pid() if pid is None else pid
        data = {
            "pid": pid,
            "name": name,
            "kind": "interactive",
            "entrypoint": "cli",
            "status": status,
            "sessionId": "11111111-2222-3333-4444-555555555555",
            "cwd": str(self.home),
        }
        if idle_ms is not None:
            data["statusUpdatedAt"] = int(time.time() * 1000) - idle_ms
        (self.sessions / f"{pid}.json").write_text(json.dumps(data))
        return pid

    def _run(self, target: str, min_idle: str | None = None):
        env = dict(os.environ, HOME=str(self.home))
        env.pop("WC_STANDBY_MIN_IDLE_S", None)
        if min_idle is not None:
            env["WC_STANDBY_MIN_IDLE_S"] = min_idle
        return subprocess.run(
            ["bash", str(SCRIPT), target],
            capture_output=True, text=True, env=env, timeout=60,
        )

    # ---- the state allowlist -------------------------------------------

    def test_a_session_waiting_on_its_user_is_refused(self):
        """The case that motivated this: `waiting` means blocked on a human, so
        suspending it destroys the question at the moment it is being asked."""
        self._record(status="waiting", idle_ms=4 * HOUR_MS)
        result = self._run("cwebtest")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("waiting", (result.stderr + result.stdout).lower())

    def test_a_session_mid_turn_is_still_refused(self):
        """The original gate's only rule, kept."""
        self._record(status="busy", idle_ms=4 * HOUR_MS)
        result = self._run("cwebtest")
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_a_status_nobody_has_seen_before_is_refused(self):
        """An allowlist, so a new CLI state falls on the safe side by default
        instead of being suspended because no rule happened to name it."""
        self._record(status="hibernating", idle_ms=4 * HOUR_MS)
        result = self._run("cwebtest")
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_an_empty_status_is_refused(self):
        """Older builds do not write the field at all; absent is unknown, and
        unknown is not idle."""
        self._record(status="", idle_ms=4 * HOUR_MS)
        result = self._run("cwebtest")
        self.assertNotEqual(result.returncode, 0, result.stdout)

    # ---- the age rule ---------------------------------------------------

    def test_a_session_idle_for_only_minutes_is_refused(self):
        """Idle is not the same as finished with. Five minutes is a pause for
        thought, not an abandoned session."""
        self._record(status="idle", idle_ms=5 * 60 * 1000)
        result = self._run("cwebtest")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("idle", (result.stderr + result.stdout).lower())

    def test_a_record_with_no_timestamp_cannot_prove_an_hour(self):
        """Refused rather than assumed old enough -- the failure of assuming is
        silent and costs a session."""
        self._record(status="idle", idle_ms=None)
        result = self._run("cwebtest")
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_the_threshold_is_configurable(self):
        """So a deliberate override is a visible argument rather than a --force
        flag that also bypasses the state check."""
        self._record(status="idle", idle_ms=5 * 60 * 1000)
        result = self._run("cwebtest", min_idle="60")
        self.assertEqual(result.returncode, 0, result.stderr)

    # ---- the accept path ------------------------------------------------

    def test_a_long_idle_session_is_suspended_for_real(self):
        """Runs to the end: the record is written and the process is gone."""
        pid = self._record(status="idle", idle_ms=2 * HOUR_MS)
        result = self._run("cwebtest")
        self.assertEqual(result.returncode, 0, result.stderr)

        record = self.home / ".claude" / "standby" / "cwebtest.json"
        self.assertTrue(record.is_file(), "no standby record written")
        saved = json.loads(record.read_text())
        self.assertEqual(saved["sessionId"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(saved["cwd"], str(self.home))

        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.2)
        else:
            self.fail(f"pid {pid} still running after standby reported success")

    def test_a_stale_record_for_a_dead_pid_is_refused(self):
        """Nothing to suspend, and the pid may since belong to something else."""
        proc = subprocess.Popen(["true"])
        proc.wait()
        self._record(status="idle", idle_ms=2 * HOUR_MS, pid=proc.pid)
        result = self._run("cwebtest")
        self.assertNotEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
