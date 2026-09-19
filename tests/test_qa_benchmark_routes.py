# tests/test_qa_benchmark_routes.py
"""QA: the three benchmark endpoints.

No model is spawned anywhere here -- `run_cell` is patched. The refusal rule is
asserted against the SAME cell and the permitted case against a DIFFERENT one,
because a test using one cell for both cannot tell "refuse on collision" from
"refuse whenever a sweep is running", and the second would make the button
useless for most of the night.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import benchmark_cell
import benchmark_reorder
import config
import db
from benchmark_cell import CellResult
from routes import db_benchmark as store
from routes.db_delegation import delegation_row_set, delegation_rows_all


class BenchmarkRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        await delegation_row_set("m1", "coding")
        await delegation_row_set("m2", "coding")

    async def test_a_forced_cell_is_stamped_manual_and_under_load(self):
        import routes.benchmark as rb
        ok = CellResult("ok", 1.0, 3, 12.8, 60.0, None)
        with patch.object(benchmark_cell, "run_cell", AsyncMock(return_value=ok)):
            await rb.measure_one_cell("m1", "coding")
        meta = {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}[("m1", "coding")]
        self.assertEqual(meta["trigger"], "manual")
        self.assertEqual(meta["measured_under_load"], 1)

    async def test_a_forced_cell_clears_dormancy(self):
        import routes.benchmark as rb
        await store.capability_meta_set("m1", "coding", dormant=1,
                                        consecutive_failures=3)
        failed = CellResult("failed", None, None, None, 5.0, "nope")
        with patch.object(benchmark_cell, "run_cell", AsyncMock(return_value=failed)):
            await rb.measure_one_cell("m1", "coding")
        meta = {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}[("m1", "coding")]
        self.assertEqual(meta["dormant"], 0,
                         "a manual re-measure clears dormancy even on failure")

    async def test_the_same_cell_is_refused_while_the_sweep_holds_it(self):
        import routes.benchmark as rb
        rb._IN_FLIGHT.add(("m1", "coding"))
        self.addCleanup(rb._IN_FLIGHT.discard, ("m1", "coding"))
        with self.assertRaises(Exception):
            await rb.guard_cell("m1", "coding")

    async def test_a_different_cell_is_permitted_while_the_sweep_runs(self):
        import routes.benchmark as rb
        rb._IN_FLIGHT.add(("m1", "coding"))
        self.addCleanup(rb._IN_FLIGHT.discard, ("m1", "coding"))
        await rb.guard_cell("m2", "coding")   # must not raise


class BenchmarkRoutesReorderTests(unittest.IsolatedAsyncioTestCase):
    """Not in the plan's Task 9 section. `measure_one_cell` wires
    `benchmark_reorder.flag_reorderings` into the manual re-measure path
    (spec 13), but the plan's own tests never drive a measurement that
    actually reorders a ladder, so nothing proves the wiring fires. This can
    fail if `measure_one_cell` computes `before`/`after` at the wrong point
    (e.g. both after the write, so they're always equal) or forgets to pass
    the measured `task_type` into `flag_reorderings`.

    Built the way Task 7's `test_a_reordering_flags_the_row` was: `ladder()`
    (tiered_delegation.py) sorts candidates cheapest-first and only ever
    walks that cost order, so two rungs that both survive can swap places
    only when a COST changes -- never from an accuracy change alone (that
    can only add/remove rungs from the tail, per Task 7's own reasoning).
    `measure_one_cell` itself never touches cost -- `benchmark_writer.
    write_cell` preserves the existing `cost_per_1m_tokens` (benchmark_
    writer.py:53) -- so the cost change has to happen DURING the manual
    remeasurement of "cheap", between `measure_one_cell`'s `before` snapshot
    and its `after` snapshot. The patched `run_cell` does that as a side
    effect: it raises "cheap"'s own cost past "dear"'s before returning its
    (unrelated) accuracy result, mimicking an operator editing the cost row
    in one tab while a re-measure runs in another. Because the row write
    that follows (`write_cell`) reads "existing" cost AFTER that side effect
    ran, the bumped cost is what ends up on the after-row too.
    """
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        # Equal accuracy so both rungs are always kept regardless of which
        # comes first (ties are kept, per ladder()'s own rule) -- only their
        # cost-sorted order can flip. "cheap" leads at the start.
        await delegation_row_set("cheap", "coding", accuracy=0.9, n=6,
                                 cost_per_1m_tokens=0.01,
                                 median_latency_s=10.0, max_context=900_000)
        await delegation_row_set("dear", "coding", accuracy=0.9, n=6,
                                 cost_per_1m_tokens=0.02,
                                 median_latency_s=12.0, max_context=1_000_000)

    async def _meta(self, model):
        return {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}[(model, "coding")]

    async def test_a_forced_cell_that_reorders_a_ladder_flags_the_row(self):
        import routes.benchmark as rb

        before_ladder = benchmark_reorder._ladder_for(
            await delegation_rows_all(), "coding")

        async def _bump_cost_then_report(model, task_type):
            # Runs between measure_one_cell's before/after snapshots -- the
            # cost change that causes the flip, not the accuracy result.
            await delegation_row_set("cheap", "coding", accuracy=0.9, n=6,
                                     cost_per_1m_tokens=0.05,
                                     median_latency_s=10.0, max_context=900_000)
            return CellResult("ok", 0.9, 6, 10.0, 30.0, None)

        with patch.object(benchmark_cell, "run_cell",
                         AsyncMock(side_effect=_bump_cost_then_report)):
            await rb.measure_one_cell("cheap", "coding")

        after_ladder = benchmark_reorder._ladder_for(
            await delegation_rows_all(), "coding")
        print(f"before ladder: {before_ladder}")
        print(f"after  ladder: {after_ladder}")
        self.assertNotEqual(before_ladder, after_ladder,
                            "fixture must actually provoke a reordering")

        self.assertEqual((await self._meta("cheap"))["reorder_flagged"], 1)
        self.assertEqual((await self._meta("dear"))["reorder_flagged"], 1)
