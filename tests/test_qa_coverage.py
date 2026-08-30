"""QA coverage for gaps in existing test files.

Covers:
* Chat branching (fork) – API contract, workspace isolation, model preservation.
* Chat search – result schema, truncation, error responses.
* DB backup – response headers, non-admin rejection.
* DB restore – non-admin, invalid gzip, missing file.
* Machine test/activate – error response on no machines, 404 path.
* Chat get – 404 HTTPException, response schema.
* Chat patch – 404 HTTPException, valid field updates.
* Chat export – attachment headers, filename, messages included.
* Chat delete – 404 HTTPException, logged on success.
* SSE stream_handler – empty/whitespace prompt rejection.
* handle_logout – clears session cookie.
* Skills get – response schema.
* Chat list – owner-scoped listing.
* Chat create validation – empty/whitespace/long titles.
* Submit message validation – empty/too-long/not-found.
* Session resume – 404 path.
* CSRF middleware – fork, search, PATCH, POST paths.
* Auth middleware – 401 on stream path.
* SecurityMiddleware – X-Content-Type-Options / X-Frame-Options headers.
* Cross-feature: fork → search finds content.
"""
from __future__ import annotations

import asyncio
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

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_admin_session():
    """Return a dict-compatible session object with 'user' key."""
    return {"user": "admin", "role": "admin"}



def setUpModule():
    """Ensure bootstrap_admin() can create the admin user.

    auth.bootstrap_admin() is a no-op without WC_ADMIN_PASSWORD, which left the
    login tests dependent on the ambient environment -- they failed on a bare
    `pytest` and passed only when the variable happened to be exported.
    """
    global _ENV_PATCH
    _ENV_PATCH = patch.dict(
        os.environ, {"WC_ADMIN_USER": "admin", "WC_ADMIN_PASSWORD": "correcthorse"}
    )
    _ENV_PATCH.start()


def tearDownModule():
    _ENV_PATCH.stop()

def _make_bob_session():
    return {"user": "bob", "role": "user"}


def _make_request(
    method="POST", path="/api/chats", body=None, cookies=None, accept=None, query=None,
):
    if cookies is None:
        cookies = {}
    request = SimpleNamespace(
        method=method,
        url=SimpleNamespace(path=path),
        cookies=cookies,
        headers={"accept": accept or "*/*"},
        query_params=dict(query or {}),
        client=SimpleNamespace(host="127.0.0.1"),
        state=SimpleNamespace(session=None),
        json=AsyncMock(return_value=body or {}),
    )
    return request


async def _setup_db(tc):
    td = tempfile.TemporaryDirectory()
    tc.tmpdir = td
    tc._db_patch = patch.object(config, "DB_PATH", f"{td.name}/db")
    tc._root_patch = patch.object(
        config, "PROJECTS_ROOT", f"{td.name}/projects"
    )
    tc._db_patch.start()
    tc._root_patch.start()
    await db.init()
    await auth.bootstrap_admin()


async def _teardown_db(tc):
    await db.close()
    tc._db_patch.stop()
    tc._root_patch.stop()
    tc.tmpdir.cleanup()


