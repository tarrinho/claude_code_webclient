"""QA: launch.sh must not kill processes it does not own.

It used to run `pkill -f "uvicorn app:app"` to clear a previous instance. That
matches every uvicorn on the machine running this app, including the throwaway
servers the test suite spawns on random ports -- and launch.sh runs on every
`systemctl restart`. So one restart swept away every test server on the box,
and the suites reported it as their own servers exiting with code -15. Ten
restarts while proving the recovery cases turned a green run into 128 failures
that had nothing to do with the code under test, and the evidence pointed at
the tests rather than at the supervision that killed them.

bin/wc-free-proxy-port.sh had already learned this for the proxy port and says
so in its comments. The listen port had not. These tests pin the shape both
scripts now share: find the process holding the port, confirm what it is from
its command line, and stop only that.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCH = ROOT / "launch.sh"
RECLAIM = ROOT / "bin" / "wc-free-proxy-port.sh"


def pattern_kills(path: Path) -> list[str]:
    """Executable lines in *path* that kill by pattern, with line numbers.

    Comments are skipped deliberately. The fix documents what the line used to
    be, so a bare substring search finds the word `pkill` in the very comment
    explaining why it is gone -- which would make this suite unable to
    distinguish the bug from its own postmortem.
    """
    offenders = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if re.search(r"\bpkill\b|\bkillall\b", stripped):
            offenders.append(f"{path.name}:{number}: {stripped}")
    return offenders


class NoPatternKillTests(unittest.TestCase):
    def setUp(self):
        self.source = LAUNCH.read_text(encoding="utf-8")

    def test_launch_does_not_pattern_kill(self):
        """The regression proper. pkill -f matches other people's processes."""
        self.assertEqual(pattern_kills(LAUNCH), [],
                         "pkill -f matches every session's test servers")

    def test_no_script_pattern_kills_uvicorn(self):
        """Any script here, not just launch.sh -- the hazard is the idiom."""
        offenders = []
        for path in [LAUNCH, *(ROOT / "bin").glob("*.sh")]:
            offenders += pattern_kills(path)
        self.assertEqual(offenders, [], "pattern kill in a supervision script")

    def test_it_resolves_the_holder_of_the_port(self):
        self.assertIn("sport = :${RECLAIM_PORT}", self.source)
        self.assertIn("/proc/${holder}/cmdline", self.source)

    def test_it_checks_the_cmdline_before_killing(self):
        """A pid on the port is not automatically ours."""
        self.assertIn('*"uvicorn app:app"*', self.source)

    def test_it_says_so_and_stops_when_the_holder_is_not_ours(self):
        self.assertIn("not ours, leaving it", self.source)

    def test_it_escalates_only_after_a_polite_stop(self):
        """SIGTERM first, SIGKILL only if it is ignored."""
        body = self.source[self.source.index("reclaiming port ${RECLAIM_PORT}"):]
        term = body.index('kill "$holder"')
        hard = body.index("kill -9")
        self.assertLess(term, hard, "SIGKILL must not come first")


class ParityWithTheProxyReclaimTests(unittest.TestCase):
    """The proxy port learned this first; the two should not drift apart."""

    def test_the_proxy_reclaim_still_checks_a_cmdline(self):
        text = RECLAIM.read_text(encoding="utf-8")
        self.assertIn("claude_proxy.py", text)
        self.assertIn("not ours, leaving it", text)

    def test_neither_script_pattern_kills(self):
        for path in (LAUNCH, RECLAIM):
            with self.subTest(script=path.name):
                self.assertEqual(pattern_kills(path), [])


def reclaim_block() -> str:
    """The reclaim block as launch.sh actually runs it."""
    source = LAUNCH.read_text(encoding="utf-8")
    start = source.index("# >>> reclaim-block") + len("# >>> reclaim-block")
    return source[start:source.index("# <<< reclaim-block")]


