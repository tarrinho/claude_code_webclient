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

    async def test_the_configured_path_is_accepted_with_the_override_flag(self):
        """`--yes-this-is-production` is the sanctioned way to cross the
        guard -- production's table is genuinely supposed to hold spec 2.6's
        rows, and §9.2's alternative is 115 cells by hand. `config.DB_PATH`
        is patched to a throwaway temp file, never the real default, so this
        never touches the actual production database."""
        prod = str(Path(self.tmp.name) / "prod.db")
        with patch.object(config, "DB_PATH", prod):
            rc = await self.module.main(
                ["--db-path", prod, "--yes-this-is-production"])
        self.assertEqual(rc, 0)
        self.assertTrue(Path(prod).exists())

    async def test_the_override_flag_alone_is_not_sufficient(self):
        """`--db-path` stays required even with the override present --
        seeding production must never happen by a single flag alone."""
        with patch.object(config, "DB_PATH", str(Path(self.tmp.name) / "prod.db")):
            with self.assertRaises(SystemExit):
                await self.module.main(["--yes-this-is-production"])

    async def test_the_override_flag_does_not_change_a_non_production_seed(self):
        """The flag is an affirmation, not a mode: passing it while seeding a
        throwaway database must behave identically to not passing it."""
        prod = str(Path(self.tmp.name) / "prod.db")
        scratch = str(Path(self.tmp.name) / "scratch.db")
        with patch.object(config, "DB_PATH", prod):
            rc = await self.module.main(
                ["--db-path", scratch, "--yes-this-is-production"])
        self.assertEqual(rc, 0)
        self.assertTrue(Path(scratch).exists())
        self.assertFalse(Path(prod).exists(), "the flag must not touch production")


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
        # that this stops finding the expected rows, the pairs test below
        # would pass trivially on an empty set rather than failing usefully.
        #
        # 34 = 23 original, plus azure_ai/gpt-5.6-terra's six (2026-09-17),
        # plus four net from the gate split the same day (the two pooled
        # `reviewer-gate` rows became three `reviewer-gate` and three
        # `security-gate` rows, now that opus is measured on both), plus
        # azure_ai/gpt-5.4-mini-copilot's voice row -- the model that actually
        # serves voice, which had no row at all until then.
        # 40 = 34, plus azure_ai/gpt-5.6-sol's six (2026-09-17). sol is
        # what takes planning's tree cost from $1.936 to $0.724, by
        # demoting sonnet from rung 1 to rung 2 rather than removing it.
        self.assertEqual(len(self._spec_pairs()), 40)

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


def _spec_cell(cell: str) -> str | None:
    """A cell's raw text, or None for spec 2.6's two spellings of "absent"
    (`TBD` and `--`). Both mean the same thing here -- section 2.6 uses `TBD`
    for "not measured yet" and an em dash for "not applicable" (an `n` next to
    an unmeasured accuracy), and this table has no row where the distinction
    between the two would change what a seeded value should be."""
    cell = cell.strip()
    return None if cell in ("TBD", "—") else cell


def _spec_pct(cell: str) -> float | None:
    """`"66%"` -> `0.66`, matching the fraction the seed script stores."""
    v = _spec_cell(cell)
    return None if v is None else round(float(v.rstrip("%")) / 100, 6)


def _spec_n(cell: str) -> int | None:
    """`n`, stripped of its trailing marker. Two exist: `*` for a latency-only
    sample (spec 2.6: "an `n` marked with `*` is a latency sample, not an
    accuracy sample") and `§` for an accuracy measured over the CLI transport
    rather than the voice transport (2026-09-17). The seed script stores the
    count either way, so neither marker carries a value to compare here."""
    v = _spec_cell(cell)
    return None if v is None else int(v.rstrip("*§"))


