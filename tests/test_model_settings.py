"""Tests for model persistence, settings, and hostname validation."""
import json
import tempfile
import unittest
from fastapi import HTTPException
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
import auth
import config
import db
import runner


class _FakeRequest:
    """Minimal request replacement for app-layer handlers."""

    def __init__(self, json_data=None, user="admin", client_host="127.0.0.1"):
        self._json = json_data or {}
        self.state = SimpleNamespace(session={"user": user})
        self.url = SimpleNamespace(path="/test")
        self.client = SimpleNamespace(host=client_host)
        self.headers = {}
        self.cookies = {"wc_session": "fake"}

    async def json(self):
        return self._json


# ── Config / version ───────────────────────────────────────────────────────────

class VersionTests(unittest.TestCase):
    """VERSION constant must contain 0.3.0."""

    def test_version_contains_030(self):
        self.assertIn("0.3.0", config.VERSION)


# ── DB: model column migration ─────────────────────────────────────────────────

class ModelMigrationTests(unittest.IsolatedAsyncioTestCase):
    """The chats table must have a ``model`` column after init."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db.config, "DB_PATH", f"{self.tmp.name}/webconsole.db")
        self.root_patch = patch.object(db.config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_model_column_exists(self):
        cursor = await db.db_conn.execute("PRAGMA table_info(chats)")
        columns = {row["name"] for row in await cursor.fetchall()}
        self.assertIn("model", columns)

    async def test_chat_set_model_persists(self):
        chat_id = "model-chat"
        work_dir = f"{self.tmp.name}/projects/{chat_id}"
        Path(work_dir).mkdir(parents=True)
        await db.chat_create(chat_id, "Model Chat", None, work_dir, "admin")
        await db.chat_set_model(chat_id, "claude-opus-5")
        chat = await db.chat_get(chat_id, "admin")
        self.assertEqual(chat["model"], "claude-opus-5")

    async def test_chat_set_model_updates(self):
        chat_id = "model-update"
        work_dir = f"{self.tmp.name}/projects/{chat_id}"
        Path(work_dir).mkdir(parents=True)
        await db.chat_create(chat_id, "Model Update", None, work_dir, "admin")
        await db.chat_set_model(chat_id, "claude-sonnet-4-20250514")
        await db.chat_set_model(chat_id, "claude-opus-5")
        chat = await db.chat_get(chat_id, "admin")
        self.assertEqual(chat["model"], "claude-opus-5")


# ── DB: settings persistence ──────────────────────────────────────────────────

class SettingsTests(unittest.IsolatedAsyncioTestCase):
    """Persistent settings GET/SET via the settings table."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db.config, "DB_PATH", f"{self.tmp.name}/webconsole.db")
        self.root_patch = patch.object(db.config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_setting_set_and_get(self):
        await db.setting_set("ai_machine_host", "10.0.0.1")
        value = await db.setting_get("ai_machine_host")
        self.assertEqual(value, "10.0.0.1")

    async def test_setting_get_missing_key_returns_none(self):
        value = await db.setting_get("nonexistent_key_xyz")
        self.assertIsNone(value)

    async def test_setting_update_overwrites(self):
        await db.setting_set("ai_machine_host", "192.168.1.1")
        await db.setting_set("ai_machine_host", "10.0.0.99")
        value = await db.setting_get("ai_machine_host")
        self.assertEqual(value, "10.0.0.99")

    async def test_setting_session_ttl(self):
        await db.setting_set("session_ttl", "7200")
        value = await db.setting_get("session_ttl")
        self.assertEqual(value, "7200")


# ── DB: model extraction from transcript ───────────────────────────────────────

class ModelExtractionTests(unittest.TestCase):
    """Transcript-backed model extraction works with real JSONL structure."""

    def test_no_transcript_returns_none(self):
        with patch.object(db, "_CLAUDE_PROJECTS_DIR", Path("/nonexistent/path/zzz")):
            self.assertIsNone(db._extract_model_from_transcript("any-session-id"))

    def test_synthetic_model_is_ignored(self):
        project_dir = Path(tempfile.mkdtemp())
        jsonl_path = project_dir / "test.jsonl"
        jsonl_path.write_text(
            json.dumps({"type": "assistant", "sessionId": "sess-1",
                        "message": {"role": "assistant", "model": "<synthetic>"}})
            + "\n"
        )
        with patch.object(db, "_CLAUDE_PROJECTS_DIR", project_dir):
            self.assertIsNone(db._extract_model_from_transcript("sess-1"))

    def test_real_model_is_returned(self):
        project_dir = Path(tempfile.mkdtemp())
        jsonl_path = project_dir / "test.jsonl"
        jsonl_path.write_text(
            json.dumps({"type": "assistant", "sessionId": "sess-2",
                        "message": {"role": "assistant", "model": "claude-opus-5"}})
            + "\n"
        )
        with patch.object(db, "_CLAUDE_PROJECTS_DIR", project_dir):
            model = db._extract_model_from_transcript("sess-2")
            self.assertEqual(model, "claude-opus-5")

    def test_last_model_wins(self):
        project_dir = Path(tempfile.mkdtemp())
        jsonl_path = project_dir / "test.jsonl"
        jsonl_path.write_text(
            json.dumps({"type": "assistant", "sessionId": "sess-3",
                        "message": {"role": "assistant", "model": "claude-sonnet-4-20250514"}})
            + "\n"
            + json.dumps({"type": "assistant", "sessionId": "sess-3",
                          "message": {"role": "assistant", "model": "claude-haiku-4-20250514"}})
            + "\n"
        )
        with patch.object(db, "_CLAUDE_PROJECTS_DIR", project_dir):
            model = db._extract_model_from_transcript("sess-3")
            self.assertEqual(model, "claude-haiku-4-20250514")

    def test_wrong_session_id_ignored(self):
        project_dir = Path(tempfile.mkdtemp())
        jsonl_path = project_dir / "test.jsonl"
        jsonl_path.write_text(
            json.dumps({"type": "assistant", "sessionId": "sess-99",
                        "message": {"role": "assistant", "model": "claude-opus-5"}})
            + "\n"
        )
        with patch.object(db, "_CLAUDE_PROJECTS_DIR", project_dir):
            self.assertIsNone(db._extract_model_from_transcript("sess-different"))

    def test_non_assistant_type_ignored(self):
        project_dir = Path(tempfile.mkdtemp())
        jsonl_path = project_dir / "test.jsonl"
        jsonl_path.write_text(
            json.dumps({"type": "user", "sessionId": "sess-4",
                        "message": {"role": "user", "content": "hello"}})
            + "\n"
        )
        with patch.object(db, "_CLAUDE_PROJECTS_DIR", project_dir):
            self.assertIsNone(db._extract_model_from_transcript("sess-4"))


# ── DB: _lookup_session_model ─────────────────────────────────────────────────

class SessionModelLookupTests(unittest.TestCase):
    """_lookup_session_model uses transcript when session file lacks model."""

    def test_cached_result_returned(self):
        db._model_cache["cached-sess"] = "claude-opus-5"
        with patch.object(db, "_extract_model_from_transcript", return_value=None):
            result = db._lookup_session_model("cached-sess")
            self.assertEqual(result, "claude-opus-5")

    def test_transcript_lookup_fallback(self):
        project_dir = Path(tempfile.mkdtemp())
        jsonl_path = project_dir / "test.jsonl"
        jsonl_path.write_text(
            json.dumps({"type": "assistant", "sessionId": "fallback-sess",
                        "message": {"role": "assistant", "model": "claude-sonnet-4-20250514"}})
            + "\n"
        )
        with patch.object(db, "_CLAUDE_PROJECTS_DIR", project_dir):
            result = db._lookup_session_model("fallback-sess")
            self.assertEqual(result, "claude-sonnet-4-20250514")


# ── App: settings endpoints ────────────────────────────────────────────────────

class SettingsApiTests(unittest.IsolatedAsyncioTestCase):
    """GET and PATCH /api/settings endpoints."""

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

    async def test_settings_get_returns_host_version(self):
        handler = app.handle_settings_get
        req = _FakeRequest()
        resp = await handler(req)
        body = json.loads(resp.body.decode())
        self.assertIn("ai_machine_host", body)
        self.assertIn("version", body)
        self.assertEqual(body["version"], "0.3.0")

    async def test_settings_patch_updates_host(self):
        handler = app.handle_settings_patch
        req = _FakeRequest(json_data={"ai_machine_host": "192.168.1.100"})
        resp = await handler(req)
        data = json.loads(resp.body.decode())
        self.assertTrue(data["ok"])
        value = await db.setting_get("ai_machine_host")
        self.assertEqual(value, "192.168.1.100")

    async def test_settings_patch_updates_session_ttl(self):
        handler = app.handle_settings_patch
        req = _FakeRequest(json_data={"session_ttl": 3600})
        resp = await handler(req)
        data = json.loads(resp.body.decode())
        self.assertTrue(data["ok"])
        value = await db.setting_get("session_ttl")
        self.assertEqual(value, "3600")

    async def test_settings_patch_updates_models(self):
        handler = app.handle_settings_patch
        req = _FakeRequest(json_data={
            "default_model": "claude-opus-5",
            "fallback_model": "claude-haiku-4-20250514",
        })
        resp = await handler(req)
        self.assertTrue(json.loads(resp.body.decode())["ok"])
        self.assertEqual(await db.setting_get("default_model"), "claude-opus-5")
        self.assertEqual(await db.setting_get("fallback_model"), "claude-haiku-4-20250514")

    async def test_settings_get_returns_models(self):
        await db.setting_set("default_model", "claude-opus-5")
        await db.setting_set("fallback_model", "claude-haiku-4-20250514")
        body = json.loads((await app.handle_settings_get(_FakeRequest())).body.decode())
        self.assertEqual(body["default_model"], "claude-opus-5")
        self.assertEqual(body["fallback_model"], "claude-haiku-4-20250514")

    async def test_settings_patch_rejects_invalid_model(self):
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_settings_patch(_FakeRequest(json_data={"default_model": "bad model"}))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_settings_patch_updates_multiple(self):
        handler = app.handle_settings_patch
        req = _FakeRequest(json_data={
            "ai_machine_host": "10.0.0.5",
            "session_ttl": 3600,
        })
        resp = await handler(req)
        data = json.loads(resp.body.decode())
        self.assertTrue(data["ok"])
        self.assertEqual(await db.setting_get("ai_machine_host"), "10.0.0.5")
        self.assertEqual(await db.setting_get("session_ttl"), "3600")

    async def test_settings_patch_rejects_non_string_host(self):
        handler = app.handle_settings_patch
        req = _FakeRequest(json_data={"ai_machine_host": 12345})
        try:
            await handler(req)
        except Exception as exc:
            self.assertEqual(exc.status_code, 400)

    async def test_settings_patch_rejects_malformed_host(self):
        handler = app.handle_settings_patch
        req = _FakeRequest(json_data={"ai_machine_host": "http://evil.com"})
        try:
            await handler(req)
        except Exception as exc:
            self.assertEqual(exc.status_code, 400)

    async def test_settings_patch_rejects_port_in_host(self):
        handler = app.handle_settings_patch
        req = _FakeRequest(json_data={"ai_machine_host": "10.0.0.1:8080"})
        try:
            await handler(req)
        except Exception as exc:
            self.assertEqual(exc.status_code, 400)

    async def test_settings_patch_rejects_invalid_ttl(self):
        handler = app.handle_settings_patch
        req = _FakeRequest(json_data={"session_ttl": 0})
        try:
            await handler(req)
        except Exception as exc:
            self.assertEqual(exc.status_code, 400)


# ── App: hostname/IP validation ───────────────────────────────────────────────

class HostValidationTests(unittest.TestCase):
    """_HOST_PATTERN must accept hostnames/IPs but reject URLs and ports."""

    def setUp(self):
        self.pattern = app._HOST_PATTERN

    def test_accept_ipv4(self):
        self.assertTrue(self.pattern.fullmatch("10.0.0.1"))

    def test_accept_localhost(self):
        self.assertTrue(self.pattern.fullmatch("127.0.0.1"))

    def test_accept_ipv6(self):
        self.assertTrue(self.pattern.fullmatch("::1"))
        self.assertTrue(self.pattern.fullmatch("fe80::1"))

    def test_accept_ipv6_bracketed(self):
        self.assertTrue(self.pattern.fullmatch("[fe80::1]"))

    def test_accept_hostname(self):
        self.assertTrue(self.pattern.fullmatch("tailscale-host"))
        self.assertTrue(self.pattern.fullmatch("machine.example.com"))
        self.assertTrue(self.pattern.fullmatch("a"))
        self.assertTrue(self.pattern.fullmatch("x.y.z"))

    def test_reject_url(self):
        self.assertFalse(self.pattern.fullmatch("http://10.0.0.1"))
        self.assertFalse(self.pattern.fullmatch("https://machine.example.com"))
        self.assertFalse(self.pattern.fullmatch("//evil.com"))

    def test_reject_path(self):
        self.assertFalse(self.pattern.fullmatch("10.0.0.1/path"))
        self.assertFalse(self.pattern.fullmatch("machine.com/a/b"))

    def test_reject_port(self):
        self.assertFalse(self.pattern.fullmatch("10.0.0.1:8080"))
        self.assertFalse(self.pattern.fullmatch("machine.com:9000"))

    def test_reject_credentials(self):
        self.assertFalse(self.pattern.fullmatch("user:pass@machine.com"))

    def test_reject_empty(self):
        self.assertFalse(self.pattern.fullmatch(""))

    def test_reject_space(self):
        self.assertFalse(self.pattern.fullmatch("10 0 0 1"))


# ── Runner: model frame handling ───────────────────────────────────────────────

class ModelFrameTests(unittest.TestCase):
    """_normalise_cli_frame emits model from system/init frames."""

    def test_init_frame_emits_model(self):
        obj = {"type": "system", "subtype": "init",
               "session_id": "sess-10", "model": "claude-opus-5"}
        events = runner._normalise_cli_frame(obj)
        model_events = [e for e in events if e.get("type") == "model"]
        self.assertEqual(len(model_events), 1)
        self.assertEqual(model_events[0]["model"], "claude-opus-5")

    def test_init_frame_without_model(self):
        obj = {"type": "system", "subtype": "init",
               "session_id": "sess-11"}
        events = runner._normalise_cli_frame(obj)
        model_events = [e for e in events if e.get("type") == "model"]
        self.assertEqual(len(model_events), 0)

    def test_non_init_subtype_ignored(self):
        obj = {"type": "system", "subtype": "error",
               "session_id": "sess-12"}
        events = runner._normalise_cli_frame(obj)
        model_events = [e for e in events if e.get("type") == "model"]
        self.assertEqual(len(model_events), 0)


# ── DB: chat model appears in response ─────────────────────────────────────────

class ChatModelFieldTest(unittest.IsolatedAsyncioTestCase):
    """The model field is present in chat responses."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db.config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(db.config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_model_in_chat_get(self):
        chat_id = "model-in-list"
        work_dir = f"{self.tmp.name}/projects/{chat_id}"
        Path(work_dir).mkdir(parents=True)
        await db.chat_create(chat_id, "Model Test", None, work_dir, "admin")
        await db.chat_set_model(chat_id, "claude-haiku-4-20250514")
        chat = await db.chat_get(chat_id, "admin")
        self.assertEqual(chat["model"], "claude-haiku-4-20250514")

    async def test_model_is_none_when_not_set(self):
        chat_id = "empty-model"
        work_dir = f"{self.tmp.name}/projects/{chat_id}"
        Path(work_dir).mkdir(parents=True)
        await db.chat_create(chat_id, "Empty Model", None, work_dir, "admin")
        chat = await db.chat_get(chat_id, "admin")
        self.assertIsNone(chat.get("model"))


if __name__ == "__main__":
    unittest.main()