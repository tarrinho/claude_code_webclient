"""QA: release 0.19.0 ships the machinery and routes nothing.

This is the release's entire claim, so it gets a test rather than a sentence
in a changelog. Spec section 12 forbids flipping `coding` operational until the
gate-type question is decided, and nothing in this release decides it.

Also covers two defects found in fix round 1 of task 10's review:

* the seed script's production-database guard compared `--db-path` against a
  self-relative *guess* at `config.DB_PATH`'s default instead of
  `config.DB_PATH` itself, so a deployment where `WC_DB_PATH` points
  somewhere else (exactly what `systemd/webconsole.service` does) let a real
  production path straight through. The reviewer proved this by actually
  seeding production.
* the seed script's `ROWS` silently omitted 6 of spec 2.6's 23 rows.

Every test below that exercises the production guard uses only temporary
paths -- never `config.DB_PATH`'s real default, never anything resembling
the actual production database.
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db

ROOT = Path(__file__).resolve().parents[1]
SEED_SCRIPT = ROOT / "bin" / "wc-seed-delegation.py"
SPEC_FILE = (
    ROOT / "docs" / "superpowers" / "specs"
    / "2026-09-14-tiered-agent-delegation-spec-v3.md"
)


def _load_seed_module():
    """Import bin/wc-seed-delegation.py as a module, without running main()
    (the ``if __name__ == "__main__"`` guard only fires when the module's
    own name is ``__main__``, and spec_from_file_location does not set that)."""
    spec = importlib.util.spec_from_file_location(
        "wc_seed_delegation_under_test", SEED_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        source = SEED_SCRIPT.read_text()
        self.assertNotIn("delegation_operational_set", source)

    def test_no_source_file_flips_coding_operational(self):
        """Section 12: 'Until it is decided, do not flip coding to operational.'"""
        for path in list(ROOT.glob("*.py")) + list((ROOT / "routes").glob("*.py")):
            source = path.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn('delegation_operational_set("coding", True)', source)


class SeedScriptProductionGuardTests(unittest.IsolatedAsyncioTestCase):
    """The seed script must refuse to write to `config.DB_PATH` -- the
    database a real deployment is actually using, `WC_DB_PATH` included --
    under a relative path, a `..` segment, or a symlink, not only under its
    own literal spelling. `config.DB_PATH` is patched to a throwaway temp
    path in every case here; the real default is never touched."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.module = _load_seed_module()

    async def test_the_exact_configured_path_is_refused(self):
        prod = str(Path(self.tmp.name) / "prod.db")
        with patch.object(config, "DB_PATH", prod):
            with self.assertRaises(SystemExit):
                await self.module.main(["--db-path", prod])
        self.assertFalse(Path(prod).exists(), "guard must refuse before touching the file")

    async def test_a_relative_path_resolving_to_the_configured_path_is_refused(self):
        prod = Path(self.tmp.name) / "prod.db"
        prod.touch()
        before = prod.stat().st_mtime_ns
        rel = os.path.relpath(prod, os.getcwd())
        with patch.object(config, "DB_PATH", str(prod)):
            with self.assertRaises(SystemExit):
                await self.module.main(["--db-path", rel])
        self.assertEqual(prod.stat().st_mtime_ns, before, "file must be untouched")

    async def test_a_symlink_to_the_configured_path_is_refused(self):
        prod = Path(self.tmp.name) / "prod.db"
        prod.touch()
        before = prod.stat().st_mtime_ns
        link = Path(self.tmp.name) / "prod_link.db"
        link.symlink_to(prod)
        with patch.object(config, "DB_PATH", str(prod)):
            with self.assertRaises(SystemExit):
                await self.module.main(["--db-path", str(link)])
        self.assertEqual(prod.stat().st_mtime_ns, before, "file must be untouched")

    async def test_a_genuinely_different_path_is_still_accepted(self):
        """Sanity: the guard must not over-refuse -- a throwaway path that is
        not the configured one seeds normally."""
        prod = str(Path(self.tmp.name) / "prod.db")
        scratch = str(Path(self.tmp.name) / "scratch.db")
        with patch.object(config, "DB_PATH", prod):
            rc = await self.module.main(["--db-path", scratch])
        self.assertEqual(rc, 0)
        self.assertTrue(Path(scratch).exists())


class SeedRowCoverageTests(unittest.TestCase):
    """The seed script's ROWS must cover every (model, task_type) pair in
    spec 2.6's table -- no more, no less. Round 1 of review found 6 of 23
    rows silently missing; this counts both sides from source so a future
    edit to either the spec table or ROWS that drifts from the other fails
    loudly instead of shipping quietly."""

    def _spec_pairs(self) -> set[tuple[str, str]]:
        text = SPEC_FILE.read_text(encoding="utf-8")
        start = text.index("### 2.6 Model benchmark table")
        end = text.index("**`max_context` is the input window", start)
        table = text[start:end]
        pairs = set()
        for line in table.splitlines():
            if not line.startswith("| `"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            model = cells[0].strip("`")
            task_type = cells[1]
            pairs.add((model, task_type))
        return pairs

    def test_spec_pairs_parsed_sanely(self):
        # Guards the parser itself: if the table's shape ever changes enough
        # that this stops finding 23 rows, the pairs test below would pass
        # trivially on an empty set rather than failing usefully.
        self.assertEqual(len(self._spec_pairs()), 23)

    def test_every_spec_2_6_row_has_a_seeded_row(self):
        module = _load_seed_module()
        seeded = {(model, task_type) for model, task_type, *_ in module.ROWS}
        missing = self._spec_pairs() - seeded
        self.assertEqual(missing, set(), f"spec 2.6 rows missing from ROWS: {sorted(missing)}")

    def test_the_seed_script_adds_no_row_the_spec_does_not_have(self):
        module = _load_seed_module()
        seeded = {(model, task_type) for model, task_type, *_ in module.ROWS}
        extra = seeded - self._spec_pairs()
        self.assertEqual(extra, set(), f"seeded rows not in spec 2.6: {sorted(extra)}")


if __name__ == "__main__":
    unittest.main()
