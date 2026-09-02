"""Layered QA coverage for model selection and backend routing.

Grouped by test level, following tests/test_qa_layers.py:

* UnitQA: base-URL normalisation, model-name validation, environment building.
* IntegrationQA: database -> runner.get_backend, including the provider gate.
* ComponentAPIQA: machine API contracts, especially credential handling.
* SystemE2EQA: a full turn, asserting the chosen model and env reach the runner.
* AcceptanceUATQA: user-visible outcomes for configuring and switching backends.

These exist because this path failed in a way every existing test missed: a
machine can be fully and correctly configured -- right host, right model, valid
key -- and still route every turn to the official Anthropic API, because
``provider`` was left at its default. The turn then fails with "model may not
exist or you may not have access to it", which points at the model rather than
at the routing.

No live model, Claude Code account, or network service is required.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import claude_proxy
import config
import db
import runner
import shared
from routes import chats as chat_routes
from routes import machines as machine_routes

# Model ids the real gateway reports via GET /v1/models.
SERVED_MODELS = (
    "vllm/Qwen3.6-35B-A3B-NVFP4",
    "azure_ai/gpt-5-mini",
    "azure_ai/gpt-5.4-mini",
    "azure_ai/gpt-5.4-mini-copilot",
    "azure_ai/gpt-5.6-sol",
    "azure_ai/gpt-5.6-luna",
)
GATEWAY = "https://llm.example-gateway.invalid"
BETAS = "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"


class TemporaryDBMixin:
    async def init_temp_db(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(
            config, "PROJECTS_ROOT", f"{self.tmp.name}/projects"
        )
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def close_temp_db(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def make_machine(self, **over):
        """Create a machine, returning its id. Defaults to a proxy machine."""
        fields = {
            "machine_id": over.pop("machine_id", "m-" + str(len(over))),
            "name": "Gateway",
            "host": "llm.example-gateway.invalid",
            "port": 443,
            "api_key": None,
            "model": SERVED_MODELS[0],
            "base_url": None,
            "description": None,
            "owner_id": "admin",
            "provider": "proxy",
        }
        fields.update(over)
        await db.ai_machine_create(**fields)
        return fields["machine_id"]


# ── Unit ───────────────────────────────────────────────────────────────────────


class UnitQA(unittest.TestCase):
    """Pure helpers: URL normalisation, model validation, env construction."""

    def test_base_url_keeps_origin_and_strips_v1(self):
        # A stored ".../v1" would make the CLI request /v1/v1/messages.
        for given in (GATEWAY, GATEWAY + "/", GATEWAY + "/v1", GATEWAY + "/v1/"):
            self.assertEqual(runner.normalise_base_url(given), GATEWAY, given)

    def test_base_url_adds_a_scheme_when_missing(self):
        self.assertEqual(
            runner.normalise_base_url("llm.example-gateway.invalid"),
            "https://llm.example-gateway.invalid",
        )

    def test_base_url_preserves_an_inner_path(self):
        # Not every gateway sits at the origin; only a trailing /v1 is ours.
        self.assertEqual(
            runner.normalise_base_url("https://host.invalid/llm"),
            "https://host.invalid/llm",
        )

    def test_base_url_empty_is_falsy(self):
        for empty in (None, "", "   "):
            self.assertFalse(runner.normalise_base_url(empty))

    def test_model_regex_accepts_every_served_id(self):
        for model in SERVED_MODELS:
            self.assertTrue(shared._MODEL_RE.fullmatch(model), model)

    def test_model_regex_accepts_the_1m_context_suffix(self):
        # Claude Code tells the user to append [1m] for a 1M window; rejecting
        # it would make its own documented advice unusable through the API.
        self.assertTrue(shared._MODEL_RE.fullmatch("vllm/Qwen3.6-35B-A3B-NVFP4[1m]"))

    def test_model_regex_still_rejects_shell_metacharacters(self):
        for bad in ("model; rm -rf /", "model$(id)", "model`id`", "model|tee",
                    "model with space", "model\nnewline"):
            self.assertIsNone(shared._MODEL_RE.fullmatch(bad), bad)

    def test_anthropic_env_carries_endpoint_and_key(self):
        env = runner._build_env(
            {"provider": "anthropic", "base_url": GATEWAY, "api_key": "k-123"}
        )
        self.assertEqual(env["ANTHROPIC_BASE_URL"], GATEWAY)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "k-123")

    def test_anthropic_env_omits_the_openai_shim_vars(self):
        # A leftover OPENAI_BASE_URL would pull the turn back to the local shim.
        env = runner._build_env(
            {"provider": "anthropic", "base_url": GATEWAY, "api_key": "k"}
        )
        self.assertNotIn("OPENAI_BASE_URL", env)
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_keyless_anthropic_env_allows_the_host_login(self):
        # CLAUDE_CODE_SIMPLE makes the CLI ignore OAuth and the keychain, so it
        # must come off when there is no key to use instead.
        env = runner._build_env({"provider": "anthropic", "base_url": GATEWAY})
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_SIMPLE", env)

    def test_betas_disabled_on_both_paths_for_every_backend(self):
        for backend in (
            None,
            {"provider": "proxy"},
            {"provider": "anthropic", "base_url": GATEWAY, "api_key": "k"},
            {"provider": "anthropic", "base_url": GATEWAY},
        ):
            self.assertEqual(runner._build_env(backend).get(BETAS), "1", repr(backend))
            self.assertEqual(
                claude_proxy._backend_env(backend).get(BETAS), "1", repr(backend)
            )

    def test_both_paths_yield_a_launchable_environment(self):
        # An env missing PATH stops `claude` starting at all.
        for env in (runner._build_env(None), claude_proxy._backend_env(None)):
            self.assertTrue(env.get("PATH"))
            self.assertTrue(env.get("HOME"))


# ── Integration ────────────────────────────────────────────────────────────────


class IntegrationQA(TemporaryDBMixin, unittest.IsolatedAsyncioTestCase):
    """Database rows resolving into the backend the runner will use."""

    async def asyncSetUp(self):
        await self.init_temp_db()
        await db.chat_create("c1", "Chat", None, f"{self.tmp.name}/projects", "admin")

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def test_proxy_provider_yields_no_backend_even_when_configured(self):
        # The production failure: correct host, correct model, valid key, and
        # base_url set -- but provider left at 'proxy', so no ANTHROPIC_BASE_URL
        # is exported and every turn silently goes to the official API.
        mid = await self.make_machine(
            machine_id="m-proxy", provider="proxy",
            base_url=GATEWAY, api_key="k-valid",
        )
        await db.ai_machine_activate(mid, "admin")
        self.assertEqual(await runner.get_backend("c1"), {})
        env = runner._build_env(await runner.get_backend("c1"))
        self.assertNotIn("ANTHROPIC_BASE_URL", env)

    async def test_anthropic_provider_yields_the_full_backend(self):
        mid = await self.make_machine(
            machine_id="m-anthropic", provider="anthropic",
            base_url=GATEWAY, api_key="k-valid",
        )
        await db.ai_machine_activate(mid, "admin")
        backend = await runner.get_backend("c1")
        self.assertEqual(backend["provider"], "anthropic")
        self.assertEqual(backend["base_url"], GATEWAY)
        self.assertEqual(backend["api_key"], "k-valid")

    async def test_stored_v1_suffix_is_normalised_on_read(self):
        mid = await self.make_machine(
            machine_id="m-v1", provider="anthropic",
            base_url=GATEWAY + "/v1", api_key="k",
        )
        await db.ai_machine_activate(mid, "admin")
        self.assertEqual((await runner.get_backend("c1"))["base_url"], GATEWAY)

    async def test_legacy_host_only_base_url_is_repaired_on_read(self):
        # Rows written before the validator stopped discarding the scheme.
        mid = await self.make_machine(
            machine_id="m-legacy", provider="anthropic",
            base_url="llm.example-gateway.invalid", api_key="k",
        )
        await db.ai_machine_activate(mid, "admin")
        self.assertEqual((await runner.get_backend("c1"))["base_url"], GATEWAY)

    async def test_blank_key_is_omitted_rather_than_sent_empty(self):
        # An empty string would authenticate as "" instead of falling back to
        # the host's own login.
        for blank in (None, "", "   "):
            mid = await self.make_machine(
                machine_id=f"m-blank-{len(str(blank))}", provider="anthropic",
                base_url=GATEWAY, api_key=blank,
            )
            await db.ai_machine_activate(mid, "admin")
            self.assertNotIn("api_key", await runner.get_backend("c1"), repr(blank))

    async def test_no_active_machine_yields_no_backend(self):
        await self.make_machine(machine_id="m-idle", provider="anthropic",
                                base_url=GATEWAY, api_key="k")
        self.assertEqual(await runner.get_backend("c1"), {})

    async def test_unknown_chat_yields_no_backend(self):
        mid = await self.make_machine(machine_id="m-x", provider="anthropic",
                                      base_url=GATEWAY, api_key="k")
        await db.ai_machine_activate(mid, "admin")
        self.assertEqual(await runner.get_backend("does-not-exist"), {})

    async def test_activation_is_exclusive(self):
        first = await self.make_machine(machine_id="m-1", provider="anthropic")
        second = await self.make_machine(machine_id="m-2", provider="anthropic")
        await db.ai_machine_activate(first, "admin")
        await db.ai_machine_activate(second, "admin")
        actives = [m["id"] for m in await db.ai_machines_list("admin") if m["active"]]
        self.assertEqual(actives, [second])

    async def test_backend_is_not_shared_across_owners(self):
        mid = await self.make_machine(machine_id="m-bob", provider="anthropic",
                                      base_url=GATEWAY, api_key="k", owner_id="bob")
        await db.ai_machine_activate(mid, "bob")
        # c1 belongs to admin, who has no active machine.
        self.assertEqual(await runner.get_backend("c1"), {})


# ── Component (API contracts) ──────────────────────────────────────────────────


class ComponentAPIQA(TemporaryDBMixin, unittest.IsolatedAsyncioTestCase):
    """Machine API handlers, with credential handling as the focus."""

    async def asyncSetUp(self):
        await self.init_temp_db()

    async def asyncTearDown(self):
        await self.close_temp_db()

    def _req(self, body=None):
        from types import SimpleNamespace
        return SimpleNamespace(
            method="POST",
            url=SimpleNamespace(path="/api/machines"),
            cookies={},
            headers={"accept": "*/*"},
            query_params={},
            client=SimpleNamespace(host="127.0.0.1"),
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value=body or {}),
        )

    async def test_list_never_returns_the_api_key(self):
        mid = await self.make_machine(machine_id="m-k", provider="anthropic",
                                      base_url=GATEWAY, api_key="super-secret")
        await db.ai_machine_activate(mid, "admin")
        import json as _json
        body = _json.loads((await machine_routes.handle_machines_list(self._req())).body)
        blob = _json.dumps(body)
        self.assertNotIn("super-secret", blob)
        for machine in body["machines"]:
            self.assertNotIn("api_key", machine)

    async def test_get_reports_key_presence_without_the_value(self):
        import json as _json
        mid = await self.make_machine(machine_id="m-k2", provider="anthropic",
                                      base_url=GATEWAY, api_key="super-secret")
        body = _json.loads((await machine_routes.handle_machine_get(self._req(), mid)).body)
        machine = body["machine"]
        self.assertNotIn("api_key", machine)
        self.assertTrue(machine["has_api_key"])
        self.assertNotIn("super-secret", _json.dumps(body))

    async def test_get_reports_absent_key_as_false(self):
        import json as _json
        mid = await self.make_machine(machine_id="m-nokey", provider="anthropic")
        body = _json.loads((await machine_routes.handle_machine_get(self._req(), mid)).body)
        self.assertFalse(body["machine"]["has_api_key"])

    async def test_base_url_survives_a_round_trip_with_scheme_and_path(self):
        # The validator used to return only the host, so the scheme and path
        # were silently discarded on save. _resolve_host is patched to keep the
        # test offline -- host validation now performs a real DNS lookup.
        #
        # Patched on net_validation, not on app. The 0.10.0 split moved this
        # cluster into its own module, and `_validate_host` calls its
        # module-local `_resolve_host` -- so rebinding the name app re-exports
        # no longer intercepts anything, and the test went back to real DNS.
        # Patch where a function lives, not where it is re-exported.
        import json as _json

        import net_validation
        url = "https://api.example.invalid/v1"
        with patch.object(net_validation, "_resolve_host",
                          return_value="93.184.216.34"):
            resp = await machine_routes.handle_machine_create(self._req({
                "name": "M", "host": "api.example.invalid", "port": 443,
                "model": SERVED_MODELS[0], "base_url": url,
            }))
        mid = _json.loads(resp.body)["id"]
        stored = await db.ai_machine_get(mid, "admin")
        self.assertEqual(stored["base_url"], url)

    async def test_create_blocks_a_host_that_resolves_internally(self):
        # _validate_host used to pattern-match only, so internal hosts were
        # persisted under a comment claiming SSRF protection.
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await machine_routes.handle_machine_create(self._req({
                "name": "M", "host": "169.254.169.254",
                "model": SERVED_MODELS[0],
            }))
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_create_rejects_a_non_http_base_url(self):
        from fastapi import HTTPException
        for bad in ("ftp://x.invalid", "file:///etc/passwd", "notaurl"):
            with self.assertRaises(HTTPException, msg=bad) as ctx:
                await machine_routes.handle_machine_create(self._req({
                    "name": "M", "host": "api.example.invalid",
                    "model": SERVED_MODELS[0], "base_url": bad,
                }))
            self.assertEqual(ctx.exception.status_code, 400, bad)

    async def test_create_rejects_an_invalid_model_name(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await machine_routes.handle_machine_create(self._req({
                "name": "M", "host": "api.example.invalid",
                "model": "bad model; rm -rf /",
            }))
        self.assertEqual(ctx.exception.status_code, 400)


# ── System (end to end through the runner seam) ────────────────────────────────


class SystemE2EQA(TemporaryDBMixin, unittest.IsolatedAsyncioTestCase):
    """A whole turn, asserting the model and endpoint reach the runner."""

    async def asyncSetUp(self):
        await self.init_temp_db()
        await db.chat_create("c1", "Chat", None, f"{self.tmp.name}/projects", "admin")
        mid = await self.make_machine(
            machine_id="m-e2e", provider="anthropic",
            base_url=GATEWAY + "/v1", api_key="k-e2e",
        )
        await db.ai_machine_activate(mid, "admin")

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def test_selected_model_reaches_the_runner(self):
        seen = {}

        async def fake_run_turn(prompt, session_id, work_dir, chat_id, model=None):
            seen["prompt"] = prompt
            seen["model"] = model
            return ["done"], None

        req = self._make_request({"content": "hi", "model": SERVED_MODELS[2]})
        with patch.object(runner, "run_turn", fake_run_turn):
            await chat_routes.handle_submit_message(req, "c1")
        self.assertEqual(seen["model"], SERVED_MODELS[2])

    async def test_turn_environment_points_at_the_configured_gateway(self):
        backend = await runner.get_backend("c1")
        env = runner._build_env(backend)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], GATEWAY)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "k-e2e")
        self.assertEqual(env[BETAS], "1")
        self.assertNotIn("OPENAI_BASE_URL", env)

    async def test_proxy_payload_carries_the_backend(self):
        # The proxy spawns the CLI, so the backend has to travel with the turn.
        backend = await runner.get_backend("c1")
        self.assertTrue(backend)
        env = claude_proxy._backend_env(backend)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], GATEWAY)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "k-e2e")

    def _make_request(self, body):
        from types import SimpleNamespace
        return SimpleNamespace(
            method="POST",
            url=SimpleNamespace(path="/api/chats/c1/messages"),
            cookies={},
            headers={"accept": "*/*"},
            query_params={},
            client=SimpleNamespace(host="127.0.0.1"),
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value=body),
        )


# ── Acceptance / UAT ───────────────────────────────────────────────────────────


class AcceptanceUATQA(TemporaryDBMixin, unittest.IsolatedAsyncioTestCase):
    """User-visible outcomes, phrased as the workflows people actually perform."""

    async def asyncSetUp(self):
        await self.init_temp_db()
        await db.chat_create("c1", "Chat", None, f"{self.tmp.name}/projects", "admin")

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def test_pointing_a_machine_at_my_own_gateway_routes_turns_there(self):
        mid = await self.make_machine(
            machine_id="m-mine", provider="anthropic",
            base_url=GATEWAY, api_key="my-key", model=SERVED_MODELS[0],
        )
        await db.ai_machine_activate(mid, "admin")
        env = runner._build_env(await runner.get_backend("c1"))
        self.assertEqual(env["ANTHROPIC_BASE_URL"], GATEWAY)

    async def test_switching_the_active_machine_switches_the_endpoint(self):
        mine = await self.make_machine(
            machine_id="m-mine", provider="anthropic",
            base_url=GATEWAY, api_key="my-key",
        )
        official = await self.make_machine(
            machine_id="m-official", provider="anthropic",
            base_url="https://api.anthropic.com", api_key=None,
        )
        await db.ai_machine_activate(mine, "admin")
        first = runner._build_env(await runner.get_backend("c1"))
        await db.ai_machine_activate(official, "admin")
        second = runner._build_env(await runner.get_backend("c1"))
        self.assertEqual(first["ANTHROPIC_BASE_URL"], GATEWAY)
        self.assertEqual(second["ANTHROPIC_BASE_URL"], "https://api.anthropic.com")
        # The official machine has no key, so the host login must stay reachable.
        self.assertNotIn("ANTHROPIC_API_KEY", second)

    async def test_leaving_provider_as_proxy_is_a_visible_no_op(self):
        # Documents the trap: a user who fills in Base URL and API Key but does
        # not change Provider gets none of it applied.
        mid = await self.make_machine(
            machine_id="m-trap", provider="proxy",
            base_url=GATEWAY, api_key="my-key",
        )
        await db.ai_machine_activate(mid, "admin")
        env = runner._build_env(await runner.get_backend("c1"))
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    async def test_my_key_is_never_echoed_back_to_the_browser(self):
        from types import SimpleNamespace
        mid = await self.make_machine(
            machine_id="m-secret", provider="anthropic",
            base_url=GATEWAY, api_key="do-not-leak-me",
        )
        await db.ai_machine_activate(mid, "admin")
        req = SimpleNamespace(
            method="GET", url=SimpleNamespace(path="/api/machines"), cookies={},
            headers={"accept": "*/*"}, query_params={},
            client=SimpleNamespace(host="127.0.0.1"),
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value={}),
        )
        for resp in (await machine_routes.handle_machines_list(req),
                     await machine_routes.handle_machine_get(req, mid)):
            self.assertNotIn("do-not-leak-me", resp.body.decode())


if __name__ == "__main__":
    unittest.main()
