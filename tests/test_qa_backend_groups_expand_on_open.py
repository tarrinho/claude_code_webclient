"""QA: every transport group starts expanded when the Backends panel opens.

Pedro's request: every `.transport-collapse-toggle` reads collapsed=false the
moment Settings opens on the Backends tab. Before this, _collapsedGroups and
_seededGroups are both module-level state that outlives a single panel
open/close -- a group the operator (or the auto-collapse-if-Disabled default
in _buildTransportHeader) had collapsed earlier in the session stayed
collapsed the next time the dialog opened, with nothing to reset it.

Asserted from source: this repo has no JS test runner for machines.js (the
quickjs harness in tests/js/d3_dom_stub.js exists only for
supervisor-map.js), and the file's own convention for machines.js is source
inspection -- see test_qa_backend_map_columns.py.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MACHINES_JS = ROOT / "web" / "assets" / "machines.js"
APP_JS = ROOT / "web" / "assets" / "app.js"


class ExpandAllTransportGroupsTests(unittest.TestCase):
    def setUp(self):
        self.js = MACHINES_JS.read_text(encoding="utf-8")
        match = re.search(
            r"export function _expandAllTransportGroups\(\)\s*\{(.*?)\n\}",
            self.js, re.DOTALL)
        self.assertIsNotNone(
            match, "_expandAllTransportGroups is missing from machines.js")
        self.body = match.group(1)

    def test_it_is_exported(self):
        self.assertIn("export function _expandAllTransportGroups", self.js)

    def test_it_clears_the_collapsed_set(self):
        self.assertIn("_collapsedGroups.clear()", self.body)

    def test_it_seeds_every_current_group(self):
        """The regression a bare .clear() would still have: a Disabled group
        never seen before this render seeds itself collapsed inside
        _buildTransportHeader, in the same pass this function's clear() is
        supposed to guarantee against. Marking every current key as already
        seeded is what stops that -- proven by the assertion below reading
        _machineGroups() (the single ordered source both columns already
        render from) rather than re-deriving group keys a second way."""
        self.assertIn("_machineGroups()", self.body)
        self.assertIn("_seededGroups.add", self.body)


class CalledEveryTimeThePanelOpensTests(unittest.TestCase):
    """Not just on first load -- loadBackends() runs on every dialog open
    (openSettingsDialog always switches to the backends tab) and on every
    later switch back to that tab, so the reset has to live there rather
    than in a one-time init path."""

    def setUp(self):
        self.js = APP_JS.read_text(encoding="utf-8")
        match = re.search(
            r"async function loadBackends\(\)\s*\{(.*?)\n\}", self.js, re.DOTALL)
        self.assertIsNotNone(match, "loadBackends not found in app.js")
        self.body = match.group(1)

    def test_app_js_imports_the_reset(self):
        self.assertIn("_expandAllTransportGroups", self.js)
        # Same querystring as every other machines.js symbol app.js imports --
        # a mismatched one would double-instantiate the module (the recurring
        # cache-buster bug this repo has hit more than once).
        machines_versions = set(re.findall(r"machines\.js\?v=(\d+)", self.js))
        self.assertEqual(
            len(machines_versions), 1,
            f"app.js references machines.js at more than one version: "
            f"{machines_versions}")

    def test_load_backends_calls_it_before_rendering(self):
        self.assertIn("_expandAllTransportGroups()", self.body)
        self.assertIn("_renderMachineList()", self.body)
        self.assertLess(
            self.body.index("_expandAllTransportGroups()"),
            self.body.index("_renderMachineList()"),
            "the reset must happen before the render it is supposed to affect",
        )

    def test_it_runs_after_machines_and_transports_are_loaded(self):
        """_expandAllTransportGroups reads _machineGroups(), which reads
        _machines and _transports -- calling it before those are populated
        would seed against last render's stale groups, not this one's."""
        self.assertIn("await loadMachines()", self.body)
        self.assertIn("await loadTransports()", self.body)
        self.assertLess(
            self.body.index("await loadMachines()"),
            self.body.index("_expandAllTransportGroups()"))
        self.assertLess(
            self.body.index("await loadTransports()"),
            self.body.index("_expandAllTransportGroups()"))


if __name__ == "__main__":
    unittest.main()
