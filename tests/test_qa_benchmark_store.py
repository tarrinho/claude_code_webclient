# tests/test_qa_benchmark_store.py
"""QA: the benchmark storage layer.

`run_current` is the scheduler's only question (spec 8.1), so it is tested in
both directions: a cooling run must be current and an elapsed one must not.
Testing only the first would pass for an implementation that calls every
finished run current, which would stop new sweeps forever.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import db
from routes import db_benchmark as store


class BenchmarkStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def _run(self, run_id="r1", started="2026-09-18T02:00:00Z",
                   expires="2026-09-28T02:00:00Z"):
        await store.run_create(
            run_id=run_id, started_at=started, expires_at=expires,
            models=["m1"], task_types=["coding"], repeats=3, cells_total=1)
        return run_id

    async def test_a_running_sweep_is_current(self):
        await self._run()
        current = await store.run_current()
        self.assertIsNotNone(current)
        self.assertEqual(current["id"], "r1")

    async def test_a_cooling_sweep_is_current(self):
        await self._run()
        await store.run_finish("r1", "2026-09-20T05:00:00Z",
                               cooling_until="2999-01-01T00:00:00Z")
        current = await store.run_current()
        self.assertIsNotNone(current)
        self.assertEqual(current["status"], "done")

    async def test_a_sweep_whose_cooling_elapsed_is_not_current(self):
        """The direction that stops a finished sweep blocking every later one."""
        await self._run()
        await store.run_finish("r1", "2026-09-20T05:00:00Z",
                               cooling_until="2000-01-01T00:00:00Z")
        self.assertIsNone(await store.run_current())

    async def test_an_expired_sweep_is_not_current(self):
        await self._run()
        await store.run_expire("r1")
        self.assertIsNone(await store.run_current())

    async def test_capability_meta_round_trips(self):
        from routes.db_delegation import delegation_row_set
        await delegation_row_set("m1", "coding", accuracy=1.0, n=3)
        await store.capability_meta_set(
            "m1", "coding", measured_at="2026-09-18T03:00:00Z",
            trigger="manual", measured_under_load=1)
        rows = {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}
        row = rows[("m1", "coding")]
        self.assertEqual(row["measured_at"], "2026-09-18T03:00:00Z")
        self.assertEqual(row["trigger"], "manual")
        self.assertEqual(row["measured_under_load"], 1)

    async def test_a_failed_cell_still_writes_a_cells_row(self):
        """Dormancy counts these rows; a failure leaving none is uncountable."""
        await self._run()
        await store.cell_record(
            run_id="r1", model="m1", task_type="coding", status="failed",
            accuracy=None, n=None, median_latency_s=None,
            elapsed_s=900.0, error="timeout after 900s")
        cells = await store.cells_for_run("r1")
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0]["status"], "failed")
        self.assertEqual(cells[0]["error"], "timeout after 900s")
