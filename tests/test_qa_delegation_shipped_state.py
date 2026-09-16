"""QA: release 0.19.0 ships the machinery and routes nothing.

This is the release's entire claim, so it gets a test rather than a sentence
in a changelog. Spec section 12 forbids flipping `coding` operational until the
gate-type question is decided, and nothing in this release decides it.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db

ROOT = Path(__file__).resolve().parents[1]


class ShippedStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def test_a_fresh_database_has_no_operational_task_type(self):
        self.assertEqual(await db.delegation_operational_all(), set())

    async def test_the_seed_script_flips_nothing_operational(self):
        """It fills measurements. Flipping a type routable is a decision, and
        section 12 has not made it."""
        source = (ROOT / "bin" / "wc-seed-delegation.py").read_text()
        self.assertNotIn("delegation_operational_set", source)

    def test_no_source_file_flips_coding_operational(self):
        """Section 12: 'Until it is decided, do not flip coding to operational.'"""
        for path in list(ROOT.glob("*.py")) + list((ROOT / "routes").glob("*.py")):
            source = path.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn('delegation_operational_set("coding", True)', source)


if __name__ == "__main__":
    unittest.main()