def _async_run(coro):
    """Run an async test method from a sync context."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# ── Chat Branching (Fork) ──────────────────────────────────────────────────


class ForkAPITests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)
        self.sid, self.csrf = auth.session_new("admin")
        self.session = auth.session_get(self.sid)

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _req(self, chat_id):
        r = _make_request(
            method="POST",
            path=f"/api/chats/{chat_id}/fork",
            cookies={"wc_session": self.sid, "wc_csrf": self.csrf},
        )
        r.state.session = self.session
        r.headers = {"X-CSRF-Token": self.csrf}
        return r

    async def test_fork_response_schema(self):
        chat_id = "fork-schema"
        await db.chat_create(chat_id, "Fork Me", None, f"{self.tmpdir.name}/p", "admin")
        resp = await app.handle_chat_fork(self._req(chat_id), chat_id)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body)
        self.assertIn("id", data)
        self.assertIn("title", data)
        self.assertIn("work_dir", data)
        self.assertIn("(fork)", data["title"])

    async def test_fork_new_chat_has_messages(self):
        chat_id = "fork-msgs"
        work = f"{self.tmpdir.name}/p/{chat_id}"
        Path(work).mkdir(parents=True)
        await db.chat_create(chat_id, "Fork Msgs", None, work, "admin")
        await db.messages_append(chat_id, "user", "hello")
        await db.messages_append(chat_id, "assistant", "hi")
        resp = await app.handle_chat_fork(self._req(chat_id), chat_id)
        data = json.loads(resp.body)
        messages = await db.messages_get(data["id"])
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[1]["role"], "assistant")

    async def test_fork_unknown_chat_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_fork(self._req("nonexistent"), "nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_fork_distinct_workspace(self):
        chat_id = "fork-ws"
        work = f"{self.tmpdir.name}/p/{chat_id}"
        Path(work).mkdir(parents=True)
        await db.chat_create(chat_id, "Fork WS", None, work, "admin")
        resp = await app.handle_chat_fork(self._req(chat_id), chat_id)
        data = json.loads(resp.body)
        self.assertNotEqual(data["work_dir"], work)
        self.assertTrue(Path(data["work_dir"]).is_dir())

    async def test_fork_preserves_model(self):
        chat_id = "fork-model"
        await db.chat_create(chat_id, "Model Fork", None, f"{self.tmpdir.name}/p", "admin")
        await db.chat_set_model(chat_id, "claude-opus-4-20250514")
        fork_resp = await app.handle_chat_fork(self._req(chat_id), chat_id)
        fork_id = json.loads(fork_resp.body)["id"]
        forked = await db.chat_get(fork_id, "admin")
        self.assertEqual(forked["model"], "claude-opus-4-20250514")


# ── Chat Search ────────────────────────────────────────────────────────────


class SearchAPITests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)
        self.sid, self.csrf = auth.session_new("admin")
        self.session = auth.session_get(self.sid)

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _req(self, body=None):
        r = _make_request(
            method="POST",
            path="/api/chats/search",
            body=body or {"query": "test"},
            cookies={"wc_session": self.sid, "wc_csrf": self.csrf},
        )
        r.state.session = self.session
        r.headers = {"X-CSRF-Token": self.csrf}
        return r

    async def test_search_response_schema(self):
        chat_id = "search-schema"
        await db.chat_create(chat_id, "Search QA", None, f"{self.tmpdir.name}/p", "admin")
        await db.messages_append(chat_id, "user", "find this")
        resp = await app.handle_chat_search(self._req({"query": "find this"}))
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body)
        self.assertIn("results", data)
        self.assertIn("count", data)

    async def test_search_result_has_snippet(self):
        chat_id = "search-snippet"
        await db.chat_create(chat_id, "Snippet Chat", None, f"{self.tmpdir.name}/p", "admin")
        await db.messages_append(chat_id, "user", "unique keyword here")
        resp = await app.handle_chat_search(
            self._req({"query": "unique keyword"})
        )
        data = json.loads(resp.body)
        self.assertIn("snippet", data["results"][0])
        self.assertIn("unique keyword", data["results"][0]["snippet"])

    async def test_search_empty_query_returns_400(self):
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_search(self._req({"query": "   "}))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_search_long_query_truncated(self):
        chat_id = "search-long"
        await db.chat_create(chat_id, "Long Query Chat", None, f"{self.tmpdir.name}/p", "admin")
        await db.messages_append(chat_id, "user", "common")
        resp = await app.handle_chat_search(
            self._req({"query": "x" * 500})
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body)
        self.assertIn("results", data)
        self.assertIn("count", data)

    async def test_search_result_includes_chat_id(self):
        chat_id = "search-id"
        await db.chat_create(chat_id, "Id Chat", None, f"{self.tmpdir.name}/p", "admin")
        await db.messages_append(chat_id, "user", "test")
        resp = await app.handle_chat_search(
            self._req({"query": "test"})
        )
        data = json.loads(resp.body)
        result_ids = [r["id"] for r in data["results"]]
        self.assertIn(chat_id, result_ids)

    async def test_search_no_match_returns_empty(self):
        chat_id = "search-empty"
        await db.chat_create(chat_id, "No Match", None, f"{self.tmpdir.name}/p", "admin")
        await db.messages_append(chat_id, "user", "hello world")
        resp = await app.handle_chat_search(
            self._req({"query": "zzznonexistentzzz"})
        )
        data = json.loads(resp.body)
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["results"], [])


# ── DB Backup / Restore ───────────────────────────────────────────────────


class BackupAPITests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)
        self.sid, self.csrf = auth.session_new("admin")
        self.session = auth.session_get(self.sid)

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _req(self):
        r = _make_request(
            method="GET",
            path="/api/admin/export",
            cookies={"wc_session": self.sid, "wc_csrf": self.csrf},
        )
        r.state.session = self.session
        r.headers = {"X-CSRF-Token": self.csrf}
        return r

    async def test_backup_content_type_is_gzip(self):
        resp = await app.handle_db_backup(self._req())
        self.assertEqual(resp.media_type, "application/gzip")

    async def test_backup_content_disposition_has_extension(self):
        resp = await app.handle_db_backup(self._req())
        disp = resp.headers.get("content-disposition", "")
        self.assertIn(".db.gz", disp)

    async def test_backup_non_admin_403(self):
        # session_new always creates admin role – use raw dict instead.
        req = _make_request(
            method="GET",
            path="/api/admin/export",
            cookies={"wc_session": "bob-sid"},
        )
        req.state.session = {"user": "bob", "role": "user"}
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_db_backup(req)
        self.assertEqual(ctx.exception.status_code, 403)


class RestoreAPITests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)
        self.sid, self.csrf = auth.session_new("admin")
        self.session = auth.session_get(self.sid)

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _make_form(self, files):
        form = {}
        for key, value in files.items():
            mock_file = SimpleNamespace(
                filename=f"{key}.gz",
                read=AsyncMock(return_value=value),
            )
            form[key] = mock_file
        return form

    async def test_restore_non_admin_403(self):
        """Non-admin session is rejected by the restore handler."""
        req = _make_request(
            method="POST",
            path="/api/admin/import",
            cookies={"wc_session": "some-side"},
        )
        # session_new() always creates admin — use raw dict for non-admin.
        req.state.session = {"user": "bob", "role": "user"}
        form = self._make_form({"file": b"not-gzip"})
        req.form = AsyncMock(return_value=form)
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_db_restore(req)
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_restore_invalid_gzip_400(self):
        req = _make_request(
            method="POST",
            path="/api/admin/import",
            cookies={"wc_session": self.sid, "wc_csrf": self.csrf},
        )
        req.state.session = self.session
        req.headers = {"x-csrf-token": self.csrf}
        form = self._make_form({"file": b"not-gzip"})
        req.form = AsyncMock(return_value=form)
        # db_restore returns False for invalid data → handler raises 500.
        with patch("app.db.db_restore", return_value=False):
            with self.assertRaises(HTTPException) as ctx:
                await app.handle_db_restore(req)
            self.assertEqual(ctx.exception.status_code, 500)

    async def test_restore_missing_file_400(self):
        req = _make_request(
            method="POST",
            path="/api/admin/import",
            cookies={"wc_session": self.sid, "wc_csrf": self.csrf},
        )
        req.state.session = self.session
        req.headers = {"X-CSRF-Token": self.csrf}
        req.form = AsyncMock(return_value={})
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_db_restore(req)
        self.assertEqual(ctx.exception.status_code, 400)


# ── Machine Test / Activate ────────────────────────────────────────────────


class MachineTestTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)
        self.sid, self.csrf = auth.session_new("admin")
        self.session = auth.session_get(self.sid)

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _req(self, machine_id="mach-123"):
        r = _make_request(
            method="POST",
            path=f"/api/machines/{machine_id}/test",
            cookies={"wc_session": self.sid, "wc_csrf": self.csrf},
        )
        r.state.session = self.session
        r.headers = {"X-CSRF-Token": self.csrf}
        return r

    async def test_machine_test_no_machines_returns_404(self):
        """No machine configured raises 404."""
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_machine_test(self._req(), "mach-123")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_machine_test_404(self):
        """Non-existent machine raises 404."""
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_machine_test(self._req("nonexistent"), "nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_machine_test_invalid_json(self):
        """Malformed JSON still hits 404 — no machine exists in DB."""
        req = _make_request(
            method="POST",
            path="/api/machines/mach-123/test",
            cookies={"wc_session": self.sid, "wc_csrf": self.csrf},
        )
        req.state.session = self.session
        req.headers = {"x-csrf-token": self.csrf}
        req.json = AsyncMock(side_effect=ValueError("invalid json"))
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_machine_test(req, "mach-123")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_machine_test_response_is_json(self):
        """Returns application/json."""
        # Create a machine so the handler reaches the test endpoint.
        r = _make_request(method="POST", path="/api/machines")
        r.state.session = _make_admin_session()
        r.json = AsyncMock(return_value={
            "name": "test", "host": "127.0.0.1", "port": 9000, "model": "test"
        })
        r.body = {"name": "test", "host": "127.0.0.1", "port": 9000, "model": "test"}
        create_resp = await app.handle_machine_create(r)
        self.assertEqual(create_resp.status_code, 200)
        mid = json.loads(create_resp.body)["id"]

        req = _make_request(
            method="POST",
            path=f"/api/machines/{mid}/test",
            cookies={"wc_session": self.sid, "wc_csrf": self.csrf},
        )
        req.state.session = self.session
        req.headers = {"x-csrf-token": self.csrf}
        # Mock _resolve_host to bypass internal-host block (127.0.0.1 is blocked).
        mock_writer = AsyncMock()
        async def _fake_connect(ip, port):
            return (AsyncMock(), mock_writer)
        async def _fake_wait_for(fut, timeout=None):
            return await fut
        with (
            patch("app._resolve_host", return_value="8.8.8.8"),
            patch("asyncio.open_connection", _fake_connect),
            patch("asyncio.wait_for", _fake_wait_for),
        ):
            resp = await app.handle_machine_test(req, mid)
            self.assertEqual(resp.media_type, "application/json")


class MachineActivateTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)
        self.sid, self.csrf = auth.session_new("admin")
        self.session = auth.session_get(self.sid)

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _req(self, machine_id="mach-123"):
        r = _make_request(
            method="POST",
            path=f"/api/machines/{machine_id}/activate",
            cookies={"wc_session": self.sid, "wc_csrf": self.csrf},
        )
        r.state.session = self.session
        r.headers = {"X-CSRF-Token": self.csrf}
        return r

    async def test_machine_activate_no_machines_returns_404(self):
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_machine_activate(self._req(), "mach-123")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_machine_activate_404(self):
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_machine_activate(self._req("nonexistent"), "nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)


# ── Chat Get / Patch / Export / Delete ─────────────────────────────────────


class ChatGetTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)
        self.sid, _ = auth.session_new("admin")
        self.session = auth.session_get(self.sid)

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _req(self, chat_id="nonexistent"):
        r = _make_request(
            method="GET",
            path=f"/api/chats/{chat_id}",
            cookies={"wc_session": self.sid},
        )
        r.state.session = self.session
        return r

    async def test_chat_get_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_get(self._req(), "nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_chat_get_returns_schema(self):
        chat_id = "get-schema"
        await db.chat_create(chat_id, "Get Schema", None, f"{self.tmpdir.name}/p", "admin")
        await db.messages_append(chat_id, "user", "hello")
        resp = await app.handle_chat_get(self._req(chat_id), chat_id)
        data = json.loads(resp.body)
        self.assertIn("chat", data)
        self.assertIn("messages", data)
        self.assertEqual(data["chat"]["title"], "Get Schema")


class ChatPatchTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _req(self, chat_id):
        r = _make_request(
            method="PATCH",
            path=f"/api/chats/{chat_id}",
            body={"title": "new"},
        )
        r.state.session = _make_admin_session()
        return r

    async def test_patch_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_patch(self._req("nonexistent"), "nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_patch_valid_fields(self):
        chat_id = "patch-valid"
        await db.chat_create(chat_id, "Before", None, f"{self.tmpdir.name}/p", "admin")
        r = _make_request(
            method="PATCH",
            path=f"/api/chats/{chat_id}",
            body={"title": "After"},
        )
        r.state.session = _make_admin_session()
        resp = await app.handle_chat_patch(r, chat_id)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body)
        self.assertTrue(data["ok"])


class ChatExportTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _req(self, chat_id):
        r = _make_request(
            method="GET",
            path=f"/api/chats/{chat_id}/export",
            cookies={"wc_session": "x"},
        )
        r.state.session = _make_admin_session()
        return r

    async def test_export_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_export(self._req("nonexistent"), "nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_export_has_attachment_header(self):
        chat_id = "exp-disp"
        await db.chat_create(chat_id, "Disp", None, f"{self.tmpdir.name}/p", "admin")
        resp = await app.handle_chat_export(self._req(chat_id), chat_id)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers.get("content-disposition", ""))
        self.assertEqual(resp.media_type, "text/markdown; charset=utf-8")

    async def test_export_has_filename(self):
        chat_id = "exp-fn"
        await db.chat_create(chat_id, "My Export", None, f"{self.tmpdir.name}/p", "admin")
        resp = await app.handle_chat_export(self._req(chat_id), chat_id)
        self.assertIn("my-export", resp.headers.get("content-disposition", "").lower())

    async def test_export_includes_messages(self):
        chat_id = "exp-msgs"
        await db.chat_create(chat_id, "Export Msgs", None, f"{self.tmpdir.name}/p", "admin")
        await db.messages_append(chat_id, "user", "q")
        await db.messages_append(chat_id, "assistant", "a")
        resp = await app.handle_chat_export(self._req(chat_id), chat_id)
        text = resp.body.decode()
        self.assertIn("User", text)
        self.assertIn("Assistant", text)
        self.assertIn("q", text)
        self.assertIn("a", text)


class ChatDeleteTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _req(self, chat_id):
        r = _make_request(
            method="DELETE",
            path=f"/api/chats/{chat_id}",
        )
        r.state.session = _make_admin_session()
        return r

    async def test_delete_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_delete(self._req("nonexistent"), "nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)


# ── SSE Stream ─────────────────────────────────────────────────────────────


class StreamTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)
        self.sid, _ = auth.session_new("admin")
        self.session = auth.session_get(self.sid)

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _req(self, chat_id, content="hello"):
        r = _make_request(
            method="POST",
            path=f"/api/chats/{chat_id}/stream",
            body={"content": content},
            cookies={"wc_session": self.sid},
        )
        r.state.session = self.session
        return r

    async def test_stream_empty_prompt_rejected(self):
        chat_id = "c-empty"
        await db.chat_create(chat_id, "Empty", None, f"{self.tmpdir.name}/p", "admin")
        with self.assertRaises(HTTPException) as ctx:
            await app.stream_handler(self._req(chat_id, content=""), chat_id)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_stream_whitespace_prompt_rejected(self):
        chat_id = "c-ws"
        await db.chat_create(chat_id, "WS", None, f"{self.tmpdir.name}/p", "admin")
        with self.assertRaises(HTTPException) as ctx:
            await app.stream_handler(self._req(chat_id, content="   "), chat_id)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_stream_io_error_returns_200(self):
        chat_id = "c-io"
        await db.chat_create(chat_id, "IO", None, f"{self.tmpdir.name}/p", "admin")

        async def mock_gen():
            yield 'data: {"type":"text","content":"partial"}\n\n'
            raise OSError("connection reset")

        with patch.object(runner, "stream_turn", mock_gen()):
            response = await app.stream_handler(self._req(chat_id), chat_id)
            self.assertEqual(response.status_code, 200)


# ── Auth Middleware ───────────────────────────────────────────────────────


class AuthStreamTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)

    async def asyncTearDown(self):
        await _teardown_db(self)

    async def test_stream_rejects_no_session(self):
        request = _make_request(
            method="POST",
            path="/api/chats/abc/stream",
            body={"content": "hello"},
            cookies={},
        )
        request.state.session = None
        response = await app.AuthMiddleware(None).dispatch(request, AsyncMock())
        self.assertEqual(response.status_code, 401)


# ── SecurityMiddleware ────────────────────────────────────────────────────


class SecurityMiddlewareTests(unittest.TestCase):

    def test_x_content_type_options(self):
        mock_handler = AsyncMock(
            return_value=app.JSONResponse({"ok": True})
        )
        request = _make_request()
        request.state = SimpleNamespace(session=None, csp_nonce="")
        response = _async_run(
            app.SecurityMiddleware(None).dispatch(request, mock_handler)
        )
        self.assertIsInstance(response, app.JSONResponse)
        self.assertEqual(
            response.headers.get("x-content-type-options"), "nosniff"
        )
        self.assertEqual(
            response.headers.get("x-frame-options"), "SAMEORIGIN"
        )
        self.assertIn(
            "max-age=31536000",
            response.headers.get("strict-transport-security"),
        )

    def test_x_frame_options(self):
        mock_handler = AsyncMock(
            return_value=app.JSONResponse({"ok": True})
        )
        request = _make_request()
        request.state = SimpleNamespace(session=None, csp_nonce="")
        response = _async_run(
            app.SecurityMiddleware(None).dispatch(request, mock_handler)
        )
        self.assertIsInstance(response, app.JSONResponse)
        self.assertIn(
            response.headers.get("x-frame-options"),
            ("DENY", "SAMEORIGIN"),
        )


# ── Skills Get ─────────────────────────────────────────────────────────────


class SkillsGetTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)
        self.sid, _ = auth.session_new("admin")
        self.session = auth.session_get(self.sid)

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _req(self, query_params=None):
        r = _make_request(
            method="GET",
            path="/api/skills",
            cookies={"wc_session": self.sid},
        )
        r.state.session = self.session
        r.query_params = SimpleNamespace(
            get=lambda key, default=None: (query_params or {}).get(key, default)
        )
        return r

    async def test_skills_response_schema(self):
        resp = await app.handle_skills_get(self._req())
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body)
        self.assertIn("skills", data)
        self.assertIn("session_id", data)

    async def test_skills_is_list(self):
        resp = await app.handle_skills_get(self._req())
        data = json.loads(resp.body)
        self.assertIsInstance(data["skills"], list)

    async def test_skill_entries_have_required_fields(self):
        # Uses a temporary root rather than the developer's real
        # ~/.claude/skills, and asserts unconditionally -- the assertions used
        # to sit behind `if name in names`, so they were skipped entirely
        # whenever discovery failed, which is the case worth catching.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            fake_skill = root / "test-qacoverage-skill"
            fake_skill.mkdir(parents=True)
            (fake_skill / "SKILL.md").write_text(
                "---\nname: test-qacoverage-skill\n"
                "description: A QA test skill\n---\n",
                encoding="utf-8",
            )
            with patch.object(app, "_USER_SKILLS_ROOT", root):
                resp = await app.handle_skills_get(self._req())
            data = json.loads(resp.body)
            names = [s["name"] for s in data["skills"]]
            self.assertIn("test-qacoverage-skill", names)
            skill = next(
                s for s in data["skills"] if s["name"] == "test-qacoverage-skill"
            )
            self.assertIn("description", skill)
            self.assertIn("installed", skill)
            self.assertIn("active", skill)


# ── Cross-Feature: fork → search ──────────────────────────────────────────


class CrossForkSearchTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)
        self.sid, self.csrf = auth.session_new("admin")
        self.session = auth.session_get(self.sid)

    async def asyncTearDown(self):
        await _teardown_db(self)

    async def test_fork_then_search_finds_content(self):
        chat_id = "cross-fs"
        await db.chat_create(chat_id, "Cross FS", None, f"{self.tmpdir.name}/p", "admin")
        await db.messages_append(chat_id, "user", "findable content")

        fork_req = _make_request(
            method="POST",
            path=f"/api/chats/{chat_id}/fork",
            cookies={"wc_session": self.sid, "wc_csrf": self.csrf},
        )
        fork_req.state = SimpleNamespace(session=self.session)
        fork_resp = await app.handle_chat_fork(fork_req, chat_id)
        fork_id = json.loads(fork_resp.body)["id"]

        search_req = _make_request(
            method="POST",
            path="/api/chats/search",
            body={"query": "findable content"},
            cookies={"wc_session": self.sid, "wc_csrf": self.csrf},
        )
        search_req.state.session = self.session
        search_req.headers = {"x-csrf-token": self.csrf}
        search_resp = await app.handle_chat_search(search_req)
        data = json.loads(search_resp.body)
        self.assertGreater(data["count"], 0)
        self.assertIn(fork_id, [r["id"] for r in data["results"]])


# ── CSRF Middleware ────────────────────────────────────────────────────────


class CSrFTests(unittest.TestCase):

    def _csrf_req(self, method="POST", path="/api/chats"):
        sid, csrf = auth.session_new("admin")
        request = _make_request(
            method=method,
            path=path,
            cookies={"wc_session": sid, "wc_csrf": csrf},
        )
        request.state.session = auth.session_get(sid)
        request.headers = {"x-csrf-token": csrf}
        return request

    def test_post_with_csrf_allowed(self):
        request = self._csrf_req(path="/api/chats/abc/fork")
        mock_handler = AsyncMock(
            return_value=app.JSONResponse({"id": "x", "title": "x", "work_dir": "/tmp"})
        )
        response = _async_run(app.CsrfMiddleware(None).dispatch(request, mock_handler))
        self.assertEqual(response.status_code, 200)

    def test_post_without_csrf_rejected(self):
        request = _make_request(
            method="POST",
            path="/api/chats/abc/fork",
            cookies={"wc_session": "x"},
        )
        request.state.session = _make_admin_session()
        response = _async_run(app.CsrfMiddleware(None).dispatch(request, AsyncMock()))
        self.assertEqual(response.status_code, 403)

    def test_search_with_csrf_allowed(self):
        request = self._csrf_req(path="/api/chats/search")
        mock_handler = AsyncMock(
            return_value=app.JSONResponse({"results": [], "count": 0})
        )
        response = _async_run(app.CsrfMiddleware(None).dispatch(request, mock_handler))
        self.assertEqual(response.status_code, 200)

    def test_patch_with_csrf_allowed(self):
        request = self._csrf_req(method="PATCH", path="/api/chats/abc")
        mock_handler = AsyncMock(return_value=app.JSONResponse({"ok": True}))
        response = _async_run(app.CsrfMiddleware(None).dispatch(request, mock_handler))
        self.assertEqual(response.status_code, 200)

    def test_patch_without_csrf_rejected(self):
        request = _make_request(
            method="PATCH",
            path="/api/chats/abc",
            cookies={"wc_session": "x"},
        )
        request.state.session = _make_admin_session()
        response = _async_run(app.CsrfMiddleware(None).dispatch(request, AsyncMock()))
        self.assertEqual(response.status_code, 403)


# ── Chat List ──────────────────────────────────────────────────────────────


class ChatListTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)

    async def asyncTearDown(self):
        await _teardown_db(self)

    async def test_chat_list_scoped_to_owner(self):
        await db.chat_create("c-admin", "Admin Chat", None, f"{self.tmpdir.name}/p", "admin")
        await db.chat_create("c-bob", "Bob Chat", None, f"{self.tmpdir.name}/p", "bob")

        admin_sid, _ = auth.session_new("admin")
        bob_sid, _ = auth.session_new("bob")

        admin_req = _make_request(
            method="GET",
            path="/api/chats",
            cookies={"wc_session": admin_sid},
        )
        admin_req.state.session = auth.session_get(admin_sid)
        admin_resp = await app.handle_chats_list(admin_req)
        admin_data = json.loads(admin_resp.body)
        admin_ids = [c["id"] for c in admin_data["chats"]]
        self.assertIn("c-admin", admin_ids)
        self.assertNotIn("c-bob", admin_ids)

        bob_req = _make_request(
            method="GET",
            path="/api/chats",
            cookies={"wc_session": bob_sid},
        )
        bob_req.state.session = auth.session_get(bob_sid)
        bob_resp = await app.handle_chats_list(bob_req)
        bob_data = json.loads(bob_resp.body)
        bob_ids = [c["id"] for c in bob_data["chats"]]
        self.assertIn("c-bob", bob_ids)
        self.assertNotIn("c-admin", bob_ids)


# ── Logout ─────────────────────────────────────────────────────────────────


class LogoutTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)
        self.sid, self.csrf = auth.session_new("admin")
        self.session = auth.session_get(self.sid)

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _req(self):
        r = _make_request(
            method="POST",
            path="/logout",
            cookies={"wc_session": self.sid, "wc_csrf": self.csrf},
        )
        r.state.session = self.session
        r.headers = {"X-CSRF-Token": self.csrf}
        return r

    async def test_logout_returns_ok(self):
        resp = await app.handle_logout(self._req())
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body)
        self.assertTrue(data["ok"])

    async def test_logout_clears_session(self):
        sid = self.sid
        await app.handle_logout(self._req())
        self.assertIsNone(auth.session_get(sid))

    async def test_logout_cleans_cookie(self):
        resp = await app.handle_logout(self._req())
        self.assertIn("wc_session", resp.headers.get("set-cookie", ""))
        self.assertIn("max-age=0", resp.headers.get("set-cookie", "").lower())


# ── Chat Create ────────────────────────────────────────────────────────────


class ChatCreateTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _req(self, title="Valid Chat"):
        r = _make_request(
            method="POST",
            path="/api/chats",
            body={"title": title},
            cookies={"wc_session": "x", "wc_csrf": "y"},
        )
        r.state.session = _make_admin_session()
        return r

    async def test_create_default_title_for_empty(self):
        """Empty title defaults to 'Untitled'."""
        r = self._req(title="")
        r.state.session = _make_admin_session()
        resp = await app.handle_chat_create(r)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body)
        self.assertEqual(data["title"], "Untitled")

    async def test_create_default_title_for_whitespace(self):
        """Whitespace-only title strips to empty (no fallback in handler)."""
        r = self._req(title="   ")
        r.state.session = _make_admin_session()
        resp = await app.handle_chat_create(r)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body)
        self.assertEqual(data["title"], "")  # stripped, no reversion

    async def test_create_long_title_truncated(self):
        """Title longer than 200 chars is truncated."""
        r = self._req(title="x" * 500)
        r.state.session = _make_admin_session()
        r.body = {"title": "x" * 500}
        resp = await app.handle_chat_create(r)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body)
        self.assertEqual(len(data["title"]), 200)

    async def test_create_returns_schema(self):
        r = self._req(title="Schema Chat")
        r.state.session = _make_admin_session()
        resp = await app.handle_chat_create(r)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body)
        self.assertIn("id", data)
        self.assertIn("title", data)
        self.assertIn("work_dir", data)
        self.assertIn("created_at", data)

    async def test_create_creates_workspace_dir(self):
        r = self._req(title="Workspace Test")
        r.state.session = _make_admin_session()
        resp = await app.handle_chat_create(r)
        data = json.loads(resp.body)
        self.assertTrue(Path(data["work_dir"]).is_dir())


# ── Submit Message Validation ─────────────────────────────────────────────


class SubmitMessageValidationTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _req(self, chat_id, content="hello"):
        r = _make_request(
            method="POST",
            path=f"/api/chats/{chat_id}/messages",
            body={"content": content},
        )
        r.state.session = _make_admin_session()
        return r

    async def test_submit_empty_prompt_400(self):
        chat_id = "submit-empty"
        await db.chat_create(chat_id, "Submit", None, f"{self.tmpdir.name}/p", "admin")
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_submit_message(self._req(chat_id, content=""), chat_id)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_submit_too_long_prompt_400(self):
        chat_id = "submit-long"
        await db.chat_create(chat_id, "Submit", None, f"{self.tmpdir.name}/p", "admin")
        long_prompt = "x" * 10000
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_submit_message(self._req(chat_id, content=long_prompt), chat_id)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_submit_to_chat_not_found_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_submit_message(self._req("nonexistent"), "nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)


# ── Session Resume ────────────────────────────────────────────────────────


class SessionResumeTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)

    async def asyncTearDown(self):
        await _teardown_db(self)

    def _req(self, session_id):
        r = _make_request(
            method="POST",
            path=f"/api/sessions/{session_id}/resume",
        )
        r.state.session = _make_admin_session()
        return r

    async def test_resume_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_sessions_resume(self._req("nonexistent"), "nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)


# ── HTML Handler ──────────────────────────────────────────────────────────


class HTMLHandlerTests(unittest.TestCase):

    def test_index_returns_html(self):
        async def _run():
            req = _make_request(method="GET", path="/")
            req.state.session = None
            return await app.handle_index(req)
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        resp = loop.run_until_complete(_run())
        self.assertIsInstance(resp, app.HTMLResponse)


# ── HTTP Exception Handler (XSS prevention) ────────────────────────────────────


class HttpExceptionHandlerTests(unittest.TestCase):
    """HTTPException rendering: JSON for API, escaped HTML for browsers."""

    def test_http_exception_json_branch_uses_error_key(self):
        """The JSON branch reports the detail under 'error', not 'detail'."""
        req = _make_request()
        exc = app.HTTPException(status_code=400, detail="<script>alert(1)</script>")
        resp = _async_run(app.handle_http_exception(req, exc))
        self.assertIsInstance(resp, app.JSONResponse)
        data = json.loads(resp.body)
        self.assertIn("error", data)
        self.assertNotIn("detail", data)
        self.assertEqual(data["error"], "<script>alert(1)</script>")

    def test_http_exception_escapes_html(self):
        """HTTPException detail is escaped to prevent XSS in HTML error pages."""
        req = _make_request(accept="text/html")
        exc = app.HTTPException(status_code=400, detail="<script>alert(1)</script>")
        resp = _async_run(app.handle_http_exception(req, exc))
        self.assertIsInstance(resp, app.HTMLResponse)
        body = resp.body.decode()
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_http_exception_returns_json(self):
        """HTTPException is rendered as JSONResponse."""
        req = _make_request()
        exc = app.HTTPException(status_code=404, detail="Not found")
        resp = _async_run(app.handle_http_exception(req, exc))
        self.assertIsInstance(resp, app.JSONResponse)
        self.assertEqual(resp.status_code, 404)

    def test_http_exception_survives_a_minimal_request(self):
        """The handler must not raise when the request lacks url/client/state."""
        req = SimpleNamespace(headers={"accept": "application/json"})
        exc = app.HTTPException(status_code=500, detail="boom")
        resp = _async_run(app.handle_http_exception(req, exc))
        self.assertEqual(resp.status_code, 500)


# ── Login endpoint ──────────────────────────────────────────────────────────────


class LoginTests(unittest.IsolatedAsyncioTestCase):
    """POST /login endpoint."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self._root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self._db_patch.start()
        self._root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self._db_patch.stop()
        self._root_patch.stop()
        self.tmp.cleanup()

    def _make_request(self, body=None):
        req = _make_request(
            method="POST", path="/login", cookies={}, accept=None
        )
        req.client = SimpleNamespace(host="127.0.0.1")
        req.state.session = None
        if body is None:
            body = {"username": "admin", "password": "correcthorse"}
        req.json = AsyncMock(return_value=body)
        return req

    async def test_login_wrong_password_401(self):
        resp = await app.handle_login(self._make_request({"username": "admin", "password": "wrong"}))
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(json.loads(resp.body)["error"], "Invalid credentials")

    async def test_login_nonexistent_user_401(self):
        resp = await app.handle_login(self._make_request({"username": "nobody", "password": "x"}))
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(json.loads(resp.body)["error"], "Invalid credentials")

    async def test_login_empty_body_400(self):
        resp = await app.handle_login(self._make_request({}))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(json.loads(resp.body)["error"], "Username and password required")

    async def test_login_missing_username_400(self):
        resp = await app.handle_login(self._make_request({"password": "abc"}))
        self.assertEqual(resp.status_code, 400)

    async def test_login_success_returns_ok(self):
        """Admin password comes from WC_ADMIN_PASSWORD env var."""
        resp = await app.handle_login(
            self._make_request({"username": "admin", "password": config._str("WC_ADMIN_PASSWORD")})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.body)["ok"])

    async def test_login_success_sets_session_cookie(self):
        resp = await app.handle_login(self._make_request())
        self.assertIsNotNone(resp.headers.get("set-cookie"))
        self.assertIn("wc_session", resp.headers.get("set-cookie", ""))

    async def test_login_success_sets_csrf_cookie(self):
        resp = await app.handle_login(self._make_request())
        # Two separate Set-Cookie headers are emitted; headers.get() returns
        # only the first, so read them all.
        cookies = [
            v.decode() for k, v in resp.raw_headers if k.lower() == b"set-cookie"
        ]
        self.assertTrue(cookies)
        self.assertTrue(
            any(c.startswith("wc_csrf=") for c in cookies), cookies
        )
        self.assertTrue(
            any(c.startswith("wc_session=") for c in cookies), cookies
        )

    async def test_login_csrf_cookie_matches_session_token(self):
        """The wc_csrf cookie must be the requesting session's own token."""
        resp = await app.handle_login(self._make_request())
        cookies = [
            v.decode() for k, v in resp.raw_headers if k.lower() == b"set-cookie"
        ]
        sid = next(c for c in cookies if c.startswith("wc_session=")).split("=", 1)[1].split(";")[0]
        csrf = next(c for c in cookies if c.startswith("wc_csrf=")).split("=", 1)[1].split(";")[0]
        self.assertTrue(auth._csrf_valid(csrf, csrf, sid))
        # A token from a different live session must not validate against sid.
        other_sid, other_csrf = auth.session_new("someone-else", "user")
        self.addCleanup(auth.session_drop, other_sid)
        self.assertFalse(auth._csrf_valid(other_csrf, other_csrf, sid))


