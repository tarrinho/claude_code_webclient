"""QA: shells that predate the wrapper alias still get routed.

Pedro asked whether existing `screen` sessions could pick up the new aliases.
Two parts of that are impossible and one is not, and the distinction is the
whole design:

* An already-running `claude` process cannot be re-routed. Its environment was
  fixed when it started.
* A running shell's alias table cannot be changed from outside it. There is no
  mechanism; the shell would have to execute `source ~/.bashrc`, and the screen
  windows are sitting inside `claude` with no prompt. cweb2's parent bash has
  been running since Aug 31 and holds the aliases as they were then.
* But bash caches the resolved *path* of a command, not the alias -- and
  `/home/kali/.local/bin/claude` is that path for every one of those shells,
  because `~/.local/bin` is first on their PATH. Replacing what lives there
  routes them on their next launch without them re-reading anything.

So `bin/claude-shim.sh` stands at that path. Two properties keep it safe, and
both are tested here because getting either wrong is severe:

* it must pass non-session invocations straight through -- the console spawns
  the CLI with `-p`, and those turns are already configured by claude_proxy
  through the same rule, so wrapping them would add a database read to the hot
  path and a second layer that can fail
* the wrapper must never invoke `claude` by name, or it re-enters the shim and
  forks until the process limit

The installer is re-asserted on a 30-second timer because the CLI self-updates
and rewrites that path -- observed moving 2.1.258 to 2.1.259 at 00:00 on
2026-09-03. Losing the shim silently would put every terminal back to unrouted,
which is the failure it exists to prevent.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHIM = REPO / "bin" / "claude-shim.sh"
INSTALLER = REPO / "bin" / "wc-install-claude-shim.sh"
WRAPPER = REPO / "bin" / "wc-claude.sh"
HEALTH = REPO / "bin" / "wc-health.sh"
MARKER = "wc-claude-shim-do-not-exec-from-wrapper"


class ShimRoutingTests(unittest.TestCase):
    """Which invocations get wrapped, and which must not."""

    def _shim_source(self) -> str:
        return SHIM.read_text(encoding="utf-8")

    def test_print_mode_is_passed_through(self):
        """The console's own spawns use -p and are already configured.

        Wrapping them would put a database read in the turn hot path, and give
        the proxy's carefully-built argv a second opinion about --model.
        """
        source = self._shim_source()
        self.assertIn("-p|--print", source)
        # The passthrough must happen before the wrapper is reached.
        self.assertLess(
            source.index("-p|--print"), source.index("exec bash"),
            "the -p passthrough must come before the wrapper exec, or every "
            "console turn goes through the wrapper",
        )

    def test_version_and_subcommands_are_passed_through(self):
        """These have their own argument grammar; injecting --model into them is
        noise at best and a parse error at worst."""
        source = self._shim_source()
        for word in ("--version", "--help", "mcp", "doctor", "update", "config"):
            with self.subTest(word=word):
                self.assertIn(word, source)

    def test_it_still_runs_the_cli_when_the_wrapper_is_missing(self):
        """Routing is worth a lot. It is not worth more than being able to work.

        If the repo moves or the wrapper is renamed, the operator must still be
        able to start claude rather than being locked out by our own shim.
        """
        source = self._shim_source()
        self.assertIn("starting unrouted", source)

    def test_it_carries_the_marker_the_wrapper_checks_for(self):
        self.assertIn(MARKER, self._shim_source())


class NoRecursionTests(unittest.TestCase):
    """The wrapper must never re-enter the shim."""

    def test_the_wrapper_never_invokes_claude_by_name(self):
        """`exec claude` in the wrapper, once the shim is at that name, forks
        until the process limit -- taking the machine with it."""
        body = WRAPPER.read_text(encoding="utf-8")
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotIn(
                "exec claude ", stripped,
                "the wrapper invokes the CLI by name, which now resolves to the "
                "shim that execs the wrapper",
            )

    def test_the_wrapper_skips_any_candidate_bearing_the_marker(self):
        body = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("CLAUDE_SHIM_MARKER", body)
        self.assertIn("_is_shim", body)

    def test_the_wrapper_fails_loudly_when_it_cannot_find_a_real_binary(self):
        """Better to refuse than to exec something that might be ourselves."""
        body = WRAPPER.read_text(encoding="utf-8")
        self.assertIn("exit 127", body)


class InstallerTests(unittest.TestCase):
    """Idempotent, self-healing, and unwilling to lock the operator out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.versions = self.root / "versions"
        self.bin.mkdir()
        self.versions.mkdir()
        self.target = self.bin / "claude"

    def _cli(self, name: str = "2.1.259") -> Path:
        real = self.versions / name
        real.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
        real.chmod(0o755)
        return real

    def _install(self) -> subprocess.CompletedProcess:
        env = {**os.environ,
               "WC_CLAUDE_SHIM_TARGET": str(self.target),
               "WC_CLAUDE_VERSIONS_DIR": str(self.versions)}
        return subprocess.run(["bash", str(INSTALLER)], capture_output=True,
                              text=True, env=env, timeout=60, check=True)

    def test_it_installs_over_a_symlink_the_updater_left(self):
        """The exact state a self-update leaves behind."""
        real = self._cli()
        self.target.symlink_to(real)
        self._install()
        self.assertIn(MARKER, self.target.read_text(encoding="utf-8"))

    def test_it_is_silent_and_idempotent_once_installed(self):
        """It runs every 30 seconds. Noise there is noise nobody reads."""
        self._cli()
        self._install()
        second = self._install()
        self.assertEqual(second.stdout.strip(), "")

    def test_it_refuses_when_there_is_no_real_cli(self):
        """Replacing the entry point with nothing behind it would leave the
        operator unable to start claude at all -- worse than unrouted."""
        result = self._install()
        self.assertIn("refusing", result.stdout)
        self.assertFalse(self.target.exists())

    def test_the_installed_shim_is_executable(self):
        self._cli()
        self._install()
        self.assertTrue(os.access(self.target, os.X_OK))

    def test_it_picks_the_newest_version_not_the_first(self):
        """Versions sort as strings in the wrong order: 2.1.9 > 2.1.10
        lexically. `sort -V` is what makes an update actually take effect."""
        for name in ("2.1.9", "2.1.10", "2.1.259"):
            self._cli(name)
        result = self._install()
        self.assertIn("2.1.259", result.stdout)


class TimerWiringTests(unittest.TestCase):
    """It has to actually be asserted, not merely assertable."""

    def test_the_health_script_calls_the_installer(self):
        self.assertIn("wc-install-claude-shim.sh",
                      HEALTH.read_text(encoding="utf-8"))

    def test_it_runs_before_the_server_guards(self):
        """wc-health.sh exits early when webconsole is stopped or mid-restart.
        The shim concerns terminals, not the server, so it must be asserted
        before those exits or a deliberately stopped server would also mean
        unrouted shells."""
        body = HEALTH.read_text(encoding="utf-8")
        self.assertLess(
            body.index("wc-install-claude-shim.sh"),
            body.index("is-enabled --quiet webconsole.service"),
            "the shim assertion sits after an early exit, so it will be "
            "skipped in exactly the states it is needed",
        )


if __name__ == "__main__":
    unittest.main()
