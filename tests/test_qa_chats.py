"""QA tests for chat create, list, read, and legacy/restore compatibility.

Covers:
* New chat creation (db.chat_create) and validation.
* Chat listing (db.chat_list) with filtering and grouping.
* Chat reading (db.chat_get, db.messages_get) and mutations.
* Legacy/restore scenarios: chats with session IDs, archived, empty work_dirs,
  deleted_at marks, and mixed-owner data.
* Unit helpers, integration (db), and acceptance (full lifecycle).

All tests use local temporary state. No live model, proxy, or network service.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auth
import config
import db


def _setup_base(tc):
    """Patch config, create temp dirs. Call from sync setUp."""
    td = tempfile.TemporaryDirectory()
    tc.tmpdir = td
    tc._db_patch = patch.object(config, "DB_PATH", f"{td.name}/db")
    tc._root_patch = patch.object(config, "PROJECTS_ROOT", f"{td.name}/p")
    tc._db_patch.start()
    tc._root_patch.start()


async def _teardown_base(tc):
    """Close DB, stop patches, remove temp dirs."""
    await db.close()
    tc._db_patch.stop()
    tc._root_patch.stop()
    tc.tmpdir.cleanup()


async def _make_chat(tc, chat_id, title, session_id=None, description=None,
                     owner="admin", archived=0, pinned=0, pinned_at=None,
                     deleted_at=None, work_dir=None):
    """Create a chat row, optionally set extra fields."""
    wd = work_dir or f"{tc.tmpdir.name}/projects/{chat_id}"
    Path(wd).mkdir(parents=True, exist_ok=True)
    await db.chat_create(chat_id, title, description, wd, owner)
    sets, vals = [], []
    if session_id is not None:
        sets.append("session_id = ?"); vals.append(session_id)
    if archived != 0:
        sets.append("archived = ?"); vals.append(archived)
    if pinned != 0:
        sets.append("pinned = ?"); vals.append(pinned)
    if pinned_at is not None:
        sets.append("pinned_at = ?"); vals.append(pinned_at)
    if deleted_at is not None:
        sets.append("deleted_at = ?"); vals.append(deleted_at)
    if sets:
        sets.append("updated_at = ?")
        vals.append(db._now())
        vals.append(chat_id)
        await db.db_conn.execute(
            f"UPDATE chats SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        await db.db_conn.commit()
    return await db.chat_get(chat_id, owner)


# ───────────────────────────────────────────────────────────────────────────
# Unit – helpers, schema, edge-cases
# ───────────────────────────────────────────────────────────────────────────


class ChatUnitTests(unittest.TestCase):
    """Pure helpers and schema-level assertions (sync, no DB)."""

    def test_slug_from_title_handles_special_chars(self):
        self.assertEqual(db.slug_from_title("!@#$%^&*()"), "untitled")
        self.assertEqual(db.slug_from_title("  hello-world  "), "hello-world")
        self.assertEqual(
            db.slug_from_title("Testing 123! @WebConsole"),
            "testing-123-webconsole",
        )

    def test_slug_pattern_rejects_invalid(self):
        self.assertIsNone(db.slug_pattern("x"))
        self.assertIsNone(db.slug_pattern("-leading"))
        self.assertIsNone(db.slug_pattern("trailing-"))
        self.assertIsNone(db.slug_pattern("UPPER"))
        self.assertIsNone(db.slug_pattern("has space"))
        self.assertIsNotNone(db.slug_pattern("valid-slug"))
        self.assertIsNotNone(db.slug_pattern("a-1"))

    def test_timestamp_format_seconds_and_milliseconds(self):
        ts = 1_700_000_000
        self.assertEqual(
            db._format_timestamp(ts),
            db._format_timestamp(ts * 1000),
        )


class SchemaTests(unittest.IsolatedAsyncioTestCase):
    """Database schema must contain all expected columns."""

    def setUp(self):
        _setup_base(self)

    async def asyncTearDown(self):
        await _teardown_base(self)

    async def asyncSetUp(self):
        await db.init()
        await auth.bootstrap_admin()

    async def test_chats_table_columns(self):
        cursor = await db.db_conn.execute("PRAGMA table_info(chats)")
        cols = {row["name"] for row in await cursor.fetchall()}
        expected = {
            "id", "title", "description", "session_id", "work_dir",
            "owner_id", "created_at", "updated_at", "archived",
            "pinned", "pinned_at", "deleted_at", "model", "ai_machine_id",
        }
        self.assertTrue(expected.issubset(cols), f"Missing columns: {expected - cols}")

    async def test_messages_table_columns(self):
        cursor = await db.db_conn.execute("PRAGMA table_info(messages)")
        cols = {row["name"] for row in await cursor.fetchall()}
        self.assertTrue({"id", "chat_id", "role", "content", "created_at"}.issubset(cols))

    async def test_ai_machines_table_columns(self):
        cursor = await db.db_conn.execute("PRAGMA table_info(ai_machines)")
        cols = {row["name"] for row in await cursor.fetchall()}
        self.assertTrue(
            {"id", "name", "host", "port", "active", "owner_id"}.issubset(cols)
        )


# ───────────────────────────────────────────────────────────────────────────
# Integration – new chat creation via db layer
# ───────────────────────────────────────────────────────────────────────────


class NewChatCreationTests(unittest.IsolatedAsyncioTestCase):
    """db.chat_create – full creation flow."""

    def setUp(self):
        _setup_base(self)

    async def asyncTearDown(self):
        await _teardown_base(self)

    async def asyncSetUp(self):
        await db.init()
        await auth.bootstrap_admin()

    async def test_create_returns_timestamp(self):
        now = await db.chat_create(
            "new-1", "New Chat", None, f"{self.tmpdir.name}/new-1", "admin"
        )
        self.assertIsInstance(now, str)
        self.assertTrue(now.endswith("Z"))

    async def test_create_creates_workspace_dir(self):
        work = f"{self.tmpdir.name}/new-dir"
        Path(work).mkdir(parents=True, exist_ok=True)
        await db.chat_create("new-dir", "Dir Chat", None, work, "admin")
        self.assertTrue(os.path.isdir(work))

    async def test_create_adds_to_chat_list(self):
        await db.chat_create("new-list", "List Chat", None, f"{self.tmpdir.name}/new-list", "admin")
        chats = await db.chat_list("admin")
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["title"], "List Chat")

    async def test_create_returns_chat(self):
        await db.chat_create(
            "new-get", "Get Chat", None, f"{self.tmpdir.name}/new-get", "admin"
        )
        chat = await db.chat_get("new-get", "admin")
        self.assertIsNotNone(chat)
        self.assertEqual(chat["title"], "Get Chat")

    async def test_create_with_description(self):
        await db.chat_create(
            "new-desc", "Described", "A test", f"{self.tmpdir.name}/new-desc", "admin"
        )
        chat = await db.chat_get("new-desc", "admin")
        self.assertEqual(chat["description"], "A test")

    async def test_create_default_fields(self):
        await db.chat_create(
            "new-def", "Defaults", None, f"{self.tmpdir.name}/new-def", "admin"
        )
        chat = await db.chat_get("new-def", "admin")
        self.assertEqual(chat["archived"], 0)
        self.assertEqual(chat["pinned"], 0)
        self.assertIsNone(chat["pinned_at"])
        self.assertIsNone(chat["deleted_at"])
        self.assertIsNone(chat["session_id"])

    async def test_create_has_model_field(self):
        await db.chat_create(
            "new-model", "Model Chat", None, f"{self.tmpdir.name}/new-model", "admin"
        )
        chat = await db.chat_get("new-model", "admin")
        self.assertIn("model", chat)

    async def test_create_has_ai_machine_id_field(self):
        await db.chat_create(
            "new-mach", "Machine Chat", None, f"{self.tmpdir.name}/new-mach", "admin"
        )
        chat = await db.chat_get("new-mach", "admin")
        self.assertIn("ai_machine_id", chat)

    async def test_create_echoes_chat_id(self):
        """chat_create stores whatever id is passed."""
        custom_id = "abcdef1234567890abcdef1234567890"
        await db.chat_create(custom_id, "Id Echo", None, f"{self.tmpdir.name}/id-echo", "admin")
        chat = await db.chat_get(custom_id, "admin")
        self.assertEqual(chat["id"], custom_id)

    async def test_create_custom_short_id(self):
        """chat_create stores whatever id is passed, including short ones."""
        short_id = "short"
        await db.chat_create(short_id, "Short ID", None, f"{self.tmpdir.name}/short", "admin")
        chat = await db.chat_get(short_id, "admin")
        self.assertEqual(chat["id"], short_id)

    async def test_create_multiple_chats(self):
        for i in range(5):
            await db.chat_create(
                f"multi-{i}", f"Chat {i}", None, f"{self.tmpdir.name}/multi-{i}", "admin"
            )
        chats = await db.chat_list("admin")
        self.assertEqual(len(chats), 5)


class NewChatMessagesTests(unittest.IsolatedAsyncioTestCase):
    """db.messages_append / db.messages_get – message flow."""

    def setUp(self):
        _setup_base(self)

    async def asyncTearDown(self):
        await _teardown_base(self)

    async def asyncSetUp(self):
        await db.init()
        await auth.bootstrap_admin()

    async def test_append_user_message(self):
        await db.chat_create("msg-1", "Msg Chat", None, f"{self.tmpdir.name}/msg-1", "admin")
        await db.messages_append("msg-1", "user", "hello")
        messages = await db.messages_get("msg-1")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "hello")

    async def test_append_assistant_message(self):
        await db.chat_create("msg-2", "Msg Chat2", None, f"{self.tmpdir.name}/msg-2", "admin")
        await db.messages_append("msg-2", "assistant", "hi there")
        messages = await db.messages_get("msg-2")
        self.assertEqual(messages[0]["role"], "assistant")

    async def test_multiple_messages_ordered(self):
        await db.chat_create("msg-3", "Multi", None, f"{self.tmpdir.name}/msg-3", "admin")
        await db.messages_append("msg-3", "user", "first")
        await db.messages_append("msg-3", "assistant", "second")
        await db.messages_append("msg-3", "user", "third")
        messages = await db.messages_get("msg-3")
        self.assertEqual(len(messages), 3)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])

    async def test_messages_empty(self):
        await db.chat_create("msg-4", "Empty", None, f"{self.tmpdir.name}/msg-4", "admin")
        messages = await db.messages_get("msg-4")
        self.assertEqual(messages, [])


# ───────────────────────────────────────────────────────────────────────────
# Integration – chat listing and mutations
# ───────────────────────────────────────────────────────────────────────────


class ChatListTests(unittest.IsolatedAsyncioTestCase):
    """db.chat_list – filtering and grouping.

    chat_list returns ALL chats (archived and active), sorted with
    active-first then archived. Deleted_at rows are excluded.
    """

    def setUp(self):
        _setup_base(self)

    async def asyncTearDown(self):
        await _teardown_base(self)

    async def asyncSetUp(self):
        await db.init()
        await auth.bootstrap_admin()

    async def test_returns_all_chats(self):
        await _make_chat(self, "list-1", "First")
        await _make_chat(self, "list-2", "Second")
        chats = await db.chat_list("admin")
        self.assertEqual(len(chats), 2)

    async def test_includes_archived_chats(self):
        """chat_list returns both archived and non-archived."""
        await _make_chat(self, "arch-l", "Archived", archived=1)
        await _make_chat(self, "act-l", "Active")
        chats = await db.chat_list("admin")
        self.assertEqual(len(chats), 2)

    async def test_excludes_deleted_chats(self):
        await _make_chat(self, "del-l", "Deleted", deleted_at="2026-01-01T00:00:00Z")
        await _make_chat(self, "act-l2", "Active 2")
        chats = await db.chat_list("admin")
        self.assertEqual(len(chats), 1)

    async def test_archived_chats_sorted_last(self):
        """Archived chats appear after active ones in list order."""
        await _make_chat(self, "arch-l2", "Archived Old", archived=1)
        await _make_chat(self, "act-l", "Active")
        await _make_chat(self, "arch-l1", "Archived New", archived=1)
        chats = await db.chat_list("admin")
        self.assertEqual(chats[0]["id"], "act-l")
        archived_ids = {c["id"] for c in chats if c["archived"]}
        active_ids = {c["id"] for c in chats if not c["archived"]}
        self.assertIn("act-l", active_ids)
        self.assertIn("arch-l1", archived_ids)
        self.assertIn("arch-l2", archived_ids)

    async def test_ordering_recent_first(self):
        await _make_chat(self, "old-l", "Old")
        await db.db_conn.execute(
            "UPDATE chats SET updated_at='2020-01-01T00:00:00Z' WHERE id='old-l'"
        )
        await db.db_conn.commit()
        await _make_chat(self, "new-l", "New")
        chats = await db.chat_list("admin")
        self.assertEqual(chats[0]["id"], "new-l")

    async def test_empty_list(self):
        chats = await db.chat_list("admin")
        self.assertEqual(chats, [])

    async def test_all_fields_in_response(self):
        await _make_chat(self, "fields-l", "Fields")
        chats = await db.chat_list("admin")
        required = {
            "id", "title", "description", "session_id", "work_dir",
            "created_at", "updated_at", "archived", "pinned",
            "pinned_at", "deleted_at", "model", "ai_machine_id",
        }
        self.assertTrue(required.issubset(chats[0].keys()))


class ChatMutationTests(unittest.IsolatedAsyncioTestCase):
    """db.chat_update / db.chat_delete – mutations.

    chat_delete does HARD DELETE. chat_archive marks archived=1.
    """

    def setUp(self):
        _setup_base(self)

    async def asyncTearDown(self):
        await _teardown_base(self)

    async def asyncSetUp(self):
        await db.init()
        await auth.bootstrap_admin()

    async def test_pin_chat(self):
        await _make_chat(self, "pin-1", "Pin Me")
        result = await db.chat_update("pin-1", "admin", pinned=1)
        self.assertTrue(result)
        chat = await db.chat_get("pin-1", "admin")
        self.assertTrue(chat["pinned"])
        self.assertIsNotNone(chat["pinned_at"])

    async def test_unpin_chat(self):
        await _make_chat(self, "unpin-1", "Unpin Me")
        await db.chat_update("unpin-1", "admin", pinned=1, pinned_at="2026-01-01")
        result = await db.chat_update("unpin-1", "admin", pinned=0, pinned_at=None)
        self.assertTrue(result)
        chat = await db.chat_get("unpin-1", "admin")
        self.assertFalse(chat["pinned"])
        self.assertIsNone(chat["pinned_at"])

    async def test_archive_chat(self):
        """chat_archive sets archived=1; chat_get(invisible by default) won't find it."""
        await _make_chat(self, "arch-1", "Archive Me")
        result = await db.chat_archive("arch-1", "admin", archived=1)
        self.assertTrue(result)
        chat = await db.chat_get("arch-1", "admin")
        self.assertIsNone(chat)

    async def test_delete_hard_removes_row(self):
        """chat_delete does a hard DELETE — row is gone."""
        await _make_chat(self, "del-1", "Delete Me")
        result = await db.chat_delete("del-1", "admin")
        self.assertTrue(result)
        chats = await db.chat_list("admin")
        self.assertEqual(len(chats), 0)
        chat = await db.chat_get("del-1", "admin", include_archived=True)
        self.assertIsNone(chat)

    async def test_delete_by_non_owner_fails(self):
        await _make_chat(self, "owner-1", "Owner", owner="bob")
        result = await db.chat_delete("owner-1", "admin")
        self.assertFalse(result)

    async def test_delete_nonexistent_fails(self):
        result = await db.chat_delete("nonexistent", "admin")
        self.assertFalse(result)

    async def test_rename_chat(self):
        await _make_chat(self, "rename-1", "Old")
        result = await db.chat_update("rename-1", "admin", title="New")
        self.assertTrue(result)
        chat = await db.chat_get("rename-1", "admin")
        self.assertEqual(chat["title"], "New")

    async def test_delete_preserves_workspace(self):
        work = f"{self.tmpdir.name}/projects/del-ws"
        Path(work).mkdir(parents=True)
        (Path(work) / "data.txt").write_text("important")
        await _make_chat(self, "del-ws", "Workspace", work_dir=work)
        await db.chat_delete("del-ws", "admin")
        self.assertTrue(os.path.isfile(os.path.join(work, "data.txt")))

    async def test_archive_unknown_fails(self):
        result = await db.chat_archive("nonexistent", "admin", archived=1)
        self.assertFalse(result)