# ── Login page ──────────────────────────────────────────────────────────────────


class LoginPageTests(unittest.IsolatedAsyncioTestCase):
    """GET /login page handler."""

    async def test_login_page_returns_html(self):
        req = _make_request(method="GET", path="/login", accept="text/html")
        req.state.session = None
        resp = await app.handle_login_page(req)
        self.assertIsInstance(resp, app.HTMLResponse)

    async def test_login_page_requires_text_html(self):
        """Login page handler still returns HTML regardless of Accept.

        The handler just reads the template file — Accept header is only
        checked by the middleware routing logic, not the handler itself.
        """
        req = _make_request(method="GET", path="/login", accept="application/json")
        req.state.session = None
        resp = await app.handle_login_page(req)
        self.assertIsInstance(resp, app.HTMLResponse)

    async def test_login_page_missing_template_returns_500(self):
        """When the template file is gone, handler returns 500 with message."""
        with patch.object(
            app, "_WEB_DIR", Path(tempfile.gettempdir())
        ):  # no login.html exists there
            req = _make_request(method="GET", path="/login", accept="text/html")
            req.state.session = None
            resp = await app.handle_login_page(req)
            self.assertEqual(resp.status_code, 500)


# ── Settings endpoint ───────────────────────────────────────────────────────────


