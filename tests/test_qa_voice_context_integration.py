"""QA: the two places voice_context touches something outside itself.

Design: docs/superpowers/specs/2026-09-21-voice-session-context-design.md

`test_qa_voice_context.py` covers the pure logic against stubs. This file
covers the parts a stub cannot vouch for:

  * `_record_turn_usage` gained an `origin` parameter so the voice summary
    could reuse it instead of becoming a third copy of that function. The
    existing suites proved the change broke no caller; they did not prove the
    new argument reaches the database, nor that the default is still "web".
    An origin that silently fell back to "web" would make voice-summary spend
    indistinguishable from web turns, which is the exact failure CLAUDE.md §5
    describes for the supervisor -- invisible spend.

  * The fetch tool's chat binding is a security property, and the stub it is
    tested against in the other file is one this module wrote. Here it runs
    against a real `messages` table holding two chats, so "it cannot read the
    other conversation" is demonstrated rather than asserted about a fake.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db  # noqa: E402
import voice_context as vc  # noqa: E402
from routes.chats import _record_turn_usage  # noqa: E402


class _DbFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        patcher.start()
        self.addCleanup(patcher.stop)
        await db.init()
        self.addAsyncCleanup(db.close)


class UsageOriginTests(_DbFixture):
    """The origin parameter added to `_record_turn_usage` for this feature."""

    FRAME = {
        "models": {"claude-sonnet-5": {
            "inputTokens": 1000, "outputTokens": 50,
            "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
        }},
        "cost_usd": 0.01,
        "duration_ms": 6000,
    }

    async def _origins(self) -> list[str]:
        cur = await db.db_conn.execute("SELECT origin FROM usage_events")
        return [row["origin"] for row in await cur.fetchall()]

    async def test_the_voice_summary_origin_reaches_the_database(self):
        """Without this, summary spend is indistinguishable from a web turn
        and the feature's cost cannot be told apart from the user's."""
        with patch("routes.chats.runner.get_backend",
                   new=AsyncMock(return_value={"provider": "anthropic"})):
            await _record_turn_usage(
                "chat-1", "admin", dict(self.FRAME),
                origin="voice-summary")
        self.assertEqual(await self._origins(), ["voice-summary"])

    async def test_the_default_is_still_web_for_every_existing_caller(self):
        """The parameter was defaulted precisely so no existing call site had
        to change. If the default drifted, every web turn would be relabelled
        and the Statistics page's origin split would quietly reshuffle."""
        with patch("routes.chats.runner.get_backend",
                   new=AsyncMock(return_value={"provider": "anthropic"})):
            await _record_turn_usage("chat-1", "admin", dict(self.FRAME))
        self.assertEqual(await self._origins(), ["web"])

    async def test_two_origins_stay_distinguishable_in_one_chat(self):
        """A voice session summarises the chat it was opened from, so both
        origins land against related ids. They must remain separable."""
        with patch("routes.chats.runner.get_backend",
                   new=AsyncMock(return_value={"provider": "anthropic"})):
            await _record_turn_usage("chat-1", "admin", dict(self.FRAME))
            await _record_turn_usage(
                "chat-1", "admin", dict(self.FRAME), origin="voice-summary")
        self.assertEqual(sorted(await self._origins()), ["voice-summary", "web"])


