"""QA: bin/wc-run-suite-remote.sh -- shape and safety properties. A live run
needs a running server and a real transport, so (mirroring
tests/test_qa_deploy_entrypoint.py and tests/test_qa_provision_script.py)
this pins what can be checked from the file itself: it mints a short-lived
token rather than the no-expiry default, and it is an HTTP client, not its
own SSH client.

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §4, §7.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "wc-run-suite-remote.sh"


def _tracked_mode(rel_path: str) -> str:
    result = subprocess.run(
        ["git", "ls-files", "-s", "--", rel_path],
        capture_output=True, text=True, cwd=ROOT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    return result.stdout.split()[0]


class RunRemoteScriptShapeTests(unittest.TestCase):
    def test_the_script_is_present(self):
        self.assertTrue(SCRIPT.is_file())

    def test_it_is_executable_where_it_counts(self):
        mode = _tracked_mode("bin/wc-run-suite-remote.sh")
        if not mode:
            self.skipTest("not a git checkout")
        self.assertEqual(mode, "100755")

    def test_it_mints_a_token_with_a_pinned_short_expiry(self):
        """"short-lived" without a number was the gap the spec review
        caught (§7) -- this pins the actual number rather than trusting the
        adjective."""
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("wc-token.py", source)
        self.assertIn("--days 1", source)

    def test_it_never_calls_ssh_directly(self):
        """Cannot open its own SSH connection to the transport -- exec_command/
        sync_transport are bound to the running app process's own in-memory
        tunnel state (spec §4). This is an HTTP client only."""
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotRegex(source, r"(?<!#\s)\bssh\s")

    def test_it_posts_to_the_qa_run_route(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("/api/qa/run", source)

    def test_it_sends_the_token_as_a_bearer_header(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Authorization: Bearer", source)

    def test_it_does_not_default_to_a_localhost_port_nothing_listens_on(self):
        """Confirmed live: nothing listens on 127.0.0.1:8080 on this
        deployment (uvicorn binds only the tailnet IP on 443, Caddy is
        inactive) -- a hardcoded default here connected to nothing."""
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("127.0.0.1:8080", source)

    def test_it_looks_up_the_tailnet_domain_like_launch_sh_does(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("tailscale status --json", source)

    def test_it_never_passes_the_token_as_a_curl_argv_flag(self):
        """argv is world-readable via /proc/<pid>/cmdline for the run's
        whole duration -- the token must travel to curl via a -K config
        file, never a -H argv argument."""
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('-H "Authorization', source)


if __name__ == "__main__":
    unittest.main()
