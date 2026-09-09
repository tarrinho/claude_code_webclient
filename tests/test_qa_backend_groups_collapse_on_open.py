"""QA: every transport group starts collapsed (shrunk) when the Backends
panel opens.

Pedro's request, corrected once: the first version of this feature expanded
every group on open. He meant the opposite -- every
`.transport-collapse-toggle` reads collapsed=true the moment Settings opens
on the Backends tab. _collapsedGroups and _seededGroups are both
module-level state that outlives a single panel open/close, so without a
reset a group the operator had expanded earlier in the session stayed
expanded the next time the dialog opened.

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


class CollapseAllTransportGroupsTests(unittest.TestCase):
    def setUp(self):
        self.js = MACHINES_JS.read_text(encoding="utf-8")
        match = re.search(
            r"export function _collapseAllTransportGroups\(\)\s*\{(.*?)\n\}",
            self.js, re.DOTALL)
        self.assertIsNotNone(
            match, "_collapseAllTransportGroups is missing from machines.js")
        self.body = match.group(1)

    def test_it_is_exported(self):
        self.assertIn("export function _collapseAllTransportGroups", self.js)

    def test_the_old_expand_version_is_gone(self):
        """Pin the correction itself -- the earlier, wrong function name must
        not linger as dead code alongside the fixed one."""
        self.assertNotIn("_expandAllTransportGroups", self.js)

    def test_it_does_not_clear_the_collapsed_set(self):
        """The regression this correction exists to catch: .clear() was the
        expand version's mechanism. Left in place here by mistake, it would
        undo every .add() call that follows it in source order and the panel
        would still open expanded."""
        self.assertNotIn("_collapsedGroups.clear()", self.body)

    def test_it_adds_every_current_group_to_collapsed(self):
        """Proven against _machineGroups() (the single ordered source both
        map columns already render from) rather than re-deriving group keys
        a second way."""
        self.assertIn("_machineGroups()", self.body)
        self.assertIn("_collapsedGroups.add", self.body)

    def test_it_seeds_every_current_group_too(self):
        """Not just collapsing -- seeding stops _buildTransportHeader's own
        seed-once-per-key logic from later making a second, independent
        collapse decision for a group first rendered by this same call."""
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
        self.assertIn("_collapseAllTransportGroups", self.js)
        self.assertNotIn("_expandAllTransportGroups", self.js)
        # Same querystring as every other machines.js symbol app.js imports --
        # a mismatched one would double-instantiate the module (the recurring
        # cache-buster bug this repo has hit more than once).
        machines_versions = set(re.findall(r"machines\.js\?v=(\d+)", self.js))
        self.assertEqual(
            len(machines_versions), 1,
            f"app.js references machines.js at more than one version: "
            f"{machines_versions}")

    def test_load_backends_calls_it_before_rendering(self):
        self.assertIn("_collapseAllTransportGroups()", self.body)
        self.assertIn("_renderMachineList()", self.body)
        self.assertLess(
            self.body.index("_collapseAllTransportGroups()"),
            self.body.index("_renderMachineList()"),
            "the reset must happen before the render it is supposed to affect",
        )

    def test_it_runs_after_machines_and_transports_are_loaded(self):
        """_collapseAllTransportGroups reads _machineGroups(), which reads
        _machines and _transports -- calling it before those are populated
        would seed against last render's stale groups, not this one's."""
        self.assertIn("await loadMachines()", self.body)
        self.assertIn("await loadTransports()", self.body)
        self.assertLess(
            self.body.index("await loadMachines()"),
            self.body.index("_collapseAllTransportGroups()"))
        self.assertLess(
            self.body.index("await loadTransports()"),
            self.body.index("_collapseAllTransportGroups()"))


if __name__ == "__main__":
    unittest.main()
