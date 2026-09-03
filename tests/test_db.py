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

    async def test_last_model_used_single_chat_matches_the_batched_form(self):
        """`db.last_model_used(chat_id, owner)` is the single-chat form of
        `last_models_used` used by GET /api/chats/{id} -- the two must never
        disagree about the same conversation."""
        await db.chat_create("c1", "C1", None, f"{self.tmp.name}/c1", "admin")
        await db.usage_record("c1", "admin", "claude-sonnet-5", "anthropic")
        await db.usage_record("c1", "admin", "claude-opus-5", "anthropic")
        single = await db.last_model_used("c1", "admin")
        batched = (await db.last_models_used("admin"))["c1"]
        self.assertEqual(single, "claude-opus-5")
        self.assertEqual(single, batched)

    async def test_last_model_used_is_empty_for_a_chat_with_no_turns(self):
        await db.chat_create("c1", "C1", None, f"{self.tmp.name}/c1", "admin")
        self.assertEqual(await db.last_model_used("c1", "admin"), "")

    async def test_last_model_used_is_owner_scoped(self):
        await db.chat_create("c1", "C1", None, f"{self.tmp.name}/c1", "admin")
        await db.usage_record("c1", "someone-else", "claude-opus-5", "anthropic")
        self.assertEqual(await db.last_model_used("c1", "admin"), "")

    async def test_last_models_used_is_the_newest_row_per_chat(self):
        await db.chat_create("c1", "C1", None, f"{self.tmp.name}/c1", "admin")
        await db.chat_create("c2", "C2", None, f"{self.tmp.name}/c2", "admin")
        # c1 switches model mid-conversation; the newest row must win, not the
        # first or an arbitrary one -- this is the whole point of the query.
        await db.usage_record("c1", "admin", "claude-sonnet-5", "anthropic")
        await db.usage_record("c1", "admin", "claude-opus-5", "anthropic")
        await db.usage_record("c2", "admin", "vllm/Qwen3.6-35B-A3B-NVFP4", "anthropic-compatible")
        last = await db.last_models_used("admin")
        self.assertEqual(last["c1"], "claude-opus-5")
        self.assertEqual(last["c2"], "vllm/Qwen3.6-35B-A3B-NVFP4")

    async def test_last_models_used_ties_break_on_row_id_not_timestamp(self):
        # Two rows landing in the same turn (multi-model usage, or two writes
        # in the same clock tick) must not make "newest" ambiguous. MAX(id) is
        # exact where MAX(created_at) is not.
        await db.chat_create("c1", "C1", None, f"{self.tmp.name}/c1", "admin")
        await db.db_conn.execute(
            "INSERT INTO usage_events (chat_id, owner_id, model, provider, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            ("c1", "admin", "claude-sonnet-5", "anthropic", "2026-01-01T00:00:00Z"),
        )
        await db.db_conn.execute(
            "INSERT INTO usage_events (chat_id, owner_id, model, provider, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            ("c1", "admin", "claude-opus-5", "anthropic", "2026-01-01T00:00:00Z"),
        )
        await db.db_conn.commit()
        last = await db.last_models_used("admin")
        self.assertEqual(last["c1"], "claude-opus-5")

    async def test_last_models_used_is_owner_scoped_and_excludes_terminal_rows(self):
        await db.chat_create("mine", "Mine", None, f"{self.tmp.name}/mine", "admin")
        await db.usage_record("mine", "admin", "claude-opus-5", "anthropic")
        # A row from a different owner must not leak into admin's view.
        await db.usage_record("theirs", "someone-else", "claude-opus-5", "anthropic")
        # chat_id='' is how a terminal-origin turn is recorded (db.py comment
        # on usage_events.session_id) and must not surface as a "chat".
        await db.usage_record("terminal-session-id", "admin", "claude-opus-5",
                              "anthropic", origin="terminal")
        await db.db_conn.execute(
            "UPDATE usage_events SET chat_id = '' WHERE origin = 'terminal'"
        )
        await db.db_conn.commit()
        last = await db.last_models_used("admin")
        self.assertEqual(set(last), {"mine"})

    async def test_last_model_used_is_independent_of_the_routing_override(self):
        """`chats.model` and last-used must never collide.

        `chats.model` is the user's routing override (runner.get_default_model
        reads it before the backend/global default); it is set explicitly, not
        derived from usage. The two must be able to disagree -- that disagreement
        is exactly what lets a chat serve on model A while still being pinned to
        model B for its next turn.
        """
        await db.chat_create("c1", "C1", None, f"{self.tmp.name}/c1", "admin")
        await db.chat_set_model("c1", "claude-opus-5")
        await db.usage_record("c1", "admin", "vllm/Qwen3.6-35B-A3B-NVFP4",
                              "anthropic-compatible")
        chat = await db.chat_get("c1", "admin")
        last = await db.last_models_used("admin")
        self.assertEqual(chat["model"], "claude-opus-5")
        self.assertEqual(last["c1"], "vllm/Qwen3.6-35B-A3B-NVFP4")

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

    async def test_concurrent_message_batches_return_their_exact_ids(self):
        import asyncio

        await db.chat_create("one", "One", None, f"{self.tmp.name}/one", "admin")
        first, second = await asyncio.gather(
            db.messages_batch("one", [("user", "first-u"), ("assistant", "first-a")]),
            db.messages_batch("one", [("user", "second-u"), ("assistant", "second-a")]),
        )
        self.assertEqual(len(set(first + second)), 4)
        messages = {message["id"]: message["content"] for message in await db.messages_get("one")}
        self.assertEqual([messages[row_id] for row_id in first], ["first-u", "first-a"])
        self.assertEqual([messages[row_id] for row_id in second], ["second-u", "second-a"])


if __name__ == "__main__":
    unittest.main()
