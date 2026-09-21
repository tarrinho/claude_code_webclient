"""QA: the chat_subagents table — schema and its idempotency guarantee."""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import db


class _SubagentDbFixture:
    """Database fixture shared with the later subagent test classes.

    Deliberately NOT a TestCase: a class that subclasses one re-runs its
    tests inside every subclass, so the schema cases below would execute
    again for each of the accessor and retention suites.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for attr, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            patcher = patch.object(config, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        await db.init()
        self.addAsyncCleanup(db.close)


class ChatSubagentsSchemaTests(_SubagentDbFixture, unittest.IsolatedAsyncioTestCase):
    async def test_the_table_exists_with_every_column(self):
        cur = await db.db_conn.execute("PRAGMA table_info(chat_subagents)")
        cols = {r["name"] for r in await cur.fetchall()}
        self.assertEqual(cols, {
            "id", "chat_id", "tool_use_id", "agent_type", "description",
            "status", "started_at", "ended_at",
        })

    async def test_the_same_tool_use_id_cannot_be_recorded_twice(self):
        """The idempotency guarantee. A transcript is re-scanned for reasons
        unrelated to this feature, so a second scan must be a no-op."""
        for _ in range(2):
            await db.db_conn.execute(
                "INSERT OR IGNORE INTO chat_subagents "
                "(chat_id, tool_use_id, agent_type, description, status, started_at) "
                "VALUES ('c1', 'tu_1', 'code-review', 'd', 'running', '2026-09-21T00:00:00Z')")
        await db.db_conn.commit()
        cur = await db.db_conn.execute("SELECT COUNT(*) FROM chat_subagents")
        self.assertEqual((await cur.fetchone())[0], 1)

    async def test_the_same_tool_use_id_in_another_chat_is_a_separate_row(self):
        for chat in ("c1", "c2"):
            await db.db_conn.execute(
                "INSERT OR IGNORE INTO chat_subagents "
                "(chat_id, tool_use_id, agent_type, description, status, started_at) "
                f"VALUES ('{chat}', 'tu_1', 'x', 'd', 'running', '2026-09-21T00:00:00Z')")
        await db.db_conn.commit()
        cur = await db.db_conn.execute("SELECT COUNT(*) FROM chat_subagents")
        self.assertEqual((await cur.fetchone())[0], 2)