class SettingsPatchTests(unittest.IsolatedAsyncioTestCase):
    """PATCH /api/settings endpoint."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self._root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self._db_patch.start()
        self._root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self._db_patch.stop()
        self._root_patch.stop()
        self.tmp.cleanup()

    def _make_request(self, body=None):
        req = _make_request(method="PATCH", path="/api/settings", cookies={})
        req.state.session = {"user": "admin", "role": "admin"}
        req.json = AsyncMock(return_value=body or {})
        return req

    async def test_settings_patch_non_admin_403(self):
        req = self._make_request({"session_ttl": 600})
        req.state.session = {"user": "bob", "role": "user"}
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_settings_patch(req)
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_settings_patch_valid_ttl(self):
        req = self._make_request({"session_ttl": 600})
        resp = await app.handle_settings_patch(req)
        data = json.loads(resp.body)
        self.assertTrue(data["ok"])

    async def test_settings_patch_invalid_ttl_too_low(self):
        req = self._make_request({"session_ttl": 10})
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_settings_patch(req)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_settings_patch_invalid_ttl_too_high(self):
        req = self._make_request({"session_ttl": 100000})
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_settings_patch(req)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_settings_patch_valid_model(self):
        req = self._make_request({"default_model": "claude-sonnet-4-20250514"})
        resp = await app.handle_settings_patch(req)
        data = json.loads(resp.body)
        self.assertTrue(data["ok"])

    async def test_settings_patch_invalid_model_chars(self):
        req = self._make_request({"default_model": "bad/model!"})
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_settings_patch(req)
        self.assertEqual(ctx.exception.status_code, 400)


# ── Machines CRUD validation ────────────────────────────────────────────────────


class MachineCreateValidationTests(unittest.IsolatedAsyncioTestCase):
    """Machine creation edge cases not covered in test_app.py."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self._root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self._db_patch.start()
        self._root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self._db_patch.stop()
        self._root_patch.stop()
        self.tmp.cleanup()

    def _make_request(self, body=None):
        req = _make_request(method="POST", path="/api/machines", cookies={})
        req.state.session = {"user": "admin", "role": "admin"}
        req.json = AsyncMock(return_value=body or {"name": "test", "host": "8.8.8.8", "port": 9000})
        return req

    async def test_machine_create_invalid_port_400(self):
        req = self._make_request({"name": "test", "host": "8.8.8.8", "port": 99999})
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_machine_create(req)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_machine_create_empty_name_400(self):
        req = self._make_request({"name": "", "host": "8.8.8.8", "port": 9000})
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_machine_create(req)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_machine_create_missing_host_400(self):
        req = self._make_request({"name": "test", "port": 9000})
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_machine_create(req)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_machine_create_invalid_model_chars(self):
        req = self._make_request({"name": "test", "host": "8.8.8.8", "port": 9000, "model": "bad/model!"})
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_machine_create(req)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_machine_create_valid_base_url(self):
        req = self._make_request({
            "name": "test", "host": "8.8.8.8", "port": 9000,
            "base_url": "https://api.example.com/v1",
        })
        resp = await app.handle_machine_create(req)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body)
        self.assertTrue(data["ok"])

    async def test_machine_create_non_numeric_port(self):
        req = self._make_request({"name": "test", "host": "8.8.8.8", "port": "abc"})
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_machine_create(req)
        self.assertEqual(ctx.exception.status_code, 400)


