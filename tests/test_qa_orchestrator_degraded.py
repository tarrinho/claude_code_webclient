"""QA: a orchestrator's degraded flag, same contract as chats' (see
tests/test_qa_chat_degraded.py) -- kind-scoped mark/clear, never raises.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import config
import db


class SupervisorDegradedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await db.orchestrator_create("sup-1", "Sup", None, "admin")

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_schema_has_the_new_columns(self):
        cursor = await db.db_conn.execute("PRAGMA table_info(orchestrators)")
        columns = {row["name"] for row in await cursor.fetchall()}
        self.assertTrue({"degraded", "degraded_reason"}.issubset(columns))

    async def test_migration_is_idempotent(self):
        await db.close()
        await db.init()
        cursor = await db.db_conn.execute("PRAGMA table_info(orchestrators)")
        names = [row["name"] for row in await cursor.fetchall()]
        self.assertEqual(names.count("degraded"), 1)

    async def test_get_and_list_both_carry_the_new_fields(self):
        sup = await db.orchestrator_get("sup-1", "admin")
        self.assertIn("degraded", sup)
        self.assertIn("degraded_reason", sup)
        listed = await db.orchestrator_list("admin")
        self.assertIn("degraded", listed[0])
        self.assertIn("degraded_reason", listed[0])

    async def test_marking_and_clearing_is_kind_scoped(self):
        await db.orchestrator_mark_degraded("sup-1", "status", "write raised")
        sup = await db.orchestrator_get("sup-1", "admin")
        self.assertEqual(sup["degraded"], 1)
        self.assertIn("status:", sup["degraded_reason"])

        await db.orchestrator_clear_degraded("sup-1", "progress")  # different kind
        sup = await db.orchestrator_get("sup-1", "admin")
        self.assertEqual(sup["degraded"], 1, "an unrelated success must not clear it")

        await db.orchestrator_clear_degraded("sup-1", "status")  # same kind
        sup = await db.orchestrator_get("sup-1", "admin")
        self.assertEqual(sup["degraded"], 0)
        self.assertIsNone(sup["degraded_reason"])

    async def test_marking_never_raises_even_if_the_write_fails(self):
        with patch.object(db.db_conn, "execute", AsyncMock(side_effect=RuntimeError("disk"))):
            await db.orchestrator_mark_degraded("sup-1", "status", "boom")  # must not raise

    async def test_a_later_kind_clearing_itself_also_clears_an_earlier_unresolved_kind(self):
        """Pins the accepted tradeoff (see orchestrator_clear_degraded's
        docstring): a single `degraded_reason` column holds only the most
        recent mark, so kind A's failure is invisible again once kind B marks
        and then clears itself. Documented, intentional -- not a bug.
        """
        await db.orchestrator_mark_degraded("sup-1", "status", "boom-a")
        await db.orchestrator_mark_degraded("sup-1", "progress", "boom-b")
        await db.orchestrator_clear_degraded("sup-1", "progress")
        sup = await db.orchestrator_get("sup-1", "admin")
        self.assertEqual(sup["degraded"], 0)
        self.assertIsNone(sup["degraded_reason"])


if __name__ == "__main__":
    unittest.main()
