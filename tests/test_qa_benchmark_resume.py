"""QA: resume, and the dormancy rules that keep a sweep from deadlocking.

Resume derives entirely from delegation_capability.measured_at (spec 6): a cell
is done when its row was written at or after the sweep started. There is no
status ledger, and this file asserts the absence -- a cell with a `benchmark_cells`
row saying 'ok' but no fresh `measured_at` is PENDING.

Dormancy (spec 12) is what stops three permanently-failing copilot cells
holding a sweep open until its ten-day expiry on every cycle. The transitions
are asserted in all three directions, and the all-dormant remainder is asserted
to COMPLETE rather than wait -- spec 12.2, and the reason is that nothing
scheduled can ever clear dormancy.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import benchmark_sweep
import config
import db
from benchmark_cell import CellResult
from routes import db_benchmark as store
from routes.db_delegation import delegation_row_set

START = "2026-09-18T02:00:00Z"


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        for model in ("m1", "m2"):
            await delegation_row_set(model, "coding")
        await store.run_create(
            run_id="r1", started_at=START, expires_at="2026-09-28T02:00:00Z",
            models=["m1", "m2"], task_types=["coding"], repeats=3,
            cells_total=2)
        self.run = await store.run_get("r1")

    async def test_a_cell_measured_after_the_start_is_done(self):
        await store.capability_meta_set("m1", "coding",
                                        measured_at="2026-09-18T03:00:00Z")
        groups = await benchmark_sweep.classify_cells(self.run)
        self.assertIn(("m1", "coding"), groups["done"])
        self.assertIn(("m2", "coding"), groups["pending"])

    async def test_a_cell_measured_before_the_start_is_pending(self):
        await store.capability_meta_set("m1", "coding",
                                        measured_at="2026-09-01T03:00:00Z")
        groups = await benchmark_sweep.classify_cells(self.run)
        self.assertIn(("m1", "coding"), groups["pending"])

    async def test_a_cells_row_alone_does_not_make_a_cell_done(self):
        """There is no status ledger. Only measured_at decides."""
        await store.cell_record(
            run_id="r1", model="m1", task_type="coding", status="ok",
            accuracy=1.0, n=3, median_latency_s=12.0, elapsed_s=60.0,
            error=None)
        groups = await benchmark_sweep.classify_cells(self.run)
        self.assertIn(("m1", "coding"), groups["pending"])

    async def test_a_dormant_cell_is_neither_done_nor_pending(self):
        await store.capability_meta_set("m1", "coding", dormant=1)
        groups = await benchmark_sweep.classify_cells(self.run)
        self.assertIn(("m1", "coding"), groups["dormant"])
        self.assertNotIn(("m1", "coding"), groups["pending"])
        self.assertNotIn(("m1", "coding"), groups["done"])


class DormancyTests(ResumeTests):
    def _failure(self):
        return CellResult("failed", None, None, None, 900.0, "timeout after 900s")

    async def _fail_n_times(self, times):
        for _ in range(times):
            await benchmark_sweep.record_failure("r1", "m1", "coding",
                                                 self._failure())

    async def _meta(self, model="m1"):
        return {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}[(model, "coding")]

    async def test_two_failures_do_not_make_a_cell_dormant(self):
        await self._fail_n_times(2)
        meta = await self._meta()
        self.assertEqual(meta["consecutive_failures"], 2)
        self.assertEqual(meta["dormant"], 0)

    async def test_three_consecutive_failures_set_dormant(self):
        await self._fail_n_times(3)
        meta = await self._meta()
        self.assertEqual(meta["dormant"], 1)

    async def test_a_success_before_the_third_resets_the_counter(self):
        await self._fail_n_times(2)
        await benchmark_sweep.record_success(
            "r1", "m1", "coding",
            CellResult("ok", 1.0, 3, 12.0, 60.0, None),
            trigger="scheduled", under_load=False)
        meta = await self._meta()
        self.assertEqual(meta["consecutive_failures"], 0)
        self.assertEqual(meta["dormant"], 0)

    async def test_a_failure_always_writes_a_cells_row(self):
        """Dormancy counts these; a failure leaving none is uncountable."""
        await self._fail_n_times(1)
        cells = await store.cells_for_run("r1")
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0]["status"], "failed")

    async def test_a_sweep_whose_remainder_is_dormant_is_complete(self):
        """Spec 12.2. Waiting would block the next sweep for ten days, and
        nothing scheduled can clear dormancy."""
        await store.capability_meta_set("m1", "coding", dormant=1)
        await store.capability_meta_set("m2", "coding", dormant=1)
        run = await store.run_get("r1")
        self.assertTrue(await benchmark_sweep.sweep_is_complete(run))

    async def test_a_sweep_with_one_pending_cell_is_not_complete(self):
        await store.capability_meta_set("m1", "coding", dormant=1)
        run = await store.run_get("r1")
        self.assertFalse(await benchmark_sweep.sweep_is_complete(run))