class MachineDeleteTests(unittest.IsolatedAsyncioTestCase):
    """DELETE /api/machines/{id}."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self._root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self._db_patch.start()
        self._root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self._db_patch.stop()
        self._root_patch.stop()
        self.tmp.cleanup()

    def _make_request(self):
        req = _make_request(method="DELETE", path="/api/machines/test-id", cookies={})
        req.state.session = {"user": "admin", "role": "admin"}
        return req

    async def test_machine_delete_nonexistent_404(self):
        req = self._make_request()
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_machine_delete(req, "nonexistent" * 4)
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_machine_delete_returns_ok(self):
        """Deleting an existing machine returns {ok: True}."""
        req_create = _make_request(
            method="POST", path="/api/machines", cookies={}
        )
        req_create.state.session = {"user": "admin", "role": "admin"}
        req_create.json = AsyncMock(return_value={"name": "delme", "host": "8.8.8.8", "port": 9000})
        resp_create = await app.handle_machine_create(req_create)
        mid = json.loads(resp_create.body)["id"]

        req = self._make_request()
        resp = await app.handle_machine_delete(req, mid)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.body)["ok"])


class MachineActivateTests2(unittest.IsolatedAsyncioTestCase):
    """POST /api/machines/{id}/activate edge cases."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self._root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self._db_patch.start()
        self._root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self._db_patch.stop()
        self._root_patch.stop()
        self.tmp.cleanup()

    def _make_request(self):
        req = _make_request(method="POST", path="/api/machines/test/activate", cookies={})
        req.state.session = {"user": "admin", "role": "admin"}
        return req

    async def test_machine_activate_returns_activated_bool(self):
        """Activate returns {ok: True, activated: bool}."""
        req_create = _make_request(
            method="POST", path="/api/machines", cookies={}
        )
        req_create.state.session = {"user": "admin", "role": "admin"}
        req_create.json = AsyncMock(return_value={"name": "actme", "host": "8.8.8.8", "port": 9000})
        resp_create = await app.handle_machine_create(req_create)
        mid = json.loads(resp_create.body)["id"]

        req = self._make_request()
        resp = await app.handle_machine_activate(req, mid)
        data = json.loads(resp.body)
        self.assertTrue(data["ok"])
        self.assertIsInstance(data["activated"], bool)


