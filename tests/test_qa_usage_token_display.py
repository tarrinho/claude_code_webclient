"""QA: every section of the Usage page answers the same question.

The page reported three different things and called them all tokens:

* the header summed ``input_tokens + output_tokens``;
* the origin rows took that and subtracted re-read context, which cut terminal
  usage from 10.24 billion to 148 million and made it read as roughly 12x
  smaller than the website when it had actually processed several times more;
* the per-model table showed input and output as separate columns and neither
  of the first two figures.

None of them was labelled, so there was no way to tell they disagreed, and all
three left out ``cache_read_tokens`` entirely -- 53.1 billion of the 67.2
billion this deployment had processed, four fifths of the real number, missing
from a figure presented as complete.

There are now exactly two numbers, defined once in SQL and never recomputed in
the browser:

``new``
    what was not re-read context. For a model reporting a cache breakdown that
    is ``input_tokens``, which already excludes the cached prefix. For a model
    reporting none, ``input_tokens`` is the whole conversation re-sent every
    turn and the split is genuinely unknown, so it is excluded and the page
    says so rather than guessing. Output is always new.

``total``
    everything read and written, cache included.

The property that matters most is the last case: the sections have to add up.
A breakdown that does not sum to its own header is how the original defect hid
in plain sight.
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db

ROOT = Path(__file__).resolve().parents[1]
USAGE_JS = ROOT / "web" / "assets" / "usage.js"


class UsageTokenMathQA(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.db_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.tmp.cleanup()

    async def _add(self, *, origin, model="claude-opus-5", inp=0, out=0,
                   cache_read=0, cache_creation=0, unsplit=0, session=""):
        await db.db_conn.execute(
            "INSERT INTO usage_events (chat_id, session_id, owner_id, model, "
            " provider, input_tokens, output_tokens, cache_read_tokens, "
            " cache_creation_tokens, is_error, created_at, origin, "
            " context_unsplit) "
            "VALUES ('c1', ?, 'admin', ?, 'cli', ?, ?, ?, ?, 0, "
            "        '2026-09-21T00:00:00Z', ?, ?)",
            (session, model, inp, out, cache_read, cache_creation, origin,
             unsplit),
        )
        await db.db_conn.commit()

    async def test_total_counts_cache_and_new_does_not(self):
        """The headline defect: cache reads were simply absent."""
        await self._add(origin="web", inp=10, out=5,
                        cache_read=1000, cache_creation=100)
        overall = await db.usage_overall("admin", None)
        self.assertEqual(overall["new_tokens"], 15)
        self.assertEqual(overall["total_tokens"], 1115)

    async def test_unsplit_input_counts_towards_total_but_not_new(self):
        """It was processed, so it is real; how much was new is unknowable.

        The old page subtracted it from the only figure it showed, which is
        what made a terminal that had processed billions look negligible.
        """
        await self._add(origin="terminal", session="s1",
                        inp=100_000, out=50, unsplit=1)
        overall = await db.usage_overall("admin", None)
        self.assertEqual(overall["new_tokens"], 50)
        self.assertEqual(overall["total_tokens"], 100_050)
        self.assertEqual(overall["unsplit_tokens"], 100_000)

    async def test_a_cache_aware_row_counts_its_input_as_new(self):
        """The complement of the case above, so the CASE cannot be inverted."""
        await self._add(origin="web", inp=100_000, out=50,
                        cache_read=900_000, unsplit=0)
        overall = await db.usage_overall("admin", None)
        self.assertEqual(overall["new_tokens"], 100_050)
        self.assertEqual(overall["total_tokens"], 1_000_050)

    async def test_the_breakdowns_sum_to_the_header(self):
        """The property the original defect violated silently.

        Deliberately mixes cache-aware and cache-blind rows across three
        origins and two models, so a formula that is right for one shape and
        wrong for another cannot pass.
        """
        await self._add(origin="web", inp=10, out=5, cache_read=100)
        await self._add(origin="terminal", session="s1", inp=7000, out=9,
                        unsplit=1)
        await self._add(origin="terminal", session="s2", inp=20, out=3,
                        cache_read=40, cache_creation=6)
        await self._add(origin="web-routed", model="claude-sonnet-5",
                        inp=30, out=4, cache_read=11)

        overall = await db.usage_overall("admin", None)
        origins = await db.usage_by_origin("admin", None)
        models = await db.usage_totals("admin", None)

        for label, rows in (("origin", origins), ("model", models)):
            self.assertEqual(
                sum(r["new_tokens"] for r in rows), overall["new_tokens"],
                f"{label} rows do not sum to the header's new total")
            self.assertEqual(
                sum(r["total_tokens"] for r in rows), overall["total_tokens"],
                f"{label} rows do not sum to the header's processed total")
            self.assertEqual(
                sum(r["requests"] for r in rows), overall["requests"],
                f"{label} rows do not sum to the header's request count")

    async def test_origins_are_ordered_by_what_the_page_leads_with(self):
        """Ordering by a figure the page no longer shows is how a list ends up
        disagreeing with its own numbers."""
        await self._add(origin="web", inp=1_000_000, out=0)
        await self._add(origin="terminal", session="s1", inp=1, out=0,
                        cache_read=9_000_000)
        rows = await db.usage_by_origin("admin", None)
        self.assertEqual([r["origin"] for r in rows], ["terminal", "web"])

    async def test_cost_reports_how_much_of_the_traffic_it_covers(self):
        """A total summed from 0.16% of rows is a sample presented as a total."""
        await self._add(origin="web", inp=1, out=1)
        await db.db_conn.execute(
            "INSERT INTO usage_events (chat_id, owner_id, model, provider, "
            " input_tokens, output_tokens, cache_read_tokens, "
            " cache_creation_tokens, is_error, created_at, origin, cost_usd) "
            "VALUES ('c1', 'admin', 'claude-opus-5', 'cli', 1, 1, 0, 0, 0, "
            "        '2026-09-21T00:00:00Z', 'web', 2.5)")
        await db.db_conn.commit()
        row = next(r for r in await db.usage_totals("admin", None)
                   if r["model"] == "claude-opus-5")
        self.assertEqual(row["requests"], 2)
        self.assertEqual(row["costed_requests"], 1)


class UsageRenderWiringQA(unittest.TestCase):
    """The browser must not recompute either figure.

    Source assertions, and weak ones by nature -- see
    tests/test_qa_chat_active_float.py for what that costs. They are here for
    one narrow property that is genuinely about the source: the old file did
    its own arithmetic, and a second definition of "how many tokens" is exactly
    what let the page disagree with itself. The numbers themselves are covered
    behaviourally above.
    """

    SOURCE = USAGE_JS.read_text()

    def test_the_client_does_not_re_derive_the_totals(self):
        self.assertNotIn("input_tokens || 0) + (row.output_tokens", self.SOURCE,
                         "usage.js is summing its own total again")
        self.assertNotIn("total - unsplit", self.SOURCE,
                         "usage.js is re-deriving the comparable figure")

    def test_both_figures_are_read_from_the_server(self):
        self.assertIn("row.new_tokens", self.SOURCE)
        self.assertIn("row.total_tokens", self.SOURCE)
        self.assertIn("overall.new_tokens", self.SOURCE)
        self.assertIn("overall.total_tokens", self.SOURCE)

    def test_every_origin_the_server_can_emit_has_a_label(self):
        """voice, voice-summary and orchestrator rendered as raw strings."""
        block = self.SOURCE[self.SOURCE.index("const LABELS"):]
        block = block[:block.index("};")]
        for origin in ("web", "web-routed", "terminal", "voice",
                       "voice-summary", "orchestrator"):
            self.assertIn(origin, block, f"{origin} has no label")


if __name__ == "__main__":
    unittest.main()
