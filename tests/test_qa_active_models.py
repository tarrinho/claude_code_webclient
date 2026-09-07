"""Per-machine active and default models.

A model id only means something against the backend serving it, so which models
are offered -- and which one a new chat starts on -- is a property of the
machine, not a global setting. Two states per machine: `active_models` (offered
in the picker) and `model` (the default).

Two rules that the tests pin because getting either wrong is silent:

* An empty active list means every served model is offered. The feature is
  opt-in, so an untouched machine can never present an empty picker.
* The selection is display-only. A turn naming a model outside it still runs --
  an old conversation whose model was later deactivated must keep working, and
  a gateway accepts ids it does not advertise.

Covers:
* parse_active_models — JSON decoding and every malformed shape.
* ai_machine_set_models — persistence, and leaving the default alone when unset.
* GET /api/models — active/default reported, ?machine_id= without activating.
* PUT /api/machines/{id}/models — validation, 404, the default-must-be-active rule.
* runner.get_default_model — the machine's default outranks the global setting.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import auth
import config
import db
import runner
from routes import machines as machine_routes

GATEWAY_BODY = json.dumps(
    {
        "data": [
            {"id": "vllm/Qwen3.6-35B-A3B-NVFP4"},
            {"id": "azure_ai/gpt-5-mini"},
            {"id": "azure_ai/gpt-5.6-luna"},
        ]
    }
).encode()


def _request(body=None, query=None):
    return SimpleNamespace(
        method="GET",
        url=SimpleNamespace(path="/api/models"),
        cookies={},
        headers={"accept": "*/*"},
        query_params=dict(query or {}),
        client=SimpleNamespace(host="127.0.0.1"),
        state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
        json=AsyncMock(return_value=body if body is not None else {}),
    )


async def _setup(tc):
    td = tempfile.TemporaryDirectory()
    tc.tmpdir = td
    tc._db_patch = patch.object(config, "DB_PATH", f"{td.name}/db")
    tc._root_patch = patch.object(config, "PROJECTS_ROOT", f"{td.name}/projects")
    tc._db_patch.start()
    tc._root_patch.start()
    await db.init()
    await auth.bootstrap_admin()
    machine_routes._models_cache.clear()


async def _teardown(tc):
    machine_routes._models_cache.clear()
    await db.close()
    tc._db_patch.stop()
    tc._root_patch.stop()
    tc.tmpdir.cleanup()


async def _gateway(machine_id="m1", active=True, model="claude-opus-5"):
    await db.ai_machine_create(
        machine_id, "Gateway", "gw.example.com", 443, "sk-test", model,
        "https://gw.example.com", None, "admin", provider="claude_code",
    )
    if active:
        await db.ai_machine_activate(machine_id, "admin")


class ParseActiveModelsTests(unittest.TestCase):
    """Anything unreadable means "offer everything", never "offer nothing"."""

    def test_json_array_decoded(self):
        self.assertEqual(db.parse_active_models('["a", "b"]'), ["a", "b"])

    def test_empty_column_is_empty_list(self):
        for raw in ("[]", "", None):
            self.assertEqual(db.parse_active_models(raw), [])

    def test_malformed_json_is_empty_not_an_error(self):
        self.assertEqual(db.parse_active_models("{not json"), [])

    def test_non_list_json_is_empty(self):
        self.assertEqual(db.parse_active_models('{"a": 1}'), [])
        self.assertEqual(db.parse_active_models('"a string"'), [])

    def test_non_string_entries_dropped(self):
        self.assertEqual(db.parse_active_models('[1, null, "keep", {}]'), ["keep"])

    def test_blank_entries_dropped(self):
        self.assertEqual(db.parse_active_models('["", "  ", "keep"]'), ["keep"])

    def test_duplicates_collapse_and_order_is_kept(self):
        self.assertEqual(db.parse_active_models('["b", "a", "b"]'), ["b", "a"])

    def test_already_a_list_passes_through(self):
        self.assertEqual(db.parse_active_models(["a", "b"]), ["a", "b"])


class SetModelsTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup(self)
        await _gateway()

    async def asyncTearDown(self):
        await _teardown(self)

    async def test_defaults_to_empty_meaning_all_served(self):
        machine = await db.ai_machine_get("m1", "admin")
        self.assertEqual(db.parse_active_models(machine["active_models"]), [])

    async def test_active_models_persist(self):
        await db.ai_machine_set_models("m1", "admin", ["a", "b"], None)
        machine = await db.ai_machine_get("m1", "admin")
        self.assertEqual(db.parse_active_models(machine["active_models"]), ["a", "b"])

    async def test_default_persists(self):
        await db.ai_machine_set_models("m1", "admin", ["a"], "a")
        self.assertEqual((await db.ai_machine_get("m1", "admin"))["model"], "a")

    async def test_default_left_alone_when_not_supplied(self):
        """Editing the offered set must not silently reset the default."""
        await db.ai_machine_set_models("m1", "admin", ["a", "b"], None)
        self.assertEqual(
            (await db.ai_machine_get("m1", "admin"))["model"], "claude-opus-5"
        )

    async def test_unknown_machine_returns_false(self):
        self.assertFalse(await db.ai_machine_set_models("nope", "admin", [], None))

    async def test_another_owner_cannot_write(self):
        self.assertFalse(await db.ai_machine_set_models("m1", "bob", ["a"], None))


class ModelsEndpointSelectionTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup(self)
        self._resolve = patch.object(machine_routes, "_resolve_host", return_value="93.184.216.34")
        self._resolve.start()
        self._probe = patch.object(machine_routes, "_probe_anthropic", lambda url, key, **_: (200, GATEWAY_BODY)
        )
        self._probe.start()

    async def asyncTearDown(self):
        self._probe.stop()
        self._resolve.stop()
        await _teardown(self)

    async def _get(self, query=None):
        response = await machine_routes.handle_models_list(_request(query=query))
        return json.loads(response.body)

    async def test_selection_reported_with_the_list(self):
        await _gateway()
        await db.ai_machine_set_models("m1", "admin", ["azure_ai/gpt-5-mini"], None)
        data = await self._get()
        self.assertEqual(data["machine_id"], "m1")
        self.assertEqual(data["active"], ["azure_ai/gpt-5-mini"])
        self.assertEqual(data["default"], "claude-opus-5")

    async def test_empty_active_is_reported_as_empty(self):
        """The UI reads empty as all-checked; the API must not invent a list."""
        await _gateway()
        self.assertEqual((await self._get())["active"], [])

    async def test_named_machine_inspected_without_activating_it(self):
        """Choosing a backend's models should not require making it live."""
        await _gateway("m1", active=True)
        await db.ai_machine_create(
            "m2", "Second", "other.example.com", 443, "sk-2", "claude-sonnet-5",
            "https://other.example.com", None, "admin", provider="claude_code",
        )
        data = await self._get({"machine_id": "m2"})
        self.assertEqual(data["machine_id"], "m2")
        self.assertEqual(data["default"], "claude-sonnet-5")
        # m1 is still the active machine.
        self.assertEqual((await db.ai_machine_active("admin"))["id"], "m1")

    async def test_unknown_machine_id_is_404(self):
        await _gateway()
        with self.assertRaises(HTTPException) as ctx:
            await machine_routes.handle_models_list(_request(query={"machine_id": "nope"}))
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_proxy_machine_still_reports_its_selection(self):
        """A proxy publishes no list, but its default is still meaningful."""
        await db.ai_machine_create(
            "p1", "Box", "10.0.0.9", 9000, None, "claude-sonnet-5",
            None, None, "admin", provider="proxy",
        )
        await db.ai_machine_activate("p1", "admin")
        data = await self._get()
        self.assertEqual(data["source"], "builtin")
        self.assertEqual(data["machine_id"], "p1")
        self.assertEqual(data["default"], "claude-sonnet-5")


class SetModelsEndpointTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup(self)
        await _gateway()

    async def asyncTearDown(self):
        await _teardown(self)

    async def _put(self, body):
        response = await machine_routes.handle_machine_models_set(_request(body), "m1")
        return json.loads(response.body)

    async def test_sets_active_and_default(self):
        data = await self._put({"active": ["a", "b"], "default": "a"})
        self.assertEqual(data["active"], ["a", "b"])
        self.assertEqual(data["default"], "a")
        machine = await db.ai_machine_get("m1", "admin")
        self.assertEqual(db.parse_active_models(machine["active_models"]), ["a", "b"])
        self.assertEqual(machine["model"], "a")

    async def test_clearing_active_restores_all_served(self):
        await self._put({"active": ["a"], "default": None})
        await self._put({"active": [], "default": None})
        machine = await db.ai_machine_get("m1", "admin")
        self.assertEqual(db.parse_active_models(machine["active_models"]), [])

    async def test_default_outside_the_active_set_rejected(self):
        """It would be applied to every new chat while being unpickable."""
        with self.assertRaises(HTTPException) as ctx:
            await self._put({"active": ["a"], "default": "b"})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("must be one of the active", ctx.exception.detail)

    async def test_default_allowed_when_active_is_empty(self):
        """Empty means everything is offered, so any served id is reachable."""
        data = await self._put({"active": [], "default": "anything/served"})
        self.assertEqual(data["default"], "anything/served")

    async def test_context_window_suffix_accepted(self):
        data = await self._put({"active": ["claude-opus-5[1m]"], "default": None})
        self.assertEqual(data["active"], ["claude-opus-5[1m]"])

    async def test_invalid_model_characters_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._put({"active": ["model; rm -rf /"], "default": None})
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_non_list_active_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._put({"active": "a,b"})
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_non_string_entries_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._put({"active": ["ok", 7]})
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_non_string_default_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await self._put({"active": [], "default": 7})
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_duplicates_collapse(self):
        data = await self._put({"active": ["a", "a", "b"], "default": None})
        self.assertEqual(data["active"], ["a", "b"])

    async def test_unknown_machine_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await machine_routes.handle_machine_models_set(_request({"active": []}), "nope")
        self.assertEqual(ctx.exception.status_code, 404)


