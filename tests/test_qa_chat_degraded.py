"""QA: a chat's degraded flag is set by the mark/clear helpers, kind-scoped.

`degraded` exists so a write that silently failed (usage recording is the
first caller) leaves a visible trace instead of only a log line -- see
docs/superpowers/specs/2026-09-04-orchestrator-observability-design.md.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import config
import db


class ChatDegradedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await db.chat_create("c1", "Chat", None, f"{self.tmp.name}/p", "admin")

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_schema_has_the_new_columns(self):
        cursor = await db.db_conn.execute("PRAGMA table_info(chats)")
        columns = {row["name"] for row in await cursor.fetchall()}
        self.assertTrue({"degraded", "degraded_reason", "degraded_at"}.issubset(columns))

    async def test_migration_is_idempotent(self):
        await db.close()
        await db.init()
        cursor = await db.db_conn.execute("PRAGMA table_info(chats)")
        names = [row["name"] for row in await cursor.fetchall()]
        self.assertEqual(names.count("degraded"), 1)

    async def test_a_new_chat_is_not_degraded(self):
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["degraded"], 0)
        self.assertIsNone(chat["degraded_reason"])

    async def test_marking_sets_the_flag_and_reason(self):
        await db.chat_mark_degraded("c1", "usage", "no frame reached the handler")
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["degraded"], 1)
        self.assertIn("usage:", chat["degraded_reason"])
        self.assertIn("no frame reached the handler", chat["degraded_reason"])
        self.assertIsNotNone(chat["degraded_at"])

    async def test_clearing_the_same_kind_resets_it(self):
        await db.chat_mark_degraded("c1", "usage", "boom")
        await db.chat_clear_degraded("c1", "usage")
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["degraded"], 0)
        self.assertIsNone(chat["degraded_reason"])
        self.assertIsNone(chat["degraded_at"])

    async def test_clearing_a_different_kind_does_not_touch_it(self):
        """A success of kind B must not hide an unresolved kind-A failure."""
        await db.chat_mark_degraded("c1", "usage", "boom")
        await db.chat_clear_degraded("c1", "some_other_kind")
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["degraded"], 1)
        self.assertIn("usage:", chat["degraded_reason"])

    async def test_marking_never_raises_even_if_the_write_fails(self):
        with patch.object(db.db_conn, "execute", AsyncMock(side_effect=RuntimeError("disk"))):
            await db.chat_mark_degraded("c1", "usage", "boom")  # must not raise

    async def test_a_later_kind_clearing_itself_also_clears_an_earlier_unresolved_kind(self):
        """Pins the accepted tradeoff (see chat_clear_degraded's docstring):

        a single `degraded_reason` column can only hold the most recent mark.
        If kind A marks, then kind B also marks (overwriting A's text), then
        B clears, the flag clears even though A was never resolved -- there
        is nothing left recording that A ever happened. This is documented,
        intentional behaviour, not a bug.
        """
        await db.chat_mark_degraded("c1", "usage", "boom-a")
        await db.chat_mark_degraded("c1", "some_other_kind", "boom-b")
        await db.chat_clear_degraded("c1", "some_other_kind")
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["degraded"], 0)
        self.assertIsNone(chat["degraded_reason"])


class ChatDegradedNotPatchableTests(unittest.TestCase):
    """`degraded` is a read-only observability signal set only by
    chat_mark_degraded/chat_clear_degraded -- a PATCH /api/chats/{id} caller
    must never be able to set it directly. No DB needed: this is a static
    check of the two column allowlists in routes/db_chats.py.
    """

    def test_degraded_is_a_known_column_but_never_a_patchable_field(self):
        from routes import db_chats

        self.assertIn("degraded", db_chats._CHAT_COLUMNS)
        self.assertNotIn("degraded", db_chats._ALLOWED_CHAT_FIELDS)


if __name__ == "__main__":
    unittest.main()
