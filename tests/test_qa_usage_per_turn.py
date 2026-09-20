"""Layered QA coverage for per-turn usage accounting.

A Claude Code ``result`` frame reports the same information twice, and the two
forms do not mean the same thing:

* flat ``usage``   -- what THIS TURN spent.
* ``modelUsage``   -- what the SESSION has spent so far, keyed by model.
* ``total_cost_usd`` -- likewise cumulative, with no per-turn counterpart.

Both parsers preferred ``modelUsage``, so every turn stored the whole session
again. Measured against a live CLI on 2026-09-20, two turns of one session:

    turn 1   usage in=13854   modelUsage in=13854   cost 0.26945
    turn 2   usage in=70      modelUsage in=13924   cost 0.418594

exact on three independent fields (13854+70=13924, 4+4=8,
20008+13869=33877). In production this had reached 1,074,714,966 cache-read
tokens on one chat across 2,664 turns, token totals up to 40,202x real, and a
reported $8,970.53 against a true $287.54 -- 96.8% of it in three rows.

* UnitQA         -- the two parsers in isolation, including their parity.
* ComponentAPIQA -- `_record_turn_usage` against a real database.
* IntegrationQA  -- both recording callers, web and orchestrator.
* AcceptanceUATQA -- the property that matters to a reader of the Usage page:
  a long session's rows sum to what the session actually cost, and no single
  row carries the whole history.

The numbers in the fixtures are the measured ones, not invented, so a
regression reproduces the real defect rather than a plausible-looking one.

No live model, Claude Code account, or network service is required.
"""
from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import auth
import claude_proxy
import config
import db
import orchestrator
import runner
from routes import chats as chat_routes

