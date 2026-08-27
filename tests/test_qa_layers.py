"""Layered QA coverage for WebConsole.

The classes are intentionally grouped by test level:

* UnitQA: pure helpers and isolated security/config behavior.
* IntegrationQA: interactions between the database, app handlers, and session files.
* ComponentAPIQA: API handler contracts with dependencies mocked at the boundary.
* SystemE2EQA: a complete chat turn through the app, database, and runner seam.
* AcceptanceUATQA: user-visible workflows mapped to product requirements.

All tests use local temporary state and fake runner/proxy boundaries. No live model,
Claude Code account, or network service is required.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import app
import auth
import config
import db
import runner


class UnitQA(unittest.TestCase):
    """Individual helpers and isolated functions/classes."""

    def test_slug_from_title_normalizes_and_caps(self):
        self.assertEqual(db.slug_from_title("  Hello, WebConsole!  "), "hello-webconsole")
        self.assertLessEqual(len(db.slug_from_title("x" * 100)), 40)
        self.assertEqual(db.slug_from_title("!!!"), "untitled")

    def test_slug_pattern_accepts_minimum_valid_slug(self):
        self.assertEqual(db.slug_pattern("a-1"), "a-1")
        self.assertIsNone(db.slug_pattern("ab"))
        self.assertIsNone(db.slug_pattern("-abc"))
        self.assertIsNone(db.slug_pattern("abc-"))
        self.assertIsNone(db.slug_pattern("ABC"))

    def test_timestamp_formats_seconds_and_milliseconds(self):
        self.assertEqual(db._format_timestamp(0), "1970-01-01T00:00:00Z")
        self.assertEqual(db._format_timestamp(1_700_000_000), db._format_timestamp(1_700_000_000_000))
        self.assertEqual(db._format_timestamp("not-a-time"), "")
        self.assertEqual(db._format_timestamp(None), "")

    def test_password_error_boundaries(self):
        self.assertIsNotNone(auth.password_error("short"))
        self.assertIsNone(auth.password_error("12345678"))
        self.assertIsNotNone(auth.password_error("x" * 257))

    def test_session_drop_invalidates_session(self):
        sid, csrf = auth.session_new("unit-user")
        self.assertTrue(csrf)
        self.assertEqual(auth.session_get(sid)["user"], "unit-user")
        auth.session_drop(sid)
        self.assertIsNone(auth.session_get(sid))

    def test_session_idle_expiry(self):
        sid, _ = auth.session_new("unit-user")
        with patch.object(auth.time, "time", return_value=auth.time.time() + config.SESSION_IDLE_S + 1):
            self.assertIsNone(auth.session_get(sid))

    def test_build_cmd_uses_argument_list_and_resume(self):
        with patch.object(runner.uuid, "uuid4", return_value="new-session"):
            fresh = runner._build_cmd_direct("$(touch /tmp/pwned)", None)
        resumed = runner._build_cmd_direct("hello", "existing-session")
        self.assertIn("$(touch /tmp/pwned)", fresh)
        self.assertNotIn("shell=True", repr(fresh))
        self.assertEqual(fresh[-2:], ["--session-id", "new-session"])
        self.assertEqual(resumed[-2:], ["--resume", "existing-session"])

    def test_build_env_strips_unapproved_host_variables(self):
        with patch.dict(os.environ, {"HOME": "/tmp/home", "SECRET_HOST_VALUE": "hidden"}, clear=True):
            env = runner._build_env()
        self.assertEqual(env["HOME"], "/tmp/home")
        self.assertNotIn("SECRET_HOST_VALUE", env)
        self.assertEqual(env["CLAUDE_CODE_SIMPLE"], "1")

    def test_frame_normalization_ignores_unknown_frame(self):
        import claude_proxy
        self.assertEqual(claude_proxy.normalise_claude_frame({"type": "unknown"}), [])

    def test_frame_normalization_flattens_multiple_text_blocks(self):
        import claude_proxy
        frames = claude_proxy.normalise_claude_frame({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "one"},
                {"type": "tool_use", "name": "x"},
                {"type": "text", "text": "two"},
            ]},
        })
        self.assertEqual([f["content"] for f in frames if f["type"] == "text"], ["one", "two"])


class TemporaryDBMixin:
    async def init_temp_db(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def close_temp_db(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()


class IntegrationQA(TemporaryDBMixin, unittest.IsolatedAsyncioTestCase):
    """Interactions between modules, SQLite, and session-file storage."""

    async def asyncSetUp(self):
        await self.init_temp_db()

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def test_database_enforces_owner_isolation(self):
        await db.chat_create("owned", "Owned", None, "/tmp/owned", "alice")
        self.assertIsNotNone(await db.chat_get("owned", "alice"))
        self.assertIsNone(await db.chat_get("owned", "bob"))
        self.assertFalse(await db.chat_delete("owned", "bob"))

    async def test_database_message_batch_preserves_order(self):
        await db.chat_create("batch", "Batch", None, "/tmp/batch", "alice")
        ids = await db.messages_batch("batch", [("user", "one"), ("assistant", "two")])
        messages = await db.messages_get("batch")
        self.assertEqual(len(ids), 2)
        self.assertEqual([m["content"] for m in messages], ["one", "two"])

    async def test_delete_removes_messages_but_preserves_workspace(self):
        work_dir = Path(self.tmp.name) / "projects" / "keep"
        work_dir.mkdir(parents=True)
        marker = work_dir / "marker.txt"
        marker.write_text("keep")
        await db.chat_create("delete", "Delete", None, str(work_dir), "alice")
        await db.messages_append("delete", "user", "hello")
        self.assertTrue(await db.chat_delete("delete", "alice"))
        self.assertTrue(marker.exists())
        self.assertEqual(await db.messages_get("delete"), [])

    async def test_session_file_round_trip(self):
        session_dir = Path(self.tmp.name) / "sessions"
        with patch.object(db, "_CLAUDE_SESSIONS_DIR", session_dir):
            db.write_claude_session_file("session-roundtrip", "Round Trip", "/tmp/work")
            sessions = await db.read_claude_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["sessionId"], "session-roundtrip")
        self.assertEqual(sessions[0]["name"], "Round Trip")
        self.assertEqual(sessions[0]["cwd"], "/tmp/work")

    async def test_session_reader_skips_invalid_and_noninteractive_files(self):
        session_dir = Path(self.tmp.name) / "sessions"
        session_dir.mkdir()
        (session_dir / "bad.json").write_text("not-json")
        (session_dir / "batch.json").write_text(json.dumps({"kind": "batch", "name": "Batch"}))
        (session_dir / "good.json").write_text(json.dumps({
            "kind": "interactive", "name": "Good", "sessionId": "good-id",
            "cwd": "/tmp/good", "startedAt": 1_700_000_000_000, "updatedAt": 1_700_000_001_000,
        }))
        with patch.object(db, "_CLAUDE_SESSIONS_DIR", session_dir):
            sessions = await db.read_claude_sessions()
        self.assertEqual([s["sessionId"] for s in sessions], ["good-id"])

    async def test_resume_handler_links_web_chat_to_cli_session(self):
        request = SimpleNamespace(state=SimpleNamespace(session={"user": "alice"}))
        with patch.object(db, "write_claude_session_file") as write_file:
            response = await app.handle_sessions_resume(request, "cli-session-123")
        payload = json.loads(response.body)
        self.assertEqual(payload["session_id"], "cli-session-123")
        chat = await db.chat_get(payload["id"], "alice", include_archived=True)
        self.assertEqual(chat["session_id"], "cli-session-123")
        write_file.assert_called_once()

    async def test_sessions_list_merges_cli_and_web_records(self):
        await db.chat_create("web-chat", "Web Chat", None, "/tmp/web", "alice")
        request = SimpleNamespace(state=SimpleNamespace(session={"user": "alice"}))
        with patch.object(db, "read_claude_sessions", AsyncMock(return_value=[{
            "id": "cli", "name": "CLI Session", "cwd": "/tmp/cli", "kind": "interactive",
            "startedAt": "", "updatedAt": "", "sessionId": "cli",
        }])):
            response = await app.handle_sessions_list(request)
        payload = json.loads(response.body)
        self.assertEqual({item["id"] for item in payload["sessions"]}, {"cli", "web-chat"})


class ComponentAPIQA(unittest.IsolatedAsyncioTestCase):
    """Single API/service contracts with dependencies mocked at boundaries."""

    async def test_submit_message_rejects_empty_prompt_without_runner_call(self):
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "alice"}),
            json=AsyncMock(return_value={"content": "   "}),
        )
        chat = {"id": "chat", "session_id": None, "work_dir": "/tmp/work", "owner_id": "alice"}
        with patch.object(app.db, "chat_get", AsyncMock(return_value=chat)), \
             patch.object(app.runner, "run_turn", AsyncMock()) as run_turn:
            with self.assertRaises(HTTPException) as ctx:
                await app.handle_submit_message(request, "chat")
        self.assertEqual(ctx.exception.status_code, 400)
        run_turn.assert_not_awaited()

    async def test_submit_message_persists_user_and_assistant_messages(self):
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "alice"}),
            json=AsyncMock(return_value={"content": "hello"}),
        )
        chat = {"id": "chat", "session_id": None, "work_dir": "/tmp/work", "owner_id": "alice"}
        with patch.object(app.db, "chat_get", AsyncMock(return_value=chat)), \
             patch.object(app.db, "messages_append", AsyncMock()) as append, \
             patch.object(app.db, "chat_set_session", AsyncMock()) as set_session, \
             patch.object(app.runner, "run_turn", AsyncMock(return_value=(["answer"], "sid"))):
            response = await app.handle_submit_message(request, "chat")
        self.assertEqual(response.status_code, 200)
        append.assert_any_await("chat", "user", "hello")
        append.assert_any_await("chat", "assistant", "answer")
        set_session.assert_awaited_once_with("chat", "sid")

    async def test_patch_rejects_unknown_fields(self):
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "alice"}),
            json=AsyncMock(return_value={"owner_id": "bob"}),
        )
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_patch(request, "chat")
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_route_contract_contains_required_api_methods(self):
        routes = {
            (method, route.path)
            for route in app.app.routes
            for method in (getattr(route, "methods", None) or set())
        }
        self.assertIn(("GET", "/api/chats"), routes)
        self.assertIn(("POST", "/api/chats/{chat_id}/stream"), routes)
        self.assertIn(("GET", "/api/sessions"), routes)
        self.assertIn(("POST", "/api/sessions/{session_id}/resume"), routes)

    async def test_stream_requires_independent_cookie_auth(self):
        request = SimpleNamespace(cookies={}, headers={}, state=SimpleNamespace(session=None))
        with self.assertRaises(HTTPException) as ctx:
            await app.stream_handler(request, "chat")
        self.assertEqual(ctx.exception.status_code, 401)


class SystemE2EQA(TemporaryDBMixin, unittest.IsolatedAsyncioTestCase):
    """Full local flow through app handlers, SQLite, and a fake Claude turn."""

    async def asyncSetUp(self):
        await self.init_temp_db()
        self.owner = "e2e-user"
        self.request = SimpleNamespace(state=SimpleNamespace(session={"user": self.owner}))

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def test_create_submit_and_reload_transcript(self):
        create_request = SimpleNamespace(
            state=SimpleNamespace(session={"user": self.owner}),
            json=AsyncMock(return_value={"title": "End to End", "description": "full flow"}),
        )
        created = await app.handle_chat_create(create_request)
        created_payload = json.loads(created.body)
        chat_id = created_payload["id"]
        with patch.object(app.runner, "run_turn", AsyncMock(return_value=(["reply", " complete"], "e2e-session"))):
            submit_request = SimpleNamespace(
                state=SimpleNamespace(session={"user": self.owner}),
                json=AsyncMock(return_value={"content": "hello"}),
            )
            response = await app.handle_submit_message(submit_request, chat_id)
        self.assertEqual(json.loads(response.body)["response"], "reply complete")
        loaded = await app.handle_chat_get(self.request, chat_id)
        payload = json.loads(loaded.body)
        self.assertEqual([m["role"] for m in payload["messages"]], ["user", "assistant"])
        self.assertEqual(payload["chat"]["session_id"], "e2e-session")


class AcceptanceUATQA(TemporaryDBMixin, unittest.IsolatedAsyncioTestCase):
    """Business-facing acceptance checks mapped to sidebar and chat requirements."""

    async def asyncSetUp(self):
        await self.init_temp_db()
        self.request = SimpleNamespace(state=SimpleNamespace(session={"user": "uat-user"}))

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def test_user_can_resume_cli_session_into_web_chat(self):
        with patch.object(db, "write_claude_session_file"):
            resumed = await app.handle_sessions_resume(self.request, "cli-uat")
        resumed_payload = json.loads(resumed.body)
        listed = await app.handle_sessions_list(self.request)
        items = json.loads(listed.body)["sessions"]
        matching = [item for item in items if item["id"] == resumed_payload["id"]]
        self.assertEqual(len(matching), 1)
        self.assertTrue(matching[0]["webchat"])
        self.assertEqual(matching[0]["sessionId"], "cli-uat")

    async def test_user_can_export_completed_conversation(self):
        await db.chat_create("uat-chat", "Release Notes", "Acceptance", "/tmp/uat", "uat-user")
        await db.messages_append("uat-chat", "user", "Summarize")
        await db.messages_append("uat-chat", "assistant", "Done")
        response = await app.handle_chat_export(self.request, "uat-chat")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Release Notes", response.body.decode())
        self.assertIn("Done", response.body.decode())
        self.assertIn("attachment", response.headers["content-disposition"])

    async def test_user_cannot_access_another_users_chat(self):
        await db.chat_create("private", "Private", None, "/tmp/private", "someone-else")
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_get(self.request, "private")
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