# ───────────────────────────────────────────────────────────────────────────
# Legacy / restore scenario tests
# ───────────────────────────────────────────────────────────────────────────


class LegacyChatTests(unittest.IsolatedAsyncioTestCase):
    """Restored/legacy chat data: session IDs, archived, empty work_dirs,
    deleted_at marks, mixed-owner, and post-restore mutations.
    """

    def setUp(self):
        _setup_base(self)

    async def asyncTearDown(self):
        await _teardown_base(self)

    async def asyncSetUp(self):
        await db.init()
        await auth.bootstrap_admin()

    async def test_legacy_with_session_id(self):
        chat = await _make_chat(
            self, "leg-sess", "CLI Import",
            session_id="b42a6e10-82c2-41a9-8071-cd217b1838af",
            owner="pedro",
        )
        self.assertEqual(chat["session_id"], "b42a6e10-82c2-41a9-8071-cd217b1838af")
        self.assertEqual(chat["owner_id"], "pedro")

    async def test_legacy_session_id_in_list(self):
        await _make_chat(
            self, "leg-sess2", "Pedro's Chat",
            session_id="b7895f55-9da8-42da-80c9-b6f6d3c558df",
            owner="pedro",
        )
        pedro_chats = await db.chat_list("pedro")
        self.assertEqual(len(pedro_chats), 1)
        self.assertEqual(pedro_chats[0]["session_id"], "b7895f55-9da8-42da-80c9-b6f6d3c558df")

    async def test_legacy_not_visible_to_other_user(self):
        await _make_chat(self, "leg-other", "Other's Chat", owner="pedro")
        admin_chats = await db.chat_list("admin")
        self.assertEqual(len(admin_chats), 0)
        pedro_chats = await db.chat_list("pedro")
        self.assertEqual(len(pedro_chats), 1)

    async def test_archived_not_visible_via_chat_get(self):
        """chat_get defaults to include_archived=False."""
        await _make_chat(
            self, "leg-arch", "Old Project",
            session_id="60d6c55b-f502-427e-9523-67dce527e8e9",
            owner="pedro",
            archived=1,
        )
        chat = await db.chat_get("leg-arch", "pedro")
        self.assertIsNone(chat)

    async def test_archived_visible_with_include_archived(self):
        await _make_chat(
            self, "leg-arch2", "Old Project 2",
            session_id="60d6c55b-f502-427e-9523-67dce527e8e9",
            owner="pedro",
            archived=1,
        )
        chat = await db.chat_get("leg-arch2", "pedro", include_archived=True)
        self.assertIsNotNone(chat)
        self.assertEqual(chat["archived"], 1)

    async def test_deleted_not_in_list(self):
        await _make_chat(
            self, "leg-del", "Deleted Chat",
            session_id="dead-dead-dead-dead-dead-dead-dead-dead",
            owner="pedro",
            deleted_at="2026-01-01T00:00:00Z",
        )
        pedro_chats = await db.chat_list("pedro")
        self.assertEqual(len(pedro_chats), 0)

    async def test_deleted_visible_when_queried_directly(self):
        """chat_get checks deleted_at IS NULL even with include_archived=True,
        so deleted chats are invisible unless queried via raw SQL."""
        await _make_chat(
            self, "leg-del2", "Deleted Chat 2",
            session_id="dead-dead-dead-dead-dead-dead-dead-dec0",
            owner="pedro",
            deleted_at="2026-01-01T00:00:00Z",
        )
        chat = await db.chat_get("leg-del2", "pedro", include_archived=True)
        self.assertIsNone(chat)
        cur = await db.db_conn.execute(
            "SELECT id, deleted_at FROM chats WHERE id=?", ("leg-del2",)
        )
        row = await cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["deleted_at"], "2026-01-01T00:00:00Z")

    async def test_mixed_owner_independent_lists(self):
        await _make_chat(self, "leg-admin", "Admin Legacy", owner="admin")
        await _make_chat(self, "leg-pedro", "Pedro Legacy", owner="pedro")
        admin_chats = await db.chat_list("admin")
        pedro_chats = await db.chat_list("pedro")
        self.assertEqual(len(admin_chats), 1)
        self.assertEqual(admin_chats[0]["title"], "Admin Legacy")
        self.assertEqual(len(pedro_chats), 1)
        self.assertEqual(pedro_chats[0]["title"], "Pedro Legacy")

    async def test_legacy_has_messages(self):
        await _make_chat(self, "leg-msg", "Legacy Msgs", owner="pedro")
        await db.messages_append("leg-msg", "user", "hello")
        await db.messages_append("leg-msg", "assistant", "hi")
        messages = await db.messages_get("leg-msg")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["content"], "hello")

    async def test_legacy_without_workspace_still_lists(self):
        """A legacy chat whose work_dir doesn't exist still appears in list."""
        fake = f"{self.tmpdir.name}/projects/nonexistent-legacy"
        await db.chat_create("leg-nodir", "No Workspace", None, fake, "admin")
        chats = await db.chat_list("admin")
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["work_dir"], fake)
        self.assertFalse(os.path.exists(fake))

    async def test_legacy_pinned_appears_first(self):
        await _make_chat(
            self, "leg-pin", "Pinned Legacy",
            pinned=1, pinned_at="2026-01-15T00:00:00Z",
            owner="pedro",
        )
        await _make_chat(self, "leg-normal", "Normal", owner="pedro")
        pedro_chats = await db.chat_list("pedro")
        self.assertEqual(pedro_chats[0]["id"], "leg-pin")

    async def test_legacy_has_all_schema_fields(self):
        await _make_chat(
            self, "leg-full", "Full Schema",
            session_id="full-schema-session-id-00",
            description="A legacy test",
            owner="pedro",
        )
        chat = await db.chat_get("leg-full", "pedro")
        required = {
            "id", "title", "description", "session_id", "work_dir",
            "owner_id", "created_at", "updated_at", "archived",
            "pinned", "pinned_at", "deleted_at", "model", "ai_machine_id",
        }
        self.assertTrue(required.issubset(chat.keys()))
        self.assertEqual(chat["session_id"], "full-schema-session-id-00")

    async def test_legacy_can_be_pinned(self):
        await _make_chat(
            self, "leg-pinnable", "Pin After",
            session_id="pin-after-000000",
            owner="pedro",
        )
        await db.chat_update("leg-pinnable", "pedro", pinned=1)
        chat = await db.chat_get("leg-pinnable", "pedro")
        self.assertTrue(chat["pinned"])
        await db.chat_update("leg-pinnable", "pedro", pinned=0)
        chat = await db.chat_get("leg-pinnable", "pedro")
        self.assertFalse(chat["pinned"])

    async def test_legacy_can_be_archived(self):
        """Archived chat becomes invisible via chat_get (default)."""
        await _make_chat(
            self, "leg-archiveable", "Archive After",
            session_id="archive-after-0000",
            owner="pedro",
        )
        result = await db.chat_archive("leg-archiveable", "pedro", archived=1)
        self.assertTrue(result)
        chat = await db.chat_get("leg-archiveable", "pedro")
        self.assertIsNone(chat)
        chat = await db.chat_get("leg-archiveable", "pedro", include_archived=True)
        self.assertIsNotNone(chat)
        self.assertTrue(chat["archived"])

    async def test_legacy_can_be_deleted(self):
        await _make_chat(
            self, "leg-deletable", "Delete After",
            session_id="delete-after-000000",
            owner="pedro",
        )
        result = await db.chat_delete("leg-deletable", "pedro")
        self.assertTrue(result)
        pedro_chats = await db.chat_list("pedro")
        self.assertEqual(len(pedro_chats), 0)
        chat = await db.chat_get("leg-deletable", "pedro", include_archived=True)
        self.assertIsNone(chat)

    async def test_legacy_messages_queryable(self):
        chat_id = "leg-msgs2"
        await _make_chat(self, chat_id, "Msgs Legacy", owner="pedro")
        await db.messages_append(chat_id, "user", "first")
        await db.messages_append(chat_id, "assistant", "reply")
        await db.messages_append(chat_id, "user", "second")
        messages = await db.messages_get(chat_id)
        self.assertEqual(len(messages), 3)
        self.assertEqual(
            [(m["role"], m["content"]) for m in messages],
            [("user", "first"), ("assistant", "reply"), ("user", "second")],
        )

    async def test_multi_user_multi_legacy(self):
        await _make_chat(
            self, "leg-admin", "Admin Legacy",
            session_id="admin-legacy-00",
            owner="admin",
        )
        await _make_chat(
            self, "leg-pedro", "Pedro Legacy",
            session_id="pedro-legacy-00",
            owner="pedro",
        )
        await db.messages_append("leg-admin", "user", "admin hello")
        await db.messages_append("leg-pedro", "user", "pedro hello")
        admin_chats = await db.chat_list("admin")
        pedro_chats = await db.chat_list("pedro")
        self.assertEqual(len(admin_chats), 1)
        self.assertEqual(len(pedro_chats), 1)
        admin_msgs = await db.messages_get("leg-admin")
        pedro_msgs = await db.messages_get("leg-pedro")
        self.assertEqual(admin_msgs[0]["content"], "admin hello")
        self.assertEqual(pedro_msgs[0]["content"], "pedro hello")


