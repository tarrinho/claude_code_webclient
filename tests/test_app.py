#!/usr/bin/env python3
"""App-layer tests for conversation management APIs.

Covers: list (pinned fields), pin/unpin, delete 404, Markdown export,
title/description/archive mutations, workspace preservation.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app
import auth
import config
import db


# ── Tests ──────────────────────────────────────────────────────────────────────

class ChatListTests(unittest.IsolatedAsyncioTestCase):
    """GET /api/chats must include pinned / pinned_at in the response."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()
        self._session = SimpleNamespace(
            user="admin",
        )

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_list_includes_pinned_and_pinned_at(self):
        chat_id = "a" * 32
        await db.chat_create(chat_id, "My Chat", None, f"{self.tmp.name}/proj", "admin")
        chats = await db.chat_list("admin")
        entry = chats[0]
        self.assertIn("pinned", entry)
        self.assertIn("pinned_at", entry)
        self.assertEqual(entry["pinned"], 0)
        self.assertIsNone(entry["pinned_at"])

    async def test_pinned_chat_has_pinned_at(self):
        chat_id = "b" * 32
        await db.chat_create(chat_id, "Pinned", None, f"{self.tmp.name}/proj2", "admin")
        await db.chat_update(chat_id, "admin", pinned=1, pinned_at="2026-01-01T00:00:00Z")
        chats = await db.chat_list("admin")
        self.assertEqual(chats[0]["pinned"], 1)
        self.assertEqual(chats[0]["pinned_at"], "2026-01-01T00:00:00Z")


class PinMutationTests(unittest.IsolatedAsyncioTestCase):
    """PATCH /api/chats/{id} with pinned=true/false."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_pin_sets_pinned_at(self):
        chat_id = "c" * 32
        await db.chat_create(chat_id, "To Pin", None, f"{self.tmp.name}/proj", "admin")
        patched = await db.chat_update(chat_id, "admin", pinned=1)
        self.assertTrue(patched)
        chat = await db.chat_get(chat_id, "admin")
        self.assertTrue(chat["pinned"])
        self.assertIsNotNone(chat["pinned_at"])

    async def test_unpin_cleared_pinned_at(self):
        chat_id = "d" * 32
        await db.chat_create(chat_id, "Unpin", None, f"{self.tmp.name}/proj", "admin")
        await db.chat_update(chat_id, "admin", pinned=1, pinned_at="2026-01-01T00:00:00Z")
        patched = await db.chat_update(chat_id, "admin", pinned=0, pinned_at=None)
        self.assertTrue(patched)
        chat = await db.chat_get(chat_id, "admin")
        self.assertFalse(chat["pinned"])
        self.assertIsNone(chat["pinned_at"])

    async def test_pin_unknown_chat_fails(self):
        result = await db.chat_update("nonexistent", "admin", pinned=1, pinned_at="2026-01-01T00:00:00Z")
        self.assertFalse(result)


class DeleteMutationTests(unittest.IsolatedAsyncioTestCase):
    """DELETE /api/chats/{id} returns 404 when chat doesn't exist,
    and actually removes records when it does."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_delete_fails_when_missing(self):
        result = await db.chat_delete("nonexistent", "admin")
        self.assertFalse(result)

    async def test_delete_removes_chat_and_messages_but_not_workspace(self):
        chat_id = "e" * 32
        work_dir = Path(self.tmp.name) / "projects" / "delete-me"
        work_dir.mkdir(parents=True)
        (work_dir / "test.txt").write_text("data")
        await db.chat_create(chat_id, "Delete Me", None, str(work_dir), "admin")
        await db.messages_append(chat_id, "user", "hello")
        self.assertTrue(await db.chat_delete(chat_id, "admin"))
        self.assertIsNone(await db.chat_get(chat_id, "admin", include_archived=True))
        self.assertEqual(await db.messages_get(chat_id), [])
        self.assertTrue(work_dir.exists())
        self.assertEqual((work_dir / "test.txt").read_text(), "data")

    async def test_delete_by_wrong_user_fails(self):
        chat_id = "f" * 32
        await db.chat_create(chat_id, "Mine", None, f"{self.tmp.name}/proj", "admin")
        self.assertFalse(await db.chat_delete(chat_id, "other-user"))


