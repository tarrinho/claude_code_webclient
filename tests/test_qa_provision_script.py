"""QA: bin/wc-provision-qa.sh exists, is executable, is idempotent, and
resolves connection details the one way this project allows -- from
ssh_transports, never re-derived.

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §1.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "wc-provision-qa.sh"


def _tracked_mode(rel_path: str) -> str:
    result = subprocess.run(
        ["git", "ls-files", "-s", "--", rel_path],
        capture_output=True, text=True, cwd=ROOT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    return result.stdout.split()[0]


class ProvisionScriptShapeTests(unittest.TestCase):
    def test_the_script_is_present(self):
        self.assertTrue(SCRIPT.is_file())

    def test_it_is_executable_where_it_counts(self):
        mode = _tracked_mode("bin/wc-provision-qa.sh")
        if not mode:
            self.skipTest("not a git checkout")
        self.assertEqual(mode, "100755")

    def test_it_resolves_connection_details_from_ssh_transports_only(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("ssh_transports", source)

    def test_it_creates_a_venv_under_the_qa_checkout_path(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("wc-qa-checkout", source)
        self.assertIn("venv", source)

    def test_it_installs_both_requirement_files(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("requirements.txt", source)
        self.assertIn("requirements-dev.txt", source)

    def test_it_installs_chromium(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("playwright install chromium", source)

    def test_it_checks_before_redoing_the_expensive_parts(self):
        """Idempotent, mirroring wc-deploy-proxy.sh's own 'safe to press
        again' property -- a pip install plus a Chromium download is real
        time and bandwidth, not something to repeat unconditionally."""
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(".venv/bin/python", source)
        self.assertTrue(
            "if [ -x" in source or "if [ ! -x" in source or "command -v" in source,
            "no existence check found before the expensive setup steps")


if __name__ == "__main__":
    unittest.main()
