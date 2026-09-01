"""QA: the proxy launcher resolves the Claude binary without help from PATH.

`systemd --user` starts `bin/wc-proxy-run.sh` with a PATH that does not include
`~/.local/bin`, which is where the Claude Code CLI is installed on this machine.
`claude_proxy.py` resolves the binary with `shutil.which(claude_path)` and
refuses to spawn when that returns None, so without an absolute path **every
turn fails** with "claude binary not found" -- a failure the web UI shows only as
a failed turn, with the cause visible nowhere a user looks.

This has now been broken twice by the same mechanism. The first fix was an
`export PATH=...` line in the launcher that lived only in the shared working
tree, was never committed, and was silently reverted by another session's
checkout. Nothing failed at the time: the running proxy kept the good
environment it had started with, so the regression stayed dormant until the next
restart and then broke every turn at once (registry #49, #59).

So the property is pinned here rather than remembered. The block is **executed**,
not read: registry #48 records nine tests that read a shell block and never ran
it, so a block that could not succeed passed all nine.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin" / "wc-proxy-run.sh"

# A PATH like the one systemd --user actually supplies: no ~/.local/bin.
SYSTEMD_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

_BLOCK = re.compile(
    r"^# >>> claude-path-block$(.*?)^# <<< claude-path-block$",
    re.MULTILINE | re.DOTALL,
)


def _block() -> str:
    """The resolution block, lifted from the launcher by its markers."""
    match = _BLOCK.search(LAUNCHER.read_text(encoding="utf-8"))
    assert match, (
        "the claude-path-block markers are gone from bin/wc-proxy-run.sh. If the "
        "block moved, move the markers with it; if the resolution was removed, "
        "this file is what should have stopped that."
    )
    return match.group(1)


def _run_block(home: str, extra_path: str = "") -> subprocess.CompletedProcess:
    """Execute the block with a controlled HOME and PATH, and report the result."""
    script = f"set -euo pipefail\n{_block()}\nprintf '%s' \"${{WC_CLAUDE_PATH:-}}\"\n"
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, check=False,
        env={"HOME": home, "PATH": (extra_path + ":" if extra_path else "") + SYSTEMD_PATH},
    )


class LauncherWiringTests(unittest.TestCase):
    def test_the_launcher_exports_wc_claude_path(self):
        """`claude_proxy.py` reads this name; nothing else is consulted."""
        source = LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("WC_CLAUDE_PATH", source)
        self.assertIn("export WC_CLAUDE_PATH", source)

    def test_the_proxy_still_reads_that_variable(self):
        """Guards the other half of the contract.

        The launcher setting a variable the proxy has stopped reading would be a
        fix that silently does nothing -- the same shape as the bug it fixes.
        """
        source = (ROOT / "claude_proxy.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("WC_CLAUDE_PATH"', source)


class ResolutionTests(unittest.TestCase):
    """Run the block for real, under the PATH systemd actually provides."""

    def test_it_finds_a_binary_in_local_bin_that_path_cannot_see(self):
        """The exact production case: installed in ~/.local/bin, absent from PATH."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            (fake_home / ".local" / "bin").mkdir(parents=True)
            binary = fake_home / ".local" / "bin" / "claude"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)

            result = _run_block(str(fake_home))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, str(binary))
            self.assertNotIn("WARNING", result.stderr)

    def test_the_resolved_path_is_absolute(self):
        """A bare name would put us straight back into PATH's hands."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            (fake_home / ".local" / "bin").mkdir(parents=True)
            binary = fake_home / ".local" / "bin" / "claude"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            resolved = _run_block(str(fake_home)).stdout
            self.assertTrue(os.path.isabs(resolved), resolved)

    def test_an_existing_value_is_respected(self):
        """An operator override must win, or the fallbacks make it unusable."""
        with tempfile.TemporaryDirectory() as tmp:
            chosen = Path(tmp) / "elsewhere"
            chosen.write_text("#!/bin/sh\nexit 0\n")
            chosen.chmod(0o755)
            script = (
                f"set -euo pipefail\nexport WC_CLAUDE_PATH={chosen}\n"
                f"{_block()}\nprintf '%s' \"$WC_CLAUDE_PATH\"\n"
            )
            result = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, check=False,
                env={"HOME": tmp, "PATH": SYSTEMD_PATH},
            )
            self.assertEqual(result.stdout, str(chosen))

    def test_it_falls_back_to_path_when_nothing_is_in_the_usual_places(self):
        """A machine that installs the CLI elsewhere must still start."""
        with tempfile.TemporaryDirectory() as tmp:
            elsewhere = Path(tmp) / "bin"
            elsewhere.mkdir()
            binary = elsewhere / "claude"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            # HOME has no .local/bin, so only the PATH fallback can succeed.
            result = _run_block(str(Path(tmp) / "empty-home"), extra_path=str(elsewhere))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, str(binary))

    def test_a_missing_binary_warns_instead_of_failing_silently(self):
        """`set -e` must not kill the launcher, and it must say what is wrong.

        Refusing to start would take the site down over a missing optional
        dependency; starting silently is how this cost two outages. So: start,
        and complain loudly enough that the reason is in the log.
        """
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_block(str(Path(tmp) / "empty-home"))
            self.assertEqual(result.returncode, 0,
                             f"the launcher aborted instead of warning: {result.stderr}")
            self.assertIn("WARNING", result.stderr)
            self.assertIn("binary not found", result.stderr)


class LiveEnvironmentTests(unittest.TestCase):
    """This machine, as it is now -- not a fixture."""

    def test_the_cli_is_where_the_launcher_looks(self):
        found = shutil.which("claude") or ""
        candidates = {
            str(Path.home() / ".local" / "bin" / "claude"),
            "/usr/local/bin/claude",
            "/usr/bin/claude",
        }
        if not found and not any(Path(c).is_file() for c in candidates):
            self.skipTest("no claude CLI on this machine")
        reachable = [c for c in candidates if os.access(c, os.X_OK)] or [found]
        self.assertTrue(
            any(reachable),
            "the CLI exists but in none of the places the launcher checks; add "
            "it to the candidate list rather than relying on PATH",
        )


if __name__ == "__main__":
    unittest.main()
