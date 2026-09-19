# tests/test_qa_benchmark_writer.py
"""QA: the single capability writer, and the one column it must protect.

Nothing reviews a measurement before it is written (spec 6), so the voice
latency exclusion in this writer is the only thing standing between a
mismeasured transport and every deadline spec v3 5.1 derives. The harness
measures over the CLI; routes/voice.py speaks to an OpenAI-compatible endpoint
directly, and the two disagree by 6.6-9.6s against ~2.0s.

The exclusion is asserted against a NON-voice task type as well, because a
writer that dropped `median_latency_s` for everything would satisfy the voice
assertion alone.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import benchmark_writer
import config
import db
from routes.db_delegation import delegation_row_set, delegation_rows_all
from routes import db_benchmark as store


class WriterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def _row(self, model, task_type):
        rows = {(r["model"], r["task_type"]): r for r in await delegation_rows_all()}
        return rows[(model, task_type)]

    async def test_a_normal_cell_writes_all_three_numbers(self):
        await delegation_row_set("m1", "coding", accuracy=0.5, n=2,
                                 median_latency_s=99.0)
        await benchmark_writer.write_cell(
            "m1", "coding", accuracy=1.0, n=3, median_latency_s=12.8,
            measured_at="2026-09-18T03:00:00Z")
        row = await self._row("m1", "coding")
        self.assertEqual(row["accuracy"], 1.0)
        self.assertEqual(row["n"], 3)
        self.assertEqual(row["median_latency_s"], 12.8)

    async def test_voice_keeps_its_previous_latency(self):
        """The exclusion, in the direction that catches its removal."""
        await delegation_row_set("m1", "voice", accuracy=0.5, n=2,
                                 median_latency_s=2.0)
        await benchmark_writer.write_cell(
            "m1", "voice", accuracy=0.9, n=6, median_latency_s=9.6,
            measured_at="2026-09-18T03:00:00Z")
        row = await self._row("m1", "voice")
        self.assertEqual(row["accuracy"], 0.9)
        self.assertEqual(row["n"], 6)
        self.assertEqual(row["median_latency_s"], 2.0,
                         "voice latency must not be overwritten from the CLI")

    async def test_voice_with_no_previous_latency_stays_null(self):
        await delegation_row_set("m1", "voice", accuracy=None, n=None,
                                 median_latency_s=None)
        await benchmark_writer.write_cell(
            "m1", "voice", accuracy=0.9, n=6, median_latency_s=9.6,
            measured_at="2026-09-18T03:00:00Z")
        row = await self._row("m1", "voice")
        self.assertIsNone(row["median_latency_s"])

    async def test_provenance_is_stamped(self):
        await delegation_row_set("m1", "coding")
        await benchmark_writer.write_cell(
            "m1", "coding", accuracy=1.0, n=3, median_latency_s=12.8,
            measured_at="2026-09-18T15:00:00Z", trigger="manual",
            under_load=True)
        meta = {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}[("m1", "coding")]
        self.assertEqual(meta["measured_at"], "2026-09-18T15:00:00Z")
        self.assertEqual(meta["trigger"], "manual")
        self.assertEqual(meta["measured_under_load"], 1)

    async def test_a_scheduled_write_overwrites_an_under_load_row(self):
        """Provenance records, it never gates. Both directions asserted so
        nobody later 'improves' the writer into refusing one of them."""
        await delegation_row_set("m1", "coding")
        await benchmark_writer.write_cell(
            "m1", "coding", accuracy=0.6, n=3, median_latency_s=30.0,
            measured_at="2026-09-18T15:00:00Z", trigger="manual",
            under_load=True)
        await benchmark_writer.write_cell(
            "m1", "coding", accuracy=1.0, n=3, median_latency_s=12.8,
            measured_at="2026-09-19T03:00:00Z", trigger="scheduled")
        row = await self._row("m1", "coding")
        self.assertEqual(row["median_latency_s"], 12.8)
        meta = {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}[("m1", "coding")]
        self.assertEqual(meta["measured_under_load"], 0)

    async def test_a_manual_write_overwrites_a_scheduled_row(self):
        await delegation_row_set("m1", "coding")
        await benchmark_writer.write_cell(
            "m1", "coding", accuracy=1.0, n=3, median_latency_s=12.8,
            measured_at="2026-09-19T03:00:00Z", trigger="scheduled")
        await benchmark_writer.write_cell(
            "m1", "coding", accuracy=0.6, n=3, median_latency_s=30.0,
            measured_at="2026-09-19T15:00:00Z", trigger="manual",
            under_load=True)
        row = await self._row("m1", "coding")
        self.assertEqual(row["median_latency_s"], 30.0)

    async def test_a_non_voice_cell_keeps_its_cost_and_context(self):
        """Regression test for the trap this task exists to avoid.

        `delegation_row_set` writes all five capability columns from its
        keyword arguments, defaulting any column it is not given to NULL.
        `write_cell` only receives accuracy, n and median_latency_s from its
        caller -- cost_per_1m_tokens and max_context never appear in its own
        signature. If write_cell forwarded its call to delegation_row_set
        without first reading back the existing row and re-passing those two
        columns, this test would fail (they would come back None) while
        every other test in this file -- none of which touches cost or
        context -- would still pass. That silent gap is exactly what the
        cost ceiling and context checks elsewhere in the system depend on
        never happening.
        """
        await delegation_row_set("m1", "coding", accuracy=0.5, n=2,
                                 median_latency_s=99.0,
                                 cost_per_1m_tokens=3.5, max_context=200000)
        await benchmark_writer.write_cell(
            "m1", "coding", accuracy=1.0, n=3, median_latency_s=12.8,
            measured_at="2026-09-18T03:00:00Z")
        row = await self._row("m1", "coding")
        self.assertEqual(row["cost_per_1m_tokens"], 3.5)
        self.assertEqual(row["max_context"], 200000)


if __name__ == "__main__":
    unittest.main()
