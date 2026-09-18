# tests/test_qa_benchmark_reorder.py
"""QA: reordering highlights, and the acknowledgement lifecycle.

A number changing is routine; a reordering changes an escalation order, and it
is the only event worth an operator's attention. Both directions are asserted:
a reorder must flag, and a pure value change must NOT -- a flagger that always
flagged would make the highlight meaningless and would still pass a
flag-only test.

`test_a_reordering_flags_the_row` builds its fixture from `ladder()`'s own
rule (tiered_delegation.py): candidates sort cheapest-first on effective cost
per task, then the walk skips a rung only when it is measured STRICTLY worse
than the current rung -- equal accuracy is kept. Changing one model's
accuracy alone can only add or remove rungs from the tail of that walk; it can
never swap two rungs that both survive, because accuracy never enters the
sort key. Only a cost change can change the relative order of two rungs that
both survive, so that is what this fixture changes: "cheap" and "dear" start
at equal accuracy (so both are always kept, whichever comes first) and
"cheap"'s cost is raised past "dear"'s, flipping which one sorts first. Both
models stay in both ladders; only their order swaps -- confirmed by printing
both ladders (see task-7-report.md) before this assertion was written.

Acknowledgement is asserted to survive a no-op sweep and to be RESET by a later
reordering. Acknowledging a reordering acknowledges that reordering, not the
cell forever (spec 13).
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import benchmark_reorder
import config
import db
from routes import db_benchmark as store
from routes.db_delegation import delegation_row_set, delegation_rows_all


class OrderTests(unittest.TestCase):
    def test_a_swap_is_a_reordering(self):
        self.assertTrue(
            benchmark_reorder.ladders_differ_in_order(["a", "b"], ["b", "a"]))

    def test_an_identical_ladder_is_not(self):
        self.assertFalse(
            benchmark_reorder.ladders_differ_in_order(["a", "b"], ["a", "b"]))

    def test_an_appended_rung_alone_is_not_a_reordering(self):
        """A new rung at the end changes the ladder without changing the
        relative order of anything that was already there."""
        self.assertFalse(
            benchmark_reorder.ladders_differ_in_order(["a", "b"], ["a", "b", "c"]))

    def test_a_removed_rung_alone_is_not_a_reordering(self):
        self.assertFalse(
            benchmark_reorder.ladders_differ_in_order(["a", "b", "c"], ["a", "c"]))


class FlagTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        # Two coding models, equal accuracy so BOTH always survive the walk
        # regardless of which one sorts first (ties are kept, per ladder()'s
        # own rule). "cheap" starts cheaper than "dear", so it leads.
        await delegation_row_set("cheap", "coding", accuracy=0.9, n=6,
                                 cost_per_1m_tokens=0.01,
                                 median_latency_s=10.0, max_context=900_000)
        await delegation_row_set("dear", "coding", accuracy=0.9, n=6,
                                 cost_per_1m_tokens=0.02,
                                 median_latency_s=12.0, max_context=1_000_000)

    async def _meta(self, model):
        return {(r["model"], r["task_type"]): r
                for r in await store.capability_meta_all()}[(model, "coding")]

    async def test_a_reordering_flags_the_row(self):
        before = await delegation_rows_all()
        # Raise "cheap"'s cost past "dear"'s. Accuracy is untouched and equal,
        # so both rungs still survive the walk -- only their cost-sorted order
        # flips. Printed and confirmed to differ before this line was written:
        #   before ladder: ['cheap', 'dear']
        #   after  ladder: ['dear', 'cheap']
        await delegation_row_set("cheap", "coding", accuracy=0.9, n=6,
                                 cost_per_1m_tokens=0.05,
                                 median_latency_s=10.0, max_context=900_000)
        after = await delegation_rows_all()
        changed = await benchmark_reorder.flag_reorderings(before, after, ["coding"])

        self.assertEqual(changed, ["coding"])
        self.assertEqual((await self._meta("cheap"))["reorder_flagged"], 1)
        self.assertEqual((await self._meta("dear"))["reorder_flagged"], 1)

    async def test_no_change_flags_nothing(self):
        rows = await delegation_rows_all()
        changed = await benchmark_reorder.flag_reorderings(rows, rows, ["coding"])
        self.assertEqual(changed, [])
        self.assertEqual((await self._meta("cheap"))["reorder_flagged"], 0)

    async def test_a_value_change_without_a_reordering_flags_nothing(self):
        """The counterpart to the reordering test: a number moving, on its
        own, with the order preserved, must not trip the highlight."""
        before = await delegation_rows_all()
        # Accuracy moves for both, cost untouched -- "cheap" still leads.
        await delegation_row_set("cheap", "coding", accuracy=0.95, n=6,
                                 cost_per_1m_tokens=0.01,
                                 median_latency_s=10.0, max_context=900_000)
        await delegation_row_set("dear", "coding", accuracy=0.99, n=6,
                                 cost_per_1m_tokens=0.02,
                                 median_latency_s=12.0, max_context=1_000_000)
        after = await delegation_rows_all()
        changed = await benchmark_reorder.flag_reorderings(before, after, ["coding"])

        self.assertEqual(changed, [])
        self.assertEqual((await self._meta("cheap"))["reorder_flagged"], 0)
        self.assertEqual((await self._meta("dear"))["reorder_flagged"], 0)

    async def test_acknowledgement_clears_the_flag(self):
        await store.capability_meta_set("cheap", "coding", reorder_flagged=1,
                                        reorder_seen_at="2026-09-18T03:00:00Z")
        await benchmark_reorder.acknowledge("cheap", "coding")
        meta = await self._meta("cheap")
        self.assertEqual(meta["reorder_flagged"], 0)
        self.assertIsNotNone(meta["reorder_acked_at"])

    async def test_a_later_reordering_resets_the_acknowledgement(self):
        await store.capability_meta_set("cheap", "coding", reorder_flagged=1,
                                        reorder_seen_at="2026-09-18T03:00:00Z")
        await benchmark_reorder.acknowledge("cheap", "coding")
        await benchmark_reorder.mark_reordered("cheap", "coding")
        meta = await self._meta("cheap")
        self.assertEqual(meta["reorder_flagged"], 1)
        self.assertIsNone(meta["reorder_acked_at"])