class DefaultModelResolutionTests(unittest.IsolatedAsyncioTestCase):
    """The active machine's default outranks the global setting."""

    async def asyncSetUp(self):
        await _setup(self)
        await db.chat_create("c1", "Chat", None, "/tmp", "admin")

    async def asyncTearDown(self):
        await _teardown(self)

    async def test_machine_default_wins_over_the_global_setting(self):
        await db.setting_set("default_model", "claude-sonnet-5")
        await _gateway(model="vllm/Qwen3.6-35B-A3B-NVFP4")
        self.assertEqual(
            await runner.get_default_model("c1"), "vllm/Qwen3.6-35B-A3B-NVFP4"
        )

    async def test_global_setting_used_when_no_machine_is_active(self):
        await db.setting_set("default_model", "claude-sonnet-5")
        self.assertEqual(await runner.get_default_model("c1"), "claude-sonnet-5")

    async def test_config_used_when_nothing_is_set(self):
        self.assertEqual(await runner.get_default_model("c1"), config.MODEL_NAME)

    async def test_unknown_chat_falls_back_to_the_global_setting(self):
        await db.setting_set("default_model", "claude-sonnet-5")
        await _gateway(model="vllm/Qwen3.6-35B-A3B-NVFP4")
        self.assertEqual(await runner.get_default_model("no-such-chat"), "claude-sonnet-5")

    async def test_no_chat_id_falls_back_to_the_global_setting(self):
        await db.setting_set("default_model", "claude-sonnet-5")
        await _gateway(model="vllm/Qwen3.6-35B-A3B-NVFP4")
        self.assertEqual(await runner.get_default_model(), "claude-sonnet-5")

    async def test_proxy_machine_default_also_applies(self):
        """The machine default is about the backend, not the provider kind."""
        await db.setting_set("default_model", "claude-sonnet-5")
        await db.ai_machine_create(
            "p1", "Box", "10.0.0.9", 9000, None, "claude-haiku-4-5",
            None, None, "admin",
        )
        await db.ai_machine_activate("p1", "admin")
        self.assertEqual(await runner.get_default_model("c1"), "claude-haiku-4-5")


class DeactivatedModelStillRunsTests(unittest.IsolatedAsyncioTestCase):
    """The selection hides models; it never refuses them.

    An old conversation pinned to a model that was later deactivated must still
    resume, and a gateway accepts ids it does not advertise.
    """

    async def asyncSetUp(self):
        await _setup(self)
        await db.chat_create("c1", "Chat", None, "/tmp", "admin")
        await _gateway()
        await db.ai_machine_set_models("m1", "admin", ["azure_ai/gpt-5-mini"], None)

    async def asyncTearDown(self):
        await _teardown(self)

    async def test_a_turn_may_name_a_model_outside_the_active_set(self):
        written: list[bytes] = []

        class _Writer:
            def write(self, data):
                written.append(data)

            async def drain(self):
                return None

            def close(self):
                return None

            async def wait_closed(self):
                return None

        class _Reader:
            async def readuntil(self, _sep):
                return b'{"type":"ack"}\n'

        async def _lines(_reader):
            yield b'{"type":"done"}'

        async def _open(*_a, **_kw):
            return _Reader(), _Writer()

        with patch.object(runner.asyncio, "open_connection", _open), \
                patch.object(runner, "_read_lines", _lines):
            await runner._execute_proxy(
                "hi", None, "/tmp", "c1", "vllm/Qwen3.6-35B-A3B-NVFP4"
            )

        turn = next(
            json.loads(frame.decode())
            for frame in written
            if json.loads(frame.decode()).get("type") == "turn"
        )
        self.assertEqual(turn["model"], "vllm/Qwen3.6-35B-A3B-NVFP4")


if __name__ == "__main__":
    unittest.main()