#: The two frames a live CLI actually emitted, turn 1 then `--resume`.
TURN1 = {
    "type": "result",
    "usage": {"input_tokens": 13854, "output_tokens": 4,
              "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 20008},
    "modelUsage": {"claude-opus-5[1m]": {
        "inputTokens": 13854, "outputTokens": 4,
        "cacheReadInputTokens": 0, "cacheCreationInputTokens": 20008,
        "costBasis": "unknown"}},
    "total_cost_usd": 0.26945,
}
TURN2 = {
    "type": "result",
    "usage": {"input_tokens": 70, "output_tokens": 4,
              "cache_read_input_tokens": 20008,
              "cache_creation_input_tokens": 13869},
    "modelUsage": {"claude-opus-5[1m]": {
        "inputTokens": 13924, "outputTokens": 8,
        "cacheReadInputTokens": 20008, "cacheCreationInputTokens": 33877,
        "costBasis": "unknown"}},
    "total_cost_usd": 0.418594,
}
MODEL = "claude-opus-5[1m]"


class UsageMixin:
    async def init_temp_db(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(
            config, "PROJECTS_ROOT", f"{self.tmp.name}/projects"
        )
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await db.user_create("admin", None, auth.hash_password("admin"))
        self.admin_id = (await db.user_get_by_name("admin"))["id"]

    async def close_temp_db(self):
        runner._models_by_chat.pop("c1", None)
        runner._usage_by_chat.pop("c1", None)
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def make_chat(self, cid="c1"):
        await db.chat_create(
            cid, cid, None, f"{self.tmp.name}/projects", self.admin_id
        )
        return cid

    async def record(self, frame, chat_id="c1", model=MODEL):
        """Record one CLI frame the way the web turn handler does."""
        if model:
            runner._models_by_chat[chat_id] = model
        await chat_routes._record_turn_usage(
            chat_id, self.admin_id, runner.usage_frame(frame))

    async def rows(self):
        """Every column of every row, oldest first.

        Read straight from the table rather than through `usage_recent`, which
        projects a subset for the Usage page and carries neither `id` nor
        `origin` -- both of which these tests assert on.
        """
        cur = await db.db_conn.execute(
            "SELECT * FROM usage_events ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


# ── Unit ──────────────────────────────────────────────────────────────────────


class UnitQA(unittest.TestCase):
    """The parsers, with no database in play."""

    def parsers(self):
        return (("proxy", claude_proxy.usage_frame),
                ("runner", runner.usage_frame))

    def test_a_resumed_turn_reports_only_its_own_tokens(self):
        for label, parse in self.parsers():
            self.assertEqual(parse(TURN1)["models"][""]["input_tokens"], 13854, label)
            second = parse(TURN2)["models"][""]
            self.assertEqual(second["input_tokens"], 70, label)
            # The cumulative figure must never be what a row receives.
            self.assertNotEqual(second["input_tokens"], 13924, label)

    def test_every_counter_is_the_turn_not_the_session(self):
        # Checked field by field: reading the right object for input tokens and
        # the wrong one for cache creation would still leave the bill wrong,
        # and cache tokens are the larger number in a real conversation.
        for label, parse in self.parsers():
            second = parse(TURN2)["models"][""]
            self.assertEqual(second["output_tokens"], 4, label)
            self.assertEqual(second["cache_creation_tokens"], 13869, label)
            self.assertNotEqual(second["cache_creation_tokens"], 33877, label)

    def test_both_parsers_agree(self):
        # They are deliberate copies -- claude_proxy's runs under
        # PROXY_ENABLED=True, runner's serves the direct path -- so a fix
        # applied to one is half a fix (CLAUDE.md rule 1).
        for frame in (TURN1, TURN2):
            self.assertEqual(claude_proxy.usage_frame(frame),
                             runner.usage_frame(frame))

    def test_model_usage_still_serves_a_frame_without_the_flat_form(self):
        # Preference, not removal. A frame carrying only modelUsage is still
        # worth recording, and keeps its per-model attribution.
        frame = {"type": "result",
                 "modelUsage": {"a": {"inputTokens": 5, "outputTokens": 1}}}
        for label, parse in self.parsers():
            models = parse(frame)["models"]
            self.assertEqual(list(models), ["a"], label)
            self.assertEqual(models["a"]["input_tokens"], 5, label)

    def test_an_all_zero_turn_records_nothing(self):
        # cost_basis is a string, so a truthiness check taken over the whole
        # dict *after* it was added would call an empty turn non-empty and
        # write a row for spend that never happened. The counts are therefore
        # tested before cost_basis joins them.
        #
        # No modelUsage here on purpose: with one present the parser falls back
        # to it and reports that model, which is long-standing behaviour and
        # not what this test is about.
        frame = {"type": "result",
                 "usage": {"input_tokens": 0, "output_tokens": 0,
                           "cache_read_input_tokens": 0,
                           "cache_creation_input_tokens": 0}}
        for label, parse in self.parsers():
            self.assertIsNone(parse(frame), label)

    def test_cost_basis_rides_along_but_only_when_one_model_is_named(self):
        # It has no flat equivalent, so it still comes from modelUsage. With
        # two models named there is no way to say which basis describes this
        # turn's tokens, and None is the honest answer.
        for label, parse in self.parsers():
            self.assertEqual(parse(TURN2)["models"][""]["cost_basis"],
                             "unknown", label)
        two = {**TURN2, "modelUsage": {
            "a": {"inputTokens": 1, "costBasis": "unknown"},
            "b": {"inputTokens": 2, "costBasis": "billed"}}}
        for label, parse in self.parsers():
            self.assertIsNone(parse(two)["models"][""]["cost_basis"], label)


# ── Component ─────────────────────────────────────────────────────────────────


class ComponentAPIQA(UsageMixin, unittest.IsolatedAsyncioTestCase):
    """`_record_turn_usage` writing real rows."""

    async def asyncSetUp(self):
        await self.init_temp_db()
        await self.make_chat()

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def test_the_row_holds_the_turn_not_the_session(self):
        await self.record(TURN1)
        await self.record(TURN2)
        rows = await self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["input_tokens"], 13854)
        self.assertEqual(rows[1]["input_tokens"], 70)

    async def test_the_model_is_resolved_from_the_runner(self):
        # The flat form names no model, so attribution has to come from the
        # model the runner saw serve this chat.
        await self.record(TURN1)
        self.assertEqual((await self.rows())[0]["model"], MODEL)

    async def test_attribution_survives_the_handlers_own_take_of_the_model(self):
        """The blocking handler pops the model before it records usage.

        `handle_send_message` does `model = runner.take_last_model(chat_id)`
        for its own JSON response, and only then calls `_record_turn_usage`.
        `take_last_model` POPS, so a recorder that reads the same registry
        afterwards finds it empty and writes "unknown".

        That shipped: two `origin='web'` rows were written this way at
        2026-09-20T22:24Z, with correct tokens and a correct cost delta but no
        model, and the console showed "unknown" above a reply that had plainly
        come from Sonnet. The earlier tests here missed it because they seeded
        `_models_by_chat` and called the recorder directly, never reproducing
        the handler's order -- exactly what CLAUDE.md rule 2 says to check.
        """
        runner._models_by_chat["c1"] = MODEL
        taken = runner.take_last_model("c1")          # what the handler does
        self.assertEqual(taken, MODEL)                # it got the model
        await chat_routes._record_turn_usage(         # and then records
            "c1", self.admin_id, runner.usage_frame(TURN1), served_model=taken)
        self.assertEqual((await self.rows())[0]["model"], MODEL)

    async def test_a_row_is_still_written_when_no_model_was_seen(self):
        # A vague row beats unrecorded spend: the tokens were spent either way
        # (CLAUDE.md rule 5).
        runner._models_by_chat.pop("c1", None)
        await self.record(TURN1, model=None)
        self.assertEqual((await self.rows())[0]["model"], "unknown")

    async def test_cost_is_the_difference_from_the_previous_turn(self):
        await self.record(TURN1)
        await self.record(TURN2)
        rows = await self.rows()
        self.assertAlmostEqual(rows[0]["cost_usd"], 0.26945, places=6)
        self.assertAlmostEqual(rows[1]["cost_usd"], 0.149144, places=6)

    async def test_a_total_that_goes_backwards_starts_a_new_run(self):
        # A fresh CLI session resets the running total. Without this guard the
        # subtraction goes negative and the turn reads as a refund.
        await self.record(TURN1)
        await self.record({**TURN2, "total_cost_usd": 0.01})
        rows = await self.rows()
        self.assertAlmostEqual(rows[1]["cost_usd"], 0.01, places=6)
        self.assertGreaterEqual(rows[1]["cost_usd"], 0)

    async def test_the_baseline_is_kept_for_the_next_turn(self):
        # Stored on the row rather than in memory: this service restarts many
        # times a day, and an in-process value would reset each time, making
        # the first turn after every restart re-charge the whole session.
        await self.record(TURN1)
        self.assertAlmostEqual(
            await db.usage_last_cumulative("c1", MODEL), 0.26945, places=6)


# ── Integration ───────────────────────────────────────────────────────────────


class IntegrationQA(UsageMixin, unittest.IsolatedAsyncioTestCase):
    """Both recording callers, which must change together (CLAUDE.md rule 5)."""

    async def asyncSetUp(self):
        await self.init_temp_db()
        await self.make_chat()

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def _orchestrator_record(self, frame):
        """Drive OrchestratorEngine._record_usage with a stub engine.

        The method touches only `self.owner_id` and `self.orchestrator_id`, so
        a namespace is enough and no engine has to be constructed.
        """
        engine = SimpleNamespace(owner_id=self.admin_id, orchestrator_id="o1")
        runner.record_usage_frame("c1", runner.usage_frame(frame))
        with patch.object(runner, "get_backend", AsyncMock(return_value={})):
            await orchestrator.OrchestratorEngine._record_usage(
                engine, "c1", MODEL)

    async def test_the_orchestrator_path_also_records_per_turn_cost(self):
        # The orchestrator records its own usage -- a fix in routes/chats.py
        # alone leaves its spend cumulative while reading as complete.
        await self._orchestrator_record(TURN1)
        await self._orchestrator_record(TURN2)
        rows = await self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["input_tokens"], 70)
        self.assertAlmostEqual(rows[1]["cost_usd"], 0.149144, places=6)

    async def test_the_orchestrator_marks_its_own_origin(self):
        # `origin` is what lets a fan-out's spend be told apart from a person
        # typing, which is the case most worth separating.
        await self._orchestrator_record(TURN1)
        self.assertEqual((await self.rows())[0]["origin"], "orchestrator")

    async def test_the_two_callers_agree_on_the_same_frames(self):
        await self.record(TURN1)
        await self.record(TURN2)
        web = [(r["input_tokens"], round(r["cost_usd"] or 0, 6))
               for r in await self.rows()]
        for row in await self.rows():
            await db.db_conn.execute(
                "DELETE FROM usage_events WHERE id = ?", (row["id"],))
        await db.db_conn.commit()
        await self._orchestrator_record(TURN1)
        await self._orchestrator_record(TURN2)
        orch = [(r["input_tokens"], round(r["cost_usd"] or 0, 6))
                for r in await self.rows()]
        self.assertEqual(web, orch)


# ── Acceptance ────────────────────────────────────────────────────────────────


class AcceptanceUATQA(UsageMixin, unittest.IsolatedAsyncioTestCase):
    """What a reader of the Usage page is entitled to believe."""

    async def asyncSetUp(self):
        await self.init_temp_db()
        await self.make_chat()

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def test_a_sessions_rows_sum_to_what_the_session_cost(self):
        # The conservation property, and the one worth stating: whatever the
        # CLI's final running total says the session cost, the rows must add up
        # to exactly that -- no more (double counting) and no less (lost spend).
        totals = [0.26945, 0.418594, 0.5, 0.77, 1.25]
        for index, cumulative in enumerate(totals):
            frame = dict(TURN1 if index == 0 else TURN2)
            frame["total_cost_usd"] = cumulative
            await self.record(frame)
        charged = sum(r["cost_usd"] or 0 for r in await self.rows())
        self.assertAlmostEqual(charged, totals[-1], places=6)

    async def test_a_long_session_does_not_inflate(self):
        # The regression in its original shape. Replaying one cumulative frame
        # many times used to write that whole total on every turn; the chat
        # that reached 1,074,714,966 cache-read tokens got there this way.
        await self.record(TURN1)
        for _ in range(20):
            await self.record(TURN2)
        rows = await self.rows()
        self.assertEqual(len(rows), 21)
        self.assertTrue(all(r["input_tokens"] <= 13854 for r in rows))
        # 20 repeats of an unchanged running total are 20 turns that spent
        # nothing more, so the bill stops at the first turn's figure.
        charged = sum(r["cost_usd"] or 0 for r in rows)
        self.assertAlmostEqual(charged, 0.418594, places=6)

    async def test_no_row_ever_carries_a_negative_charge(self):
        # $2,893 rows were the visible symptom; a negative row would be the
        # same class of defect wearing the opposite sign.
        for cumulative in (0.26945, 0.418594, 0.05, 0.9, 0.1):
            frame = dict(TURN2)
            frame["total_cost_usd"] = cumulative
            await self.record(frame)
        for row in await self.rows():
            self.assertGreaterEqual(row["cost_usd"] or 0, 0)


if __name__ == "__main__":
    unittest.main()
