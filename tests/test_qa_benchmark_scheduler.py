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

import benchmark_sweep
import config
import db
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