class FetchToolAgainstRealRowsTests(_DbFixture):
    """The chat binding, demonstrated against a real messages table."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        now = db._now()
        # `messages` carries a foreign key to `chats`, so the chats exist
        # first. Interleaved on purpose: the two conversations' ids alternate,
        # so a wide range cannot separate them and only the binding can.
        await db.chat_create("chat-mine", "mine", None, "/tmp", "admin")
        await db.chat_create("chat-theirs", "theirs", None, "/tmp", "admin")
        for mine, theirs in (("mine one", "SECRET a"),
                             ("mine two", "SECRET b"),
                             ("mine three", "SECRET c")):
            for chat_id, body in (("chat-mine", mine), ("chat-theirs", theirs)):
                await db.db_conn.execute(
                    "INSERT INTO messages (chat_id, role, content, created_at) "
                    "VALUES (?, ?, ?, ?)", (chat_id, "user", body, now))
        await db.db_conn.commit()
        self.db_path = f"{self.tmp.name}/db"

    def _tool(self, chat_id):
        """The real `make_fetch_tool`, over a synchronous reader against the
        same database file -- so this exercises the shipped tool rather than a
        stub written by the same module that is under test."""
        import sqlite3

        def read(cid, low, high):
            con = sqlite3.connect(self.db_path)
            con.row_factory = sqlite3.Row
            try:
                rows = con.execute(
                    "SELECT id, role, content FROM messages "
                    "WHERE chat_id = ? AND id BETWEEN ? AND ? ORDER BY id",
                    (cid, low, high)).fetchall()
            finally:
                con.close()
            return [dict(r) for r in rows]

        return vc.make_fetch_tool(chat_id, read)

    async def test_it_returns_its_own_chats_messages(self):
        result = self._tool("chat-mine")(0, 10**9)
        self.assertEqual([m["content"] for m in result["messages"]],
                         ["mine one", "mine two", "mine three"])

    async def test_a_range_spanning_everything_returns_nothing_of_the_other_chat(self):
        """The strongest form of the question: the two chats' ids interleave,
        so a range wide enough to cover both cannot exclude one. Only the
        binding can, and the model has no parameter with which to change it."""
        result = self._tool("chat-mine")(0, 10**9)
        contents = " ".join(m["content"] for m in result["messages"])
        self.assertNotIn("SECRET", contents)

    async def test_the_other_chats_ids_really_are_inside_that_range(self):
        """Guards the test above from passing vacuously: if the two chats did
        not share an id range, excluding one would prove nothing."""
        cur = await db.db_conn.execute(
            "SELECT MIN(id), MAX(id) FROM messages WHERE chat_id = 'chat-theirs'")
        low, high = await cur.fetchone()
        mine = await (await db.db_conn.execute(
            "SELECT MIN(id), MAX(id) FROM messages WHERE chat_id = 'chat-mine'")
        ).fetchone()
        self.assertLess(low, mine[1], "the other chat's ids must interleave")
        self.assertLess(high, 10**9)


class BudgetFitsTheChosenLadderTests(unittest.TestCase):
    """Spec §1.1's arithmetic, pinned as constants rather than read from the
    live delegation table.

    Deliberately not measured against production rows. A test whose result
    tracks live data flips with re-benchmarking and reports as a code failure
    -- the same defect already identified in
    `test_qa_delegation_all_task_types_e2e.py`, whose fixture mirrors
    production. These numbers are the ones the design was decided against, and
    if reality moves away from them the design should be revisited on purpose.
    """

    LUNA, SONNET, OPUS = 9.2, 6.0, 11.9
    LUNA_ACC, SONNET_ACC, OPUS_ACC = 0.583, 1.0, 1.0

    def setUp(self):
        self._t = 0.0

    def _clock(self):
        return vc.BudgetClock(budget_s=vc.SUMMARY_BUDGET_S, now=lambda: self._t)

    def test_walking_from_luna_cannot_reach_a_second_rung(self):
        """The finding that changed the design: luna fails at 9.2s, leaving
        5.8s, and sonnet needs 6.0s -- short by 0.2s."""
        clock = self._clock()
        self._t = self.LUNA
        self.assertAlmostEqual(clock.remaining(), 5.8, places=6)
        self.assertFalse(clock.allows(self.SONNET))

    def test_skipping_luna_leaves_room_for_a_retry(self):
        """Why MIN_RUNG_ACCURACY exists: starting at sonnet finishes at 6.0s
        and leaves 9.0s, which does not fit opus at 11.9s but fits a second
        sonnet attempt comfortably."""
        clock = self._clock()
        self._t = self.SONNET
        self.assertAlmostEqual(clock.remaining(), 9.0, places=6)
        self.assertTrue(clock.allows(self.SONNET))
        self.assertFalse(clock.allows(self.OPUS))

    def test_the_threshold_drops_exactly_the_rung_the_arithmetic_blames(self):
        ladder = ["azure_ai/gpt-5.6-luna", "claude-sonnet-5", "claude-opus-5"]
        acc = {"azure_ai/gpt-5.6-luna": self.LUNA_ACC,
               "claude-sonnet-5": self.SONNET_ACC,
               "claude-opus-5": self.OPUS_ACC}
        self.assertEqual(vc.eligible_rungs(ladder, acc),
                         ["claude-sonnet-5", "claude-opus-5"])


if __name__ == "__main__":
    unittest.main()
