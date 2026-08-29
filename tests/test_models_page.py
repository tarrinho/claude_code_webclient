"""Coverage for GET /api/models -- the Settings → Models list.

The page used to render a hardcoded four-entry datalist that nothing ever
checked against the backend, so a model the service does not serve looked
identical to one it does, and the conversation picker additionally offered
every model id harvested out of old transcripts. The endpoint asks the active
machine what it actually serves, and says so when it cannot.

Covers:
* Parsing — Anthropic and OpenAI-compatible /v1/models shapes, junk entries.
* Live path — ids returned, key sent, response cached.
* Fallback path — no machine, proxy machine, auth failure, bad status,
  unparseable body, empty list; each with a stated reason.
* SSRF — the blocklist still applies to a user-configured endpoint.
* _MODEL_RE — the documented [1m] context-window suffix is accepted.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import app
import auth
import config
import db

ANTHROPIC_BODY = json.dumps(
    {
        "data": [
            {"id": "claude-opus-5", "display_name": "Claude Opus 5"},
            {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"},
        ],
        "has_more": False,
    }
).encode()

# A LiteLLM gateway answers the same route with OpenAI-shaped entries: an id
# and no display_name.
GATEWAY_BODY = json.dumps(
    {
        "data": [
            {"id": "vllm/Qwen3.6-35B-A3B-NVFP4", "object": "model"},
            {"id": "azure_ai/gpt-5-mini", "object": "model"},
        ]
    }
).encode()


def _make_request():
    return SimpleNamespace(
        method="GET",
        url=SimpleNamespace(path="/api/models"),
        cookies={},
        headers={"accept": "*/*"},
        query_params={},
        client=SimpleNamespace(host="127.0.0.1"),
        state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
        json=AsyncMock(return_value={}),
    )


async def _setup_db(tc):
    td = tempfile.TemporaryDirectory()
    tc.tmpdir = td
    tc._db_patch = patch.object(config, "DB_PATH", f"{td.name}/db")
    tc._root_patch = patch.object(config, "PROJECTS_ROOT", f"{td.name}/projects")
    tc._db_patch.start()
    tc._root_patch.start()
    await db.init()
    await auth.bootstrap_admin()
    app._models_cache.clear()


async def _teardown_db(tc):
    app._models_cache.clear()
    await db.close()
    tc._db_patch.stop()
    tc._root_patch.stop()
    tc.tmpdir.cleanup()


async def _add_anthropic_machine(base_url="https://gateway.example.com", key="sk-test"):
    await db.ai_machine_create(
        "m1", "Gateway", "gateway.example.com", 443, key,
        "claude-opus-5", base_url, None, "admin", provider="anthropic",
    )
    await db.ai_machine_activate("m1", "admin")


class ParseModelListTests(unittest.TestCase):
    """One parser covers both response shapes the endpoint can return."""

    def test_anthropic_shape(self):
        models = app._parse_model_list(ANTHROPIC_BODY)
        self.assertEqual([m["id"] for m in models], ["claude-opus-5", "claude-sonnet-5"])
        self.assertEqual(models[0]["display_name"], "Claude Opus 5")

    def test_gateway_shape_falls_back_to_id_for_display(self):
        models = app._parse_model_list(GATEWAY_BODY)
        self.assertEqual(models[0]["id"], "azure_ai/gpt-5-mini")
        self.assertEqual(models[0]["display_name"], "azure_ai/gpt-5-mini")

    def test_results_are_sorted(self):
        models = app._parse_model_list(GATEWAY_BODY)
        self.assertEqual([m["id"] for m in models], sorted(m["id"] for m in models))

    def test_entries_without_an_id_are_skipped(self):
        body = json.dumps(
            {"data": [{"object": "model"}, {"id": ""}, {"id": "  "}, {"id": "real"}]}
        ).encode()
        self.assertEqual([m["id"] for m in app._parse_model_list(body)], ["real"])

    def test_duplicate_ids_collapse(self):
        body = json.dumps({"data": [{"id": "a"}, {"id": "a"}]}).encode()
        self.assertEqual(len(app._parse_model_list(body)), 1)

    def test_non_dict_entries_ignored(self):
        body = json.dumps({"data": ["a string", 7, None, {"id": "real"}]}).encode()
        self.assertEqual([m["id"] for m in app._parse_model_list(body)], ["real"])

    def test_missing_data_key_raises(self):
        with self.assertRaises(TypeError):
            app._parse_model_list(b'{"models": []}')

    def test_invalid_json_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            app._parse_model_list(b"not json")


class ModelsEndpointTests(unittest.IsolatedAsyncioTestCase):
    """The live path reports what the machine serves."""

    async def asyncSetUp(self):
        await _setup_db(self)
        self._resolve = patch.object(app, "_resolve_host", return_value="93.184.216.34")
        self._resolve.start()

    async def asyncTearDown(self):
        self._resolve.stop()
        await _teardown_db(self)

    async def _call(self, probe):
        with patch.object(app, "_probe_anthropic", probe):
            response = await app.handle_models_list(_make_request())
        return json.loads(response.body)

    async def test_returns_models_from_the_endpoint(self):
        await _add_anthropic_machine()
        data = await self._call(lambda url, key: (200, GATEWAY_BODY))
        self.assertEqual(data["source"], "endpoint")
        self.assertIsNone(data["reason"])
        self.assertEqual(
            [m["id"] for m in data["models"]],
            ["azure_ai/gpt-5-mini", "vllm/Qwen3.6-35B-A3B-NVFP4"],
        )

    async def test_queries_the_models_route_with_the_key(self):
        await _add_anthropic_machine()
        seen = {}

        def _probe(url, key):
            seen["url"] = url
            seen["key"] = key
            return 200, ANTHROPIC_BODY

        await self._call(_probe)
        self.assertTrue(seen["url"].startswith("https://gateway.example.com/v1/models"))
        self.assertEqual(seen["key"], "sk-test")

    async def test_endpoint_reported_back(self):
        await _add_anthropic_machine()
        data = await self._call(lambda url, key: (200, ANTHROPIC_BODY))
        self.assertEqual(data["endpoint"], "https://gateway.example.com")

    async def test_result_is_cached(self):
        """Opening Settings repeatedly must not re-query the endpoint."""
        await _add_anthropic_machine()
        calls = []

        def _probe(url, key):
            calls.append(url)
            return 200, ANTHROPIC_BODY

        await self._call(_probe)
        await self._call(_probe)
        self.assertEqual(len(calls), 1)

    async def test_stored_v1_suffix_does_not_double_up(self):
        await _add_anthropic_machine(base_url="https://gateway.example.com/v1")
        seen = {}

        def _probe(url, key):
            seen["url"] = url
            return 200, ANTHROPIC_BODY

        await self._call(_probe)
        self.assertTrue(seen["url"].startswith("https://gateway.example.com/v1/models"))
        self.assertNotIn("/v1/v1/", seen["url"])

    async def test_key_never_appears_in_the_response(self):
        await _add_anthropic_machine()
        with patch.object(app, "_probe_anthropic", lambda url, key: (200, ANTHROPIC_BODY)):
            response = await app.handle_models_list(_make_request())
        self.assertNotIn(b"sk-test", response.body)

    async def test_private_endpoint_blocked(self):
        """The request goes out from the server, so the SSRF guard still applies."""
        await _add_anthropic_machine()
        self._resolve.stop()
        try:
            with patch.object(
                app,
                "_resolve_host",
                side_effect=HTTPException(status_code=403, detail="Internal hosts"),
            ), self.assertRaises(HTTPException) as ctx:
                await app.handle_models_list(_make_request())
            self.assertEqual(ctx.exception.status_code, 403)
        finally:
            self._resolve.start()


class ModelsFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Every failure says why, instead of silently showing a frozen list."""

    async def asyncSetUp(self):
        await _setup_db(self)
        self._resolve = patch.object(app, "_resolve_host", return_value="93.184.216.34")
        self._resolve.start()

    async def asyncTearDown(self):
        self._resolve.stop()
        await _teardown_db(self)

    async def _call(self, probe=None):
        if probe is None:
            response = await app.handle_models_list(_make_request())
        else:
            with patch.object(app, "_probe_anthropic", probe):
                response = await app.handle_models_list(_make_request())
        return json.loads(response.body)

    async def test_no_active_machine(self):
        data = await self._call()
        self.assertEqual(data["source"], "builtin")
        self.assertIn("No machine is active", data["reason"])
        self.assertEqual(
            [m["id"] for m in data["models"]], list(config.KNOWN_MODELS)
        )

    async def test_proxy_machine_has_no_model_list(self):
        await db.ai_machine_create(
            "m1", "Box", "10.0.0.9", 9000, None, "claude-sonnet-5", None, None, "admin"
        )
        await db.ai_machine_activate("m1", "admin")
        data = await self._call()
        self.assertEqual(data["source"], "builtin")
        self.assertIn("proxy", data["reason"])

    async def test_rejected_key(self):
        await _add_anthropic_machine()
        data = await self._call(lambda url, key: (401, b""))
        self.assertEqual(data["source"], "builtin")
        self.assertIn("rejected the API key", data["reason"])

    async def test_missing_key(self):
        await _add_anthropic_machine(key=None)
        data = await self._call(lambda url, key: (403, b""))
        self.assertIn("requires an API key", data["reason"])

    async def test_unexpected_status_named(self):
        await _add_anthropic_machine()
        data = await self._call(lambda url, key: (503, b""))
        self.assertIn("503", data["reason"])

    async def test_unreachable(self):
        await _add_anthropic_machine()

        def _boom(url, key):
            raise OSError("no route to host")

        data = await self._call(_boom)
        self.assertIn("Could not reach", data["reason"])

    async def test_unparseable_body(self):
        await _add_anthropic_machine()
        data = await self._call(lambda url, key: (200, b"<html>nope</html>"))
        self.assertIn("unreadable", data["reason"])

    async def test_empty_list_is_not_presented_as_the_truth(self):
        await _add_anthropic_machine()
        data = await self._call(lambda url, key: (200, b'{"data": []}'))
        self.assertEqual(data["source"], "builtin")
        self.assertIn("listed no models", data["reason"])

    async def test_failure_is_not_cached(self):
        """A transient error must not freeze the fallback for a minute."""
        await _add_anthropic_machine()
        await self._call(lambda url, key: (503, b""))
        data = await self._call(lambda url, key: (200, ANTHROPIC_BODY))
        self.assertEqual(data["source"], "endpoint")


class ModelNameSuffixTests(unittest.TestCase):
    """The CLI tells users to append [1m] for the 1M context window."""

    def test_context_window_suffix_accepted(self):
        self.assertTrue(app._MODEL_RE.fullmatch("claude-opus-5[1m]"))

    def test_gateway_id_with_suffix_accepted(self):
        self.assertTrue(app._MODEL_RE.fullmatch("vllm/Qwen3.6-35B-A3B-NVFP4[1m]"))

    def test_plain_ids_still_accepted(self):
        for model in ("claude-opus-5", "vllm/Qwen3.6-35B-A3B-NVFP4", "azure_ai/gpt-5-mini"):
            self.assertTrue(app._MODEL_RE.fullmatch(model), model)

    def test_shell_metacharacters_still_rejected(self):
        for bad in ("model; rm -rf /", "model$(id)", "model`id`", "model|tee", "a b"):
            self.assertIsNone(app._MODEL_RE.fullmatch(bad), bad)


if __name__ == "__main__":
    unittest.main()
