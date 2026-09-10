"""QA: the voice model dropdown must offer the selected backend's models.

The Settings dialog's App tab has a voice backend picker and a voice model
picker. A model id is only meaningful against the backend serving it —
`azure_ai/...` and `vllm/...` exist on the gateway only, `claude-opus-5` on
api.anthropic.com only — so offering one backend's models while another is
selected is not a cosmetic mismatch, it is an invitation to configure a voice
chat that cannot run.

It was doing exactly that. `/api/settings` computed `voice_model_options` from
the *stored* backend id, and the dialog's onchange handler "refreshed" the list
by re-GETting the same endpoint, which necessarily returned the same stored
backend's models however the dropdown had just been changed. The list only ever
caught up after a Save and a reopen. (It never even got that far in practice:
the handler called a function that was private to voice-settings.js and never
imported, so it raised ReferenceError first.)

The fix ships each backend's own `models` inside `voice_backend_options`, so the
dialog repopulates from data it already holds and never asks again. These tests
pin the server half of that contract:

* every backend carries its own list, with timing attached;
* the lists are actually per-backend and not one list repeated;
* `voice_model_options` still matches the selected backend, since it is what
  the dialog renders before anyone touches the dropdown.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from routes import misc as misc_routes


class _Request:
    def __init__(self, user: str = "admin"):
        self.state = type("S", (), {"session": {"user": user}})()


# Two backends whose model lists must not be confused for one another: a
# gateway serving azure_ai/vllm ids, and Anthropic serving claude ids.
_GATEWAY = "m-gateway"
_ANTHROPIC = "m-anthropic"
_ROWS = [
    {"id": _GATEWAY, "name": "CF AI Machine", "provider": "claude_code",
     "active_models": '["azure_ai/gpt-5.4-mini", "vllm/Qwen3.6-35B-A3B-NVFP4"]'},
    {"id": _ANTHROPIC, "name": "Anthropic Oauth", "provider": "claude_code",
     "active_models": '["claude-opus-5", "claude-sonnet-5"]'},
    {"id": "m-bare", "name": "Anthropic API", "provider": "claude_code",
     "active_models": "[]"},
]


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None


async def _settings(selected: str | None):
    """Call handle_settings_get with the machine table stubbed.

    Each call represents a *different* stored settings state, so it drops the
    handler's response cache first. Without this, a test that calls the helper
    twice gets its first answer back both times: the cache is keyed on owner
    and lives for 30s, and switching backend here means mutating a stub dict
    rather than issuing the PATCH that would invalidate it in production.
    conftest's autouse reset is per test, which is one clear too few for the
    subtest loop below.
    """
    misc_routes._settings_invalidate()
    settings = {
        "voice_backend_id": selected,
        "voice_model": "azure_ai/gpt-5.4-mini",
        "voice_speech_rate": "1.0",
    }

    async def setting_get(key):
        return settings.get(key)

    async def setting_get_all(keys):
        """handle_settings_get batch-reads its settings in one query rather
        than calling setting_get thirteen times, so stubbing setting_get alone
        leaves every value unread.

        That is how this test broke without its subject changing: with the
        settings dict ignored, voice_backend_id fell through to
        config.VOICE_BACKEND_ID_DEFAULT, which is not one of the stub rows, so
        the handler's own validity check nulled it and voice_model_options came
        back empty. Both stubs are kept -- setting_get is still the API
        elsewhere in the handler, and a stub that quietly stops being consulted
        is exactly the failure being fixed here.

        Absent keys are omitted rather than returned as None, matching
        db.setting_get_all: a key with no stored row is missing from its dict,
        which is what lets `_get(key) or default` in the handler work.
        """
        return {
            key: value for key, value in settings.items()
            if key in keys and value is not None
        }

    async def execute(sql, *_args):
        if "FROM ai_machines" in sql:
            return _Cursor(_ROWS)
        return _Cursor([])

    async def machine_get(machine_id, _owner):
        return next((r for r in _ROWS if r["id"] == machine_id), None)

    with patch.object(misc_routes.db, "setting_get", AsyncMock(side_effect=setting_get)), \
            patch.object(misc_routes.db, "setting_get_all",
                         AsyncMock(side_effect=setting_get_all)), \
            patch.object(misc_routes.db, "ai_machine_get",
                         AsyncMock(side_effect=machine_get)), \
            patch.object(misc_routes.db, "db_conn",
                         type("C", (), {"execute": staticmethod(execute)})), \
            patch("routes.voice.voice_model_timing_averages",
                  AsyncMock(side_effect=lambda ids: {
                      i: {"avg_ttft_ms": None, "turn_count": 0} for i in ids})):
        response = await misc_routes.handle_settings_get(_Request())
    return json.loads(bytes(response.body))


class VoiceModelsTravelWithTheirBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_backend_carries_its_own_model_list(self):
        body = await _settings(_GATEWAY)
        by_id = {b["id"]: b for b in body["voice_backend_options"]}
        self.assertIn("models", by_id[_GATEWAY])
        self.assertIn("models", by_id[_ANTHROPIC])

    async def test_the_lists_are_per_backend_and_not_one_list_repeated(self):
        """The actual defect: one backend's models shown for another."""
        body = await _settings(_GATEWAY)
        by_id = {b["id"]: b for b in body["voice_backend_options"]}
        gateway = [m["id"] for m in by_id[_GATEWAY]["models"]]
        anthropic = [m["id"] for m in by_id[_ANTHROPIC]["models"]]
        self.assertEqual(
            gateway, ["azure_ai/gpt-5.4-mini", "vllm/Qwen3.6-35B-A3B-NVFP4"])
        self.assertEqual(anthropic, ["claude-opus-5", "claude-sonnet-5"])
        self.assertEqual(
            set(gateway) & set(anthropic), set(),
            "these two backends share no models; overlap means the lists were "
            "not really computed per backend",
        )

    async def test_a_backend_with_no_declared_models_offers_none(self):
        """Not an error, and not somebody else's list either."""
        body = await _settings(_GATEWAY)
        by_id = {b["id"]: b for b in body["voice_backend_options"]}
        self.assertEqual(by_id["m-bare"]["models"], [])

    async def test_voice_model_options_matches_the_selected_backend(self):
        for selected, expected in (
            (_GATEWAY, ["azure_ai/gpt-5.4-mini", "vllm/Qwen3.6-35B-A3B-NVFP4"]),
            (_ANTHROPIC, ["claude-opus-5", "claude-sonnet-5"]),
        ):
            with self.subTest(selected=selected):
                body = await _settings(selected)
                self.assertEqual(
                    [m["id"] for m in body["voice_model_options"]], expected)

    async def test_timing_fields_are_attached_to_every_option(self):
        """The dialog renders "~1.2s avg, 9 turns" or "not yet used" from
        these; a missing key renders as undefined rather than either."""
        body = await _settings(_GATEWAY)
        for option in body["voice_model_options"]:
            self.assertIn("avg_ttft_ms", option)
            self.assertIn("turn_count", option)


if __name__ == "__main__":
    unittest.main()