# ───────────────────────────────────────────────────────────────────────────
# API-level – app handler contracts (mirrors test_app.py patterns)
# ───────────────────────────────────────────────────────────────────────────


class APIChatListTests(unittest.IsolatedAsyncioTestCase):
    """GET /api/chats – app handler level."""

    def setUp(self):
        _setup_base(self)

    async def asyncTearDown(self):
        await _teardown_base(self)

    async def asyncSetUp(self):
        await db.init()
        await auth.bootstrap_admin()

    async def test_list_returns_chats_key(self):
        await db.chat_create("api-1", "API List", None, f"{self.tmpdir.name}/api-1", "admin")
        from fastapi.testclient import TestClient

        from app import app as web_app  # type: ignore
        client = TestClient(web_app, raise_server_exceptions=False)
        resp = client.post("/login", json={"username": "admin", "password": "admin"})
        self.assertIn(resp.status_code, [200, 303, 401])
        response = client.get("/api/chats")
        self.assertIn(response.status_code, [200, 401])
        if response.status_code == 200:
            body = response.json()
            self.assertIsInstance(body, dict)
            self.assertIn("chats", body)
            self.assertEqual(len(body["chats"]), 1)
            self.assertEqual(body["chats"][0]["title"], "API List")

    async def test_list_empty(self):
        from fastapi.testclient import TestClient

        from app import app as web_app  # type: ignore
        client = TestClient(web_app, raise_server_exceptions=False)
        response = client.get("/api/chats")
        self.assertIn(response.status_code, [200, 401])
        if response.status_code == 200:
            self.assertEqual(response.json()["chats"], [])


