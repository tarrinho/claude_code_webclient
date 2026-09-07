"""tests/test_voice_turn.py — backend tests for the voice conversation
feature. Follows this project's real, existing test convention exactly
(copied from tests/test_qa_auto_answer_routes.py, not invented): a
tempdir-patched DB/PROJECTS_ROOT per test, unittest.IsolatedAsyncioTestCase,
_client()/_login() for HTTP-level tests. The model call is always mocked
(monkeypatch on AsyncOpenAI's chat.completions.create) — no test may reach a
real gateway, matching voice-chat-app's own tests/test_chat.py convention.
"""
from __future__ import annotations

import secrets
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import auth
import config
import db
import runner

HTTPS = "https://testserver"


def _client(follow_redirects: bool = True):
    from fastapi.testclient import TestClient
    from app import app as web_app
    return TestClient(
        web_app, raise_server_exceptions=False, base_url=HTTPS,
        follow_redirects=follow_redirects,
    )


class VoiceTurnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    def _login(self, who: str, password: str):
        client = _client()
        response = client.post("/login", json={"username": who, "password": password})
        self.assertEqual(response.status_code, 200, "the fixture must log in")
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    async def _make_voice_machine(
        self,
        machine_id: str,
        owner: str,
        models: str = '["azure_ai/gpt-5.6-luna", "vllm/Qwen3.5-0.8B"]',
    ) -> str:
        """A backend this owner actually has, for the voice settings to name."""
        await db.ai_machine_create(
            machine_id, "Voice Gateway", "gw.example.invalid", 443, None,
            "azure_ai/gpt-5.6-luna", "https://gw.example.invalid", None, owner,
            provider="claude_code",
        )
        await db.db_conn.execute(
            "UPDATE ai_machines SET active_models = ? WHERE id = ?",
            (models, machine_id),
        )
        await db.db_conn.commit()
        return machine_id

    async def _make_admin_and_chat(self, chat_id: str, voice_mode: bool = False):
        password = secrets.token_urlsafe(16)
        await db.user_create("admin", None, auth.hash_password(password))
        await db.chat_create(chat_id, "Test Chat", None, f"{self.tmp.name}/p/{chat_id}", "admin")
        if voice_mode:
            await db.chat_update(chat_id, "admin", voice_mode=1)
        return password

    async def test_voice_mode_column_exists_and_defaults_to_zero(self):
        await db.chat_create("c1", "Test Chat", None, f"{self.tmp.name}/p/c1", "admin")
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["voice_mode"], 0)

    async def test_chat_update_accepts_voice_mode(self):
        await db.chat_create("c2", "Test Chat", None, f"{self.tmp.name}/p/c2", "admin")
        updated = await db.chat_update("c2", "admin", voice_mode=1)
        self.assertTrue(updated)
        chat = await db.chat_get("c2", "admin")
        self.assertEqual(chat["voice_mode"], 1)

    async def test_voice_turn_timing_records_and_averages(self):
        from routes import voice

        await voice.record_voice_turn_timing("azure_ai/gpt-5.6-luna", 1100, 1200)
        await voice.record_voice_turn_timing("azure_ai/gpt-5.6-luna", 900, 1000)
        await voice.record_voice_turn_timing("vllm/Qwen3.6-35B-A3B-NVFP4", 20000, 21000)

        averages = await voice.voice_model_timing_averages([
            "azure_ai/gpt-5.6-luna",
            "vllm/Qwen3.6-35B-A3B-NVFP4",
            "vllm/Qwen3.5-0.8B",
        ])
        self.assertEqual(averages["azure_ai/gpt-5.6-luna"]["turn_count"], 2)
        self.assertAlmostEqual(averages["azure_ai/gpt-5.6-luna"]["avg_ttft_ms"], 1000, delta=1)
        self.assertEqual(averages["vllm/Qwen3.6-35B-A3B-NVFP4"]["turn_count"], 1)
        self.assertEqual(averages["vllm/Qwen3.5-0.8B"]["turn_count"], 0)
        self.assertIsNone(averages["vllm/Qwen3.5-0.8B"]["avg_ttft_ms"])

    async def test_voice_turn_timing_respects_7_day_window(self):
        """Verify that rows outside the 7-day window are excluded."""
        from routes import voice

        # Calculate timestamps in the same format as db._now()
        eight_days_ago = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - 8 * 24 * 3600)
        )
        six_days_ago = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - 6 * 24 * 3600)
        )

        # Insert a row 8 days ago (should NOT be counted)
        await db.db_conn.execute(
            "INSERT INTO voice_turn_timing (model, ttft_ms, total_ms, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            ("test_model", 500, 600, eight_days_ago),
        )
        # Insert a row 6 days ago (SHOULD be counted)
        await db.db_conn.execute(
            "INSERT INTO voice_turn_timing (model, ttft_ms, total_ms, recorded_at) "
            "VALUES (?, ?, ?, ?)",
            ("test_model", 2000, 2100, six_days_ago),
        )
        # Insert a fresh row (SHOULD be counted)
        await voice.record_voice_turn_timing("test_model", 1000, 1100)
        await db.db_conn.commit()

        averages = await voice.voice_model_timing_averages(["test_model"])

        # Should count only the 6-day-old and fresh rows (2 rows, avg 1500)
        self.assertEqual(averages["test_model"]["turn_count"], 2)
        self.assertAlmostEqual(averages["test_model"]["avg_ttft_ms"], 1500, delta=1)

    async def test_settings_get_includes_voice_defaults(self):
        password = await self._make_admin_and_chat("c3")
        client, headers = self._login("admin", password)
        response = client.get("/api/settings", headers=headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["voice_model"], config.VOICE_MODEL_DEFAULT)
        self.assertEqual(body["voice_speech_rate"], config.VOICE_SPEECH_RATE_DEFAULT)
        self.assertEqual(body["voice_ai_machine_id"], config.VOICE_AI_MACHINE_ID_DEFAULT)

    async def test_settings_get_reflects_stored_override(self):
        password = await self._make_admin_and_chat("c4")
        await db.setting_set("voice_speech_rate", "1.8")
        client, headers = self._login("admin", password)
        response = client.get("/api/settings", headers=headers)
        self.assertEqual(response.json()["voice_speech_rate"], 1.8)

    async def test_stream_voice_turn_yields_matching_sse_frames(self):
        from types import SimpleNamespace
        from routes import voice
        import runner

        class _FakeStream:
            def __init__(self, chunks):
                self._chunks = chunks

            def __aiter__(self):
                return self._iter()

            async def _iter(self):
                for text in self._chunks:
                    yield SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))]
                    )

        await db.chat_create("c5", "Voice Chat", None, f"{self.tmp.name}/p/c5", "admin")
        await db.chat_update("c5", "admin", voice_mode=1, model="azure_ai/gpt-5.6-luna",
                              ai_machine_id="fake-machine-id")
        chat = await db.chat_get("c5", "admin")

        async def fake_get_backend(cid, owner=None):
            return {"provider": "claude_code", "base_url": "https://example.test",
                    "api_key": "fake-key"}

        created_kwargs = {}

        async def fake_create(**kwargs):
            created_kwargs.update(kwargs)
            return _FakeStream(["Hello", ", ", "there."])

        with patch.object(runner, "get_backend", fake_get_backend), \
                patch.object(voice.AsyncOpenAI, "__init__", lambda self, **kw: None), \
                patch.object(voice.AsyncOpenAI, "chat", SimpleNamespace(
                    completions=SimpleNamespace(create=fake_create)), create=True), \
                patch.object(voice.AsyncOpenAI, "close", AsyncMock()):
            frames = [f async for f in voice.stream_voice_turn(chat, "hi", "admin")]

        joined = "".join(frames)
        self.assertIn('"type": "text"', joined)
        self.assertIn('"content": "Hello"', joined)
        self.assertIn('"type": "done"', joined)
        self.assertEqual(created_kwargs["model"], "azure_ai/gpt-5.6-luna")

        messages, _ = await db.messages_page("c5", limit=10)
        roles = [m["role"] for m in messages]
        self.assertEqual(roles, ["user", "assistant"])

    async def test_stream_voice_turn_error_frame_omits_raw_exception_text(self):
        """A failed turn must not leak the exception's own text to the
        client -- openai/httpx exceptions commonly embed the request URL
        (connection errors, timeouts, DNS failures), which would leak the
        resolved gateway's base_url to the browser via the SSE error frame.
        """
        from types import SimpleNamespace
        from routes import voice
        import runner

        await db.chat_create("c6", "Voice Chat", None, f"{self.tmp.name}/p/c6", "admin")
        await db.chat_update("c6", "admin", voice_mode=1, model="azure_ai/gpt-5.6-luna",
                              ai_machine_id="fake-machine-id")
        chat = await db.chat_get("c6", "admin")

        sentinel_base_url = "https://internal-gateway.fake.example:9443/v1"

        async def fake_get_backend(cid, owner=None):
            return {"provider": "claude_code", "base_url": sentinel_base_url,
                    "api_key": "fake-key"}

        async def fake_create(**kwargs):
            raise ConnectionError(f"Connection error connecting to {sentinel_base_url}")

        with patch.object(runner, "get_backend", fake_get_backend), \
                patch.object(voice.AsyncOpenAI, "__init__", lambda self, **kw: None), \
                patch.object(voice.AsyncOpenAI, "chat", SimpleNamespace(
                    completions=SimpleNamespace(create=fake_create)), create=True), \
                patch.object(voice.AsyncOpenAI, "close", AsyncMock()):
            frames = [f async for f in voice.stream_voice_turn(chat, "hi", "admin")]

        joined = "".join(frames)
        self.assertIn('"type": "error"', joined)
        self.assertNotIn(sentinel_base_url, joined)
        self.assertNotIn("internal-gateway.fake.example", joined)

    async def test_stream_voice_turn_empty_response_reports_error_not_done(self):
        """A clean completion with no real text is a non-answer, the same
        class runner.py's _is_non_answer exists to catch on the CLI path --
        it must not be stored as a successful empty assistant message.
        """
        from types import SimpleNamespace
        from routes import voice
        import runner

        class _FakeStream:
            def __init__(self, chunks):
                self._chunks = chunks

            def __aiter__(self):
                return self._iter()

            async def _iter(self):
                for text in self._chunks:
                    yield SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))],
                        usage=None,
                    )

        await db.chat_create("c8", "Voice Chat", None, f"{self.tmp.name}/p/c8", "admin")
        await db.chat_update("c8", "admin", voice_mode=1, model="azure_ai/gpt-5.6-luna",
                              ai_machine_id="fake-machine-id")
        chat = await db.chat_get("c8", "admin")

        async def fake_get_backend(cid, owner=None):
            return {"provider": "claude_code", "base_url": "https://example.test",
                    "api_key": "fake-key"}

        async def fake_create(**kwargs):
            # No content deltas at all -- e.g. the model's whole reply landed
            # in a reasoning block never surfaced as text.
            return _FakeStream([None, ""])

        with patch.object(runner, "get_backend", fake_get_backend), \
                patch.object(voice.AsyncOpenAI, "__init__", lambda self, **kw: None), \
                patch.object(voice.AsyncOpenAI, "chat", SimpleNamespace(
                    completions=SimpleNamespace(create=fake_create)), create=True), \
                patch.object(voice.AsyncOpenAI, "close", AsyncMock()):
            frames = [f async for f in voice.stream_voice_turn(chat, "hi", "admin")]

        joined = "".join(frames)
        self.assertIn('"type": "error"', joined)
        self.assertNotIn('"type": "done"', joined)

        messages, _ = await db.messages_page("c8", limit=10)
        self.assertEqual(messages, [], "an empty reply must not be stored as a real turn")

        averages = await voice.voice_model_timing_averages(["azure_ai/gpt-5.6-luna"])
        self.assertEqual(
            averages["azure_ai/gpt-5.6-luna"]["turn_count"], 1,
            "an empty-response attempt still spent time/tokens and must be recorded",
        )

        cur = await db.db_conn.execute(
            "SELECT is_error, model FROM usage_events WHERE chat_id = ?", ("c8",),
        )
        row = await cur.fetchone()
        self.assertIsNotNone(row, "a voice attempt must record usage even when it produced no text")
        self.assertEqual(row["is_error"], 1)
        self.assertEqual(row["model"], "azure_ai/gpt-5.6-luna")

    async def test_stream_voice_turn_records_usage_on_success(self):
        from types import SimpleNamespace
        from routes import voice
        import runner

        class _FakeStream:
            def __init__(self, chunks):
                self._chunks = chunks

            def __aiter__(self):
                return self._iter()

            async def _iter(self):
                for text in self._chunks:
                    yield SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))],
                        usage=None,
                    )
                yield SimpleNamespace(
                    choices=[],
                    usage=SimpleNamespace(prompt_tokens=12, completion_tokens=7),
                )

        await db.chat_create("c9", "Voice Chat", None, f"{self.tmp.name}/p/c9", "admin")
        await db.chat_update("c9", "admin", voice_mode=1, model="azure_ai/gpt-5.6-luna",
                              ai_machine_id="fake-machine-id")
        chat = await db.chat_get("c9", "admin")

        async def fake_get_backend(cid, owner=None):
            return {"provider": "claude_code", "base_url": "https://example.test",
                    "api_key": "fake-key"}

        async def fake_create(**kwargs):
            return _FakeStream(["Hi", " there."])

        with patch.object(runner, "get_backend", fake_get_backend), \
                patch.object(voice.AsyncOpenAI, "__init__", lambda self, **kw: None), \
                patch.object(voice.AsyncOpenAI, "chat", SimpleNamespace(
                    completions=SimpleNamespace(create=fake_create)), create=True), \
                patch.object(voice.AsyncOpenAI, "close", AsyncMock()):
            frames = [f async for f in voice.stream_voice_turn(chat, "hi", "admin")]

        self.assertIn('"type": "done"', "".join(frames))

        cur = await db.db_conn.execute(
            "SELECT input_tokens, output_tokens, is_error, origin FROM usage_events "
            "WHERE chat_id = ?", ("c9",),
        )
        row = await cur.fetchone()
        self.assertIsNotNone(row, "a successful voice turn must record usage")
        self.assertEqual(row["input_tokens"], 12)
        self.assertEqual(row["output_tokens"], 7)
        self.assertEqual(row["is_error"], 0)
        self.assertEqual(row["origin"], "voice")

    async def test_stream_handler_uses_voice_path_for_voice_chats(self):
        from routes import voice
        from routes import chats

        password = await self._make_admin_and_chat("c6", voice_mode=True)
        await db.chat_update("c6", "admin", model="azure_ai/gpt-5.6-luna",
                              ai_machine_id="fake-machine-id")

        async def fake_stream_voice_turn(chat, prompt, owner):
            yield 'data: {"type": "text", "content": "voice-path-used"}\n\n'
            yield 'data: {"type": "done"}\n\n'

        client, headers = self._login("admin", password)
        with patch.object(chats, "stream_voice_turn", fake_stream_voice_turn):
            response = client.post(
                "/api/chats/c6/stream", json={"content": "hi"}, headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("voice-path-used", response.text)

    async def test_voice_stream_respects_the_sse_slot_cap(self):
        """The voice branch must reserve a slot exactly like every other
        stream endpoint (shared.acquire_sse_slot), or one account can open
        unlimited concurrent voice streams while every other stream type is
        capped at _MAX_SSE_PER_OWNER.
        """
        import shared
        from routes import chats

        password = await self._make_admin_and_chat("c10", voice_mode=True)
        await db.chat_update("c10", "admin", model="azure_ai/gpt-5.6-luna",
                              ai_machine_id="fake-machine-id")

        async def fake_stream_voice_turn(chat, prompt, owner):
            yield 'data: {"type": "done"}\n\n'

        client, headers = self._login("admin", password)
        self.addCleanup(shared._sse_slots.pop, "admin", None)
        shared._sse_slots["admin"] = shared._MAX_SSE_PER_OWNER

        with patch.object(chats, "stream_voice_turn", fake_stream_voice_turn):
            response = client.post(
                "/api/chats/c10/stream", json={"content": "hi"}, headers=headers,
            )
        self.assertEqual(
            response.status_code, 429,
            "voice stream must be capped the same as every other SSE endpoint",
        )

        shared._sse_slots["admin"] = 0
        with patch.object(chats, "stream_voice_turn", fake_stream_voice_turn):
            response = client.post(
                "/api/chats/c10/stream", json={"content": "hi"}, headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        # The generator's finally must release the slot even on a clean
        # finish, or a handful of ordinary voice turns would exhaust the cap.
        self.assertEqual(shared._sse_slots.get("admin", 0), 0)

    async def test_chat_create_with_voice_mode_pins_machine_and_model(self):
        password = secrets.token_urlsafe(16)
        await db.user_create("admin", None, auth.hash_password(password))
        # The machine has to exist and belong to this user. Pinning a bare id
        # that names nobody's machine used to be accepted, and the failure
        # surfaced only later, at turn time, as a voice chat with no reachable
        # backend -- the owner-scoped lookup finds nothing for a machine the
        # caller does not own.
        await self._make_voice_machine("voice-machine-1", "admin")
        await db.setting_set("voice_ai_machine_id", "voice-machine-1")
        await db.setting_set("voice_model", "azure_ai/gpt-5.6-luna")

        client, headers = self._login("admin", password)
        response = client.post(
            "/api/chats", json={"title": "Voice Chat", "voice_mode": True}, headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        chat_id = response.json()["id"]
        chat = await db.chat_get(chat_id, "admin")
        self.assertEqual(chat["voice_mode"], 1)
        self.assertEqual(chat["ai_machine_id"], "voice-machine-1")
        self.assertEqual(chat["model"], "azure_ai/gpt-5.6-luna")

    async def test_settings_get_includes_voice_model_options(self):
        from routes import voice

        password = await self._make_admin_and_chat("c7")
        # A real owned machine rather than a patched lookup: /api/settings only
        # offers the models of a backend the caller actually has, so stubbing
        # the read tested a path the endpoint no longer takes.
        await self._make_voice_machine("voice-machine-2", "admin")
        await db.setting_set("voice_ai_machine_id", "voice-machine-2")
        await voice.record_voice_turn_timing("azure_ai/gpt-5.6-luna", 1100, 1200)

        client, headers = self._login("admin", password)
        response = client.get("/api/settings", headers=headers)
        options = {o["id"]: o for o in response.json()["voice_model_options"]}
        self.assertEqual(options["azure_ai/gpt-5.6-luna"]["turn_count"], 1)
        self.assertEqual(options["vllm/Qwen3.5-0.8B"]["turn_count"], 0)
