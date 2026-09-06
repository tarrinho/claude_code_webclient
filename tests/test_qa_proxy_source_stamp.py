"""QA: the running proxy stamps the source file it is actually serving.

`claude_proxy.py` has no orchestrator watching whether the file on disk still
matches the process that is running -- systemd restarts it if it exits, but a
process that is alive and simply stale (started before the last edit landed)
looks identical to a healthy one from the outside. This has already broken
three separate features silently -- the proxy token, the backend environment,
and usage accounting -- each presenting as "the feature does nothing", with no
error anywhere, because the fix was on disk but not in the running process.

The mitigation `main()` carries is a log line: it stamps `source_mtime=...`,
computed from `os.stat(__file__).st_mtime`, into the startup "listening on"
message, so staleness is checkable by comparing that timestamp against the
file's mtime on disk. This test pins that the stamp is still present, still
computed from the real file, and still logged -- by actually starting the
proxy process and reading its own log line, not by grepping the source for a
string that could be present and unreachable (registry #48: nine tests read a
shell block and never ran it, so a block that could not succeed passed all
nine).
"""
from __future__ import annotations

import datetime as dt
import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "claude_proxy.py"


def _expected_source_mtime() -> str:
    return (
        dt.datetime.fromtimestamp(PROXY.stat().st_mtime, tz=dt.timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ProxySourceStampTests(unittest.TestCase):
    def test_the_startup_log_names_the_file_it_is_actually_running(self):
        port = _free_port()
        env = dict(os.environ)
        env["WC_PROXY_TOKEN"] = "x" * 32
        proc = subprocess.Popen(
            [sys.executable, str(PROXY), "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            line = ""
            while time.monotonic() < deadline:
                fragment = proc.stdout.readline()
                if not fragment:
                    if proc.poll() is not None:
                        break
                    continue
                if "listening on" in fragment:
                    line = fragment
                    break
            self.assertIn(
                "listening on", line,
                "proxy never logged its startup line -- did not start, or "
                "the message text changed",
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

        self.assertIn(
            "source_mtime=", line,
            "the startup log no longer stamps source_mtime -- staleness "
            "(a running process serving an old version of this file) would "
            "go undetectable again",
        )
        stamped = line.split("source_mtime=", 1)[1].strip()
        self.assertEqual(
            stamped, _expected_source_mtime(),
            "the stamped value does not match claude_proxy.py's real mtime -- "
            "the stamp is being computed from the wrong file, or drifted "
            "from what os.stat(__file__) reports",
        )


if __name__ == "__main__":
    unittest.main()
