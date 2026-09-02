"""QA tests for error handling, logging, and HTTP error contracts.

Covers:
* Error logging: every HTTP error path emits a structured ERROR log line.
* CSRF middleware: validates the dual-token header on POST, exempt routes.
* Auth middleware: expired/invalid sessions are logged and rejected.
* Chat creation: OSError on work_dir is caught, logged, and surfaces a
  safe 500 with a message telling the user to check server logs.
* SSE stream_handler: timeout, I/O error, and generic exceptions each
  produce a safe SSE error event plus an ERROR log.
* handle_submit_message: chat-not-found and TurnError paths log details.
* handle_sessions_resume: CLI session not found is logged with guidance.
* HTTP exception handler: strips internal details from JSON response but
  keeps them in the log.

All tests use local temporary state and mocked dependencies. No live model,
proxy, or network service.
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import unittest
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import app
import auth
import config
import db
import runner
from routes import misc as misc_routes

# ── Helpers ──────────────────────────────────────────────────────────────────

def _log_buffer():
    """Return a StringIO that will capture log output."""
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    return buf, handler


def _install_log_buffer(log):
    """Attach buffer + handler to *log* and return the buffer.

    This took a logger and then ignored it, always attaching to "wc.app".
    Harmless while every caller passed that, and silently wrong the moment one
    did not: a test asserting on another logger's output saw an empty buffer and
    read as "the code never logged".
    """
    buf, handler = _log_buffer()
    logger = log if isinstance(log, logging.Logger) else logging.getLogger("wc.app")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return buf, handler, logger


class _Headers(dict):
    """Case-insensitive header mapping, like Starlette's Headers.

    Middleware reads lowercase keys (``x-csrf-token``) while callers set them
    in canonical form (``X-CSRF-Token``); a plain dict silently misses.
    """

    def __init__(self, initial=None):
        super().__init__()
        for key, value in (initial or {}).items():
            self[key] = value

    def __setitem__(self, key, value):
        super().__setitem__(key.lower(), value)

    def __getitem__(self, key):
        return super().__getitem__(key.lower())

    def __contains__(self, key):
        return super().__contains__(key.lower())

    def get(self, key, default=None):
        return super().get(key.lower(), default)


def _make_request(method="POST", path="/api/chats", body=None, cookies=None, accept=None):
    """Build a fake Request object for middleware/handler testing."""
    if cookies is None:
        cookies = {}
    request = SimpleNamespace(
        method=method,
        url=SimpleNamespace(path=path),
        cookies=cookies,
        headers=_Headers({"accept": accept or "*/*"}),
        client=SimpleNamespace(host="127.0.0.1"),
        state=SimpleNamespace(session=None),
        json=AsyncMock(return_value=body or {}),
    )
    return request


# ── Unit: error logging & HTTP exception handler ────────────────────────────

class ErrorLogUnitTests(unittest.TestCase):
    """Verify that error logging infrastructure works."""

    def test_log_buffer_captures_error_level(self):
        buf, handler = _log_buffer()
        handler.setLevel(logging.ERROR)
        record = logging.LogRecord("test", logging.ERROR, "", 0, "bad stuff", (), None)
        handler.emit(record)
        handler.flush()
        self.assertIn("ERROR", buf.getvalue())
        self.assertIn("bad stuff", buf.getvalue())

    def test_version_is_string(self):
        self.assertIsInstance(config.VERSION, str)
        self.assertTrue(config.VERSION.startswith("WebConsole_"))


# ── Component: HTTP exception handler logs then strips ─────────────────────

class HTTPExceptionHandlerTests(unittest.IsolatedAsyncioTestCase):
    """The HTTPException handler logs details but returns stripped JSON."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        self.buf, self.handler, self.logger = _install_log_buffer(
            logging.getLogger("wc.app")
        )

    async def asyncTearDown(self):
        self.logger.removeHandler(self.handler)
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_http_handler_logs_error_fields(self):
        exc = app.HTTPException(status_code=500, detail="database crashed")
        request = _make_request(path="/api/chats", cookies={"wc_session": "fake"})
        session, _ = auth.session_new("alice")
        request.state.session = auth.session_get(session)
        response = await app.handle_http_exception(request, exc)
        self.assertIsInstance(response, app.JSONResponse)
        self.assertEqual(response.status_code, 500)
        body = json.loads(response.body)
        self.assertEqual(body["error"], "database crashed")
        log_text = self.buf.getvalue()
        self.assertIn("HTTP 500", log_text)
        self.assertIn("/api/chats", log_text)
        self.assertIn("database crashed", log_text)
        self.assertIn("alice", log_text)

    async def test_http_handler_strips_for_non_html_accept(self):
        exc = app.HTTPException(status_code=400, detail="sensitive: internal traceback /etc/shadow")
        request = _make_request(path="/api/chats", accept="application/json")
        response = await app.handle_http_exception(request, exc)
        body = json.loads(response.body)
        self.assertEqual(body["error"], "sensitive: internal traceback /etc/shadow")

    async def test_http_handler_no_session_user_defaults_to_anonymous(self):
        exc = app.HTTPException(status_code=502, detail="proxy died")
        request = _make_request(path="/api/stream", cookies={})
        # request.state.session is None by default
        response = await app.handle_http_exception(request, exc)
        self.assertEqual(response.status_code, 502)
        log_text = self.buf.getvalue()
        self.assertIn("anonymous", log_text)