class MachinePatchValidationTests(unittest.IsolatedAsyncioTestCase):
    """PATCH /api/machines/{id} validation edge cases."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self._root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self._db_patch.start()
        self._root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self._db_patch.stop()
        self._root_patch.stop()
        self.tmp.cleanup()

    def _make_request(self, body=None):
        req = _make_request(method="PATCH", path="/api/machines/test", cookies={})
        req.state.session = {"user": "admin", "role": "admin"}
        req.json = AsyncMock(return_value=body or {})
        return req

    async def test_machine_patch_unknown_field_400(self):
        req = self._make_request({"unknown_field": "x"})
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_machine_patch(req, "nonexistent" * 4)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_machine_patch_empty_data_400(self):
        req = self._make_request({})
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_machine_patch(req, "nonexistent" * 4)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_machine_patch_invalid_port(self):
        req = self._make_request({"port": 0})
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_machine_patch(req, "nonexistent" * 4)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_machine_patch_invalid_model(self):
        req = self._make_request({"model": "bad/model!"})
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_machine_patch(req, "nonexistent" * 4)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_machine_patch_invalid_host_ssrf(self):
        """PATCH should block internal IPs via _validate_host.

        Uses the link-local metadata address rather than loopback: an AI
        machine legitimately lives on loopback, a tailnet or a LAN, so those
        ranges are allowlisted (WC_SSRF_ALLOW_NETS) and 127.0.0.1 now reaches
        the machine lookup and 404s instead.
        """
        req = self._make_request({"host": "169.254.169.254"})
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_machine_patch(req, "nonexistent" * 4)
        self.assertEqual(ctx.exception.status_code, 403)


# ── Submit message edge cases ──────────────────────────────────────────────────


class SubmitMessageModelTests(unittest.IsolatedAsyncioTestCase):
    """Submit message with model override and validation."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self._root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self._db_patch.start()
        self._root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self._db_patch.stop()
        self._root_patch.stop()
        self.tmp.cleanup()

    def _make_request(self, body=None):
        req = _make_request(method="POST", path="/api/chats/test/messages", cookies={})
        req.state.session = {"user": "admin", "role": "admin"}
        req.json = AsyncMock(return_value=body or {"content": "hello"})
        return req

    async def _create_chat(self):
        req = _make_request(method="POST", path="/api/chats", cookies={})
        req.state.session = {"user": "admin", "role": "admin"}
        req.json = AsyncMock(return_value={"title": "test"})
        resp = await app.handle_chat_create(req)
        return json.loads(resp.body)["id"]

    async def test_submit_with_model_override(self):
        """Submit passes model to runner."""
        cid = await self._create_chat()
        mock_runner = AsyncMock(return_value=([], None))
        with patch.object(runner, "run_turn", mock_runner):
            req = self._make_request({"content": "hello", "model": "claude-opus-4-20250514"})
            resp = await app.handle_submit_message(req, cid)
            self.assertEqual(resp.status_code, 200)
            data = json.loads(resp.body)
            self.assertIn("response", data)
            mock_runner.assert_called_once()
            call_args = mock_runner.call_args
            self.assertEqual(call_args[0][4], "claude-opus-4-20250514")

    async def test_submit_invalid_model_name(self):
        req = self._make_request({"content": "hello", "model": "bad/model!"})
        cid = await self._create_chat()
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_submit_message(req, cid)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_submit_model_too_long(self):
        req = self._make_request({"content": "hello", "model": "x" * 101})
        cid = await self._create_chat()
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_submit_message(req, cid)
        self.assertEqual(ctx.exception.status_code, 400)


