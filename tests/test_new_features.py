"""Tests for new features: search, backup/restore, and chat fork."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app
import auth
import config
import db
from routes import misc as misc_routes


class ChatSearchTests(unittest.IsolatedAsyncioTestCase):
    """Full-text chat search via FTS5."""

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

    async def test_empty_search_returns_no_results(self):
        results = await db.chat_search("admin", "")
        self.assertEqual(results, [])

    async def test_search_matches_message_content(self):
        chat_id = "a" * 32
        work_dir = Path(self.tmp.name) / "proj"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Security Audit", None, str(work_dir), "admin")
        await db.messages_append(chat_id, "user", "review this code")
        await db.messages_append(chat_id, "assistant", "found a vulnerability")
        results = await db.chat_search("admin", "vulnerability")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], chat_id)
        self.assertIn("snippet", results[0])
        self.assertIn("vulnerability", results[0]["snippet"])

    async def test_search_matches_title(self):
        chat_id = "b" * 32
        work_dir = Path(self.tmp.name) / "proj"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Security Audit", None, str(work_dir), "admin")
        await db.messages_append(chat_id, "user", "hello")
        results = await db.chat_search("admin", "Security")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], chat_id)

    async def test_search_respects_owner_isolation(self):
        chat_id = "c" * 32
        work_dir = Path(self.tmp.name) / "proj"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "My Chat", None, str(work_dir), "admin")
        await db.messages_append(chat_id, "user", "secret data")
        results = await db.chat_search("other-user", "secret")
        self.assertEqual(len(results), 0)

    async def test_search_empty_query_returns_empty(self):
        results = await db.chat_search("admin", "   ")
        self.assertEqual(results, [])


class ChatSearchApiTests(unittest.IsolatedAsyncioTestCase):
    """POST /api/chats/search endpoint tests."""

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

    def _make_request(self, **extra):
        return SimpleNamespace(state=SimpleNamespace(session={"user": "admin"}), **extra)

    async def test_search_api_empty_query(self):
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_chat_search(
                self._make_request(json=AsyncMock(return_value={"query": ""}))
            )
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_search_api_no_query_key(self):
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_chat_search(
                self._make_request(json=AsyncMock(return_value={}))
            )
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_search_api_truncates_long_query(self):
        chat_id = "d" * 32
        work_dir = Path(self.tmp.name) / "proj"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Test", None, str(work_dir), "admin")
        await db.messages_append(chat_id, "user", "hello")
        req = self._make_request(
            json=AsyncMock(return_value={"query": "x" * 300})
        )
        resp = await app.handle_chat_search(req)
        data = json.loads(resp.body)
        self.assertIn("results", data)
        self.assertIn("count", data)

    async def test_search_api_returns_chat_details(self):
        chat_id = "e" * 32
        work_dir = Path(self.tmp.name) / "proj"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Search Test", None, str(work_dir), "admin")
        await db.messages_append(chat_id, "user", "find me please")
        req = self._make_request(
            json=AsyncMock(return_value={"query": "find me"})
        )
        resp = await app.handle_chat_search(req)
        data = json.loads(resp.body)
        self.assertEqual(data["count"], 1)
        result = data["results"][0]
        self.assertEqual(result["title"], "Search Test")
        self.assertIsInstance(result["snippet"], str)


class ChatForkTests(unittest.IsolatedAsyncioTestCase):
    """chat_fork creates a new chat with duplicated messages."""

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

    async def test_fork_creates_new_chat_with_forked_messages(self):
        chat_id = "f" * 32
        work_dir = Path(self.tmp.name) / "proj"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Original", None, str(work_dir), "admin")
        await db.messages_append(chat_id, "user", "first message")
        await db.messages_append(chat_id, "assistant", "reply here")

        new_chat = await db.chat_fork(chat_id, "admin")
        self.assertIsNotNone(new_chat)
        self.assertNotEqual(new_chat["id"], chat_id)
        self.assertTrue(new_chat["title"].endswith("(fork)"))

        messages = await db.messages_get(new_chat["id"])
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["content"], "first message")
        self.assertEqual(messages[1]["content"], "reply here")

    async def test_fork_preserves_model_and_machine(self):
        chat_id = "g" * 32
        work_dir = Path(self.tmp.name) / "proj"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Model Chat", None, str(work_dir), "admin")
        await db.chat_set_model(chat_id, "claude-opus-4-20250514")
        await db.chat_fork(chat_id, "admin")
        new_chats = [c for c in await db.chat_list("admin") if c["title"].endswith("(fork)")]
        self.assertEqual(len(new_chats), 1)
        self.assertEqual(new_chats[0]["model"], "claude-opus-4-20250514")

    async def test_fork_unknown_chat_returns_none(self):
        result = await db.chat_fork("nonexistent", "admin")
        self.assertIsNone(result)

    async def test_fork_preserves_messages_order(self):
        chat_id = "h" * 32
        work_dir = Path(self.tmp.name) / "proj"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Order Chat", None, str(work_dir), "admin")
        await db.messages_append(chat_id, "user", "msg1")
        await db.messages_append(chat_id, "assistant", "msg2")
        await db.messages_append(chat_id, "user", "msg3")
        await db.messages_append(chat_id, "assistant", "msg4")

        new_chat = await db.chat_fork(chat_id, "admin")
        messages = await db.messages_get(new_chat["id"])
        contents = [m["content"] for m in messages]
        self.assertEqual(contents, ["msg1", "msg2", "msg3", "msg4"])


class ChatForkApiTests(unittest.IsolatedAsyncioTestCase):
    """POST /api/chats/{id}/fork endpoint tests."""

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

    def _make_request(self, **extra):
        return SimpleNamespace(state=SimpleNamespace(session={"user": "admin"}), **extra)

    async def test_fork_api_returns_chat_id_and_title(self):
        chat_id = "i" * 32
        work_dir = Path(self.tmp.name) / "proj"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Fork Me", None, str(work_dir), "admin")

        resp = await app.handle_chat_fork(self._make_request(), chat_id)
        data = json.loads(resp.body)
        self.assertIn("id", data)
        self.assertEqual(data["title"], "Fork Me (fork)")
        self.assertIn("work_dir", data)

    async def test_fork_api_404_for_missing_chat(self):
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_chat_fork(
                self._make_request(), "nonexistent"
            )
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_fork_creates_distinct_workspace(self):
        chat_id = "j" * 32
        work_dir = Path(self.tmp.name) / "proj"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Workspace Fork", None, str(work_dir), "admin")

        resp = await app.handle_chat_fork(self._make_request(), chat_id)
        data = json.loads(resp.body)
        new_work_dir = Path(data["work_dir"])
        self.assertTrue(new_work_dir.exists())
        self.assertNotEqual(str(work_dir), str(new_work_dir))


class BackupRestoreTests(unittest.IsolatedAsyncioTestCase):
    """Database backup and restore."""

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

    async def test_backup_returns_gzip_data(self):
        data = await db.db_backup()
        self.assertIsInstance(data, bytes)
        self.assertGreater(len(data), 0)
        # Verify it's valid gzip by decompressing
        import gzip
        decompressed = gzip.decompress(data)
        self.assertGreater(len(decompressed), 0)
        # Should be valid SQLite (magic bytes)
        self.assertEqual(decompressed[:16], b'SQLite format 3\x00')

    async def test_backup_contains_chat_data(self):
        chat_id = "k" * 32
        work_dir = Path(self.tmp.name) / "proj"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Backup Test", None, str(work_dir), "admin")
        await db.messages_append(chat_id, "user", "data to backup")

        data = await db.db_backup()
        import gzip
        raw = gzip.decompress(data)
        self.assertIn(b"Backup Test", raw)
        self.assertIn(b"data to backup", raw)

    async def test_backup_preserves_session(self):
        chat_id = "l" * 32
        work_dir = Path(self.tmp.name) / "proj"
        work_dir.mkdir(parents=True)
        await db.chat_create(chat_id, "Session Backup", None, str(work_dir), "admin")
        await db.chat_set_session(chat_id, "session-abc")

        # Backup
        backup_data = await db.db_backup()

        # Restore to new DB
        await db.db_restore(backup_data)

        # Check data survived
        chat = await db.chat_get(chat_id, "admin")
        self.assertEqual(chat["session_id"], "session-abc")

    async def test_restore_rejects_invalid_data(self):
        result = await db.db_restore(b"not a gzip file at all")
        self.assertFalse(result)

    async def test_restore_rejects_empty_data(self):
        result = await db.db_restore(b"")
        self.assertFalse(result)


class BackupApiTests(unittest.IsolatedAsyncioTestCase):
    """Admin backup/restore endpoint tests."""

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

    def _make_request(self, **extra):
        return SimpleNamespace(state=SimpleNamespace(session={"user": "admin"}), **extra)

    def _make_admin_request(self, **extra):
        return SimpleNamespace(state=SimpleNamespace(session={"user": "admin", "role": "admin"}), **extra)

    async def test_backup_requires_admin(self):
        # Regular user (no role) cannot access backup
        req = self._make_request()
        req.state.session.pop("role", None)
        with self.assertRaises(app.HTTPException) as ctx:
            await misc_routes.handle_db_backup(req)
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_backup_admin_returns_gzip(self):
        resp = await misc_routes.handle_db_backup(self._make_admin_request())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.media_type, "application/gzip")
        disp = resp.headers.get("content-disposition", "")
        self.assertIn(".db.gz", disp)

    async def test_restore_requires_admin(self):
        req = self._make_request()
        req.state.session.pop("role", None)
        form = await self._make_form({"file": b"data"})
        req.form = AsyncMock(return_value=form)
        with self.assertRaises(app.HTTPException) as ctx:
            await misc_routes.handle_db_restore(req)
        self.assertEqual(ctx.exception.status_code, 403)

    async def _make_form(self, data):
        """Create a mock form object with binary data."""
        form = {}
        for key, value in data.items():
            mock_file = SimpleNamespace(filename=f"{key}.gz", read=AsyncMock(return_value=value))
            form[key] = mock_file
        return form

    async def test_restore_rejects_no_file(self):
        req = self._make_admin_request()
        req.form = AsyncMock(return_value={})
        with self.assertRaises(app.HTTPException) as ctx:
            await misc_routes.handle_db_restore(req)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_restore_rejects_empty_file(self):
        req = self._make_admin_request()
        form = await self._make_form({"file": b""})
        req.form = AsyncMock(return_value=form)
        with self.assertRaises(app.HTTPException) as ctx:
            await misc_routes.handle_db_restore(req)
        self.assertEqual(ctx.exception.status_code, 400)