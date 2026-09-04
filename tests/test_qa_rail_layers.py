"""QA: computeLayers is a pure, DOM-free function -- structural checks only,
matching this repo's convention for frontend logic (see test_frontend.py).
The actual topological-sort behavior is verified by hand via `node -e` per
this task's plan step, since there is no JS test runner in this repo.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"


class RailLayersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ASSETS / "supervisor" / "rail.js").read_text()

    def test_compute_layers_is_exported_and_pure(self):
        self.assertIn("export function computeLayers(tasks)", self.source)
        # No DOM access in this function specifically -- renderRail (added in
        # a later task) is the only place in this file allowed to touch it.
        start = self.source.index("export function computeLayers(")
        rest = self.source[start:]
        end = rest.find("\nexport function ", 1)
        body = rest[:end] if end != -1 else rest
        self.assertNotIn("document.", body)

    def test_cycle_safety_is_present(self):
        """A cycle should never reach here (PlanParser excludes
        self-references) but must not hang the UI if one somehow does."""
        self.assertIn("pass <= tasks.length", self.source)


if __name__ == "__main__":
    unittest.main()
