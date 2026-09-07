# Voice Conversation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persisted, voice-driven conversation feature to WebConsole — mic capture, streaming replies, TTS playback, barge-in — ported from the standalone `voice-chat-app` project, reusing WebConsole's existing chat/turn/streaming pipeline.

**Architecture:** A new `chats.voice_mode` flag makes `stream_handler` branch to a direct `AsyncOpenAI` call (bypassing the `claude` CLI entirely — a deliberate, scoped exception, see spec) instead of `runner.stream_turn`, reusing the existing `get_backend()` credential resolution. Frontend: a mic button creates a voice chat; ported STT/TTS/barge-in logic (from voice-chat-app) lives in new sibling JS files and hooks into the existing `send()`/SSE-render code with the smallest possible touch, since both `app.js` and `conversation.js` are already far over this project's 300-line-per-file cap.

**Tech Stack:** Python 3 / FastAPI / aiosqlite (existing), `openai` Python client (new dependency for the direct-call path), vanilla JS / Web Speech API (ported from voice-chat-app), the project's own existing test conventions (`unittest.IsolatedAsyncioTestCase` + tempdir-patched DB for backend tests, `tests/test_frontend_browser.py`'s `_BrowserFixture` for live-browser tests — confirmed by reading real existing test files, not assumed).

**Spec:** `docs/superpowers/specs/2026-09-06-voice-conversation-design.md` — the plan argues from this spec; read both.

## Global Constraints

- **File size:** every Python file stays under 400 lines, every JavaScript file under 300 lines (`rules.md`). `web/assets/app.js` (2254 lines) and `web/assets/conversation.js` (1363 lines) are **already over this cap** — per `rules.md`'s own rule ("do not add to it — extract the new code into a separate file and import it"), every task touching those two files adds **at most a few lines** (a single hook call), with all real logic in new sibling files.
- **Commits:** stage by explicit path only, never `-A` / `git add .` / `git commit -a` — this tree is shared by multiple concurrent sessions (confirmed via `git status` showing many other files mid-edit).
- **Documentation:** English-only, everywhere committed (`rules.md`).
- **No CLI second-guessing:** voice turns bypassing the `claude` CLI is a deliberate, user-approved exception (spec section "Deliberate exception"). Do not add `--tools`/CLI-flag-based tool restriction to this path — there must be no `claude` subprocess spawn in the voice code path at all.
- **Testing:** `.venv/bin/python -m pytest` (bare, not `pytest tests/`). No test may make a real network call to a model gateway — mock `AsyncOpenAI.chat.completions.create` exactly as `voice-chat-app/tests/test_chat.py` already does. Backend tests follow this project's **real, confirmed** convention: `unittest.IsolatedAsyncioTestCase`, `config.DB_PATH`/`config.PROJECTS_ROOT` patched to a `tempfile.TemporaryDirectory()` in `asyncSetUp`, `db.init()`/`db.close()` — copied from `tests/test_qa_auto_answer_routes.py`, not invented. HTTP-level tests use that same file's `_client()` (a `fastapi.testclient.TestClient` against `app.app`, `base_url="https://testserver"`) and `_login(who)` (POSTs `/login`, returns `(client, {"X-CSRF-Token": ...})`).

---

## File Structure

**New backend files:**
- `routes/voice.py` — the direct-call turn generator (`stream_voice_turn`), model-options-with-timing query, all voice-specific backend logic in one place with one clear responsibility.

**Modified backend files (minimal touches, following existing patterns exactly):**
- `db.py` — `voice_mode` column migration (in `_ensure_chat_columns`), `voice_turn_timing` table (in the main schema `CREATE TABLE` block).
- `routes/db_chats.py` — add `"voice_mode"` to `_ALLOWED_CHAT_FIELDS`.
- `routes/chats.py` — `stream_handler` branches to `routes.voice.stream_voice_turn` when `chat["voice_mode"]`; `handle_chat_create` gains an optional `voice_mode` request field.
- `routes/misc.py` — `handle_settings_get`/`handle_settings_patch` gain three new keys plus `voice_model_options`.
- `config.py` — three new env-var defaults.

**New frontend files:**
- `web/assets/voice-conversation.js` — ported STT/TTS/barge-in state machine (adapted from voice-chat-app's `speech-recognition.js` + `thinking-sound.js` + `app.js`'s TTS half), self-contained, exposes one object conversation.js calls into.
- `web/assets/voice-settings.js` — Settings dialog's two new App-tab rows (populate/save), self-contained, exposes two functions app.js calls.

**Modified frontend files (minimal touches):**
- `web/index.html` — sidebar mic button (×2, mobile+desktop), composer row Mic/Live/Stop icons, two new Settings App-tab rows, two new `<script>` tags.
- `web/assets/app.js` — one call each in the settings-load and settings-save functions, one call in the new-chat click handler branch, one call in the create-chat dialog's save handler.
- `web/assets/conversation.js` — one call each in `send()`'s `text`/`done`/`error` branches (lines ~1241, ~1155, and the surrounding `catch`).