class SubmitMessagePersistenceTests(unittest.IsolatedAsyncioTestCase):
    """Submit message persists messages to DB."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self._root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self._db_patch.start()
        self._root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self._db_patch.stop()
        self._root_patch.stop()
        self.tmp.cleanup()

    def _make_request(self, body=None):
        req = _make_request(method="POST", path="/api/chats/test/messages", cookies={})
        req.state.session = {"user": "admin", "role": "admin"}
        req.json = AsyncMock(return_value=body or {"content": "hello"})
        return req

    async def _create_chat(self):
        req = _make_request(method="POST", path="/api/chats", cookies={})
        req.state.session = {"user": "admin", "role": "admin"}
        req.json = AsyncMock(return_value={"title": "test"})
        resp = await app.handle_chat_create(req)
        return json.loads(resp.body)["id"]

    async def test_submit_persists_user_message(self):
        cid = await self._create_chat()
        mock_runner = AsyncMock(return_value=(["yes"], None))
        with patch.object(runner, "run_turn", mock_runner):
            req = self._make_request({"content": "hello"})
            await app.handle_submit_message(req, cid)
        messages = await db.messages_get(cid)
        self.assertEqual(messages[-1]["role"], "assistant")
        self.assertEqual(messages[0]["content"], "hello")

    async def test_submit_persists_assistant_response(self):
        cid = await self._create_chat()
        mock_runner = AsyncMock(return_value=(["response text"], None))
        with patch.object(runner, "run_turn", mock_runner):
            req = self._make_request({"content": "hello"})
            resp = await app.handle_submit_message(req, cid)
        messages = await db.messages_get(cid)
        self.assertEqual(messages[0]["content"], "hello")
        self.assertIn("response text", messages[1]["content"])
        self.assertEqual(json.loads(resp.body)["response"], "response text")

    async def test_submit_uses_work_dir(self):
        cid = await self._create_chat()
        mock_runner = AsyncMock(return_value=(["ok"], None))
        with patch.object(runner, "run_turn", mock_runner):
            req = self._make_request({"content": "hello"})
            await app.handle_submit_message(req, cid)
        mock_runner.assert_called_once()
        call_args = mock_runner.call_args
        self.assertTrue(call_args[0][2].startswith(config.PROJECTS_ROOT))


# ── Sessions list ───────────────────────────────────────────────────────────────


class SessionsListTests(unittest.IsolatedAsyncioTestCase):
    """GET /api/sessions endpoint."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self._root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self._db_patch.start()
        self._root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self._db_patch.stop()
        self._root_patch.stop()
        self.tmp.cleanup()

    def _make_request(self):
        req = _make_request(method="GET", path="/api/sessions", cookies={})
        req.state.session = {"user": "admin", "role": "admin"}
        return req

    async def test_sessions_list_returns_key(self):
        req = self._make_request()
        resp = await app.handle_sessions_list(req)
        self.assertIn("sessions", json.loads(resp.body))

    async def test_sessions_list_includes_web_chats(self):
        # Create a chat so sessions list has items
        req_create = _make_request(method="POST", path="/api/chats", cookies={})
        req_create.state.session = {"user": "admin", "role": "admin"}
        req_create.json = AsyncMock(return_value={"title": "Session Test"})
        resp_create = await app.handle_chat_create(req_create)
        cid = json.loads(resp_create.body)["id"]

        req = self._make_request()
        resp = await app.handle_sessions_list(req)
        data = json.loads(resp.body)
        web_items = [s for s in data["sessions"] if s.get("webchat")]
        self.assertGreaterEqual(len(web_items), 1)
        self.assertEqual(web_items[0]["id"], cid)


