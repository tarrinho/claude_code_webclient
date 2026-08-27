import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app
import auth
import claude_proxy
import runner


class ProxyAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_rejects_invalid_token_before_launch(self):
        launched = False

        async def fake_spawn(*args, **kwargs):
            nonlocal launched
            launched = True

        with patch.object(claude_proxy.asyncio, "create_subprocess_exec", fake_spawn):
            server = await asyncio.start_server(
                lambda reader, writer: claude_proxy.handle_client(
                    reader, writer, "claude", "correct-token-with-at-least-32-characters",
                ),
                "127.0.0.1",
                0,
            )
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write((json.dumps({
                "type": "handshake", "protocol": claude_proxy.PROTOCOL,
                "token": "wrong-token-with-at-least-32-characters",
            }) + "\n").encode())
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), timeout=1), b"")
            writer.close()
            await writer.wait_closed()
            server.close()
            await server.wait_closed()

        self.assertFalse(launched)


class FrameNormalisationTests(unittest.TestCase):
    def test_system_init_yields_session_id(self):
        frames = claude_proxy.normalise_claude_frame({
            "type": "system", "subtype": "init", "session_id": "session-1",
        })
        self.assertEqual(frames, [{"type": "session_id", "session_id": "session-1"}])

    def test_assistant_text_is_flattened(self):
        frames = claude_proxy.normalise_claude_frame({
            "type": "assistant",
            "message": {"role": "assistant", "id": "msg-1", "content": [
                {"type": "text", "text": "WebConsole works"},
            ]},
        })
        self.assertEqual(frames[-1], {"type": "text", "content": "WebConsole works"})
        self.assertFalse(any(frame["type"] == "session_id" for frame in frames))

    def test_api_retry_yields_status(self):
        frames = claude_proxy.normalise_claude_frame({
            "type": "system", "subtype": "api_retry", "attempt": 2,
            "max_retries": 10, "retry_delay_ms": 1000, "error": "rate_limit",
        })
        self.assertEqual(frames[0]["type"], "status")
        self.assertEqual(frames[0]["attempt"], 2)

    def test_error_result_yields_session_and_error(self):
        frames = claude_proxy.normalise_claude_frame({
            "type": "result", "subtype": "error_max_turns", "is_error": True,
            "session_id": "session-2", "result": "failed",
        })
        self.assertEqual(frames, [
            {"type": "session_id", "session_id": "session-2"},
            {"type": "error", "error": "failed"},
        ])


class LoginRateLimitTests(unittest.TestCase):
    def setUp(self):
        auth._login_attempts.clear()

    def tearDown(self):
        auth._login_attempts.clear()

    def test_failures_are_retained_and_trigger_limit(self):
        with patch.object(auth.config, "LOGIN_RATE_MAX", 3), \
             patch.object(auth.config, "LOGIN_RATE_WIN", 300), \
             patch.object(auth.config, "LOGIN_BACKOFF", 30), \
             patch.object(auth.time, "time", return_value=1000):
            self.assertEqual(auth.login_record_failure("127.0.0.1"), (False, 0))
            self.assertEqual(auth.login_record_failure("127.0.0.1"), (False, 0))
            self.assertEqual(auth.login_record_failure("127.0.0.1"), (True, 30))
            self.assertTrue(auth.login_attempt_flood("127.0.0.1"))

    def test_expired_failures_are_discarded(self):
        auth._login_attempts["127.0.0.1"] = [100.0]
        with patch.object(auth.config, "LOGIN_RATE_WIN", 300), \
             patch.object(auth.time, "time", return_value=401.0):
            self.assertFalse(auth.login_attempt_flood("127.0.0.1"))
            self.assertEqual(auth._login_attempts["127.0.0.1"], [])


class ProxyRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.tmp.name)
        runner._sem = None

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def _serve(self, frames, hold_open=0):
        received = {}

        async def handler(reader, writer):
            received["handshake"] = json.loads(await reader.readline())
            writer.write(b'{"type":"ack"}\n')
            await writer.drain()
            received["turn"] = json.loads(await reader.readline())
            for frame in frames:
                writer.write((json.dumps(frame) + "\n").encode())
                await writer.drain()
            if hold_open:
                await asyncio.sleep(hold_open)
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        return server, server.sockets[0].getsockname()[1], received

    async def test_blocking_proxy_returns_text_and_session(self):
        server, port, received = await self._serve([
            {"type": "session_id", "session_id": "session-3"},
            {"type": "text", "content": "WebConsole works"},
            {"type": "done"},
        ])
        with patch.object(runner.config, "PROXY_HOST", "127.0.0.1"), \
             patch.object(runner.config, "PROXY_PORT", port), \
             patch.object(runner.config, "PROXY_TURN_TIMEOUT_S", 2):
            chunks, session_id = await runner._execute_proxy(
                "test", None, str(self.work_dir), "chat-1")
        server.close()
        await server.wait_closed()
        self.assertEqual(chunks, ["WebConsole works"])
        self.assertEqual(session_id, "session-3")
        self.assertEqual(received["turn"]["prompt"], "test")
        self.assertEqual(received["handshake"]["token"], runner.config.PROXY_TOKEN)

    async def test_stream_proxy_preserves_event_order(self):
        frames = [
            {"type": "session_id", "session_id": "session-4"},
            {"type": "status", "status": "api_retry", "attempt": 1},
            {"type": "text", "content": "ok"},
            {"type": "done"},
        ]
        server, port, _ = await self._serve(frames)
        with patch.object(runner.config, "PROXY_HOST", "127.0.0.1"), \
             patch.object(runner.config, "PROXY_PORT", port), \
             patch.object(runner.config, "PROXY_TURN_TIMEOUT_S", 2):
            actual = [event async for event in runner._do_proxy_stream(
                "test", None, str(self.work_dir), "chat-2")]
        server.close()
        await server.wait_closed()
        self.assertEqual(actual, frames)

    async def test_blocking_proxy_raises_terminal_error(self):
        server, port, _ = await self._serve([
            {"type": "error", "error": "rate limited"}, {"type": "done"},
        ])
        with patch.object(runner.config, "PROXY_HOST", "127.0.0.1"), \
             patch.object(runner.config, "PROXY_PORT", port), \
             patch.object(runner.config, "PROXY_TURN_TIMEOUT_S", 2):
            with self.assertRaisesRegex(runner.TurnError, "rate limited"):
                await runner._execute_proxy("test", None, str(self.work_dir), "chat-3")
        server.close()
        await server.wait_closed()

    async def test_stream_proxy_times_out(self):
        server, port, _ = await self._serve([], hold_open=0.2)
        with patch.object(runner.config, "PROXY_HOST", "127.0.0.1"), \
             patch.object(runner.config, "PROXY_PORT", port), \
             patch.object(runner.config, "PROXY_TURN_TIMEOUT_S", 0.05):
            actual = [event async for event in runner._do_proxy_stream(
                "test", None, str(self.work_dir), "chat-4")]
        server.close()
        await server.wait_closed()
        self.assertEqual(actual[0]["type"], "error")
        self.assertIn("timed out", actual[0]["error"])


class AppPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocking_handler_persists_session(self):
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin"}),
            json=AsyncMock(return_value={"content": "hello"}),
        )
        chat = {
            "id": "chat-1", "session_id": None,
            "work_dir": "/tmp/project", "owner_id": "admin",
        }
        with patch.object(app.db, "chat_get", AsyncMock(return_value=chat)), \
             patch.object(app.db, "messages_append", AsyncMock()) as append, \
             patch.object(app.db, "chat_set_session", AsyncMock()) as set_session, \
             patch.object(app.runner, "run_turn", AsyncMock(return_value=(["hello", " world"], "session-5"))):
            response = await app.handle_submit_message(request, "chat-1")

        self.assertEqual(response.status_code, 200)
        set_session.assert_awaited_once_with("chat-1", "session-5")
        append.assert_any_await("chat-1", "assistant", "hello world")


if __name__ == "__main__":
    unittest.main()