**New test files:**
- `tests/test_voice_turn.py` — backend: schema migration, settings, `stream_voice_turn` (mocked `AsyncOpenAI`), `stream_handler`/chat-creation branching.
- `tests/test_qa_voice_conversation_ui.py` — frontend markup/wiring, source-inspection style (matches `tests/test_qa_auto_answer_ui.py`'s exact convention).
- `tests/test_qa_voice_conversation_browser.py` — live-browser, extends `tests/test_frontend_browser.py`'s `_BrowserFixture`.

---

## Task 1: `chats.voice_mode` column + allow it in `chat_update`

**Files:**
- Modify: `db.py` (`_ensure_chat_columns`, the `migrations` dict — currently ends `"degraded_at": "ALTER TABLE chats ADD COLUMN degraded_at TEXT",` around line 748)
- Modify: `routes/db_chats.py` (`_ALLOWED_CHAT_FIELDS`, line 23-31)
- Create: `tests/test_voice_turn.py`

**Interfaces:**
- Produces: `chats.voice_mode` column (`INTEGER NOT NULL DEFAULT 0`), usable via existing `db.chat_update(chat_id, owner_id, voice_mode=1)` and read via existing `db.chat_get(chat_id, owner_id)`. Also produces the test file's shared scaffolding (`VoiceTurnTests` class, `asyncSetUp`, `_client`, `_login`) that every later task's tests are added to as more methods.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_voice_turn.py -v`
Expected: FAIL — `voice_mode` KeyError/None, or `chat_update` returns `False` (field not in `_ALLOWED_CHAT_FIELDS`).

- [ ] **Step 3: Add the column migration**

In `db.py`, inside `_ensure_chat_columns`'s `migrations` dict, add one entry (after the existing `"degraded_at"` line):

```python
        "voice_mode": (
            # Set once at chat creation from the sidebar's voice button,
            # immutable after — no mid-conversation toggle. Gates whether
            # stream_handler dispatches to the direct-model-call path
            # (routes/voice.py) instead of the claude CLI.
            "ALTER TABLE chats ADD COLUMN voice_mode INTEGER NOT NULL DEFAULT 0"
        ),
```

Also add `voice_mode` to the main `CREATE TABLE IF NOT EXISTS chats (...)` block (for fresh databases, so a new install doesn't rely on the migration path at all) — insert right after the existing `ai_machine_id TEXT` line:

```python
            ai_machine_id TEXT,
            voice_mode    INTEGER NOT NULL DEFAULT 0
```

- [ ] **Step 4: Allow `voice_mode` through `chat_update`**

In `routes/db_chats.py`, add to `_ALLOWED_CHAT_FIELDS`:

```python
_ALLOWED_CHAT_FIELDS = {
    "title",
    "description",
    "archived",
    "pinned",
    "pinned_at",
    "model",
    "ai_machine_id",
    "voice_mode",
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_voice_turn.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add db.py routes/db_chats.py tests/test_voice_turn.py
git commit -m "feat: add chats.voice_mode column for voice conversations"
```

---

## Task 2: `voice_turn_timing` table + record/query helpers

**Files:**
- Modify: `db.py` (new `CREATE TABLE` in the main schema block, alongside `messages`/`settings`)
- Create: `routes/voice.py`
- Modify: `tests/test_voice_turn.py` (add methods to `VoiceTurnTests`)

**Interfaces:**
- Consumes: `db.db_conn` (existing module-level connection), `db._now()` (existing timestamp helper, used by every other table's `created_at`).
- Produces:
  - `async def record_voice_turn_timing(model: str, ttft_ms: int, total_ms: int) -> None` (in `routes/voice.py`)
  - `async def voice_model_timing_averages(active_models: list[str]) -> dict[str, dict]` (in `routes/voice.py`) — returns `{model_id: {"avg_ttft_ms": float | None, "turn_count": int}}` for every id in `active_models`, computed over the last 7 days; a model with zero turns is present with `{"avg_ttft_ms": None, "turn_count": 0}`.

- [ ] **Step 1: Write the failing test**

Add this method to `VoiceTurnTests` in `tests/test_voice_turn.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_voice_turn.py -k timing_records_and_averages -v`
Expected: FAIL — `ModuleNotFoundError` or `AttributeError: module 'routes.voice' has no attribute...`

- [ ] **Step 3: Add the table**

In `db.py`, in the main schema block, right after the `messages`/`messages_fts` block (before `settings`):

```python
        -- One row per completed voice-mode turn. Exists for two reasons:
        -- bypassing the claude CLI for voice also loses its automatic
        -- usage/cost recording (see CLAUDE.md's documented orchestrator.py
        -- lesson for what happens when a caller skips this), and it powers
        -- the "average reply time per model" annotation in the Settings
        -- dialog's voice model picker.
        CREATE TABLE IF NOT EXISTS voice_turn_timing (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            model        TEXT NOT NULL,
            ttft_ms      INTEGER NOT NULL,
            total_ms     INTEGER NOT NULL,
            recorded_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_voice_turn_timing_model
            ON voice_turn_timing(model, recorded_at);
```

- [ ] **Step 4: Create `routes/voice.py`**

```python
"""Voice conversation backend: the direct-model-call turn path (bypassing
the claude CLI, a deliberate scoped exception — see
docs/superpowers/specs/2026-09-06-voice-conversation-design.md) and its
usage-timing recording, which also powers the Settings dialog's per-model
average-reply-time display.
"""
from __future__ import annotations

import db


async def record_voice_turn_timing(model: str, ttft_ms: int, total_ms: int) -> None:
    await db.db_conn.execute(
        "INSERT INTO voice_turn_timing (model, ttft_ms, total_ms, recorded_at) "
        "VALUES (?, ?, ?, ?)",
        (model, ttft_ms, total_ms, db._now()),
    )
    await db.db_conn.commit()


async def voice_model_timing_averages(active_models: list[str]) -> dict[str, dict]:
    """Rolling 7-day average TTFT per model, for every id in active_models.

    A model with no recorded turns yet is still present in the result
    (avg_ttft_ms=None, turn_count=0) rather than omitted — the Settings UI
    shows "not yet used" for it instead of hiding the option.
    """
    result = {model_id: {"avg_ttft_ms": None, "turn_count": 0} for model_id in active_models}
    if not active_models:
        return result
    placeholders = ",".join("?" for _ in active_models)
    cur = await db.db_conn.execute(
        f"SELECT model, AVG(ttft_ms) AS avg_ttft, COUNT(*) AS n "
        f"FROM voice_turn_timing "
        f"WHERE model IN ({placeholders}) "
        f"AND recorded_at > datetime('now', '-7 days') "
        f"GROUP BY model",
        active_models,
    )
    for row in await cur.fetchall():
        result[row["model"]] = {
            "avg_ttft_ms": row["avg_ttft"],
            "turn_count": row["n"],
        }
    return result
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_voice_turn.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add db.py routes/voice.py tests/test_voice_turn.py
git commit -m "feat: add voice_turn_timing table and averaging helpers"
```

---

## Task 3: Voice settings — config defaults + settings API

**Files:**
- Modify: `config.py` (three new env-var defaults)
- Modify: `routes/misc.py` (`handle_settings_get`, `handle_settings_patch`)
- Modify: `tests/test_voice_turn.py` (add methods)

**Interfaces:**
- Consumes: `db.setting_get(key)` / `db.setting_set(key, value)` (existing, `routes/db_users.py`), `config.py` module attributes.
- Produces: `config.VOICE_AI_MACHINE_ID_DEFAULT`, `config.VOICE_MODEL_DEFAULT`, `config.VOICE_SPEECH_RATE_DEFAULT`; three new keys in the `GET /api/settings` response and three new writable fields in `PATCH /api/settings`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_voice_turn.py -k voice_defaults -v`
Expected: FAIL — `KeyError`/`AssertionError`, `voice_model` missing from the response.

- [ ] **Step 3: Add config defaults**

In `config.py`, near the existing `MODEL_NAME`/model-related settings. First confirm the exact shape of the existing `_int`/`_str` helpers with `grep -n "^def _int\|^def _str" config.py`, then add a `_float` matching that same shape, plus the three new constants:

```python
def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Voice conversation defaults (Settings dialog can override at runtime via
# the generic settings table; these are the fallback when unset).
VOICE_AI_MACHINE_ID_DEFAULT = _str("WC_VOICE_AI_MACHINE_ID_DEFAULT", None)
VOICE_MODEL_DEFAULT = _str("WC_VOICE_MODEL_DEFAULT", "azure_ai/gpt-5.6-luna")
VOICE_SPEECH_RATE_DEFAULT = _float("WC_VOICE_SPEECH_RATE_DEFAULT", 1.0)
```

- [ ] **Step 4: Extend `handle_settings_get`**

In `routes/misc.py`, inside `handle_settings_get`, following the exact pattern already used for `session_ttl`/`turn_timeout`/`prompt_max`:

```python
    voice_ai_machine_id = await db.setting_get("voice_ai_machine_id") or config.VOICE_AI_MACHINE_ID_DEFAULT
    voice_model = await db.setting_get("voice_model") or config.VOICE_MODEL_DEFAULT
    try:
        voice_speech_rate = float(
            await db.setting_get("voice_speech_rate") or config.VOICE_SPEECH_RATE_DEFAULT
        )
    except (TypeError, ValueError):
        voice_speech_rate = config.VOICE_SPEECH_RATE_DEFAULT
```

Add to the returned `JSONResponse` dict:

```python
            "voice_ai_machine_id": voice_ai_machine_id,
            "voice_model": voice_model,
            "voice_speech_rate": voice_speech_rate,
```

- [ ] **Step 5: Extend `handle_settings_patch`**

In `routes/misc.py`, inside `handle_settings_patch` (after the existing `ai_machine_host` block):

```python
    if "voice_ai_machine_id" in data:
        value = data.get("voice_ai_machine_id")
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=400, detail="Voice AI machine id must be text")
        await db.setting_set("voice_ai_machine_id", (value or "").strip())
    if "voice_model" in data:
        value = data.get("voice_model")
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(status_code=400, detail="Voice model is required")
        await db.setting_set("voice_model", value.strip())
    if "voice_speech_rate" in data:
        value = data.get("voice_speech_rate")
        try:
            rate = float(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Voice speech rate must be a number")
        if not (0.5 <= rate <= 5.0):
            raise HTTPException(status_code=400, detail="Voice speech rate must be between 0.5 and 5")
        await db.setting_set("voice_speech_rate", str(rate))
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_voice_turn.py -v`
Expected: PASS (5 tests)

- [ ] **Step 7: Commit**

```bash
git add config.py routes/misc.py tests/test_voice_turn.py
git commit -m "feat: add voice conversation settings (model, speech rate, AI machine)"
```

---

## Task 4: Direct-call turn generator (`stream_voice_turn`)

This is the core of the CLI-bypass exception. Re-read the spec's "Deliberate exception" and "Backend changes" sections before starting — the credential handling discipline (never log the resolved `api_key`) matters here specifically.

**Files:**
- Modify: `routes/voice.py`
- Modify: `tests/test_voice_turn.py` (add methods)

**Interfaces:**
- Consumes: `runner.get_backend(chat_id, owner) -> dict` (existing, returns `{"provider": ..., "base_url": ..., "api_key": ...}` or `{}`), `db.messages_batch(chat_id, rows: list[tuple[str, str]])` (existing).
- Produces: `async def stream_voice_turn(chat: dict, prompt: str, owner: str) -> AsyncGenerator[str, None]` — yields the exact same SSE frame strings `stream_handler` already yields (`data: {"type": "text", "content": ...}\n\n`, `data: {"type": "done"}\n\n`, `data: {"type": "error", "error": ...}\n\n`), so the existing frontend needs zero changes to parse them.

- [ ] **Step 1: Write the failing test**

```python
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
            return {"provider": "anthropic", "base_url": "https://example.test",
                    "api_key": "fake-key"}

        created_kwargs = {}

        async def fake_create(**kwargs):
            created_kwargs.update(kwargs)
            return _FakeStream(["Hello", ", ", "there."])

        with patch.object(runner, "get_backend", fake_get_backend), \
                patch.object(voice.AsyncOpenAI, "__init__", lambda self, **kw: None), \
                patch.object(voice.AsyncOpenAI, "chat", SimpleNamespace(
                    completions=SimpleNamespace(create=fake_create)), create=True), \
                patch.object(voice.AsyncOpenAI, "close", lambda self: _noop()):
            frames = [f async for f in voice.stream_voice_turn(chat, "hi", "admin")]

        joined = "".join(frames)
        self.assertIn('"type": "text"', joined)
        self.assertIn('"content": "Hello"', joined)
        self.assertIn('"type": "done"', joined)
        self.assertEqual(created_kwargs["model"], "azure_ai/gpt-5.6-luna")

        messages, _ = await db.messages_page("c5", limit=10)
        roles = [m["role"] for m in messages]
        self.assertEqual(roles, ["user", "assistant"])


async def _noop():
    return None
```

(`AsyncOpenAI.close` is itself async in the real client; the `patch.object` above must produce an awaitable — if `lambda self: _noop()` doesn't type-check cleanly against the real signature when this is actually run, replace it with `AsyncMock()` from `unittest.mock`, which is the standard fix for mocking an async method — check `from unittest.mock import AsyncMock` is available in this Python version, and prefer it over the lambda if there's any friction.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_voice_turn.py::VoiceTurnTests::test_stream_voice_turn_yields_matching_sse_frames -v`
Expected: FAIL — `AttributeError: module 'routes.voice' has no attribute 'stream_voice_turn'`

- [ ] **Step 3: Implement `stream_voice_turn`**

Add to `routes/voice.py`:

```python
import json
import time

from openai import AsyncOpenAI

import runner

# Conversational tone/brevity — adapted from voice-chat-app's SYSTEM_PROMPT.
# Kept even though this path genuinely has no tool schema available to the
# model (unlike the CLI path, where --tools "" makes the same true and this
# line would be redundant): it still steers tone, and costs nothing here.
VOICE_SYSTEM_PROMPT = (
    "You are a conversational thinking partner in a spoken voice chat. You "
    "have no tools, no file access, and cannot run code or take any action "
    "of any kind — you can only talk. Keep replies short and natural for "
    "speech: plain sentences, no markdown, no bullet lists, no code blocks."
)


async def stream_voice_turn(chat: dict, prompt: str, owner: str):
    """Yields SSE frame strings identical in shape to stream_handler's own
    (type: text/done/error), so the existing frontend parser needs no
    changes. Bypasses the claude CLI entirely -- see the spec's "Deliberate
    exception" section for why this is intentional, not a shortcut.
    """
    chat_id = chat["id"]
    model = chat.get("model") or ""
    backend = await runner.get_backend(chat_id, owner)
    base_url = backend.get("base_url")
    api_key = backend.get("api_key")
    if not base_url or not model:
        yield f"data: {json.dumps({'type': 'error', 'error': 'Voice chat has no configured model/backend'})}\n\n"
        return
    # get_backend()'s base_url comes from normalise_base_url(), which strips
    # a trailing /v1 for the CLI's Anthropic-messages shape -- the opposite
    # of what an OpenAI-compatible client needs. Never log base_url/api_key.
    if not base_url.rstrip("/").endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"

    client = AsyncOpenAI(base_url=base_url, api_key=api_key or "unused")
    t0 = time.time()
    ttft_ms = None
    assistant_text = ""
    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": VOICE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                if ttft_ms is None:
                    ttft_ms = int((time.time() - t0) * 1000)
                assistant_text += delta
                yield f"data: {json.dumps({'type': 'text', 'content': delta})}\n\n"
        await db.messages_batch(chat_id, [("user", prompt), ("assistant", assistant_text)])
        total_ms = int((time.time() - t0) * 1000)
        await record_voice_turn_timing(model, ttft_ms or total_ms, total_ms)
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
    except Exception as exc:  # noqa: BLE001 - surfaced to the client as an SSE event
        yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"
    finally:
        await client.close()
```

Add `openai` to `requirements.txt` if not already present (check first: `grep -i '^openai' requirements.txt`).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_voice_turn.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add routes/voice.py requirements.txt tests/test_voice_turn.py
git commit -m "feat: implement stream_voice_turn, the direct-model-call path"
```

---

## Task 5: Wire `stream_handler` to branch on `voice_mode`

**Files:**
- Modify: `routes/chats.py` (`stream_handler`, starting around line 1040)
- Modify: `tests/test_voice_turn.py` (add methods)

**Interfaces:**
- Consumes: `routes.voice.stream_voice_turn(chat, prompt, owner)` (Task 4).
- Produces: `POST /api/chats/{id}/stream` transparently uses the voice path for `voice_mode` chats, the existing CLI path for everything else — no change to the route's request/response contract.

- [ ] **Step 1: Write the failing test**

```python
    async def test_stream_handler_uses_voice_path_for_voice_chats(self):
        from routes import voice

        password = await self._make_admin_and_chat("c6", voice_mode=True)
        await db.chat_update("c6", "admin", model="azure_ai/gpt-5.6-luna",
                              ai_machine_id="fake-machine-id")

        async def fake_stream_voice_turn(chat, prompt, owner):
            yield 'data: {"type": "text", "content": "voice-path-used"}\n\n'
            yield 'data: {"type": "done"}\n\n'

        client, headers = self._login("admin", password)
        with patch.object(voice, "stream_voice_turn", fake_stream_voice_turn):
            response = client.post(
                "/api/chats/c6/stream", json={"content": "hi"}, headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("voice-path-used", response.text)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_voice_turn.py -k voice_path_for_voice_chats -v`
Expected: FAIL — real CLI path attempted instead (voice_mode ignored), or a timeout/error since no real `claude` CLI is available in the test environment.

- [ ] **Step 3: Add the branch**

In `routes/chats.py`'s `stream_handler`, immediately after the existing `chat = await db.chat_get(chat_id, session["user"])` / not-found check (around line 1054-1060), before the `data = await request.json()` line that currently follows into CLI-routing logic, add:

```python
    if chat.get("voice_mode"):
        data = await request.json()
        prompt = (data.get("content") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")
        if len(prompt) > config.PROMPT_MAX_CHARS:
            raise HTTPException(status_code=400, detail="Prompt is too long")

        from routes.voice import stream_voice_turn

        async def voice_event_generator():
            async for frame in stream_voice_turn(chat, prompt, session["user"]):
                yield frame

        return StreamingResponse(
            voice_event_generator(), media_type="text/event-stream"
        )
```

Check `StreamingResponse` is already imported in `routes/chats.py` (`grep -n "^from fastapi.responses import" routes/chats.py`) and add it to that import line if missing.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_voice_turn.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add routes/chats.py tests/test_voice_turn.py
git commit -m "feat: branch stream_handler to the voice turn path when voice_mode is set"
```

---

## Task 6: Voice chat creation

**Files:**
- Modify: `routes/chats.py` (`handle_chat_create`, around line 190)
- Modify: `tests/test_voice_turn.py` (add methods)

**Interfaces:**
- Consumes: `db.chat_update(chat_id, owner_id, **fields)` (existing, Task 1 added `voice_mode` to its allowlist), `db.setting_get` (Task 3's new keys).
- Produces: `POST /api/chats` accepts an optional `voice_mode: true` field; when set, the created chat is pinned to the configured voice AI machine/model and flagged.

- [ ] **Step 1: Write the failing test**

```python
    async def test_chat_create_with_voice_mode_pins_machine_and_model(self):
        password = secrets.token_urlsafe(16)
        await db.user_create("admin", None, auth.hash_password(password))
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_voice_turn.py -k voice_mode_pins_machine -v`
Expected: FAIL — `voice_mode` ignored, chat created as a normal chat (`chat["voice_mode"] == 0`).

- [ ] **Step 3: Extend `handle_chat_create`**

In `routes/chats.py`'s `handle_chat_create`, after the existing `now = await db.chat_create(...)` call and its `_log.info(...)` line, before the `return JSONResponse(...)`:

```python
    voice_mode = bool(data.get("voice_mode"))
    if voice_mode:
        voice_machine_id = await db.setting_get("voice_ai_machine_id") or config.VOICE_AI_MACHINE_ID_DEFAULT
        voice_model = await db.setting_get("voice_model") or config.VOICE_MODEL_DEFAULT
        await db.chat_update(
            chat_id, session["user"],
            voice_mode=1, model=voice_model, ai_machine_id=voice_machine_id,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_voice_turn.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add routes/chats.py tests/test_voice_turn.py
git commit -m "feat: support voice_mode flag in chat creation"
```

---

## Task 7: Voice model options for the Settings dropdown

**Files:**
- Modify: `routes/misc.py` (`handle_settings_get`)
- Modify: `tests/test_voice_turn.py` (add methods)

**Interfaces:**
- Consumes: `routes.db_machines.ai_machine_backend_by_id`/`parse_active_models` (existing), `routes.voice.voice_model_timing_averages` (Task 2).
- Produces: `GET /api/settings` response gains `"voice_model_options": [{"id": ..., "avg_ttft_ms": ..., "turn_count": ...}, ...]`.

- [ ] **Step 1: Write the failing test**

```python
    async def test_settings_get_includes_voice_model_options(self):
        from routes import db_machines, voice

        password = await self._make_admin_and_chat("c7")
        await db.setting_set("voice_ai_machine_id", "voice-machine-2")
        await voice.record_voice_turn_timing("azure_ai/gpt-5.6-luna", 1100, 1200)

        async def fake_ai_machine_backend_by_id(machine_id, owner_id):
            return {"id": machine_id, "active_models": '["azure_ai/gpt-5.6-luna", "vllm/Qwen3.5-0.8B"]'}

        client, headers = self._login("admin", password)
        with patch.object(db_machines, "ai_machine_backend_by_id", fake_ai_machine_backend_by_id):
            response = client.get("/api/settings", headers=headers)
        options = {o["id"]: o for o in response.json()["voice_model_options"]}
        self.assertEqual(options["azure_ai/gpt-5.6-luna"]["turn_count"], 1)
        self.assertEqual(options["vllm/Qwen3.5-0.8B"]["turn_count"], 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_voice_turn.py -k voice_model_options -v`
Expected: FAIL — `KeyError: 'voice_model_options'`

- [ ] **Step 3: Extend `handle_settings_get`**

In `routes/misc.py`, after the voice settings block added in Task 3, add:

```python
    from routes.db_machines import ai_machine_backend_by_id, parse_active_models
    from routes.voice import voice_model_timing_averages

    voice_model_options = []
    if voice_ai_machine_id:
        session = request.state.session
        machine = await ai_machine_backend_by_id(voice_ai_machine_id, session["user"])
        if machine:
            active_models = parse_active_models(machine.get("active_models"))
            averages = await voice_model_timing_averages(active_models)
            voice_model_options = [
                {"id": model_id, **averages[model_id]} for model_id in active_models
            ]
```

If `handle_settings_get` does not already have `session = request.state.session` earlier in the function (check first — it may already use `request.state.session` for something else), add that line once near the top rather than fetching it twice.

Add `"voice_model_options": voice_model_options,` to the returned dict.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_voice_turn.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add routes/misc.py tests/test_voice_turn.py
git commit -m "feat: expose per-model average voice reply time in settings"
```

---

## Task 8: Settings dialog UI — two new App-tab rows

**Files:**
- Modify: `web/index.html` (`panelApp`, around line 308-330)
- Create: `web/assets/voice-settings.js`
- Modify: `web/assets/app.js` (two single-line hook calls — do **not** add the field-handling logic directly here, `app.js` is already 2254 lines, over the 300-line cap)
- Create: `tests/test_qa_voice_conversation_ui.py`

**Interfaces:**
- Produces: `renderVoiceSettingsFields(data)` and `collectVoiceSettingsFields(body, loadedSettings)` (both in `voice-settings.js`, called from `app.js`).

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_qa_voice_conversation_ui.py — markup/wiring for the voice
conversation feature. Source-inspection style, matching
tests/test_qa_auto_answer_ui.py exactly -- no browser needed for markup
assertions (see tests/test_qa_voice_conversation_browser.py, Task 12, for
the live-browser counterpart).
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
ASSETS = WEB / "assets"


class VoiceConversationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text()

    def test_voice_settings_rows_exist_in_app_tab(self):
        panel = self.html.split('id="panelApp"')[1].split('</div>\n    </div>')[0]
        self.assertIn('id="voiceModelSelect"', panel)
        self.assertIn('id="voiceSpeechRate"', panel)

    def test_voice_settings_js_is_loaded(self):
        self.assertIn('voice-settings.js', self.html)

    def test_voice_settings_js_file_stays_under_300_lines(self):
        content = (ASSETS / "voice-settings.js").read_text()
        self.assertLess(len(content.splitlines()), 300)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_voice_conversation_ui.py -v`
Expected: FAIL — markup/file don't exist yet.

- [ ] **Step 3: Add the two rows to `web/index.html`**

Inside `panelApp` (after the existing `webconsoleUrl` row, before the closing `</div>`):

```html
        <div class="app-setting-row">
          <label for="voiceModelSelect">Voice conversation model</label>
          <select id="voiceModelSelect"></select>
          <span class="setting-hint">Fetched from the voice AI machine's active models, each labeled with its average reply time from real usage. Separate from the model used in regular text chats — voice cares more about reply speed.</span>
        </div>
        <div class="app-setting-row">
          <label for="voiceSpeechRate">Voice speech rate</label>
          <input id="voiceSpeechRate" type="range" min="0.5" max="5" step="0.1">
          <span class="setting-hint" id="voiceSpeechRateValue">1.0x</span>
        </div>
```

Check the existing script tags' exact path/query-string convention first (`grep -n '<script src="/assets/' web/index.html`) and add a matching one for `voice-settings.js` right before `app.js`'s tag, since `app.js` will call into it.

- [ ] **Step 4: Create `web/assets/voice-settings.js`**

```javascript
// Voice conversation settings: the two "Voice conversation model" / "Voice
// speech rate" rows in the Settings dialog's App tab. Split out of app.js
// (already 2254 lines, over this project's 300-line-per-file cap) rather
// than added to it — see rules.md's "no big files" rule.

function renderVoiceSettingsFields(data) {
  const select = document.getElementById('voiceModelSelect');
  const rateInput = document.getElementById('voiceSpeechRate');
  const rateValue = document.getElementById('voiceSpeechRateValue');
  if (!select || !rateInput) return;

  select.innerHTML = '';
  const options = data.voice_model_options || [];
  for (const opt of options) {
    const el = document.createElement('option');
    el.value = opt.id;
    const timing = opt.turn_count > 0
      ? `~${(opt.avg_ttft_ms / 1000).toFixed(1)}s avg, ${opt.turn_count} turns`
      : 'not yet used';
    el.textContent = `${opt.id} (${timing})`;
    if (opt.id === data.voice_model) el.selected = true;
    select.appendChild(el);
  }

  const rate = data.voice_speech_rate ?? 1.0;
  rateInput.value = rate;
  rateValue.textContent = `${Number(rate).toFixed(1)}x`;
  rateInput.oninput = () => {
    rateValue.textContent = `${parseFloat(rateInput.value).toFixed(1)}x`;
  };
}

function collectVoiceSettingsFields(body, loadedSettings) {
  const select = document.getElementById('voiceModelSelect');
  const rateInput = document.getElementById('voiceSpeechRate');
  if (select && select.value && select.value !== loadedSettings?.voice_model) {
    body.voice_model = select.value;
  }
  if (rateInput) {
    const rate = parseFloat(rateInput.value);
    if (!Number.isNaN(rate) && rate !== loadedSettings?.voice_speech_rate) {
      body.voice_speech_rate = rate;
    }
  }
}
```

- [ ] **Step 5: Wire the two hook calls into `app.js`**

At `web/assets/app.js` line ~1522 (right after the existing `if (data.webconsole_url) byId('webconsoleUrl').value = data.webconsole_url;`), add one line:

```javascript
      renderVoiceSettingsFields(data);
```

At line ~341 (right after the existing `webconsoleUrl` block in the settings-save function), add one line:

```javascript
    collectVoiceSettingsFields(body, _loadedSettings);
```

Confirm `_loadedSettings` is the exact variable name already used for the previously-loaded settings object at both call sites (`grep -n "_loadedSettings" web/assets/app.js`) and match exactly.

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_voice_conversation_ui.py -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Commit**

```bash
git add web/index.html web/assets/voice-settings.js web/assets/app.js tests/test_qa_voice_conversation_ui.py
git commit -m "feat: add voice model/speech-rate rows to Settings App tab"
```

---

## Task 9: Sidebar mic button + voice chat creation dialog flow

**Files:**
- Modify: `web/index.html` (two `.new-chat-btn` locations, lines 14 and 21)
- Modify: `web/assets/app.js` (the `.new-chat-btn` click handler at line 2125, `openChatDialog`, its save handler)
- Modify: `tests/test_qa_voice_conversation_ui.py` (add methods)

**Interfaces:**
- Produces: a `🎙` icon button beside each `.new-chat-btn`; clicking it opens the same create-conversation dialog, tagged so its save handler POSTs `voice_mode: true`.

- [ ] **Step 1: Write the failing test**

```python
    def test_mic_new_chat_button_exists_beside_new_conversation(self):
        occurrences = self.html.count('class="btn-new new-chat-btn"')
        voice_occurrences = self.html.count('voice-new-chat-btn')
        self.assertEqual(occurrences, 2, "expected mobile + desktop new-chat buttons")
        self.assertEqual(voice_occurrences, 2, "expected mobile + desktop voice buttons")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_voice_conversation_ui.py -k mic_new_chat -v`
Expected: FAIL — `voice_occurrences == 0`.

- [ ] **Step 3: Add the buttons to `web/index.html`**

At line 14 (mobile sidebar), change:

```html
  <button class="btn-new new-chat-btn">＋ New conversation</button>
```

to:

```html
  <div style="display:flex; gap:8px;">
    <button class="btn-new new-chat-btn" style="flex:1">＋ New conversation</button>
    <button class="btn-icon voice-new-chat-btn" title="New voice conversation" aria-label="New voice conversation">🎙</button>
  </div>
```

Apply the identical change at line 21 (desktop sidebar).

- [ ] **Step 4: Wire the click handler**

In `web/assets/app.js`, at line 2125, change:

```javascript
  document.querySelectorAll('.new-chat-btn').forEach(button => button.addEventListener('click', () => openChatDialog('create')));
```

to:

```javascript
  document.querySelectorAll('.new-chat-btn').forEach(button => button.addEventListener('click', () => openChatDialog('create')));
  document.querySelectorAll('.voice-new-chat-btn').forEach(button => button.addEventListener('click', () => openChatDialog('create', undefined, {voiceMode: true})));
```

Modify `openChatDialog` (line 167) to accept and stash the third parameter:

```javascript
function openChatDialog(mode, chat = state.currentChat, options = {}) {
  dialogMode = mode;
  dialogChat = chat;
  dialogVoiceMode = Boolean(options.voiceMode);
```

Declare `let dialogVoiceMode = false;` alongside the existing `let dialogMode;`/`let dialogChat;` module-level variables (`grep -n "let dialogMode\|let dialogChat" web/assets/app.js`).

Find the dialog's save/submit handler (`grep -n "dialogSave\|function saveDialog\|dialogMode === 'create'" web/assets/app.js`) and add `voice_mode: dialogVoiceMode` to whatever request body it already builds for chat creation. Reset `dialogVoiceMode = false;` in `closeDialog()` (line 188) alongside the existing `dialogChat = null;`.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_voice_conversation_ui.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add web/index.html web/assets/app.js tests/test_qa_voice_conversation_ui.py
git commit -m "feat: add sidebar voice-conversation button"
```

---

## Task 10: Composer row — Mic/Live/Stop icons + ported STT/TTS state machine

This is the largest task: porting voice-chat-app's `speech-recognition.js` (STT state machine, silence-timeout turn-conclusion, growing-re-transcription dedup, the pause-only-during-speaking echo fix, the spoken-"stop" barge-in trigger and its restart-on-unexpected-end fix) and `thinking-sound.js` (the audio cue) into one new file, wired to `conversation.js`'s existing `send(forcedContent)`.

**Files:**
- Modify: `web/index.html` (composer row, line ~123-131; one new `<script>` tag)
- Create: `web/assets/voice-conversation.js`
- Modify: `tests/test_qa_voice_conversation_ui.py` (add methods)

**Interfaces:**
- Consumes: `send(forcedContent)` (existing, `conversation.js` line 1160 — already accepts text directly), `state.currentChat` (existing global; check `state.currentChat.voice_mode` is present in whatever the chat object already includes when loaded via `grep -n "state.currentChat = " web/assets/conversation.js`).
- Produces: `window.voiceConversation = { onReplyChunk(text), onReplyDone(), onReplyError() }` — the three hooks `conversation.js` calls (Task 11).

- [ ] **Step 1: Write the failing test**

```python
    def test_composer_row_has_voice_icons(self):
        composer = self.html.split('id="composerArea"')[1].split('</section>')[0]
        for control in ('voiceMicBtn', 'voiceLiveBtn', 'voiceStopBtn'):
            self.assertIn(f'id="{control}"', composer)

    def test_voice_conversation_js_file_stays_under_300_lines(self):
        content = (ASSETS / "voice-conversation.js").read_text()
        self.assertLess(len(content.splitlines()), 300)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_voice_conversation_ui.py -k composer_row -v`
Expected: FAIL — controls don't exist.

- [ ] **Step 3: Add the composer icons**

In `web/index.html`, change the composer's button row (around line 131) from:

```html
      <button class="btn-send" id="sendBtn" aria-label="Send message">➜</button>
```

to:

```html
      <button class="btn-icon voice-control" id="voiceMicBtn" title="Mic" aria-label="Start voice input">🎙</button>
      <button class="btn-icon voice-control" id="voiceLiveBtn" title="Live Conversation" aria-label="Start live voice conversation">🔁</button>
      <button class="btn-icon voice-control" id="voiceStopBtn" title="Stop" aria-label="Stop voice conversation" hidden>⏹</button>
      <button class="btn-send" id="sendBtn" aria-label="Send message">➜</button>
```

`voiceMicBtn`/`voiceLiveBtn` are hidden for non-`voice_mode` chats by `voice-conversation.js` itself (Step 4's `updateVoiceButtonVisibility`); `voiceStopBtn` starts `hidden` (Stop only appears once voice mode is actually active, per the confirmed UI decision) and `sendBtn` is hidden in its place when active.

Add the script tag after `conversation.js`'s (`grep -n 'conversation.js' web/index.html` for its exact tag first):

```html
  <script src="/assets/voice-conversation.js?v=1"></script>
```

- [ ] **Step 4: Create `web/assets/voice-conversation.js`**

Port the state machine from voice-chat-app's `static/speech-recognition.js` and `static/thinking-sound.js`. Key adaptations from the source: `sendMessage(text)` calls become `send(text)` (WebConsole's existing function, not a new fetch); the pause-during-speaking/hands-free-resume/barge-in logic is unchanged in spirit but driven by the three hooks below instead of `app.js`'s own fetch-reading loop; everything in this file is a no-op unless the current chat has `voice_mode` truthy.

```javascript
// Voice conversation: mic capture, hands-free Live Conversation, TTS
// playback, barge-in. Ported from voice-chat-app's speech-recognition.js /
// thinking-sound.js / app.js, adapted to drive WebConsole's existing
// send(forcedContent) and conversation rendering instead of a bare fetch.
// Kept in its own file since conversation.js (1363 lines) and app.js (2254
// lines) are both already over this project's 300-line-per-file cap.

const voiceMicBtn = document.getElementById('voiceMicBtn');
const voiceLiveBtn = document.getElementById('voiceLiveBtn');
const voiceStopBtn = document.getElementById('voiceStopBtn');
const voiceSendBtn = document.getElementById('sendBtn');

const SILENCE_TIMEOUT_MS = 1000;
let recognition = null;
let bargeInRecognition = null;
let recognizing = false;
let handsFreeMode = false;
let intentionalStop = false;
let intentionalBargeInStop = false;
let recognitionFatalError = false;
let accumulatedText = '';
let lastFinalChunk = '';
let silenceTimer = null;
let pendingSpeechCount = 0;
let speechBuffer = '';
let voiceStatus = 'idle'; // idle | listening | thinking | speaking

function updateVoiceButtonVisibility() {
  const active = Boolean(window.state?.currentChat?.voice_mode);
  voiceMicBtn.hidden = !active;
  voiceLiveBtn.hidden = !active;
  const inTurn = voiceStatus !== 'idle';
  voiceMicBtn.disabled = inTurn;
  voiceLiveBtn.disabled = inTurn;
  voiceStopBtn.hidden = !active || !inTurn;
  voiceSendBtn.hidden = active && inTurn;
}

function setVoiceStatus(next) {
  const previous = voiceStatus;
  voiceStatus = next;
  updateVoiceButtonVisibility();
  if (next === 'speaking' && previous !== 'speaking' && recognition && recognizing) {
    intentionalStop = true;
    recognition.stop();
  }
  if (next === 'speaking' && previous !== 'speaking') startBargeInListening();
  if (next !== 'speaking' && previous === 'speaking') stopBargeInListening();
  if (next === 'idle' && previous !== 'idle' && handsFreeMode) startListening(true);
}

function performVoiceStop(endConversation) {
  if (endConversation) handsFreeMode = false;
  window.speechSynthesis.cancel();
  pendingSpeechCount = 0;
  if (recognition && recognizing) {
    intentionalStop = true;
    recognition.stop();
  }
  accumulatedText = '';
  lastFinalChunk = '';
  setVoiceStatus('idle');
}

function mergeFinalChunk(chunk) {
  const trimmedChunk = chunk.trim();
  if (!trimmedChunk) return;
  const lowerChunk = trimmedChunk.toLowerCase();
  if (lastFinalChunk && lowerChunk.startsWith(lastFinalChunk.toLowerCase())) {
    accumulatedText = (
      accumulatedText.slice(0, accumulatedText.length - lastFinalChunk.length) + trimmedChunk
    ).trim();
    lastFinalChunk = trimmedChunk;
    return;
  }
  if (accumulatedText.toLowerCase().endsWith(lowerChunk)) return;
  accumulatedText = (accumulatedText + ' ' + trimmedChunk).trim();
  lastFinalChunk = trimmedChunk;
}

function resetSilenceTimer() {
  if (silenceTimer) clearTimeout(silenceTimer);
  silenceTimer = setTimeout(() => {
    silenceTimer = null;
    const finalText = accumulatedText.trim();
    if (!finalText) return;
    accumulatedText = '';
    lastFinalChunk = '';
    setVoiceStatus('thinking');
    send(finalText);
  }, SILENCE_TIMEOUT_MS);
}

const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition;
if (SpeechRecognitionImpl) {
  recognition = new SpeechRecognitionImpl();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.onstart = () => { recognizing = true; resetSilenceTimer(); };
  recognition.onresult = (event) => {
    if (voiceStatus !== 'listening') return;
    resetSilenceTimer();
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (event.results[i].isFinal) mergeFinalChunk(event.results[i][0].transcript);
    }
  };
  recognition.onerror = (event) => {
    if (['not-allowed', 'audio-capture', 'service-not-allowed'].includes(event.error)) {
      recognitionFatalError = true;
      handsFreeMode = false;
    }
  };
  recognition.onend = () => {
    recognizing = false;
    if (voiceStatus !== 'listening') return;
    if (silenceTimer && !recognitionFatalError) {
      try { recognition.start(); return; } catch { recognitionFatalError = true; }
    }
    if (silenceTimer) clearTimeout(silenceTimer);
    silenceTimer = null;
    recognitionFatalError = false;
    setVoiceStatus('idle');
  };

  bargeInRecognition = new SpeechRecognitionImpl();
  bargeInRecognition.continuous = true;
  bargeInRecognition.interimResults = true;
  bargeInRecognition.onresult = (event) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (/\bstop\b/i.test(event.results[i][0].transcript)) {
        performVoiceStop(false);
        return;
      }
    }
  };
  bargeInRecognition.onend = () => {
    if (intentionalBargeInStop) { intentionalBargeInStop = false; return; }
    if (voiceStatus === 'speaking') { try { bargeInRecognition.start(); } catch { /* ignore */ } }
  };
}

function startBargeInListening() { try { bargeInRecognition?.start(); } catch { /* ignore */ } }
function stopBargeInListening() {
  intentionalBargeInStop = true;
  try { bargeInRecognition?.stop(); } catch { intentionalBargeInStop = false; }
}

function startListening(handsFree) {
  if (!recognition) return;
  handsFreeMode = handsFree;
  recognitionFatalError = false;
  accumulatedText = '';
  if (recognizing) { setVoiceStatus('listening'); return; }
  try { setVoiceStatus('listening'); recognition.start(); }
  catch { handsFreeMode = false; setVoiceStatus('idle'); }
}

voiceMicBtn.addEventListener('click', () => { if (voiceStatus === 'idle') startListening(false); });
voiceLiveBtn.addEventListener('click', () => { if (voiceStatus === 'idle') startListening(true); });
voiceStopBtn.addEventListener('click', () => performVoiceStop(true));

function speakSentence(sentence) {
  const trimmed = sentence.trim();
  if (!trimmed) return;
  pendingSpeechCount++;
  const utterance = new SpeechSynthesisUtterance(trimmed);
  utterance.rate = window.state?.settings?.voice_speech_rate || 1.0;
  const finish = () => {
    pendingSpeechCount = Math.max(0, pendingSpeechCount - 1);
    if (pendingSpeechCount === 0 && voiceStatus === 'speaking') setVoiceStatus('idle');
  };
  utterance.onend = finish;
  utterance.onerror = finish;
  window.speechSynthesis.speak(utterance);
}

function flushSpeechBuffer(finalFlush) {
  const sentenceEnd = /[^.!?]*[.!?]+(\s|$)/g;
  let match;
  let consumed = 0;
  while ((match = sentenceEnd.exec(speechBuffer)) !== null) {
    speakSentence(match[0]);
    consumed = sentenceEnd.lastIndex;
  }
  speechBuffer = speechBuffer.slice(consumed);
  if (finalFlush && speechBuffer.trim()) { speakSentence(speechBuffer); speechBuffer = ''; }
}

window.voiceConversation = {
  onReplyChunk(text) {
    if (!window.state?.currentChat?.voice_mode) return;
    setVoiceStatus('speaking');
    speechBuffer += text;
    flushSpeechBuffer(false);
  },
  onReplyDone() {
    if (!window.state?.currentChat?.voice_mode) return;
    flushSpeechBuffer(true);
  },
  onReplyError() {
    if (!window.state?.currentChat?.voice_mode) return;
    speechBuffer = '';
    if (pendingSpeechCount === 0) setVoiceStatus('idle');
  },
};

updateVoiceButtonVisibility();
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_voice_conversation_ui.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add web/index.html web/assets/voice-conversation.js tests/test_qa_voice_conversation_ui.py
git commit -m "feat: port voice-chat-app's STT/TTS/barge-in state machine into WebConsole"
```

---

## Task 11: TTS hook in `conversation.js`

**Files:**
- Modify: `web/assets/conversation.js` (three single-line additions in `send()`'s `streamLoop`, around lines 1155, 1241, and the surrounding `catch`)
- Modify: `tests/test_qa_voice_conversation_ui.py` (add method)

**Interfaces:**
- Consumes: `window.voiceConversation.onReplyChunk/onReplyDone/onReplyError` (Task 10).

- [ ] **Step 1: Write the failing test**

```python
    def test_conversation_js_calls_voice_hooks(self):
        conv = (ASSETS / "conversation.js").read_text()
        self.assertIn('window.voiceConversation?.onReplyChunk', conv)
        self.assertIn('window.voiceConversation?.onReplyDone', conv)
        self.assertIn('window.voiceConversation?.onReplyError', conv)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_qa_voice_conversation_ui.py -k voice_hooks -v`
Expected: FAIL — hooks not called yet.

- [ ] **Step 3: Add the three hook calls**

In `web/assets/conversation.js`, in `send()`:

At line 1241, right after `fullText += event.content || '';`, add:

```javascript
            window.voiceConversation?.onReplyChunk(event.content || '');
```

At the `event.type === 'done'` branch (`streamCompleted = true;`, around line 1155), add just before `break streamLoop;`:

```javascript
            window.voiceConversation?.onReplyDone();
```

Find `send()`'s surrounding `catch` block (`grep -n "} catch (err) {" web/assets/conversation.js`, the one enclosing the whole `try` that contains `streamLoop`) and add one line inside it:

```javascript
      window.voiceConversation?.onReplyError();
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_voice_conversation_ui.py -v`
Expected: PASS (all tests in this file)

- [ ] **Step 5: Commit**

```bash
git add web/assets/conversation.js tests/test_qa_voice_conversation_ui.py
git commit -m "feat: hook TTS playback into the existing SSE reply stream"
```

---

## Task 12: Live-browser QA test, following `_BrowserFixture`

**Files:**
- Create: `tests/test_qa_voice_conversation_browser.py`

**Interfaces:**
- Consumes: `tests.test_frontend_browser._BrowserFixture` (existing base class — spawns a real uvicorn, drives a real Chromium via `sync_playwright`).

- [ ] **Step 1: Find a real subclass to copy the pattern from**

```bash
grep -rl "_BrowserFixture" tests/*.py | grep -v test_qa_browser_fixture.py | grep -v test_frontend_browser.py
```

Open the first result and read its `setUpClass`/`_login`/one real test method in full — that exact structure (not the stub-based `test_qa_browser_fixture.py`, which deliberately never boots a real browser) is what this task's test must match.

- [ ] **Step 2: Write the test, using that exact structure**

```python
"""Live-browser QA: the voice conversation composer icons actually appear
and the Mic/Live buttons are hidden for non-voice chats. Extends
_BrowserFixture the same way every other live-browser QA test in this
project does (see tests/test_frontend_browser.py) -- no separate pytest-
playwright fixture convention introduced.
"""
from __future__ import annotations

import tests.test_frontend_browser as fb


class VoiceConversationBrowserTests(fb._BrowserFixture):
    def test_voice_icons_hidden_for_regular_chat(self):
        self._login()
        self.page.click('.new-chat-btn')
        self.page.fill('#chatTitleInput', 'Regular Chat')
        self.page.click('#dialogSave')
        self.page.wait_for_selector('#composerArea')
        assert self.page.is_hidden('#voiceMicBtn')

    def test_voice_icons_visible_for_voice_chat(self):
        self._login()
        self.page.click('.voice-new-chat-btn')
        self.page.fill('#chatTitleInput', 'Voice Chat')
        self.page.click('#dialogSave')
        self.page.wait_for_selector('#composerArea')
        assert self.page.is_visible('#voiceMicBtn')
        assert self.page.is_visible('#voiceLiveBtn')
        assert self.page.is_hidden('#voiceStopBtn')
```

Reconcile every call above (`self._login()`'s real argument list, `self.page`'s exact attribute name, whether `setUpClass` needs anything this class doesn't inherit for free) against what Step 1's real example actually does, and fix any mismatch before moving on — this is copying a working pattern, not guessing one.

- [ ] **Step 3: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_qa_voice_conversation_browser.py -v`
Expected: PASS (2 tests). If a capability guard skips it (matching the "exactly 6 skips" convention from `CLAUDE.md` for the browser layer), confirm the skip reason names a missing capability, not a real failure.

- [ ] **Step 4: Commit**

```bash
git add tests/test_qa_voice_conversation_browser.py
git commit -m "test: add live-browser QA for voice conversation composer icons"
```

---

## Task 13: Full test suite + rules.md signature-stage pass

**Files:** none (verification-only task)

- [ ] **Step 1: Run the full suite**

```bash
.venv/bin/python -m pytest
```

Expected: all prior tasks' tests pass, plus the full existing suite (hundreds of `test_qa_*.py` files) — confirms nothing in this feature broke an existing test. Note the exact skip count reported and compare against the "exactly 6 skips" baseline `CLAUDE.md` documents for a trustworthy `.venv`-run; investigate any new skip or failure before proceeding, don't wave it through.

- [ ] **Step 2: Run `rules.md`'s §8 signature security-audit checks against the new surface**

Manually verify each new threat-model row from the spec:

```bash
# Tool-use escape: confirm no `claude`/subprocess spawn anywhere in the voice path
grep -n "subprocess\|claude_proxy\|runner.stream_turn\|runner.run_turn" routes/voice.py || echo "PASS: no CLI invocation in routes/voice.py"

# Credential handling: api_key must never be logged
grep -n "print(\|_log\..*api_key\|_log\..*base_url" routes/voice.py || echo "PASS: no credential logging"

# voice_mode must only be settable via chat_update's allowlisted path, never raw SQL
grep -n "UPDATE chats SET voice_mode" routes/*.py db.py || echo "PASS: no raw SQL touches voice_mode outside chat_update"
```

- [ ] **Step 3: Update `CHANGELOG.md` and bump `config.VERSION`**

Follow this project's own existing version-bump convention exactly — find its "Version consistency" check in `rules.md` (`grep -n "Version consistency" rules.md`) and run its exact commands after bumping, don't just bump and assume it matches.

- [ ] **Step 4: Final commit**

```bash
git add CHANGELOG.md config.py
git commit -m "chore: bump version for voice conversation feature"
```

---

## Self-Review Notes (for whoever executes this plan)

- **Spec coverage:** every numbered "Backend changes" and "Frontend changes" item in the spec has a task above. The spec's 5 "Open questions" are resolved concretely in this plan: (1) TTS hook is `conversation.js` lines ~1155/1241/its `catch` (Task 11); (2) no new `GET /models`-equivalent needed — `parse_active_models()` on the pinned ai_machine's existing `active_models` column is reused as-is (Task 7); (3) frontend logging: this plan does not port voice-chat-app's `debugLog()`/`/debug-log` file-logging pattern at all (multi-user, no per-user log file makes sense) — a deliberate simplification over the spec's open question, not an oversight; (4) QA test convention: follows this project's own existing `_BrowserFixture`/source-inspection split (Tasks 8-12), not voice-chat-app's pytest-playwright fixture, confirmed by reading real existing test files; (5) `voice_turn_timing` schema is concrete in Task 2 (append-only, 7-day rolling window in the query, not the storage — no pruning implemented in this pass, noted here for a future pass if the table's growth ever matters).
- **A real, load-bearing correction to the spec found during planning:** the spec's "Testing" section assumed WebConsole "has no existing browser-automation (QA) layer today" — false. It has an extensive one (`tests/test_qa_*.py`, dozens of files, plus `tests/test_frontend_browser.py`'s `_BrowserFixture`). Tasks 1-12 follow that real, existing convention (confirmed by reading `tests/test_qa_auto_answer_routes.py` and `tests/test_qa_chats.py` directly) instead of inventing a new one.
- **Type/interface consistency check:** `stream_voice_turn`'s SSE frame shapes (`type: text/done/error`) match `stream_handler`'s existing ones exactly, confirmed against `routes/chats.py`'s real code. `voice_model_timing_averages`'s return shape (`{"avg_ttft_ms": ..., "turn_count": ...}`) is used identically in Task 2's test, Task 7's settings wiring, and Task 8's frontend rendering. The test class name (`VoiceTurnTests`) and its helper methods (`_client`, `_login`, `_make_admin_and_chat`) are introduced once in Task 1 and reused by every later backend task's added methods — no drift in name or signature across tasks.