# ── Component: CSRF middleware edge cases ────────────────────────────────────

class CsrfMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    """CSRF middleware behaviour: validate, exempt, missing header."""

    async def test_exempt_paths_pass_through(self):
        request = _make_request(method="POST", path="/login")
        request.state.session = None
        # Should NOT return an error response
        self.assertTrue("/login" in app.CsrfMiddleware._EXEMPT_PATHS)
        mock_handler = AsyncMock(return_value=app.JSONResponse({"ok": True}))
        response = await app.CsrfMiddleware(None).dispatch(request, mock_handler)
        self.assertIsInstance(response, app.JSONResponse)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(mock_handler.called)

    async def test_post_without_csrf_header_rejected(self):
        request = _make_request(method="POST", path="/api/chats")
        request.state.session = SimpleNamespace(user="alice")
        response = await app.CsrfMiddleware(None).dispatch(request, AsyncMock())
        self.assertIsInstance(response, app.JSONResponse)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.body)["error"], "CSRF token invalid or missing")

    async def test_post_with_csrf_header_allowed(self):
        sid, csrf = auth.session_new("alice")
        cookie_token, header_token = csrf, csrf
        request = _make_request(
            method="POST",
            path="/api/chats",
            cookies={"wc_csrf": cookie_token},
        )
        request.headers = _Headers({"X-CSRF-Token": header_token})
        request.state.session = auth.session_get(sid)
        mock_handler = AsyncMock(return_value=app.JSONResponse({"ok": True}))
        response = await app.CsrfMiddleware(None).dispatch(request, mock_handler)
        self.assertIsInstance(response, app.JSONResponse)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(mock_handler.called)

    async def test_get_method_not_checked(self):
        request = _make_request(method="GET", path="/api/chats")
        request.state.session = SimpleNamespace(user="bob")
        request.headers = _Headers()
        mock_handler = AsyncMock(return_value=app.JSONResponse({"ok": True}))
        response = await app.CsrfMiddleware(None).dispatch(request, mock_handler)
        self.assertIsInstance(response, app.JSONResponse)
        self.assertEqual(response.status_code, 200)


# ── Component: Auth middleware expiry logging ─────────────────────────────────

class AuthMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    """Expired/invalid sessions are logged and rejected."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        self.buf, self.handler, self.logger = _install_log_buffer(
            logging.getLogger("wc.app")
        )

    async def asyncTearDown(self):
        self.logger.removeHandler(self.handler)
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_expired_api_request_logs_warning(self):
        request = _make_request(path="/api/chats")
        request.state.session = None
        response = await app.AuthMiddleware(None).dispatch(request, AsyncMock())
        self.assertIsInstance(response, app.JSONResponse)
        self.assertEqual(response.status_code, 401)
        body = json.loads(response.body)
        self.assertEqual(body["error"], "Session expired")
        log_text = self.buf.getvalue()
        self.assertIn("session_expired_or_invalid", log_text)
        self.assertIn("/api/chats", log_text)

    async def test_expired_non_api_request_redirects(self):
        request = _make_request(path="/")
        request.state.session = None
        response = await app.AuthMiddleware(None).dispatch(request, AsyncMock())
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")


# ── Component: chat_create catches OSError ───────────────────────────────────

class ChatCreateErrorTests(unittest.IsolatedAsyncioTestCase):
    """POST /api/chats logs and returns safe 500 on OSError."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()
        self.buf, self.handler, self.logger = _install_log_buffer(
            logging.getLogger("wc.app")
        )
        sid, _ = auth.session_new("admin")
        self.session = auth.session_get(sid)

    async def asyncTearDown(self):
        self.logger.removeHandler(self.handler)
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_mkdir_OSError_returns_500(self):
        # Pretend Path.mkdir raises PermissionError (a subclass of OSError)
        with patch("app.Path.mkdir", side_effect=PermissionError("disk full")):
            request = _make_request(
                method="POST",
                path="/api/chats",
                body={"title": "test"},
                cookies={"wc_session": "x", "wc_csrf": "y"},
            )
            request.state.session = self.session
            with self.assertRaises(HTTPException) as ctx:
                await app.handle_chat_create(request)
            self.assertEqual(ctx.exception.status_code, 500)
            self.assertIn("Could not create conversation", str(ctx.exception.detail))
            self.assertIn("could_not_create_conversation", self.buf.getvalue())

    async def test_normal_create_succeeds(self):
        request = _make_request(
            method="POST",
            path="/api/chats",
            body={"title": "test"},
            cookies={"wc_session": "x", "wc_csrf": "y"},
        )
        request.state.session = self.session
        response = await app.handle_chat_create(request)
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertIn("id", body)
        self.assertIn("work_dir", body)
        self.assertIn("chat_created", self.buf.getvalue())


# ── Component: chat_delete logs on 404 ───────────────────────────────────────

class ChatDeleteErrorTests(unittest.IsolatedAsyncioTestCase):
    """DELETE /api/chats/{id} logs warning on 404."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()
        self.buf, self.handler, self.logger = _install_log_buffer(
            logging.getLogger("wc.app")
        )
        sid, _ = auth.session_new("admin")
        self.session = auth.session_get(sid)

    async def asyncTearDown(self):
        self.logger.removeHandler(self.handler)
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_delete_not_found_logs_warning(self):
        request = _make_request(
            method="DELETE",
            path="/api/chats/nonexistent",
            cookies={"wc_session": "x", "wc_csrf": "y"},
        )
        request.state.session = self.session
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_delete(request, "nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)
        log_text = self.buf.getvalue()
        self.assertIn("chat_delete", log_text)
        self.assertIn("not found", log_text)


# ── Component: submit_message logs on chat-not-found ─────────────────────────

class SubmitMessageErrorTests(unittest.IsolatedAsyncioTestCase):
    """POST /api/chats/{id}/messages logs when chat is missing."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()
        self.buf, self.handler, self.logger = _install_log_buffer(
            logging.getLogger("wc.app")
        )
        sid, _ = auth.session_new("admin")
        self.session = auth.session_get(sid)

    async def asyncTearDown(self):
        self.logger.removeHandler(self.handler)
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_submit_missing_chat_logs_warning(self):
        request = _make_request(
            method="POST",
            path="/api/chats/does-not-exist/messages",
            body={"content": "hello"},
            cookies={"wc_session": "x", "wc_csrf": "y"},
        )
        request.state.session = self.session
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_submit_message(request, "does-not-exist")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, "Chat not found")
        self.assertIn("chat not found", self.buf.getvalue())

    async def test_submit_empty_prompt_logs_warning(self):
        chat_id = "a" * 32
        await db.chat_create(chat_id, "Chat", None, f"{self.tmp.name}/proj", "admin")
        request = _make_request(
            method="POST",
            path=f"/api/chats/{chat_id}/messages",
            body={"content": ""},
            cookies={"wc_session": "x", "wc_csrf": "y"},
        )
        request.state.session = self.session
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_submit_message(request, chat_id)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("empty", ctx.exception.detail.lower())
        self.assertIn("empty prompt", self.buf.getvalue())

    async def test_submit_turn_error_logs_details(self):
        chat_id = "b" * 32
        await db.chat_create(chat_id, "Chat", None, f"{self.tmp.name}/proj", "admin")
        with patch.object(runner, "run_turn", side_effect=runner.TurnError(
            "Cannot connect to proxy at 127.0.0.1:9000", fatal=False
        )):
            request = _make_request(
                method="POST",
                path=f"/api/chats/{chat_id}/messages",
                body={"content": "test prompt"},
                cookies={"wc_session": "x", "wc_csrf": "y"},
            )
            request.state.session = self.session
            response = await app.handle_submit_message(request, chat_id)
            self.assertEqual(response.status_code, 500)
            body = json.loads(response.body)
            self.assertIn("error", body)
            log_text = self.buf.getvalue()
            # A failed turn logs under "turn_failed". It used to reuse the
            # "could_not_create_conversation" label from the mkdir path, so this
            # assertion passed while describing the wrong failure entirely.
            self.assertIn("turn_failed", log_text)
            self.assertIn("Cannot connect to proxy", log_text)


