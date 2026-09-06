"""QA: machine-wizard.js is imported and called from app.js and machines.js.

Before the fix, machine-wizard.js existed at web/assets/machine-wizard.js but
was never imported by app.js — the `_renderSshWizard` function was dead code.
The import and call-line landed in app.js in a separate commit. This test
verifies the wiring is present by reading source text, so it runs without
playwright or a browser.
"""
from __future__ import annotations

import asyncio
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web" / "assets"
WEB = ROOT / "web"


def _read(path: str) -> str:
    return (ASSETS / path).read_text()


def _read_web(path: str) -> str:
    return (WEB / path).read_text()


class MachineWizardImportQA(unittest.TestCase):
    """Check that app.js imports _renderSshWizard from machine-wizard.js."""

    def test_app_js_imports_the_wizard_function(self):
        """app.js must import _renderSshWizard from machine-wizard.js."""
        src = _read("app.js")
        match = re.search(
            r"import\s+{\s*_renderSshWizard\s*}\s+from\s+"
            r"'./machine-wizard\.js\?v=\d+'\s*;",
            src,
        )
        self.assertIsNotNone(
            match,
            "app.js does not import _renderSshWizard from machine-wizard.js — "
            "the wizard will not render in Settings",
        )

    def test_app_js_calls_renderSshWizard_with_machines(self):
        """loadBackends() must call _renderSshWizard(_machines)."""
        src = _read("app.js")
        match = re.search(
            r"_renderSshWizard\s*\(\s*_machines\s*\)",
            src,
        )
        self.assertIsNotNone(
            match,
            "app.js does not call _renderSshWizard(_machines) anywhere — "
            "the wizard will never appear even if imported",
        )

    def test_machine_wizard_has_the_panel_selector(self):
        """machine-wizard.js must create #sshWizard and insert it into the
        settings panel."""
        src = _read("machine-wizard.js")
        self.assertIn(
            "box.id = 'sshWizard'",
            src,
            "machine-wizard.js must assign id='sshWizard' to its panel div",
        )
        self.assertIn(
            "panel.insertBefore(box",
            src,
            "machine-wizard.js must insert the wizard panel into #panelSettings",
        )

    def test_machine_wizard_has_cancel_and_run_buttons(self):
        """The wizard must have #wizardCancel and #wizardStart buttons."""
        src = _read("machine-wizard.js")
        self.assertIn(
            'id="wizardCancel"',
            src,
            "cancel button #wizardCancel missing from wizard template",
        )
        self.assertIn(
            'id="wizardStart"',
            src,
            "run button #wizardStart missing from wizard template",
        )

    def test_wizard_calls_the_three_api_endpoints(self):
        """The wizard must POST to /api/init/ssh-test, /api/init/probe-remote,
        and /api/tunnel/start in that order."""
        src = _read("machine-wizard.js")
        # Strip comments to avoid false matches from docstrings.
        cleaned = re.sub(r"//.*?$|/\*.*?\*/", "", src, flags=re.MULTILINE | re.DOTALL)
        endpoints = [
            "/api/init/ssh-test",
            "/api/init/probe-remote",
            "/api/tunnel/start",
        ]
        last_pos = -1
        for ep in endpoints:
            pos = cleaned.find(ep, last_pos + 1)
            self.assertTrue(
                pos > last_pos,
                f"endpoint {ep} not found after {endpoints[endpoints.index(ep)-1] if endpoints.index(ep) > 0 else 'start'}",
            )
            last_pos = pos


class AppJsModuleVersionQA(unittest.TestCase):
    """app.js?v=N cache-buster must be consistent with other imports."""

    def test_app_js_version_is_a_positive_integer(self):
        """app.js cache-buster in index.html must be a positive integer."""
        src = _read_web("index.html")
        match = re.search(
            r'<script[^>]+src="/assets/app\.js\?v=(\d+)"',
            src,
        )
        self.assertIsNotNone(match, "index.html does not set app.js?v")
        version = int(match.group(1))
        self.assertGreater(
            version, 0,
            "app.js version in index.html must be a positive integer",
        )


if __name__ == "__main__":
    unittest.main()
