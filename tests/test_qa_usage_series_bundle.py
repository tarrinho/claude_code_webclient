"""QA: one pass over usage_events must answer exactly what five passes did.

Why this exists. The statistics page took 6.4 to 19.1 seconds to load, and the
reason was not the database being slow at any one thing: `usage_series`,
`usage_model_series` and `usage_agent_series` ran **five** full scans of
usage_events between them -- one for the route series, and a ranking scan plus
a grouping scan for each of the model and agent series -- all reading the same
rows. Nothing could make them cheap individually, because the grouping key is
a computed expression (`datetime(created_at, 'localtime')`) that no index can
serve, so each scan ends in a temp B-tree.

Measured against a copy of the production database on 2026-09-15 (169,751
rows, 126 MB): the five scans took 19.1s for daily buckets, one combined scan
took 8.6s, and the Python folding that replaces the four dropped queries costs
0.00s.

`usage_series_bundle` is that one scan. These tests exist because it is a
rewrite of code whose output feeds four charts, and "faster" is worthless if
the numbers move. The three original functions are kept as the definition of
the correct shape, and every test here compares the bundle against them on the
same data rather than against expected values written by hand -- a hand-written
expectation would only prove the bundle matches my belief about the old
behaviour, which is exactly what a rewrite is likely to get wrong.

The fixture is deliberately awkward in the ways the real table is:

* Two ids for one model (`vllm/X` and `nvidia/X`), which must rank and fold as
  one series, because that normalisation is the reason ranking cannot happen
  in SQL.
* Rows carrying a session id, rows carrying only a chat id, and rows carrying
  neither -- the last must land in "Other" rather than being dropped, or the
  chart stops adding up to the page total.
* A row with `context_unsplit = 1`, whose input tokens are a re-read of the
  whole conversation and must be counted as unsplit rather than as billable.
* Rows with no `billing_route`, which is the state 99.9% of this table was in,
  so the route has to be inferred from the model id.
* More distinct models and agents than the top-N cut allows, so the "Other"
  bucket is exercised on both axes.
"""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path

_STAMP = "2026-09-14T{hour:02d}:{minute:02d}:00Z"


class UsageSeriesBundleEquivalenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wc-bundle-")) / "wc.db"
        os.environ["WC_DB_PATH"] = str(tmp)
        os.environ["WC_SESSION_DB_PATH"] = str(tmp.parent / "sessions.db")
        os.environ.setdefault("WC_PROXY_ENABLED", "0")
        import config

        importlib.reload(config)
        import db

        db.config = config
        self.db = db
        await db.init()
        await self._seed()

    async def asyncTearDown(self):
        await self.db.close()

    async def _seed(self):
        """Rows spanning both buckets, both routes, split/unsplit, and every
        shape of agent id."""
        rows = []
        # (model, session_id, chat_id, route, unsplit)
        shapes = [
            ("claude-opus-5", "sess-1", None, "subscription", 0),
            ("claude-opus-5", "sess-1", None, None, 0),
            ("claude-sonnet-5", "sess-2", None, None, 0),
            ("vllm/Qwen3.6-35B-A3B-NVFP4", "sess-3", None, "gateway", 0),
            # The same weights under the id the gateway reports back.
            ("nvidia/Qwen3.6-35B-A3B-NVFP4", "sess-3", None, "gateway", 0),
            ("azure_ai/gpt-5-mini", None, "chat-1", "gateway", 0),
            # No cache breakdown: input is the whole conversation re-read.
            ("vllm/Qwen3.6-35B-A3B-NVFP4", "sess-4", None, "gateway", 1),
            # Neither id: real spend attributable to nothing openable.
            # Production holds 0 such rows today, but usage_agent_series
            # documents the case and folds it into "Other", so the bundle
            # has to agree rather than diverge on an untravelled path.
            ("claude-opus-5", None, None, None, 0),
            # Enough extra models and agents to push past the top-N cuts.
            ("azure_ai/gpt-5.6-luna", "sess-5", None, "gateway", 0),
            ("azure_ai/gpt-5.4-mini", "sess-6", None, "gateway", 0),
            ("claude-haiku-4-5", "sess-7", None, "subscription", 0),
            ("vllm/Qwen3.5-0.8B", "sess-8", None, "gateway", 0),
            ("azure_ai/gpt-5-mini", "sess-9", None, "gateway", 0),
            ("claude-opus-5", "sess-10", None, "subscription", 0),
        ]
        for index, (model, session, chat, route, unsplit) in enumerate(shapes):
            for hour in (9, 21):        # two day-buckets apart in half-hours
                rows.append((
                    # chat_id is NOT NULL in this table, so "no chat" is the
                    # empty string -- which is what the agent key expression
                    # tests for with TRIM(), and what production holds.
                    # Both chat_id and billing_route are NOT NULL here, so
                    # "absent" is the empty string -- which is exactly what
                    # COALESCE(billing_route,'') in the queries reads, and the
                    # state 99.9% of production rows are in.
                    chat or "", session, "admin", model, route or "",
                    100 + index, 10 + index, 5 + index, 2 + index,
                    0.01 * (index + 1), unsplit,
                    _STAMP.format(hour=hour, minute=index * 3 % 60),
                ))
        for row in rows:
            await self.db.db_conn.execute(
                "INSERT INTO usage_events "
                "(chat_id, session_id, owner_id, model, billing_route, "
                " input_tokens, output_tokens, cache_read_tokens, "
                " cache_creation_tokens, cost_usd, context_unsplit, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
        await self.db.db_conn.commit()

    def _sorted(self, rows, *keys):
        """Compared as sets of rows rather than in order: the bundle groups on
        one pass and the originals on three, so first-seen order can differ
        without any charted number differing. Ordering within a bucket is not
        a property the charts depend on -- they key on bucket."""
        return sorted(rows, key=lambda r: tuple(str(r.get(k)) for k in keys))

    async def _both(self, days, bucket):
        bundle = await self.db.usage_series_bundle(
            "admin", days, bucket, model_top=4, agent_top=3)
        legacy = {
            "series": await self.db.usage_series("admin", days, bucket),
            "models": await self.db.usage_model_series(
                "admin", days, bucket, top=4),
            "agents": await self.db.usage_agent_series(
                "admin", days, bucket, top=3),
        }
        return bundle, legacy

    async def test_the_route_series_matches(self):
        for bucket in ("day", "hour", "halfhour", "month"):
            with self.subTest(bucket=bucket):
                bundle, legacy = await self._both(30, bucket)
                self.assertEqual(
                    self._sorted(bundle["series"], "bucket", "route"),
                    self._sorted(legacy["series"], "bucket", "route"),
                )

    async def test_the_model_series_matches(self):
        for bucket in ("day", "halfhour"):
            with self.subTest(bucket=bucket):
                bundle, legacy = await self._both(30, bucket)
                got = self._sorted(bundle["models"], "bucket", "model")
                want = self._sorted(legacy["models"], "bucket", "model")
                # ids are collected in encounter order, which one pass and two
                # passes need not agree on; the set of ids is the claim.
                for row in got + want:
                    row["ids"] = sorted(row.get("ids") or [])
                self.assertEqual(got, want)

    async def test_the_agent_series_matches(self):
        for bucket in ("day", "halfhour"):
            with self.subTest(bucket=bucket):
                bundle, legacy = await self._both(30, bucket)
                self.assertEqual(
                    self._sorted(bundle["agents"], "bucket", "agent_id"),
                    self._sorted(legacy["agents"], "bucket", "agent_id"),
                )

    async def test_it_matches_with_no_window_at_all(self):
        """days=None is the "All time" option, and it takes a different code
        path: the WHERE clause disappears entirely."""
        bundle, legacy = await self._both(None, "day")
        self.assertEqual(
            self._sorted(bundle["series"], "bucket", "route"),
            self._sorted(legacy["series"], "bucket", "route"),
        )
        self.assertEqual(
            self._sorted(bundle["agents"], "bucket", "agent_id"),
            self._sorted(legacy["agents"], "bucket", "agent_id"),
        )

    async def test_an_empty_table_gives_empty_series_not_an_error(self):
        await self.db.db_conn.execute("DELETE FROM usage_events")
        await self.db.db_conn.commit()
        bundle, legacy = await self._both(30, "day")
        self.assertEqual(bundle, {"series": [], "models": [], "agents": []})
        self.assertEqual(legacy["models"], [])
        self.assertEqual(legacy["agents"], [])

    async def test_unattributable_spend_is_kept_under_Other(self):
        """The row with neither a session nor a chat id. Dropping it would make
        the agent chart quietly stop adding up to the page's own total."""
        bundle, _ = await self._both(30, "day")
        agents = {row["agent_id"] for row in bundle["agents"]}
        self.assertIn("Other", agents)
        charted = sum(row["requests"] for row in bundle["agents"])
        routes = sum(row["requests"] for row in bundle["series"])
        self.assertEqual(
            charted, routes,
            "the agent series and the route series disagree on how many turns "
            "there were, so one of them is dropping rows",
        )

    async def test_the_two_ids_for_one_model_fold_into_one_series(self):
        bundle, _ = await self._both(30, "day")
        qwen = [row for row in bundle["models"]
                if "Qwen3.6" in (row["model"] or "")]
        self.assertTrue(qwen, f"no Qwen series: {bundle['models']}")
        for row in qwen:
            if len(row["ids"]) > 1:
                self.assertEqual(
                    sorted(row["ids"]),
                    ["nvidia/Qwen3.6-35B-A3B-NVFP4",
                     "vllm/Qwen3.6-35B-A3B-NVFP4"],
                    "both raw ids must be reported, or a merge nobody can see "
                    "is a merge nobody can check",
                )
                break
        else:
            self.fail(f"the two Qwen ids never folded together: {qwen}")


if __name__ == "__main__":
    unittest.main()
