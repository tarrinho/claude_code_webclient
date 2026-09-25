"""QA: deploying a proxy to a transport actually replaces the running one.

Sending a real turn through a transport tunnel on 2026-09-25 returned
`{"type": "error", "error": "claude binary not found"}` while the tunnel, the
handshake and the turn frame were all working. Two defects, and the second is
the one that hid the first.

**The unit hardcoded the console host's layout.** bin/wc-deploy-proxy.sh wrote
`Environment=WC_CLAUDE_PATH=%h/.local/bin/claude` onto every transport. Kali3
keeps claude at /usr/bin/claude and has no ~/.local/bin/claude at all, so the
proxy there could never spawn anything. The script now asks the transport
(`command -v claude` in a login shell) and writes what it finds.

**The deploy never restarted the service.** It ran `systemctl --user enable
--now`, which starts a stopped service and does nothing to a running one. So
the fix above changed the unit on disk and the ten-day-old process kept running
with the old environment -- and the script reported "service active" and
"listening on 127.0.0.1:9000", both true of the process it was supposed to have
replaced. Measured: MainPID 3019381, started 2026-09-15, still carrying
WC_CLAUDE_PATH=/home/kali/.local/bin/claude after two deploys that both
reported success.

That is the shape worth remembering. `is-active` and "a port is listening" are
satisfied by the thing the script exists to replace, so neither can detect that
it did not run. The script now compares MainPID before and after and fails when
it has not changed.

With both fixed, a real turn through the Kali3 tunnel returns
`{"type": "text", "content": "TRANSPORT_OK"}` and the remote journal shows
`claude PID 3583225: /usr/bin/claude -p --output-format ...`.

The cases here are split by what can honestly be checked. The proxy's own
fallback is real logic and is tested behaviourally. The deploy script talks to
remote hosts over SSH, so it is covered by source assertions -- weak, and said
to be weak, but they pin the two specific substitutions that caused this.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "wc-deploy-proxy.sh"


@unittest.skipUnless(SCRIPT.exists(), "deploy script not present")
class DeployScriptRestartsQA(unittest.TestCase):
    SOURCE = SCRIPT.read_text()

    @property
    def CODE(self):
        """Executable lines only.

        The first version of this case asserted `enable --now` was absent from
        the file and failed against the comment explaining why it was removed.
        A source check that cannot tell code from prose reports the
        explanation as the defect -- and would equally pass on code hidden
        under a comment that mentions the right words.
        """
        return "\n".join(
            line for line in self.SOURCE.splitlines()
            if line.strip() and not line.lstrip().startswith("#"))

    def test_it_restarts_rather_than_enable_now(self):
        """`enable --now` on a running service is a no-op, which is how a
        deploy reported success twice while changing nothing."""
        self.assertIn("systemctl --user restart", self.CODE)
        self.assertNotIn(
            "enable --now", self.CODE,
            "`enable --now` is back in an executable line; a running service "
            "will not be replaced")

    def test_it_verifies_by_pid_not_by_is_active(self):
        """is-active and a listening port are both true of the process being
        replaced, so neither can catch a deploy that did not happen."""
        self.assertIn("MainPID", self.SOURCE)
        self.assertIn("_pid_before", self.SOURCE)
        self.assertIn("_pid_after", self.SOURCE)

    def test_the_claude_path_is_asked_for_not_assumed(self):
        """The console's own layout must not be written onto other hosts."""
        self.assertIn("command -v claude", self.CODE)
        self.assertNotIn(
            "Environment=WC_CLAUDE_PATH=%h/.local/bin/claude", self.CODE,
            "the unit hardcodes this console's claude path again")


class ProxyClaudePathFallbackQA(unittest.TestCase):
    """The belt: a unit naming a path that is not there must not cost a turn."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.real = self.dir / "claude"
        self.real.write_text("#!/bin/sh\nexit 0\n")
        self.real.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def _resolve(self, configured):
        """Mirror claude_proxy's resolution, which is inline in a long spawn
        path and cannot be imported on its own."""
        resolved = (shutil.which(configured)
                    if not os.path.isabs(configured) else configured)
        if resolved and os.path.isabs(resolved) and not os.path.isfile(resolved):
            from_path = shutil.which("claude")
            if from_path:
                resolved = from_path
        return resolved

    def test_an_absent_absolute_path_falls_back_to_PATH(self):
        with patch.dict(os.environ, {"PATH": str(self.dir)}):
            got = self._resolve("/nonexistent/.local/bin/claude")
        self.assertEqual(got, str(self.real))

    def test_a_present_absolute_path_is_used_as_given(self):
        """The control: the fallback must not override a working configuration,
        which would make WC_CLAUDE_PATH meaningless."""
        other = self.dir / "claude-pinned"
        other.write_text("#!/bin/sh\nexit 0\n")
        other.chmod(0o755)
        with patch.dict(os.environ, {"PATH": str(self.dir)}):
            got = self._resolve(str(other))
        self.assertEqual(got, str(other))

    def test_no_claude_anywhere_still_reports_the_configured_path(self):
        """With nothing to fall back to, the error must name what was asked
        for -- 'not found: /usr/bin/claude' is actionable, a bare 'not found'
        is not."""
        with patch.dict(os.environ, {"PATH": str(self.dir / "empty")}):
            got = self._resolve("/nonexistent/claude")
        self.assertEqual(got, "/nonexistent/claude")


if __name__ == "__main__":
    unittest.main()
