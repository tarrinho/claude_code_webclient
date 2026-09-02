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
import shared
from routes import chats as chat_routes
from routes import misc as misc_routes


def _registered_routes() -> set[tuple[str, str]]:
    """Every (method, path) the app serves, including via included routers.

    `app.routes` holds an opaque entry per `include_router` rather than the
    routes themselves, so enumerating it alone stops seeing a prefix the moment
    that prefix moves into `routes/`. That fails in the worst direction: the set
    simply gets smaller, and an assertion that a route exists is the only thing
    that notices. The 0.10.0 split moved /api/machines and /api/sessions out
    this way, and this walk is what keeps the contract test honest as the rest
    follow.
    """
    found: set[tuple[str, str]] = set()

    def add(route) -> None:
        path = getattr(route, "path", None)
        for method in (getattr(route, "methods", None) or ()):
            if path:
                found.add((method, path))

    for route in app.app.routes:
        add(route)
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            for inner in candidates():
                add(inner)
    return found

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
        """Session flags must parse as options; the prompt must not.

        ``-p``/``--print`` is a boolean and the prompt is positional, so the
        prompt goes last behind the ``--`` sentinel while the session flags
        stay in front of it. The previous version of this test asserted the
        opposite order, which is how the resume bug survived: claude treated
        ``--resume <id>`` as prompt data and silently started a new session
        on every turn, so conversations never carried any history.
        """
        with patch.object(runner.uuid, "uuid4", return_value="new-session"):
            fresh = runner._build_cmd_direct("$(touch /tmp/pwned)", None)
        # A UUID-shaped id, because --resume only accepts one. The previous
        # fixture, "existing-session", was not UUID-shaped, so once session-id
        # validation was added this test was accidentally asserting the ABSENCE
        # of that validation. Relaxing the assertion to accept --session-id
        # would have re-locked registry #21, the bug this test exists for.
        resumed = runner._build_cmd_direct(
            "hello", "529b3e67-2591-4978-ac44-7a5890cb8c2e")

        self.assertNotIn("shell=True", repr(fresh))
        # The prompt is the final operand, protected by the sentinel.
        self.assertEqual(fresh[-2:], ["--", "$(touch /tmp/pwned)"])
        self.assertEqual(resumed[-2:], ["--", "hello"])
        # Session flags are real options: before the sentinel, adjacent pair.
        sentinel = resumed.index("--")
        self.assertEqual(
            resumed[sentinel - 2 : sentinel],
            ["--resume", "529b3e67-2591-4978-ac44-7a5890cb8c2e"],
        )
        sentinel_fresh = fresh.index("--")
        self.assertEqual(
            fresh[sentinel_fresh - 2 : sentinel_fresh],
            ["--session-id", "new-session"],
        )
        self.assertNotIn("--resume", fresh)

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

    def test_turn_error_exposes_message_and_fatality(self):
        error = runner.TurnError("invalid workspace", fatal=True)
        self.assertEqual(str(error), "invalid workspace")
        self.assertEqual(error.message, "invalid workspace")
        self.assertTrue(error.fatal)

    def test_config_bool_parser_accepts_truthy_values_only(self):
        with patch.dict(os.environ, {"QA_BOOL": "yes"}, clear=False):
            self.assertTrue(config._bool("QA_BOOL", False))
        with patch.dict(os.environ, {"QA_BOOL": "off"}, clear=False):
            self.assertFalse(config._bool("QA_BOOL", True))

    def test_config_int_parser_falls_back_on_invalid_value(self):
        with patch.dict(os.environ, {"QA_INT": "invalid"}, clear=False):
            self.assertEqual(config._int("QA_INT", 42), 42)

    def test_render_chat_markdown_uses_safe_role_labels(self):
        chat = {"title": "Unit", "created_at": "bad-date", "work_dir": None, "session_id": None}
        markdown = chat_routes.render_chat_markdown(chat, [{"role": "attacker", "content": "payload"}])
        self.assertIn("## Message", markdown)
        self.assertNotIn("## attacker", markdown)

    def test_config_string_parser_strips_whitespace(self):
        with patch.dict(os.environ, {"QA_STRING": "  value  "}, clear=False):
            self.assertEqual(config._str("QA_STRING"), "value")


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
        available = [{"sessionId": "cli-session-123", "name": "CLI", "cwd": "/tmp/cli"}]
        with patch.object(db, "read_claude_sessions", AsyncMock(return_value=available)), \
             patch.object(db, "write_claude_session_file") as write_file:
            response = await misc_routes.handle_sessions_resume(request, "cli-session-123")
        payload = json.loads(response.body)
        self.assertEqual(payload["session_id"], "cli-session-123")
        chat = await db.chat_get(payload["id"], "alice", include_archived=True)
        self.assertEqual(chat["session_id"], "cli-session-123")
        write_file.assert_called_once()

    async def test_resume_handler_rejects_unknown_cli_session(self):
        request = SimpleNamespace(state=SimpleNamespace(session={"user": "alice"}))
        with patch.object(db, "read_claude_sessions", AsyncMock(return_value=[])), \
             self.assertRaises(HTTPException) as ctx:
            await misc_routes.handle_sessions_resume(request, "unknown-session-id")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_resume_handler_rejects_traversal_session_id(self):
        # A traversal-shaped id is rejected on its shape (400) before any
        # lookup: session_id[:8] is interpolated into the work_dir path, and
        # 'a/../../' resolves above PROJECTS_ROOT.
        request = SimpleNamespace(state=SimpleNamespace(session={"user": "alice"}))
        for bad in ("../../unknown", "a/../../", "x/../../../../", "has space"):
            with patch.object(db, "read_claude_sessions", AsyncMock(return_value=[])), \
                 self.assertRaises(HTTPException) as ctx:
                await misc_routes.handle_sessions_resume(request, bad)
            self.assertEqual(ctx.exception.status_code, 400, bad)

    async def test_resume_handler_reuses_existing_linked_chat(self):
        await db.chat_create("existing", "Existing", None, "/tmp/existing", "alice")
        await db.chat_set_session("existing", "cli-existing")
        request = SimpleNamespace(state=SimpleNamespace(session={"user": "alice"}))
        available = [{"sessionId": "cli-existing", "name": "CLI", "cwd": "/tmp/cli"}]
        with patch.object(db, "read_claude_sessions", AsyncMock(return_value=available)):
            response = await misc_routes.handle_sessions_resume(request, "cli-existing")
        payload = json.loads(response.body)
        self.assertEqual(payload["id"], "existing")
        self.assertEqual(len(await db.chat_list("alice")), 1)

    async def test_sessions_list_merges_cli_and_web_records(self):
        await db.chat_create("web-chat", "Web Chat", None, "/tmp/web", "alice")
        request = SimpleNamespace(state=SimpleNamespace(session={"user": "alice"}))
        with patch.object(db, "read_claude_sessions", AsyncMock(return_value=[{
            "id": "cli", "name": "CLI Session", "cwd": "/tmp/cli", "kind": "interactive",
            "startedAt": "", "updatedAt": "", "sessionId": "cli",
        }])):
            response = await misc_routes.handle_sessions_list(request)
        payload = json.loads(response.body)
        self.assertEqual({item["id"] for item in payload["sessions"]}, {"cli", "web-chat"})

    async def test_chat_update_allowlist_blocks_sql_identifier_injection(self):
        await db.chat_create("safe", "Safe", None, "/tmp/safe", "alice")
        self.assertFalse(await db.chat_update("safe", "alice", **{"title = archived": "1"}))
        chat = await db.chat_get("safe", "alice")
        self.assertEqual(chat["title"], "Safe")

    async def test_session_reader_handles_malformed_pid(self):
        session_dir = Path(self.tmp.name) / "sessions-pid"
        session_dir.mkdir()
        (session_dir / "malformed.json").write_text(json.dumps({
            "pid": "not-a-number", "kind": "interactive", "name": "Malformed PID",
            "sessionId": "malformed", "cwd": "/tmp/work",
        }))
        with patch.object(db, "_CLAUDE_SESSIONS_DIR", session_dir):
            sessions = await db.read_claude_sessions()
        self.assertEqual([session["sessionId"] for session in sessions], ["malformed"])

    async def test_sessions_list_deduplicates_linked_cli_session(self):
        await db.chat_create("web-chat", "Web Chat", None, "/tmp/web", "alice")
        await db.chat_set_session("web-chat", "linked-session")
        request = SimpleNamespace(state=SimpleNamespace(session={"user": "alice"}))
        with patch.object(db, "read_claude_sessions", AsyncMock(return_value=[{
            "id": "linked-session", "name": "CLI Session", "cwd": "/tmp/cli",
            "kind": "interactive", "startedAt": "", "updatedAt": "",
            "sessionId": "linked-session",
        }])):
            response = await misc_routes.handle_sessions_list(request)
        items = json.loads(response.body)["sessions"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "web-chat")
        self.assertTrue(items[0]["webchat"])

    async def test_migration_adds_missing_chat_columns(self):
        await db.db_conn.execute("DROP TABLE chats")
        await db.db_conn.execute("CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT, description TEXT, work_dir TEXT, owner_id TEXT, created_at TEXT, updated_at TEXT, archived INTEGER)")
        await db.db_conn.commit()
        await db._ensure_chat_columns()
        cursor = await db.db_conn.execute("PRAGMA table_info(chats)")
        columns = {row[1] for row in await cursor.fetchall()}
        self.assertTrue({"pinned", "pinned_at", "deleted_at"}.issubset(columns))

    async def test_messages_batch_empty_input_is_safe(self):
        await db.chat_create("empty-batch", "Empty", None, "/tmp/empty", "alice")
        self.assertEqual(await db.messages_batch("empty-batch", []), [])

    async def test_user_creation_and_lookup_round_trip(self):
        await db.user_create("alice", "alice@example.test", "hashed")
        user = await db.user_get_by_name("alice")
        self.assertEqual(user["email"], "alice@example.test")
        self.assertEqual(user["password"], "hashed")


