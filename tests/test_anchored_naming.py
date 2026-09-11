"""Anchored naming: chat IDs stay static, only the suffix updates.

Tests the full naming code path (including the DB write) in-process
without needing a full integration setup with TestClient.
"""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import auth
import config
import db
from routes import chats as chat_routes
from routes.naming import generate_name as _gen

# Use a real UUID — the 'admin' literal was migrated out.
_OWNER = "8e4af31c7eb8421bb7ff99168f923ed5"


class AnchoredNamingTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def _make_mock_get(self, initial_title):
        """Return a chat_get that reads from the DB on the second call.

        _write_agent_name calls chat_get twice: once before the update,
        once after. The first call returns initial_title so the logic
        decides to update. The second call reads the real DB so the
        assertion sees the updated value too.
        """
        call_count = {"n": 0}

        async def _mock_get(chat_id_arg, owner, include_archived=False):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {
                    "id": chat_id_arg,
                    "title": initial_title,
                    "owner_id": _OWNER,
                }
            # Second call: read the actual DB (the write already happened).
            cur = await db.db_conn.execute(
                "SELECT id, title, owner_id FROM chats WHERE id = ? AND owner_id = ?",
                (chat_id_arg, _OWNER),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

        return _mock_get

    async def test_first_turn_uses_chat_id_prefix(self):
        """First turn writes the full name with a 6-char hex prefix."""
        chat_id = "abcdef0123456789"
        session_id = "sess00000000000001"
        await db.chat_create(chat_id, "Untitled", None, f"{self.tmp.name}/proj", _OWNER)

        with patch.object(db, "write_claude_session_file"), \
             patch.object(chat_routes, "_resolve_transport_name", AsyncMock(return_value="kali")), \
             patch.object(chat_routes.db, "chat_get", await self._make_mock_get("Untitled")):
            await chat_routes._write_agent_name(
                session_id, "fix memory leak in app", chat_id, _OWNER
            )

        chat = await db.chat_get(chat_id, _OWNER)
        self.assertIn(" - ", chat["title"])
        self.assertTrue(chat["title"].startswith(chat_id[:6] + " - "))
        self.assertNotEqual(chat["title"], "Untitled")

    async def test_second_turn_updates_suffix_only(self):
        """Second turn preserves the prefix and only changes the suffix."""
        chat_id = "fedcba9876543210"
        session_id = "sess00000000000002"

        # First turn.
        await db.chat_create(chat_id, "Untitled", None, f"{self.tmp.name}/proj", _OWNER)
        with patch.object(db, "write_claude_session_file"), \
             patch.object(chat_routes, "_resolve_transport_name", AsyncMock(return_value="kali")), \
             patch.object(chat_routes.db, "chat_get", await self._make_mock_get("Untitled")):
            await chat_routes._write_agent_name(
                session_id, "first task here", chat_id, _OWNER
            )

        chat1 = await db.chat_get(chat_id, _OWNER)
        self.assertIn(" - ", chat1["title"])

        # Second turn.
        with patch.object(db, "write_claude_session_file"), \
             patch.object(chat_routes, "_resolve_transport_name", AsyncMock(return_value="kali")), \
             patch.object(chat_routes.db, "chat_get", await self._make_mock_get(chat1["title"])):
            await chat_routes._write_agent_name(
                session_id, "second task later", chat_id, _OWNER
            )

        chat2 = await db.chat_get(chat_id, _OWNER)
        self.assertNotEqual(chat1["title"], chat2["title"])

        prefix = chat1["title"].rsplit(" - ", 1)[0]
        self.assertTrue(chat2["title"].startswith(prefix + " - "))

    async def test_a_user_typed_title_takes_the_task_slot_once(self):
        """A title the user typed at creation is folded into the standard
        {transport} : {n} : {task} skeleton, with their own words verbatim
        in the task slot -- not appended to with " - {latest task}" on
        every turn (the old behavior, which grew without bound since a
        title with no " - " in it was treated as the whole prefix)."""
        chat_id = "aaaaaa0000000000"
        session_id = "sess00000000000003"

        await db.chat_create(chat_id, "voice-chat-app", None, f"{self.tmp.name}/proj", _OWNER)
        with patch.object(db, "write_claude_session_file"), \
             patch.object(chat_routes, "_resolve_transport_name", AsyncMock(return_value="kali")), \
             patch.object(chat_routes.db, "chat_get", await self._make_mock_get("voice-chat-app")):
            await chat_routes._write_agent_name(
                session_id, "do something new", chat_id, _OWNER
            )

        chat = await db.chat_get(chat_id, _OWNER)
        self.assertTrue(chat["title"].endswith(": voice-chat-app"))
        self.assertRegex(chat["title"], r"^kali : \d+ : voice-chat-app$")

    async def test_a_user_typed_title_never_updates_again(self):
        """Once converted, the user's words are fixed -- a second turn's
        task summary must not replace or append to them."""
        chat_id = "bbbbbb0000000000"
        session_id = "sess00000000000004"

        await db.chat_create(chat_id, "voice-chat-app", None, f"{self.tmp.name}/proj", _OWNER)
        with patch.object(db, "write_claude_session_file"), \
             patch.object(chat_routes, "_resolve_transport_name", AsyncMock(return_value="kali")), \
             patch.object(chat_routes.db, "chat_get", await self._make_mock_get("voice-chat-app")):
            await chat_routes._write_agent_name(
                session_id, "do something new", chat_id, _OWNER
            )
        chat1 = await db.chat_get(chat_id, _OWNER)

        with patch.object(db, "write_claude_session_file"), \
             patch.object(chat_routes, "_resolve_transport_name", AsyncMock(return_value="kali")), \
             patch.object(chat_routes.db, "chat_get", await self._make_mock_get(chat1["title"])):
            await chat_routes._write_agent_name(
                session_id, "a completely different task", chat_id, _OWNER
            )
        chat2 = await db.chat_get(chat_id, _OWNER)

        self.assertEqual(chat1["title"], chat2["title"])

    async def test_naming_task_part_strips_transport_prefix(self):
        """_naming_task_part removes the 'transport : n :' prefix."""
        name = _gen("kali", "fix memory leak")
        clean = chat_routes._naming_task_part(name)
        self.assertFalse(clean.startswith("kali :"))