def run_reclaim(port: int) -> subprocess.CompletedProcess:
    """Execute the block under the same shell options launch.sh uses.

    `set -euo pipefail` is the whole point: the bug was a pipeline exiting 1
    under those options, and a test that runs the block without them cannot
    see it.
    """
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{reclaim_block()}"],
        capture_output=True, text=True, check=False,
        env={**os.environ, "WC_PORT": str(port)},
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ItRunsAtAllTests(unittest.TestCase):
    """Execute the block, rather than reading it.

    Everything above this class inspects the source. That is worth having, but
    it is exactly what let a block that could never succeed pass nine
    assertions: `grep` exits 1 when it finds nothing, `pipefail` propagates it,
    and `set -e` killed launch.sh right after the banner. The site was down for
    43 restart attempts and no traceback was written anywhere, because nothing
    had crashed -- the script had simply stopped.
    """

    def test_it_succeeds_when_nothing_holds_the_port(self):
        """The overwhelmingly common case, and the one that broke production.

        On a clean start there is no previous instance. If the reclaim can only
        exit 0 when it finds something to reclaim, then the server starts only
        when the problem exists -- which is precisely backwards.
        """
        result = run_reclaim(free_port())
        self.assertEqual(result.returncode, 0,
                         f"a cold start cannot proceed: {result.stderr}")

    def test_it_says_nothing_when_there_is_nothing_to_say(self):
        result = run_reclaim(free_port())
        self.assertEqual(result.stderr.strip(), "")

    def test_it_returns_promptly_when_the_port_is_already_free(self):
        """The wait must observe the port, not just sleep on principle.

        The release-wait loop tested `! ss ... >/dev/null`, but ss exits 0 for
        any successful query whether or not anything matched -- so it never
        broke early and slept its full budget on every start, while checking
        nothing. It passed every assertion here, because exit status and
        stderr were both exactly right. Only the clock could tell.
        """
        started = time.monotonic()
        result = run_reclaim(free_port())
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 3.0,
                        f"took {elapsed:.1f}s with nothing holding the port: "
                        "the release-wait is not observing anything")


class LiveHolderTests(unittest.TestCase):
    """With something really listening, on a spare port."""

    def _listener(self, port: int, *extra_argv: str) -> subprocess.Popen:
        """A process holding *port*, with *extra_argv* on its command line."""
        code = (
            "import socket, sys, time\n"
            f"s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
            f"s.bind(('127.0.0.1', {port})); s.listen(1)\n"
            "sys.stderr.write('ready\\n'); sys.stderr.flush()\n"
            "time.sleep(120)\n"
        )
        proc = subprocess.Popen([sys.executable, "-c", code, *extra_argv],
                                stderr=subprocess.PIPE)
        self.addCleanup(self._reap, proc)
        proc.stderr.readline()          # wait until it is actually bound
        return proc

    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)

    def test_a_holder_that_is_not_ours_is_left_running(self):
        """The reason this checks a cmdline instead of killing what it finds."""
        port = free_port()
        proc = self._listener(port, "some-unrelated-service")
        result = run_reclaim(port)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not ours, leaving it", result.stderr)
        self.assertIsNone(proc.poll(), "it killed a process that was not ours")

    def test_our_own_stale_instance_is_stopped(self):
        """The case the block exists for."""
        port = free_port()
        proc = self._listener(port, "-m", "uvicorn", "app:app")
        result = run_reclaim(port)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reclaiming port", result.stderr)
        proc.wait(timeout=10)
        self.assertIsNotNone(proc.poll(), "the stale instance is still running")

    def test_a_test_server_on_another_port_is_untouched(self):
        """The regression that started all this: sweeping the whole box.

        A server whose command line matches exactly, but which holds a
        different port, is somebody else's and must survive.
        """
        theirs = self._listener(free_port(), "-m", "uvicorn", "app:app")
        result = run_reclaim(free_port())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(theirs.poll(),
                          "it killed a server on a port it was not claiming")


class ShellIsValidTests(unittest.TestCase):
    def test_launch_parses(self):
        """A supervision script that does not parse takes the site down."""
        bash = shutil.which("bash")
        if not bash:  # pragma: no cover - bash is present everywhere here
            self.skipTest("bash not on PATH")
        result = subprocess.run([bash, "-n", str(LAUNCH)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