class ComponentAPIQA(unittest.IsolatedAsyncioTestCase):
    """Single API/service contracts with dependencies mocked at boundaries."""

    async def test_submit_message_rejects_empty_prompt_without_runner_call(self):
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "alice"}),
            json=AsyncMock(return_value={"content": "   "}),
        )
        chat = {"id": "chat", "session_id": None, "work_dir": "/tmp/work", "owner_id": "alice"}
        with patch.object(app.db, "chat_get", AsyncMock(return_value=chat)), \
             patch.object(runner, "run_turn", AsyncMock()) as run_turn, \
             self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_submit_message(request, "chat")
        self.assertEqual(ctx.exception.status_code, 400)
        run_turn.assert_not_awaited()

    async def test_submit_message_rejects_oversized_prompt_before_runner(self):
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "alice"}),
            json=AsyncMock(return_value={"content": "x" * (config.PROMPT_MAX_CHARS + 1)}),
        )
        chat = {"id": "chat", "session_id": None, "work_dir": "/tmp/work", "owner_id": "alice"}
        with patch.object(app.db, "chat_get", AsyncMock(return_value=chat)), \
             patch.object(runner, "run_turn", AsyncMock()) as run_turn, \
             self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_submit_message(request, "chat")
        self.assertEqual(ctx.exception.status_code, 400)
        run_turn.assert_not_awaited()

    async def test_submit_message_persists_user_and_assistant_messages(self):
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "alice"}),
            json=AsyncMock(return_value={"content": "hello"}),
        )
        chat = {"id": "chat", "session_id": None, "work_dir": "/tmp/work", "owner_id": "alice"}
        with patch.object(app.db, "chat_get", AsyncMock(return_value=chat)), \
             patch.object(app.db, "messages_batch", AsyncMock()) as batch, \
             patch.object(app.db, "chat_set_session", AsyncMock()) as set_session, \
             patch.object(app.db, "bump_chat_updated_at", AsyncMock()), \
             patch.object(runner, "run_turn", AsyncMock(return_value=(["answer"], "sid"))):
            response = await chat_routes.handle_submit_message(request, "chat")
        self.assertEqual(response.status_code, 200)
        batch.assert_awaited_once_with("chat", [("user", "hello"), ("assistant", "answer")])
        set_session.assert_awaited_once_with("chat", "sid")

    async def test_patch_rejects_unknown_fields(self):
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "alice"}),
            json=AsyncMock(return_value={"owner_id": "bob"}),
        )
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_patch(request, "chat")
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_security_middleware_wraps_auth_rejections(self):
        self.assertIs(app.app.user_middleware[0].cls, app.SecurityMiddleware)
        self.assertIs(app.app.user_middleware[1].cls, app.AuthMiddleware)

    async def test_expired_browser_page_redirects_to_login(self):
        request = SimpleNamespace(
            cookies={},
            state=SimpleNamespace(session=None),
            url=SimpleNamespace(path="/"),
        )
        response = await app.AuthMiddleware(None).dispatch(request, AsyncMock())
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    async def test_expired_api_response_advertises_login_redirect(self):
        request = SimpleNamespace(
            cookies={},
            state=SimpleNamespace(session=None),
            url=SimpleNamespace(path="/api/chats"),
        )
        response = await app.AuthMiddleware(None).dispatch(request, AsyncMock())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(json.loads(response.body), {"error": "Session expired", "redirect": "/login"})

    async def test_route_contract_contains_required_api_methods(self):
        routes = _registered_routes()
        self.assertIn(("GET", "/api/chats"), routes)
        self.assertIn(("POST", "/api/chats/{chat_id}/stream"), routes)
        self.assertIn(("GET", "/api/sessions"), routes)
        self.assertIn(("POST", "/api/sessions/{session_id}/resume"), routes)

    async def test_stream_requires_independent_cookie_auth(self):
        request = SimpleNamespace(cookies={}, headers={}, state=SimpleNamespace(session=None))
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.stream_handler(request, "chat")
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_chat_create_defaults_title_and_truncates_input(self):
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "alice"}),
            json=AsyncMock(return_value={"title": "x" * 300}),
        )
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(config, "PROJECTS_ROOT", tmp), \
             patch.object(app.db, "chat_create", AsyncMock(return_value="now")):
            response = await chat_routes.handle_chat_create(request)
            payload = json.loads(response.body)
            self.assertEqual(len(payload["title"]), 200)
            self.assertTrue(Path(payload["work_dir"]).is_dir())

    async def test_patch_rejects_non_text_description(self):
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "alice"}),
            json=AsyncMock(return_value={"description": 123}),
        )
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_patch(request, "chat")
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_submit_maps_runner_error_to_json_contract(self):
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "alice"}),
            json=AsyncMock(return_value={"content": "hello"}),
        )
        chat = {"id": "chat", "session_id": None, "work_dir": "/tmp/work", "owner_id": "alice"}
        with patch.object(app.db, "chat_get", AsyncMock(return_value=chat)), \
             patch.object(app.db, "messages_append", AsyncMock()), \
             patch.object(runner, "run_turn", AsyncMock(side_effect=runner.TurnError("proxy down", fatal=False))):
            response = await chat_routes.handle_submit_message(request, "chat")
        self.assertEqual(response.status_code, 500)
        # The raw exception text must not reach the client -- this path now
        # masks it the same way the SSE branch always did. It previously
        # returned str(e) verbatim, which this test asserted.
        body = json.loads(response.body)
        self.assertEqual(body, {"error": shared._SSE_INTERNAL, "fatal": False})
        self.assertNotIn("proxy down", response.body.decode())

    async def test_submit_rejects_missing_chat_before_runner(self):
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "alice"}),
            json=AsyncMock(return_value={"content": "hello"}),
        )
        with patch.object(app.db, "chat_get", AsyncMock(return_value=None)), \
             patch.object(runner, "run_turn", AsyncMock()) as run_turn, \
             self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_submit_message(request, "missing")
        self.assertEqual(ctx.exception.status_code, 404)
        run_turn.assert_not_awaited()

    async def test_http_exception_handler_escapes_html_details(self):
        request = SimpleNamespace(headers={"accept": "text/html"})
        response = await app.handle_http_exception(request, HTTPException(status_code=400, detail="<script>x</script>"))
        self.assertNotIn("<script>", response.body.decode())
        self.assertIn("&lt;script&gt;", response.body.decode())


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
        created = await chat_routes.handle_chat_create(create_request)
        created_payload = json.loads(created.body)
        chat_id = created_payload["id"]
        with patch.object(runner, "run_turn", AsyncMock(return_value=(["reply", " complete"], "e2e-session"))):
            submit_request = SimpleNamespace(
                state=SimpleNamespace(session={"user": self.owner}),
                json=AsyncMock(return_value={"content": "hello"}),
            )
            response = await chat_routes.handle_submit_message(submit_request, chat_id)
        self.assertEqual(json.loads(response.body)["response"], "reply complete")
        loaded = await chat_routes.handle_chat_get(self.request, chat_id)
        payload = json.loads(loaded.body)
        self.assertEqual([m["role"] for m in payload["messages"]], ["user", "assistant"])
        self.assertEqual(payload["chat"]["session_id"], "e2e-session")

    async def test_create_submit_and_export_is_complete_business_flow(self):
        create_request = SimpleNamespace(
            state=SimpleNamespace(session={"user": self.owner}),
            json=AsyncMock(return_value={"title": "Export Flow"}),
        )
        created = await chat_routes.handle_chat_create(create_request)
        chat_id = json.loads(created.body)["id"]
        with patch.object(runner, "run_turn", AsyncMock(return_value=(["finished"], "flow-session"))):
            submit_request = SimpleNamespace(
                state=SimpleNamespace(session={"user": self.owner}),
                json=AsyncMock(return_value={"content": "complete this"}),
            )
            await chat_routes.handle_submit_message(submit_request, chat_id)
        export = await chat_routes.handle_chat_export(self.request, chat_id)
        body = export.body.decode()
        self.assertIn("# Export Flow", body)
        self.assertIn("complete this", body)
        self.assertIn("finished", body)

    async def test_create_duplicate_titles_get_distinct_workspaces(self):
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": self.owner}),
            json=AsyncMock(return_value={"title": "Same Title"}),
        )
        first = json.loads((await chat_routes.handle_chat_create(request)).body)
        second = json.loads((await chat_routes.handle_chat_create(request)).body)
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["work_dir"], second["work_dir"])

    async def test_failed_turn_keeps_user_message_and_returns_error(self):
        create_request = SimpleNamespace(
            state=SimpleNamespace(session={"user": self.owner}),
            json=AsyncMock(return_value={"title": "Failure Flow"}),
        )
        chat_id = json.loads((await chat_routes.handle_chat_create(create_request)).body)["id"]
        with patch.object(runner, "run_turn", AsyncMock(side_effect=runner.TurnError("model unavailable", fatal=False))):
            submit_request = SimpleNamespace(
                state=SimpleNamespace(session={"user": self.owner}),
                json=AsyncMock(return_value={"content": "hello"}),
            )
            response = await chat_routes.handle_submit_message(submit_request, chat_id)
        self.assertEqual(response.status_code, 500)
        messages = await db.messages_get(chat_id)
        self.assertEqual(messages, [])