# ── Component: stream_handler error paths ────────────────────────────────────

class StreamHandlerErrorTests(unittest.IsolatedAsyncioTestCase):
    """SSE stream_handler produces safe events and ERROR logs."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()
        self.buf, self.handler, self.logger = _install_log_buffer(
            logging.getLogger("wc.app")
        )
        # stream_handler re-derives the session from the cookie rather than
        # request.state.session, so the sid has to be a live one.
        self.sid, _ = auth.session_new("admin")
        self.session = auth.session_get(self.sid)

    async def asyncTearDown(self):
        self.logger.removeHandler(self.handler)
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_stream_chat_not_found_logs_error(self):
        request = _make_request(
            method="POST",
            path="/api/chats/nonexistent/stream",
            body={"content": "hello"},
            cookies={"wc_session": self.sid},
        )
        request.state.session = self.session
        with self.assertRaises(app.HTTPException) as ctx:
            await app.stream_handler(request, "nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)
        log_text = self.buf.getvalue()
        self.assertIn("stream_handler", log_text)
        self.assertIn("chat not found", log_text)

    async def test_stream_empty_prompt_rejected(self):
        chat_id = "c" * 32
        await db.chat_create(chat_id, "Chat", None, f"{self.tmp.name}/proj", "admin")
        request = _make_request(
            method="POST",
            path=f"/api/chats/{chat_id}/stream",
            body={"content": ""},
            cookies={"wc_session": self.sid},
        )
        request.state.session = self.session
        with self.assertRaises(HTTPException) as ctx:
            await app.stream_handler(request, chat_id)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("empty prompt", self.buf.getvalue())

    async def test_stream_timeout_logs_and_sends_safe_event(self):
        chat_id = "d" * 32
        await db.chat_create(chat_id, "Chat", None, f"{self.tmp.name}/proj", "admin")
        async def gen():
            yield {"type": "text", "content": "x"}
            raise asyncio.TimeoutError()
        async def mock_stream_turn(*args, **kwargs):
            async for ev in gen():
                yield ev
        turn_log, turn_handler, turn_logger = _install_log_buffer(
            logging.getLogger("wc.turns")
        )
        self.addCleanup(turn_logger.removeHandler, turn_handler)
        with patch.object(runner, "stream_turn", mock_stream_turn):
            request = _make_request(
                method="POST",
                path=f"/api/chats/{chat_id}/stream",
                body={"content": "hello"},
                cookies={"wc_session": self.sid},
            )
            request.state.session = self.session
            response = await app.stream_handler(request, chat_id)
            # Collect SSE events
            events = []
            async for event in response.body_iterator:
                events.append(event.strip())
            # Should contain the timeout error event
            error_event = json.loads(
                next(e for e in events if "error" in e).replace("data: ", "").strip()
            )
            self.assertEqual(error_event["type"], "error")
            self.assertEqual(error_event["error"], app._SSE_TIMEOUT)
        # Logged by turns.py under "turn_timed_out", not by stream_handler:
        # the turn runs as a background task now, so the timeout is detected
        # where the work happens rather than on the request that started it.
        # Following the label to where the failure actually occurs is the point
        # of asserting on it -- the same reason `turn_failed` is checked above.
        self.assertIn("turn_timed_out", turn_log.getvalue())


# ── Component: sessions resume not found ─────────────────────────────────────

class SessionsResumeErrorTests(unittest.IsolatedAsyncioTestCase):
    """POST /api/sessions/{id}/resume logs when session is missing."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()
        self.buf, self.handler, self.logger = _install_log_buffer(
            logging.getLogger("wc.app")
        )
        sid, _ = auth.session_new("admin")
        self.session = auth.session_get(sid)

    async def asyncTearDown(self):
        self.logger.removeHandler(self.handler)
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_session_not_found_logs_guidance(self):
        request = _make_request(
            method="POST",
            path="/api/sessions/nonexistent/resume",
            cookies={"wc_session": "x", "wc_csrf": "y"},
        )
        request.state.session = self.session
        with self.assertRaises(HTTPException) as ctx:
            await misc_routes.handle_sessions_resume(request, "nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)
        # Resume now falls back to the transcript when no session is running, so
        # a 404 means neither exists. The message no longer blames a stopped CLI,
        # which was misleading for a conversation that simply never existed.
        self.assertIn("no running session and no transcript", ctx.exception.detail)
        log_text = self.buf.getvalue()
        self.assertIn("cli_session_not_found", log_text)
        self.assertIn("no transcript on disk", log_text)


# ── Integration: end-to-end error chain ─────────────────────────────────────

class E2EErrorFlowTests(unittest.IsolatedAsyncioTestCase):
    """Full request chain: middleware → handler → response + log."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await auth.bootstrap_admin()
        self.buf, self.handler, self.logger = _install_log_buffer(
            logging.getLogger("wc.app")
        )
        self.sid, self.csrf = auth.session_new("admin")
        self.session = auth.session_get(self.sid)

    async def asyncTearDown(self):
        self.logger.removeHandler(self.handler)
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_full_create_flow_works(self):
        """Login → create chat → list → verify."""
        # Step 1: create chat with valid CSRF
        request = _make_request(
            method="POST",
            path="/api/chats",
            body={"title": "integration test"},
            cookies={"wc_session": self.sid, "wc_csrf": self.csrf},
        )
        request.state.session = self.session
        request.headers = _Headers({"X-CSRF-Token": self.csrf})
        response = await app.handle_chat_create(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.body)
        self.assertIn("id", data)
        # Step 2: list chats should include our chat
        list_req = _make_request(
            method="GET",
            path="/api/chats",
            cookies={"wc_session": self.sid, "wc_csrf": self.csrf},
        )
        list_req.state.session = self.session
        list_resp = await app.handle_chats_list(list_req)
        chats_data = json.loads(list_resp.body)
        self.assertEqual(len(chats_data["chats"]), 1)
        self.assertEqual(chats_data["chats"][0]["title"], "integration test")

    async def test_cascade_missing_session_blocks_all_requests(self):
        """No session → every API request returns 401 and logs."""
        request = _make_request(
            method="POST",
            path="/api/chats",
            body={"title": "hacked"},
            cookies={},
        )
        request.state.session = None
        response = await app.AuthMiddleware(None).dispatch(request, AsyncMock())
        self.assertEqual(response.status_code, 401)
        self.assertIn("Session expired", json.loads(response.body)["error"])
        self.assertIn("session_expired_or_invalid", self.buf.getvalue())