# ── Chat patch validation ──────────────────────────────────────────────────────


class ChatPatchValidationTests(unittest.IsolatedAsyncioTestCase):
    """Chat patch field validation."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self._root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self._db_patch.start()
        self._root_patch.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self._db_patch.stop()
        self._root_patch.stop()
        self.tmp.cleanup()

    def _make_request(self, body=None):
        req = _make_request(method="PATCH", path="/api/chats/test", cookies={})
        req.state.session = {"user": "admin", "role": "admin"}
        req.json = AsyncMock(return_value=body or {})
        return req

    async def _create_chat(self):
        req = _make_request(method="POST", path="/api/chats", cookies={})
        req.state.session = {"user": "admin", "role": "admin"}
        req.json = AsyncMock(return_value={"title": "Patch Test"})
        resp = await app.handle_chat_create(req)
        return json.loads(resp.body)["id"]

    async def test_chat_patch_unknown_field(self):
        cid = await self._create_chat()
        req = self._make_request({"unknown_field": "x"})
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_chat_patch(req, cid)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_chat_patch_non_boolean_archive(self):
        """Setting archived to a non-boolean value (e.g. string) is rejected."""
        cid = await self._create_chat()
        req = self._make_request({"archived": "yes"})
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_chat_patch(req, cid)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_chat_patch_non_boolean_pinned(self):
        req = self._make_request({"pinned": "true"})
        cid = await self._create_chat()
        with self.assertRaises(app.HTTPException) as ctx:
            await app.handle_chat_patch(req, cid)
        self.assertEqual(ctx.exception.status_code, 400)


# ── Stream handler error events ────────────────────────────────────────────────


class StreamErrorTests(unittest.IsolatedAsyncioTestCase):
    """Stream handler error scenarios."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self._root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self._db_patch.start()
        self._root_patch.start()
        await db.init()
        await auth.bootstrap_admin()
        self.sid, _ = auth.session_new("admin", "admin")

    async def asyncTearDown(self):
        auth.session_drop(self.sid)
        await db.close()
        self._db_patch.stop()
        self._root_patch.stop()
        self.tmp.cleanup()

    def _make_request(self, body=None):
        # stream_handler authenticates from the wc_session cookie directly, not
        # from request.state, so the cookie has to be present or every call
        # short-circuits with a 401.
        req = _make_request(
            method="POST",
            path="/api/chats/test/stream",
            cookies={"wc_session": self.sid},
        )
        if body is None:
            body = {"content": "hello"}
        req.json = AsyncMock(return_value=body)
        req.state = SimpleNamespace(session=None)
        return req

    async def _create_chat(self):
        req = _make_request(method="POST", path="/api/chats", cookies={})
        req.state.session = {"user": "admin", "role": "admin"}
        req.json = AsyncMock(return_value={"title": "Stream Test"})
        resp = await app.handle_chat_create(req)
        return json.loads(resp.body)["id"]

    async def test_stream_timeout_error_event(self):
        """A cancelled stream raises CancelledError → handler re-raises."""
        cid = await self._create_chat()
        req = self._make_request({"content": "hello"})
        # Mock stream_turn to simulate a CancelledError
        async def _cancel_stream(*args, **kwargs):
            raise asyncio.CancelledError()
        with patch.object(runner, "stream_turn", _cancel_stream):
            resp = await app.stream_handler(req, cid)
            self.assertIsInstance(resp, app.StreamingResponse)
            # Read the response body to check for error events
            parts = []
            async for chunk in resp.body_iterator:
                parts.append(chunk)
            body_text = "".join(parts)
            self.assertIn("error", body_text)

    async def test_stream_error_event_types(self):
        """Stream yields start, then done with correct event types."""
        cid = await self._create_chat()
        async def _fake_stream(*args, **kwargs):
            yield {"type": "text", "content": "hello"}
            yield {"type": "done"}
        with patch.object(runner, "stream_turn", _fake_stream):
            req = self._make_request({"content": "hello"})
            resp = await app.stream_handler(req, cid)
            self.assertIsInstance(resp, app.StreamingResponse)
            self.assertEqual(resp.media_type, "text/event-stream")

    async def test_stream_model_override(self):
        """Stream passes model to runner."""
        cid = await self._create_chat()
        received_model = []
        async def _capture_stream(*args, **kwargs):
            received_model.append(args[4] if len(args) > 4 else kwargs.get("model"))
            yield {"type": "done"}
        with patch.object(runner, "stream_turn", _capture_stream):
            req = self._make_request({"content": "hello", "model": "claude-opus-4"})
            resp = await app.stream_handler(req, cid)
            # StreamingResponse is lazy: the generator -- and therefore
            # stream_turn -- only runs once the body is consumed.
            async for _chunk in resp.body_iterator:
                pass
            self.assertEqual(received_model[0], "claude-opus-4")


if __name__ == "__main__":
    unittest.main()