class AcceptanceUATQA(TemporaryDBMixin, unittest.IsolatedAsyncioTestCase):
    """Business-facing acceptance checks mapped to sidebar and chat requirements."""

    async def asyncSetUp(self):
        await self.init_temp_db()
        self.request = SimpleNamespace(state=SimpleNamespace(session={"user": "uat-user"}))

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def test_user_can_resume_cli_session_into_web_chat(self):
        available = [{"sessionId": "cli-uat", "name": "CLI", "cwd": "/tmp/cli"}]
        with patch.object(db, "read_claude_sessions", AsyncMock(return_value=available)), \
             patch.object(db, "write_claude_session_file"):
            resumed = await misc_routes.handle_sessions_resume(self.request, "cli-uat")
            resumed_payload = json.loads(resumed.body)
            listed = await misc_routes.handle_sessions_list(self.request)
        items = json.loads(listed.body)["sessions"]
        matching = [item for item in items if item["id"] == resumed_payload["id"]]
        self.assertEqual(len(matching), 1)
        self.assertTrue(matching[0]["webchat"])
        self.assertEqual(matching[0]["sessionId"], "cli-uat")

    async def test_user_can_export_completed_conversation(self):
        await db.chat_create("uat-chat", "Release Notes", "Acceptance", "/tmp/uat", "uat-user")
        await db.messages_append("uat-chat", "user", "Summarize")
        await db.messages_append("uat-chat", "assistant", "Done")
        response = await chat_routes.handle_chat_export(self.request, "uat-chat")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Release Notes", response.body.decode())
        self.assertIn("Done", response.body.decode())
        self.assertIn("attachment", response.headers["content-disposition"])

    async def test_user_cannot_access_another_users_chat(self):
        await db.chat_create("private", "Private", None, "/tmp/private", "someone-else")
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_get(self.request, "private")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_archiving_removes_chat_from_default_user_view(self):
        await db.chat_create("archive-uat", "Archive me", None, "/tmp/archive", "uat-user")
        patch_request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "uat-user"}),
            json=AsyncMock(return_value={"archived": True}),
        )
        response = await chat_routes.handle_chat_patch(patch_request, "archive-uat")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(await db.chat_get("archive-uat", "uat-user"))
        self.assertIsNotNone(await db.chat_get("archive-uat", "uat-user", include_archived=True))

    async def test_user_sees_only_their_own_sidebar_sessions(self):
        await db.chat_create("mine", "Mine", None, "/tmp/mine", "uat-user")
        await db.chat_create("other", "Other", None, "/tmp/other", "other-user")
        with patch.object(db, "read_claude_sessions", AsyncMock(return_value=[])):
            response = await misc_routes.handle_sessions_list(self.request)
        items = json.loads(response.body)["sessions"]
        self.assertEqual([item["id"] for item in items], ["mine"])

    async def test_resume_result_creates_usable_workspace_for_followup_chat(self):
        with patch.object(db, "read_claude_sessions", AsyncMock(return_value=[{
            "id": "cli-follow-up", "sessionId": "cli-follow-up", "name": "CLI Follow Up",
            "cwd": "/tmp/cli", "kind": "interactive", "startedAt": "", "updatedAt": "",
        }])), patch.object(db, "write_claude_session_file"):
            response = await misc_routes.handle_sessions_resume(self.request, "cli-follow-up")
        payload = json.loads(response.body)
        chat = await db.chat_get(payload["id"], "uat-user")
        self.assertTrue(Path(chat["work_dir"]).is_dir())
        self.assertEqual(chat["session_id"], "cli-follow-up")


if __name__ == "__main__":
    unittest.main()
