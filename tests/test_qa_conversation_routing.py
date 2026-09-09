"""Layered QA coverage for per-conversation backend and model selection.

Before this, the backend was global: runner.get_backend resolved only the
owner's *active* machine, so switching backend in Settings moved every
conversation at once and two conversations could never sit on different
backends. ``chats.ai_machine_id`` had existed in the schema, been selected in
_CHAT_COLUMNS, and been copied by chat_fork the whole time -- but nothing wrote
it and nothing read it for routing.

* UnitQA — the routing resolver in isolation.
* IntegrationQA — pins, fallbacks, and independence between conversations.
* ComponentAPIQA — the PATCH contract and its ownership check.
* AcceptanceUATQA — the workflows Pedro asked for, in his words: move a
  conversation from Anthropic to an AI machine, and from model A to model B.

No live model, Claude Code account, or network service is required.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import db
import runner
from routes import chats as chat_routes

OFFICIAL = "https://api.anthropic.com"
GATEWAY = "https://llm.example-gateway.invalid"


class RoutingMixin:
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

    async def make_machine(self, mid, *, base_url, model, owner="admin",
                           provider="claude_code", api_key=None):
        await db.ai_machine_create(mid, mid, "host.invalid", 443, api_key, model,
                                   base_url, None, owner, provider)
        return mid

    async def make_chat(self, cid, owner="admin"):
        await db.chat_create(cid, cid, None, f"{self.tmp.name}/projects", owner)
        return cid

    def _req(self, body):
        return SimpleNamespace(
            method="PATCH",
            url=SimpleNamespace(path="/api/chats/x"),
            cookies={},
            headers={"accept": "*/*"},
            query_params={},
            client=SimpleNamespace(host="127.0.0.1"),
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value=body),
        )

    async def patch_chat(self, chat_id, body):
        return await chat_routes.handle_chat_patch(self._req(body), chat_id)


# ── Unit ───────────────────────────────────────────────────────────────────────


class UnitQA(RoutingMixin, unittest.IsolatedAsyncioTestCase):
    """db.chat_routing resolves owner, model and machine in one read."""

    async def asyncSetUp(self):
        await self.init_temp_db()
        await self.make_machine("act", base_url=OFFICIAL, model="claude-opus-5")
        await self.make_machine("gw", base_url=GATEWAY, model="vllm/Q", api_key="k")
        await db.ai_machine_activate("act", "admin")
        await self.make_chat("c1")

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def test_unpinned_follows_the_active_machine(self):
        routing = await db.chat_routing("c1")
        self.assertEqual(routing["owner"], "admin")
        self.assertEqual(routing["machine"]["id"], "act")
        self.assertFalse(routing["pinned"])

    async def test_pinned_uses_its_own_machine(self):
        await db.chat_set_machine("c1", "admin", "gw")
        routing = await db.chat_routing("c1")
        self.assertEqual(routing["machine"]["id"], "gw")
        self.assertTrue(routing["pinned"])

    async def test_pin_carries_the_api_key_for_the_runner(self):
        await db.chat_set_machine("c1", "admin", "gw")
        self.assertEqual((await db.chat_routing("c1"))["machine"]["api_key"], "k")

    async def test_pin_to_a_deleted_machine_falls_back(self):
        # A dangling pin must not fail the turn.
        await db.chat_set_machine("c1", "admin", "gw")
        await db.ai_machine_delete("gw", "admin")
        routing = await db.chat_routing("c1")
        self.assertEqual(routing["machine"]["id"], "act")
        self.assertFalse(routing["pinned"])

    async def test_pin_to_another_owners_machine_is_not_honoured(self):
        await self.make_machine("bobs", base_url=GATEWAY, model="m", owner="bob")
        # Written directly, bypassing the API's ownership check.
        await db.db_conn.execute(
            "UPDATE chats SET ai_machine_id = ? WHERE id = ?", ("bobs", "c1")
        )
        await db.db_conn.commit()
        self.assertEqual((await db.chat_routing("c1"))["machine"]["id"], "act")

    async def test_clearing_the_pin_returns_to_following(self):
        await db.chat_set_machine("c1", "admin", "gw")
        await db.chat_set_machine("c1", "admin", None)
        self.assertEqual((await db.chat_routing("c1"))["machine"]["id"], "act")

    async def test_set_machine_is_owner_scoped(self):
        self.assertFalse(await db.chat_set_machine("c1", "mallory", "gw"))
        self.assertFalse((await db.chat_routing("c1"))["pinned"])

    async def test_unknown_chat_resolves_to_nothing(self):
        routing = await db.chat_routing("does-not-exist")
        self.assertIsNone(routing["owner"])
        self.assertIsNone(routing["machine"])


# ── Integration ────────────────────────────────────────────────────────────────


class IntegrationQA(RoutingMixin, unittest.IsolatedAsyncioTestCase):
    """What the runner resolves for a turn."""

    async def asyncSetUp(self):
        await self.init_temp_db()
        await self.make_machine("official", base_url=OFFICIAL, model="claude-opus-5")
        await self.make_machine("gw", base_url=GATEWAY, model="vllm/Q", api_key="k")
        await db.ai_machine_activate("official", "admin")
        await self.make_chat("chatA")
        await self.make_chat("chatB")

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def test_two_conversations_can_use_different_backends(self):
        # The whole point of the feature.
        await db.chat_set_machine("chatB", "admin", "gw")
        a = await runner.get_backend("chatA")
        b = await runner.get_backend("chatB")
        self.assertEqual(a["base_url"], OFFICIAL)
        self.assertEqual(b["base_url"], GATEWAY)
        self.assertNotIn("api_key", a)
        self.assertEqual(b["api_key"], "k")

    async def test_switching_the_active_machine_leaves_a_pin_alone(self):
        await db.chat_set_machine("chatB", "admin", "gw")
        await db.ai_machine_activate("gw", "admin")
        await db.chat_set_machine("chatA", "admin", "official")
        self.assertEqual((await runner.get_backend("chatA"))["base_url"], OFFICIAL)
        self.assertEqual((await runner.get_backend("chatB"))["base_url"], GATEWAY)

    async def test_unpinned_conversations_follow_a_change(self):
        self.assertEqual((await runner.get_backend("chatA"))["base_url"], OFFICIAL)
        await db.ai_machine_activate("gw", "admin")
        self.assertEqual((await runner.get_backend("chatA"))["base_url"], GATEWAY)

    async def test_model_defaults_to_the_conversations_backend(self):
        await db.chat_set_machine("chatB", "admin", "gw")
        self.assertEqual(await runner.get_default_model("chatA"), "claude-opus-5")
        self.assertEqual(await runner.get_default_model("chatB"), "vllm/Q")

    async def test_a_pinned_model_beats_the_backend_default(self):
        await db.chat_set_machine("chatB", "admin", "gw")
        await db.chat_update("chatB", "admin", model="azure_ai/gpt-5.4-mini")
        self.assertEqual(
            await runner.get_default_model("chatB"), "azure_ai/gpt-5.4-mini"
        )
        # And does not leak into the other conversation.
        self.assertEqual(await runner.get_default_model("chatA"), "claude-opus-5")

    async def test_a_keyless_claude_code_backend_yields_no_url(self):
        # A claude_code machine with no base_url returns {"provider": "claude_code"}
        # from get_backend — the env builder strips ANTHROPIC_BASE_URL in OAuth mode.
        await self.make_machine("keyless", base_url=None, model="m")
        await db.chat_set_machine("chatA", "admin", "keyless")
        backend = await runner.get_backend("chatA")
        self.assertEqual(backend["provider"], "claude_code")
        self.assertNotIn("base_url", backend)
        env = runner._build_env(backend)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)

    async def test_pin_survives_a_fork(self):
        # chat_fork already copied ai_machine_id; now that it routes, a fork
        # must land on the same backend as its parent.
        await db.chat_set_machine("chatB", "admin", "gw")
        forked = await db.chat_fork("chatB", "admin")
        self.assertEqual(
            (await runner.get_backend(forked["id"]))["base_url"], GATEWAY
        )

    async def test_env_for_a_pinned_conversation_points_at_its_gateway(self):
        await db.chat_set_machine("chatB", "admin", "gw")
        env = runner._build_env(await runner.get_backend("chatB"))
        self.assertEqual(env["ANTHROPIC_BASE_URL"], GATEWAY)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "k")


# ── Component: the PATCH contract ──────────────────────────────────────────────


class ComponentAPIQA(RoutingMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.init_temp_db()
        await self.make_machine("gw", base_url=GATEWAY, model="vllm/Q", api_key="k")
        await self.make_machine("official", base_url=OFFICIAL, model="claude-opus-5")
        await db.ai_machine_activate("official", "admin")
        await self.make_chat("c1")

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def test_patch_sets_the_backend(self):
        """The response's contract, not its exact shape.

        This asserted `== {"ok": True}`, which stopped being true when PATCH
        started returning the updated chat so the sidebar row could be
        refreshed in place instead of re-fetching the whole list. That is a
        deliberate change, and an equality assertion on a response body makes
        every future addition to it a test failure -- while pinning none of
        what this test is actually about, which is that the routing moved.
        """
        resp = await self.patch_chat("c1", {"ai_machine_id": "gw"})
        body = json.loads(resp.body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["chat"]["id"], "c1")
        self.assertEqual((await db.chat_routing("c1"))["machine"]["id"], "gw")

    async def test_patch_clears_the_backend_with_null(self):
        await self.patch_chat("c1", {"ai_machine_id": "gw"})
        await self.patch_chat("c1", {"ai_machine_id": None})
        self.assertFalse((await db.chat_routing("c1"))["pinned"])

    async def test_patch_treats_blank_as_clearing(self):
        await self.patch_chat("c1", {"ai_machine_id": "gw"})
        await self.patch_chat("c1", {"ai_machine_id": "   "})
        self.assertFalse((await db.chat_routing("c1"))["pinned"])

    async def test_patch_sets_and_clears_the_model(self):
        await self.patch_chat("c1", {"model": "azure_ai/gpt-5.4-mini"})
        self.assertEqual((await db.chat_get("c1", "admin"))["model"],
                         "azure_ai/gpt-5.4-mini")
        await self.patch_chat("c1", {"model": None})
        self.assertIsNone((await db.chat_get("c1", "admin"))["model"])

    async def test_patch_can_change_both_at_once(self):
        await self.patch_chat("c1", {"ai_machine_id": "gw", "model": "vllm/Q"})
        routing = await db.chat_routing("c1")
        self.assertEqual(routing["machine"]["id"], "gw")
        self.assertEqual(routing["model"], "vllm/Q")

    async def test_patch_rejects_another_users_machine(self):
        await self.make_machine("bobs", base_url=GATEWAY, model="m", owner="bob")
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await self.patch_chat("c1", {"ai_machine_id": "bobs"})
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertFalse((await db.chat_routing("c1"))["pinned"])

    async def test_patch_rejects_an_unknown_machine(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await self.patch_chat("c1", {"ai_machine_id": "nope"})
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_patch_rejects_a_dangerous_model_name(self):
        from fastapi import HTTPException
        for bad in ("model; rm -rf /", "model$(id)", "model with space"):
            with self.assertRaises(HTTPException, msg=bad) as ctx:
                await self.patch_chat("c1", {"model": bad})
            self.assertEqual(ctx.exception.status_code, 400, bad)

    async def test_patch_rejects_wrong_types(self):
        from fastapi import HTTPException
        for body in ({"ai_machine_id": 42}, {"model": 42}, {"model": ["a"]}):
            with self.assertRaises(HTTPException, msg=str(body)) as ctx:
                await self.patch_chat("c1", body)
            self.assertEqual(ctx.exception.status_code, 400, str(body))

    async def test_patch_still_rejects_unknown_fields(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await self.patch_chat("c1", {"work_dir": "/etc"})
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_patch_on_another_users_chat_is_a_404(self):
        from fastapi import HTTPException
        await self.make_chat("bobschat", owner="bob")
        with self.assertRaises(HTTPException) as ctx:
            await self.patch_chat("bobschat", {"ai_machine_id": "gw"})
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_the_chat_payload_exposes_its_routing(self):
        # The picker needs both fields to show the conversation's own state.
        await self.patch_chat("c1", {"ai_machine_id": "gw", "model": "vllm/Q"})
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["ai_machine_id"], "gw")
        self.assertEqual(chat["model"], "vllm/Q")


# ── Acceptance ─────────────────────────────────────────────────────────────────


class AcceptanceUATQA(RoutingMixin, unittest.IsolatedAsyncioTestCase):
    """The two workflows as requested: swap backend, and swap model."""

    async def asyncSetUp(self):
        await self.init_temp_db()
        await self.make_machine("anthropic-api", base_url=OFFICIAL,
                                model="claude-opus-5", api_key="key1")
        await self.make_machine("ai-machine", base_url=GATEWAY,
                                model="vllm/Qwen3.6-35B-A3B-NVFP4", api_key="k")
        await db.ai_machine_activate("anthropic-api", "admin")
        await self.make_chat("work")

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def _endpoint(self, chat_id):
        return runner._build_env(await runner.get_backend(chat_id)).get(
            "ANTHROPIC_BASE_URL"
        )

    async def _backend_id(self, chat_id):
        backend = await runner.get_backend(chat_id)
        return backend.get("provider")

    async def test_i_can_move_a_conversation_from_anthropic_to_my_ai_machine(self):
        self.assertEqual(await self._endpoint("work"), OFFICIAL)
        await self.patch_chat("work", {"ai_machine_id": "ai-machine"})
        self.assertEqual(await self._endpoint("work"), GATEWAY)
        self.assertEqual(
            await runner.get_default_model("work"), "vllm/Qwen3.6-35B-A3B-NVFP4"
        )

    async def test_and_back_again(self):
        await self.patch_chat("work", {"ai_machine_id": "ai-machine"})
        await self.patch_chat("work", {"ai_machine_id": "anthropic-api"})
        self.assertEqual(await self._endpoint("work"), OFFICIAL)

    async def test_i_can_move_from_model_a_to_model_b(self):
        await self.patch_chat("work", {"model": "claude-opus-5"})
        self.assertEqual(await runner.get_default_model("work"), "claude-opus-5")
        await self.patch_chat("work", {"model": "claude-sonnet-5"})
        self.assertEqual(await runner.get_default_model("work"), "claude-sonnet-5")

    async def test_my_other_conversations_are_untouched(self):
        await self.make_chat("other")
        await self.patch_chat("work", {"ai_machine_id": "ai-machine",
                                       "model": "vllm/Qwen3.6-35B-A3B-NVFP4"})
        self.assertEqual(await self._endpoint("other"), OFFICIAL)
        self.assertEqual(await runner.get_default_model("other"), "claude-opus-5")

    async def test_the_choice_survives_reopening_the_conversation(self):
        await self.patch_chat("work", {"ai_machine_id": "ai-machine",
                                       "model": "azure_ai/gpt-5.4-mini"})
        # Simulate a fresh page load: read the chat back as the UI would.
        chat = await db.chat_get("work", "admin")
        self.assertEqual(chat["ai_machine_id"], "ai-machine")
        self.assertEqual(chat["model"], "azure_ai/gpt-5.4-mini")

    async def test_switching_backend_keeps_the_conversation_session(self):
        # A backend change must not silently discard continuity: --resume reads
        # a local transcript, so the session id is still valid afterwards.
        await db.chat_set_session("work", "sess-123")
        await self.patch_chat("work", {"ai_machine_id": "ai-machine"})
        self.assertEqual((await db.chat_get("work", "admin"))["session_id"],
                         "sess-123")

    async def test_usage_is_attributed_to_the_backend_that_ran_the_turn(self):
        # Two turns on two backends from the same conversation must not be
        # merged into one provider in the usage page.
        frame = {"type": "usage",
                 "models": {"claude-opus-5": {"input_tokens": 10, "output_tokens": 1}},
                 "cost_usd": None, "duration_ms": 1, "is_error": False}
        await chat_routes._record_turn_usage("work", "admin", frame)
        await self.patch_chat("work", {"ai_machine_id": "ai-machine"})
        await db.ai_machine_activate("ai-machine", "admin")
        frame2 = {"type": "usage",
                  "models": {"vllm/Q": {"input_tokens": 20, "output_tokens": 2}},
                  "cost_usd": None, "duration_ms": 1, "is_error": False}
        await chat_routes._record_turn_usage("work", "admin", frame2)
        providers = {r["model"]: r["provider"]
                     for r in await db.usage_totals("admin", None)}
        self.assertEqual(providers["claude-opus-5"], "through_claude_code")
        self.assertEqual(providers["vllm/Q"], "through_claude_code")


if __name__ == "__main__":
    unittest.main()


# ── Regression: the detail payload must carry the routing ──────────────────────


class ChatPayloadQA(RoutingMixin, unittest.IsolatedAsyncioTestCase):
    """GET /api/chats/{id} must expose ai_machine_id.

    Found by the browser smoke test, not by any unit test: handle_chat_get
    builds its response from an explicit field list, so a new column is invisible
    to the client until it is added there. Without it the workspace picker could
    not show which backend a reopened conversation was pinned to.
    """

    async def asyncSetUp(self):
        await self.init_temp_db()
        await self.make_machine("gw", base_url=GATEWAY, model="vllm/Q", api_key="k")
        await db.ai_machine_activate("gw", "admin")
        await self.make_chat("c1")

    async def asyncTearDown(self):
        await self.close_temp_db()

    def _get_req(self):
        return SimpleNamespace(
            method="GET",
            url=SimpleNamespace(path="/api/chats/c1"),
            cookies={},
            headers={"accept": "*/*"},
            query_params={},
            client=SimpleNamespace(host="127.0.0.1"),
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value={}),
        )

    async def test_detail_payload_exposes_the_pinned_backend(self):
        await self.patch_chat("c1", {"ai_machine_id": "gw"})
        body = json.loads((await chat_routes.handle_chat_get(self._get_req(), "c1")).body)
        self.assertEqual(body["chat"]["ai_machine_id"], "gw")

    async def test_detail_payload_exposes_an_absent_pin_as_null(self):
        body = json.loads((await chat_routes.handle_chat_get(self._get_req(), "c1")).body)
        self.assertIn("ai_machine_id", body["chat"])
        self.assertIsNone(body["chat"]["ai_machine_id"])

    async def test_detail_payload_exposes_the_model(self):
        await self.patch_chat("c1", {"model": "vllm/Q"})
        body = json.loads((await chat_routes.handle_chat_get(self._get_req(), "c1")).body)
        self.assertEqual(body["chat"]["model"], "vllm/Q")

    async def test_list_payload_also_carries_the_pin(self):
        # The picker is populated from whichever payload arrives first.
        await self.patch_chat("c1", {"ai_machine_id": "gw"})
        req = self._get_req()
        body = json.loads((await chat_routes.handle_chats_list(req)).body)
        entry = next(c for c in body["chats"] if c["id"] == "c1")
        self.assertEqual(entry["ai_machine_id"], "gw")

    async def test_the_key_never_appears_in_a_chat_payload(self):
        # A distinctive value, so the assertion cannot pass by coincidence.
        await self.make_machine("secret-machine", base_url=GATEWAY, model="m",
                                api_key="SENTINEL-KEY-9f3a")
        await self.patch_chat("c1", {"ai_machine_id": "secret-machine"})
        for resp in (await chat_routes.handle_chat_get(self._get_req(), "c1"),
                     await chat_routes.handle_chats_list(self._get_req())):
            body = resp.body.decode()
            self.assertNotIn("SENTINEL-KEY-9f3a", body)
            self.assertNotIn("api_key", body)