class ExportTests(unittest.IsolatedAsyncioTestCase):
    """GET /api/chats/{id}/export returns a Markdown attachment."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_export_rendering(self):
        chat_id = "g" * 32
        await db.chat_create(chat_id, "Export Me", "A description", f"{self.tmp.name}/proj", "admin")
        await db.messages_append(chat_id, "user", "question")
        await db.messages_append(chat_id, "assistant", "answer")
        chat = await db.chat_get(chat_id, "admin", include_archived=True)
        messages = await db.messages_get(chat_id)
        md = app._render_chat_markdown(chat, messages)
        self.assertIn("# Export Me", md)
        self.assertIn("> A description", md)
        self.assertIn("question", md)
        self.assertIn("answer", md)
        self.assertIn("- Workspace:", md)
        self.assertIn("- Session:", md)

    async def test_export_404_for_missing(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_export(
                SimpleNamespace(state=SimpleNamespace(session={"user": "admin"})),
                "nonexistent",
            )
        self.assertEqual(ctx.exception.status_code, 404)


class ChatTitleDescriptionTests(unittest.IsolatedAsyncioTestCase):
    """PATCH /api/chats/{id} with title / description."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_title_update(self):
        chat_id = "h" * 32
        await db.chat_create(chat_id, "Old Title", None, f"{self.tmp.name}/proj", "admin")
        self.assertTrue(await db.chat_update(chat_id, "admin", title="New Title"))
        chat = await db.chat_get(chat_id, "admin")
        self.assertEqual(chat["title"], "New Title")

    async def test_description_update(self):
        chat_id = "i" * 32
        await db.chat_create(chat_id, "Chat", "first desc", f"{self.tmp.name}/proj", "admin")
        self.assertTrue(await db.chat_update(chat_id, "admin", description="second"))
        chat = await db.chat_get(chat_id, "admin")
        self.assertEqual(chat["description"], "second")

    async def test_archive_toggle(self):
        chat_id = "j" * 32
        await db.chat_create(chat_id, "Archive", None, f"{self.tmp.name}/proj", "admin")
        self.assertTrue(await db.chat_update(chat_id, "admin", archived=1))
        chat = await db.chat_get(chat_id, "admin", include_archived=True)
        self.assertTrue(chat["archived"])
        self.assertTrue(await db.chat_update(chat_id, "admin", archived=0))
        chat2 = await db.chat_get(chat_id, "admin")
        self.assertFalse(chat2["archived"])

    async def test_truncate_title_via_patch_handler(self):
        """The app handler (not the raw db) truncates titles to 200 chars."""
        chat_id = "k" * 32
        work_dir = Path(self.tmp.name) / "projects" / "trunc"
        work_dir.mkdir(parents=True, exist_ok=True)
        await db.chat_create(chat_id, "Old", None, str(work_dir), "admin")

        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin"}),
            json=AsyncMock(return_value={"title": "x" * 300}),
        )
        response = await app.handle_chat_patch(request, chat_id)
        self.assertEqual(response.status_code, 200)

        chat = await db.chat_get(chat_id, "admin")
        self.assertEqual(chat["title"], "x" * 200)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Full FastAPI route tests with mock auth/session to cover the handlers end-to-end."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_chat_delete_404_missing(self):
        from fastapi import HTTPException

        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin"}),
        )
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_delete(request, "nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_chat_delete_success(self):
        from fastapi import HTTPException

        chat_id = "l" * 32
        work_dir = Path(self.tmp.name) / "projects" / "ok"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Ok", None, str(work_dir), "admin")
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin"}),
        )
        response = await app.handle_chat_delete(request, chat_id)
        self.assertEqual(response.status_code, 200)
        # Second call should raise 404
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_delete(request, chat_id)
        self.assertEqual(ctx.exception.status_code, 404)
        # Workspace still exists
        self.assertTrue(work_dir.exists())

    async def test_patch_rejects_non_boolean_flags(self):
        chat_id = "n" * 32
        work_dir = Path(self.tmp.name) / "projects" / "strict"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Strict", None, str(work_dir), "admin")
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin"}),
            json=AsyncMock(return_value={"pinned": "yes"}),
        )
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_patch(request, chat_id)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_patch_rejects_blank_title_and_caps_description(self):
        chat_id = "o" * 32
        work_dir = Path(self.tmp.name) / "projects" / "validate"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Valid", None, str(work_dir), "admin")
        from fastapi import HTTPException
        blank = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin"}),
            json=AsyncMock(return_value={"title": "   "}),
        )
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_patch(blank, chat_id)
        self.assertEqual(ctx.exception.status_code, 400)
        capped = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin"}),
            json=AsyncMock(return_value={"description": "d" * 600}),
        )
        await app.handle_chat_patch(capped, chat_id)
        chat = await db.chat_get(chat_id, "admin")
        self.assertEqual(len(chat["description"]), 500)

    async def test_chat_export_returns_markdown(self):
        chat_id = "m" * 32
        work_dir = Path(self.tmp.name) / "projects" / "export-test"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Export Test", "desc here", str(work_dir), "admin")
        await db.messages_append(chat_id, "user", "hi")
        await db.messages_append(chat_id, "assistant", "hello!")
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin"}),
        )
        response = await app.handle_chat_export(request, chat_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "text/markdown; charset=utf-8")
        self.assertIn("Export Test", response.body.decode("utf-8"))
        disp = response.headers.get("content-disposition", "")
        self.assertIn("export-test.md", disp)


if __name__ == "__main__":
    unittest.main()