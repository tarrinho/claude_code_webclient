import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db.config, "DB_PATH", f"{self.tmp.name}/webconsole.db")
        self.root_patch = patch.object(db.config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_schema_and_default_chat_fields(self):
        cursor = await db.db_conn.execute("PRAGMA table_info(chats)")
        columns = {row["name"] for row in await cursor.fetchall()}
        self.assertTrue({"pinned", "pinned_at", "deleted_at"}.issubset(columns))
        await db.chat_create("one", "One", None, f"{self.tmp.name}/projects/one", "admin")
        chat = await db.chat_get("one", "admin")
        self.assertEqual(chat["pinned"], 0)
        self.assertIsNone(chat["pinned_at"])
        self.assertIsNone(chat["deleted_at"])

    async def test_migration_is_idempotent(self):
        await db.close()
        await db.init()
        cursor = await db.db_conn.execute("PRAGMA table_info(chats)")
        names = [row["name"] for row in await cursor.fetchall()]
        self.assertEqual(names.count("pinned"), 1)

    async def test_chat_list_groups_pinned_recent_and_archived(self):
        for chat_id in ("recent", "pinned-old", "pinned-new", "archived"):
            await db.chat_create(chat_id, chat_id, None, f"{self.tmp.name}/{chat_id}", "admin")
        await db.db_conn.execute("UPDATE chats SET updated_at = '2026-01-01T00:00:00Z' WHERE id = 'recent'")
        await db.chat_update("pinned-old", "admin", pinned=1, pinned_at="2026-01-01T00:00:00Z")
        await db.chat_update("pinned-new", "admin", pinned=1, pinned_at="2026-02-01T00:00:00Z")
        await db.chat_update("archived", "admin", archived=1)
        chats = await db.chat_list("admin")
        self.assertEqual([c["id"] for c in chats], ["pinned-new", "pinned-old", "recent", "archived"])

    async def test_archived_chat_requires_explicit_lookup(self):
        await db.chat_create("archived", "Archived", None, f"{self.tmp.name}/archived", "admin")
        await db.chat_update("archived", "admin", archived=1)
        self.assertIsNone(await db.chat_get("archived", "admin"))
        self.assertIsNotNone(await db.chat_get("archived", "admin", include_archived=True))
        self.assertIsNone(await db.chat_get("archived", "other", include_archived=True))

    async def test_delete_removes_records_but_preserves_workspace(self):
        work_dir = Path(self.tmp.name) / "projects" / "kept"
        work_dir.mkdir(parents=True)
        await db.chat_create("delete", "Delete", None, str(work_dir), "admin")
        await db.messages_append("delete", "user", "hello")
        await db.messages_append("delete", "assistant", "hi")
        self.assertTrue(await db.chat_delete("delete", "admin"))
        self.assertIsNone(await db.chat_get("delete", "admin", include_archived=True))
        self.assertEqual(await db.messages_get("delete"), [])
        self.assertTrue(work_dir.exists())
        self.assertFalse(await db.chat_delete("missing", "admin"))

    async def test_chat_update_rejects_unknown_columns(self):
        await db.chat_create("one", "One", None, f"{self.tmp.name}/one", "admin")
        self.assertFalse(await db.chat_update("one", "admin", session_id="unsafe"))
        self.assertFalse(await db.chat_update("one", "other", title="No"))


if __name__ == "__main__":
    unittest.main()
