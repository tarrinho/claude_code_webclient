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
from unittest.mock import patch

import auth
import config
import db

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
