"""QA: the nightly decision, cooling, and expiry.

Spec 8.1: a sweep is current from its start until two days after it finishes,
so the scheduler asks one question -- is there a current sweep -- and acts.
Both cooling states are asserted, because a cooling window that never elapses
would stop every future sweep and a cooling window that never applies would
mean the box benchmarks every single night.

Every window here is set by fixture rather than by waiting on a clock. That is
the whole reason `expires_at` and `cooling_until` are stored on the run row
instead of being recomputed when the scheduler wakes.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import benchmark_cell
import benchmark_sweep
import config
import db
from benchmark_cell import CellResult
from routes import db_benchmark as store
from routes.db_delegation import delegation_row_set


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        await delegation_row_set("m1", "coding")

    async def test_no_sweep_at_all_starts_one(self):
        action, _ = await benchmark_sweep.scheduled_decision()
        self.assertEqual(action, "start")

    async def test_a_running_sweep_is_advanced(self):
        await benchmark_sweep.start_sweep(["m1"], ["coding"])
        action, run = await benchmark_sweep.scheduled_decision()
        self.assertEqual(action, "advance")
        self.assertEqual(run["status"], "running")

    async def test_a_cooling_sweep_produces_no_measurement(self):
        run = await benchmark_sweep.start_sweep(["m1"], ["coding"])
        await store.run_finish(run["id"], db._now(), "2999-01-01T00:00:00Z")
        action, _ = await benchmark_sweep.scheduled_decision()
        self.assertEqual(action, "cooling")

    async def test_a_sweep_whose_cooling_elapsed_starts_a_new_one(self):
        """The direction that stops one finished sweep blocking all others."""
        run = await benchmark_sweep.start_sweep(["m1"], ["coding"])
        await store.run_finish(run["id"], "2026-09-01T05:00:00Z",
                               "2026-09-03T05:00:00Z")
        action, _ = await benchmark_sweep.scheduled_decision()
        self.assertEqual(action, "start")

    async def test_an_expired_sweep_starts_a_new_one_without_cooling(self):
        """Expiry does not serve a cooling period: an expired sweep is by
        definition one that failed to keep the table current (spec 8.2)."""
        run = await benchmark_sweep.start_sweep(["m1"], ["coding"])
        await store.run_expire(run["id"])
        action, _ = await benchmark_sweep.scheduled_decision()
        self.assertEqual(action, "start")

    async def test_a_sweep_past_its_expiry_is_expired_and_keeps_its_cells(self):
        run = await benchmark_sweep.start_sweep(["m1"], ["coding"])
        await db.db_conn.execute(
            "UPDATE benchmark_runs SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", run["id"]))
        await db.db_conn.commit()
        await store.capability_meta_set("m1", "coding",
                                        measured_at="2026-09-18T03:00:00Z")
        fresh = await store.run_get(run["id"])
        outcome = await benchmark_sweep.run_night(fresh)
        self.assertEqual(outcome, "expired")
        self.assertEqual((await store.run_get(run["id"]))["status"], "expired")
        meta = {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}
        self.assertEqual(meta[("m1", "coding")]["measured_at"],
                         "2026-09-18T03:00:00Z")

    async def test_a_busy_box_stops_the_night_without_measuring(self):
        run = await benchmark_sweep.start_sweep(["m1"], ["coding"])
        with patch.object(benchmark_sweep, "box_is_busy",
                          new_callable=AsyncMock,
                          return_value=(True, "a turn is in flight")):
            outcome = await benchmark_sweep.run_night(run)
        self.assertEqual(outcome, "stopped")
        self.assertEqual((await store.run_get(run["id"]))["status"], "running")
        self.assertEqual(await store.cells_for_run(run["id"]), [])

    async def test_an_all_dormant_remainder_completes_immediately(self):
        run = await benchmark_sweep.start_sweep(["m1"], ["coding"])
        await store.capability_meta_set("m1", "coding", dormant=1)
        with patch.object(benchmark_sweep, "box_is_busy",
                          new_callable=AsyncMock,
                          return_value=(False, "idle")):
            outcome = await benchmark_sweep.run_night(await store.run_get(run["id"]))
        self.assertEqual(outcome, "complete")
        finished = await store.run_get(run["id"])
        self.assertEqual(finished["status"], "done")
        self.assertIsNotNone(finished["cooling_until"])
        self.assertEqual(finished["cells_dormant"], 1)
        self.assertEqual(finished["cells_total"], 1)

    async def test_a_failing_cell_is_attempted_at_most_once_per_night(self):
        """Spec 12 counts three CONSECUTIVE SWEEPS. Without a per-night
        attempt guard, a failed cell writes no capability row (spec 6), stays
        pending, and gets retried immediately -- burning the whole
        three-strike dormancy allowance, and up to 3 x MAX_CELL_BUDGET_S, on
        one night instead of three."""
        await delegation_row_set("bad", "coding")
        await delegation_row_set("good", "coding")
        run = await benchmark_sweep.start_sweep(["bad", "good"], ["coding"])
        calls: list[tuple[str, str]] = []

        async def fake_run_cell(model, task_type, repeats=3, timeout_s=None):
            calls.append((model, task_type))
            if model == "bad":
                return CellResult("failed", None, None, None, 0.1, "boom")
            return CellResult("ok", 0.9, 3, 1.0, 0.1, None)

        with patch.object(benchmark_cell, "run_cell", fake_run_cell), \
             patch.object(benchmark_sweep, "box_is_busy", new_callable=AsyncMock,
                          return_value=(False, "idle")):
            outcome = await benchmark_sweep.run_night(run)

        self.assertEqual(outcome, "stopped")
        self.assertEqual(calls.count(("bad", "coding")), 1)
        self.assertEqual(calls.count(("good", "coding")), 1)
        meta = {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}
        self.assertEqual(meta[("bad", "coding")]["consecutive_failures"], 1)
        self.assertEqual((await store.run_get(run["id"]))["status"], "running")

    async def test_record_failure_sets_dormant_with_no_capability_row_yet(self):
        """Fix 2 (blocker): a cell that has never once succeeded has no
        delegation_capability row -- `write_cell` is the only inserter, and
        record_failure's failure path never calls it. Every other fixture in
        this file pre-creates the row with `delegation_row_set` before
        failing it, which is exactly why this survived: against the old
        bare `UPDATE ... WHERE model=? AND task_type=?`, the UPDATE matches
        zero rows on a cell with no row yet, so `consecutive_failures` never
        advances and `dormant` never sets -- this test fails on that
        implementation with consecutive_failures stuck at 0 and dormant
        None/0 after all three calls. INSERT ... ON CONFLICT DO UPDATE fixes
        it by writing the counters whether or not a row already exists."""
        run = await benchmark_sweep.start_sweep(["never-measured"], ["coding"])
        result = CellResult("failed", None, None, None, 0.1, "boom")
        for _ in range(3):
            await benchmark_sweep.record_failure(
                run["id"], "never-measured", "coding", result)
        meta = {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}
        row = meta[("never-measured", "coding")]
        self.assertEqual(row["consecutive_failures"], 3)
        self.assertEqual(row["dormant"], 1)

    async def test_expiry_crossed_mid_night_stops_and_keeps_written_cells(self):
        """Fix 2: expiry is checked before EVERY cell, not only at loop
        entry, so a long night that crosses the ten-day boundary mid-run
        stops there instead of measuring on into the next invocation."""
        await delegation_row_set("m2", "coding")
        run = await benchmark_sweep.start_sweep(["m1", "m2"], ["coding"])

        async def fake_run_cell(model, task_type, repeats=3, timeout_s=None):
            if model == "m1":
                # Simulate the clock crossing expires_at partway through
                # tonight's run, after m1's cell has already been written.
                run["expires_at"] = "2000-01-01T00:00:00Z"
            return CellResult("ok", 0.9, 3, 1.0, 0.1, None)

        with patch.object(benchmark_cell, "run_cell", fake_run_cell), \
             patch.object(benchmark_sweep, "box_is_busy", new_callable=AsyncMock,
                          return_value=(False, "idle")):
            outcome = await benchmark_sweep.run_night(run)

        self.assertEqual(outcome, "expired")
        self.assertEqual((await store.run_get(run["id"]))["status"], "expired")
        cells = await store.cells_for_run(run["id"])
        self.assertEqual([(c["model"], c["task_type"]) for c in cells],
                         [("m1", "coding")])