def _spec_float(cell: str) -> float | None:
    """A plain decimal cell (`cost_per_1M_tokens`, `median_latency_s`).

    Both columns may carry a trailing marker, and there are three:
    `†` for "assumed, not billed" (2026-09-17, `azure_ai/gpt-5.6-terra`),
    `‡` for "blended from this deployment's own usage_events rather than a
    published price" (2026-09-17, `azure_ai/gpt-5.4-mini-copilot`), and `◊`
    for a `median_latency_s` borrowed from the CLI transport because the model
    has never served a voice turn (2026-09-18, both voice rungs). All three are
    distinct from `_spec_n`'s `*`, so all three are stripped here rather than
    folded into that one. The seed script stores the number either way; a
    marker carries no value to compare.

    This function has now been the failure point twice in one day: first on
    `**1.4760‡**` when a correction table was added to 2.6, and then on
    `11.0◊` when a marker was introduced on a column that had never carried
    one. Adding a marker to 2.6 means adding it here in the same change."""
    v = _spec_cell(cell)
    return None if v is None else float(v.rstrip("†‡◊").replace(",", ""))


def _spec_int(cell: str) -> int | None:
    """An integer cell (`max_context`) written with thousands separators."""
    v = _spec_cell(cell)
    return None if v is None else int(v.replace(",", ""))


class SeedRowValuesTests(unittest.TestCase):
    """The seed script must carry spec 2.6's *measured values*, not only the
    right (model, task_type) pairs.

    `SeedRowCoverageTests` above only ever compared
    `{(model, task_type) for model, task_type, *_ in module.ROWS}` -- the
    `*_` discards accuracy, n, cost, latency and max_context outright, so a
    seed row could carry any numbers at all, including stale ones, and that
    test would stay green. That is exactly how `claude-sonnet-5` /
    `reviewer-gate` shipped with `median_latency_s=None` after spec 2.6 was
    updated on 2026-09-16 to record 20* / 3.675s (pooled from three passes) --
    the pair matched, so nothing here ever compared the value.

    This parses spec 2.6's table properly, honouring its own conventions
    (`_spec_cell`/`_spec_pct`/`_spec_n`/`_spec_float`/`_spec_int` above):
    `TBD` and an em dash both mean absent, an `n` may carry a trailing `*`
    meaning a latency sample rather than an accuracy sample, and numbers are
    written with thousands separators. It then asserts every seeded tuple
    equals the spec row for the same (model, task_type) pair, column by
    column, so a value can no longer drift silently.
    """

    def _spec_rows(self) -> dict[tuple[str, str], tuple]:
        text = SPEC_FILE.read_text(encoding="utf-8")
        start = text.index("### 2.6 Model benchmark table")
        end = text.index("**`max_context` is the input window", start)
        table = text[start:end]
        rows: dict[tuple[str, str], tuple] = {}
        for line in table.splitlines():
            if not line.startswith("| `"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            model = cells[0].strip("`")
            task_type = cells[1]
            rows[(model, task_type)] = (
                _spec_pct(cells[2]),
                _spec_n(cells[3]),
                _spec_float(cells[4]),
                _spec_float(cells[5]),
                _spec_int(cells[6]),
            )
        return rows

    def test_every_seeded_row_matches_its_spec_2_6_values(self):
        module = _load_seed_module()
        spec_rows = self._spec_rows()
        columns = ("accuracy", "n", "cost_per_1m_tokens", "median_latency_s",
                   "max_context")
        for model, task_type, accuracy, n, cost, latency, context in module.ROWS:
            seeded = (accuracy, n, cost, latency, context)
            expected = spec_rows[(model, task_type)]
            for column, got, want in zip(columns, seeded, expected):
                with self.subTest(model=model, task_type=task_type, column=column):
                    if isinstance(want, float):
                        self.assertIsNotNone(
                            got,
                            f"{model} / {task_type}: {column} is None in ROWS "
                            f"but spec 2.6 has {want!r}")
                        self.assertAlmostEqual(
                            got, want, places=6,
                            msg=f"{model} / {task_type}: {column} is {got!r} "
                                f"in ROWS but spec 2.6 has {want!r}")
                    else:
                        self.assertEqual(
                            got, want,
                            f"{model} / {task_type}: {column} is {got!r} in "
                            f"ROWS but spec 2.6 has {want!r}")


if __name__ == "__main__":
    unittest.main()