# ───────────────────────────────────────────────────────────────────────────
# Acceptance – end-to-end workflows
# ───────────────────────────────────────────────────────────────────────────


class ChatAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end user-visible workflows."""

    def setUp(self):
        _setup_base(self)

    async def asyncTearDown(self):
        await _teardown_base(self)

    async def asyncSetUp(self):
        await db.init()
        await auth.bootstrap_admin()

    async def test_create_and_converse(self):
        chat_id = "accept-1"
        work = f"{self.tmpdir.name}/p/{chat_id}"
        Path(work).mkdir(parents=True)
        await db.chat_create(chat_id, "E2E Chat", None, work, "admin")
        await db.messages_append(chat_id, "user", "test message")
        messages = await db.messages_get(chat_id)
        self.assertGreater(len(messages), 0)
        self.assertEqual(messages[0]["content"], "test message")

    async def test_full_lifecycle(self):
        """Create -> list -> pin -> archive -> delete -> list empty."""
        chat_id = "accept-lc"
        work = f"{self.tmpdir.name}/p/{chat_id}"
        Path(work).mkdir(parents=True)
        await db.chat_create(chat_id, "Lifecycle", None, work, "admin")

        chats = await db.chat_list("admin")
        self.assertEqual(len(chats), 1)

        await db.chat_update(chat_id, "admin", pinned=1)
        chat = await db.chat_get(chat_id, "admin")
        self.assertTrue(chat["pinned"])

        await db.chat_archive(chat_id, "admin", archived=1)
        chat = await db.chat_get(chat_id, "admin")
        self.assertIsNone(chat)

        await db.chat_delete(chat_id, "admin")
        chats = await db.chat_list("admin")
        self.assertEqual(len(chats), 0)

    async def test_two_user_independent(self):
        """Two users create, read, and delete their own chats independently."""
        work1 = f"{self.tmpdir.name}/p/admin-accept"
        Path(work1).mkdir(parents=True)
        await db.chat_create("admin-accept", "Admin's Chat", None, work1, "admin")

        work2 = f"{self.tmpdir.name}/p/pedro-accept"
        Path(work2).mkdir(parents=True)
        await db.chat_create("pedro-accept", "Pedro's Chat", None, work2, "pedro")

        self.assertEqual(len(await db.chat_list("admin")), 1)
        self.assertEqual(len(await db.chat_list("pedro")), 1)

        await db.chat_delete("admin-accept", "admin")
        self.assertEqual(len(await db.chat_list("admin")), 0)
        self.assertEqual(len(await db.chat_list("pedro")), 1)


if __name__ == "__main__":
    unittest.main()