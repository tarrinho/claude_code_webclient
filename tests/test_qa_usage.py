"""Layered QA coverage for usage accounting.

* UnitQA — parsing Claude Code's ``result`` frame into a usage event.
* IntegrationQA — recording, aggregating, pruning, owner isolation.
* ComponentAPIQA — the GET /api/usage contract, including cost suppression.
* ParityQA — every turn path records usage.

ParityQA is the point of this file. Streaming/blocking divergence in exactly
this code has caused two production bugs: the streaming turn payload omitted
``backend`` while the blocking one carried it, and ``has_api_key`` was derived
from a column its query no longer selected. Both survived because one path was
checked and the other assumed. So each path is asserted separately here rather
than inferred from its sibling.

No live model, Claude Code account, or network service is required.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app
import claude_proxy
import config
import db
import runner
from routes import machines as machine_routes

# A real `result` frame, captured from Claude Code 2.1.251 against a LiteLLM
# gateway. Trimmed to the fields the parser reads.
RESULT_FRAME = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 2122,
    "duration_api_ms": 1628,
    "num_turns": 1,
    "total_cost_usd": 0.078455,
    "session_id": "2f32978e-e1f9-4736-a038-896193f628e4",
    "usage": {
        "input_tokens": 15531,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 32,
    },
    "modelUsage": {
        "vllm/Qwen3.6-35B-A3B-NVFP4": {
            "inputTokens": 15531,
            "outputTokens": 32,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0,
            "webSearchRequests": 0,
            "costUSD": 0.078455,
            "contextWindow": 200000,
            "maxOutputTokens": 32000,
            "canonicalModel": "vllm/qwen3.6-35b-a3b-nvfp4",
            "provider": "firstParty",
            # The CLI's own verdict on its cost figure for this model.
            "costBasis": "unknown",
        }
    },
}
GATEWAY = "https://llm.example-gateway.invalid"


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


# ── Unit: frame parsing ────────────────────────────────────────────────────────


class UnitQA(unittest.TestCase):
    """Both parsers turn a result frame into the same usage event."""

    def parsers(self):
        return (("proxy", claude_proxy.usage_frame), ("runner", runner.usage_frame))

    def test_both_parsers_agree_on_a_real_frame(self):
        proxy = claude_proxy.usage_frame(RESULT_FRAME)
        direct = runner.usage_frame(RESULT_FRAME)
        self.assertEqual(proxy, direct)
        self.assertEqual(
            proxy["models"]["vllm/Qwen3.6-35B-A3B-NVFP4"],
            {
                "input_tokens": 15531,
                "output_tokens": 32,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "cost_basis": "unknown",
            },
        )
        self.assertEqual(proxy["cost_usd"], 0.078455)
        self.assertEqual(proxy["duration_ms"], 2122)
        self.assertFalse(proxy["is_error"])

    def test_model_usage_is_preferred_over_the_flat_totals(self):
        # modelUsage is keyed by model, so it is the only form that can attribute
        # a turn correctly. If both are present it must win.
        frame = {
            **RESULT_FRAME,
            "usage": {"input_tokens": 999999, "output_tokens": 999999},
        }
        for label, parse in self.parsers():
            models = parse(frame)["models"]
            self.assertEqual(list(models), ["vllm/Qwen3.6-35B-A3B-NVFP4"], label)
            self.assertEqual(models["vllm/Qwen3.6-35B-A3B-NVFP4"]["input_tokens"],
                             15531, label)

    def test_multiple_models_are_attributed_separately(self):
        frame = {
            "type": "result",
            "modelUsage": {
                "claude-opus-5": {"inputTokens": 100, "outputTokens": 10},
                "vllm/Qwen3.6-35B-A3B-NVFP4": {"inputTokens": 200, "outputTokens": 20},
            },
        }
        for label, parse in self.parsers():
            models = parse(frame)["models"]
            self.assertEqual(len(models), 2, label)
            self.assertEqual(models["claude-opus-5"]["input_tokens"], 100, label)
            self.assertEqual(
                models["vllm/Qwen3.6-35B-A3B-NVFP4"]["output_tokens"], 20, label
            )

    def test_flat_usage_reports_an_unknown_model(self):
        frame = {"type": "result", "usage": {"input_tokens": 12, "output_tokens": 3}}
        for label, parse in self.parsers():
            models = parse(frame)["models"]
            self.assertEqual(list(models), [""], label)
            self.assertEqual(models[""]["input_tokens"], 12, label)

    def test_no_usage_yields_no_frame(self):
        for label, parse in self.parsers():
            self.assertIsNone(parse({"type": "result"}), label)
            self.assertIsNone(parse({"type": "result", "usage": {}}), label)
            self.assertIsNone(
                parse({"type": "result", "usage": {"input_tokens": 0,
                                                   "output_tokens": 0}}), label)

    def test_malformed_values_do_not_raise(self):
        frame = {
            "type": "result",
            "modelUsage": {
                "good": {"inputTokens": "12", "outputTokens": None},
                "bad-stats": "not a dict",
                42: {"inputTokens": 1},
            },
            "total_cost_usd": "free",
            "duration_ms": "soon",
        }
        for label, parse in self.parsers():
            out = parse(frame)
            self.assertEqual(out["models"]["good"]["input_tokens"], 12, label)
            self.assertEqual(out["models"]["good"]["output_tokens"], 0, label)
            self.assertNotIn("bad-stats", out["models"], label)
            self.assertIsNone(out["cost_usd"], label)
            self.assertIsNone(out["duration_ms"], label)

    def test_negative_counts_are_floored_at_zero(self):
        frame = {"type": "result",
                 "modelUsage": {"m": {"inputTokens": -5, "outputTokens": 7}}}
        for label, parse in self.parsers():
            self.assertEqual(parse(frame)["models"]["m"]["input_tokens"], 0, label)

    def test_cost_basis_is_captured_when_the_cli_reports_it(self):
        # Recorded to explain a suppressed cost, not to decide it.
        for label, parse in self.parsers():
            models = parse(RESULT_FRAME)["models"]
            self.assertEqual(
                models["vllm/Qwen3.6-35B-A3B-NVFP4"]["cost_basis"], "unknown", label
            )

    def test_cost_basis_is_none_when_absent_or_malformed(self):
        frame = {"type": "result",
                 "modelUsage": {"a": {"inputTokens": 1},
                                "b": {"inputTokens": 1, "costBasis": 42}}}
        for label, parse in self.parsers():
            models = parse(frame)["models"]
            self.assertIsNone(models["a"]["cost_basis"], label)
            self.assertIsNone(models["b"]["cost_basis"], label)

    def test_error_turns_are_marked(self):
        frame = {**RESULT_FRAME, "is_error": True}
        for label, parse in self.parsers():
            self.assertTrue(parse(frame)["is_error"], label)

    def test_result_branch_emits_the_frame_in_both_normalisers(self):
        proxy_types = [f["type"] for f in claude_proxy.normalise_claude_frame(RESULT_FRAME)]
        direct_types = [e["type"] for e in runner._normalise_cli_frame(RESULT_FRAME)]
        self.assertIn("usage", proxy_types)
        self.assertIn("usage", direct_types)

    def test_unknown_model_is_resolved_from_the_session(self):
        runner._models_by_chat.pop("c-resolve", None)
        runner._usage_by_chat.pop("c-resolve", None)
        runner._models_by_chat["c-resolve"] = "claude-opus-5"
        runner.record_usage_frame(
            "c-resolve", {"type": "usage", "models": {"": {"input_tokens": 5}}}
        )
        stored = runner.take_last_usage("c-resolve")
        self.assertEqual(list(stored["models"]), ["claude-opus-5"])
        runner._models_by_chat.pop("c-resolve", None)

    def test_unknown_model_without_a_session_is_labelled_not_dropped(self):
        runner._models_by_chat.pop("c-none", None)
        runner.record_usage_frame(
            "c-none", {"type": "usage", "models": {"": {"input_tokens": 5}}}
        )
        self.assertEqual(list(runner.take_last_usage("c-none")["models"]), ["unknown"])


# ── Unit: provider classification ─────────────────────────────────────────────


class ProviderClassificationQA(unittest.TestCase):
    """Cost is only meaningful on the official API."""

    def test_official_api_is_trusted_for_cost(self):
        for machine in (
            {"provider": "anthropic", "base_url": None},
            {"provider": "anthropic", "base_url": ""},
            {"provider": "anthropic", "base_url": "https://api.anthropic.com"},
            {"provider": "anthropic", "base_url": "https://api.anthropic.com/"},
        ):
            self.assertEqual(app.backend_kind(machine), "anthropic", repr(machine))

    def test_a_gateway_is_not_trusted_for_cost(self):
        # provider='anthropic' only describes the wire protocol; a self-hosted
        # gateway speaks it too, and the CLI still prices it at Anthropic rates.
        for base in (GATEWAY, "http://10.0.0.5:4000/llm", "https://litellm.internal"):
            self.assertEqual(
                app.backend_kind({"provider": "anthropic", "base_url": base}),
                "anthropic-compatible",
                base,
            )

    def test_proxy_and_missing_machines_are_untrusted(self):
        self.assertEqual(app.backend_kind({"provider": "proxy"}), "proxy")
        self.assertEqual(app.backend_kind(None), "proxy")
        self.assertEqual(app.backend_kind({}), "proxy")


# ── Integration ────────────────────────────────────────────────────────────────


class IntegrationQA(TemporaryDBMixin, unittest.IsolatedAsyncioTestCase):
    """Recording and aggregating against a real database."""

    async def asyncSetUp(self):
        await self.init_temp_db()
        await db.chat_create("c1", "teste", None, f"{self.tmp.name}/projects", "admin")

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def test_record_then_aggregate(self):
        await db.usage_record("c1", "admin", "m-a", "anthropic",
                              input_tokens=100, output_tokens=10)
        await db.usage_record("c1", "admin", "m-a", "anthropic",
                              input_tokens=50, output_tokens=5, is_error=True)
        await db.usage_record("c1", "admin", "m-b", "proxy",
                              input_tokens=7, output_tokens=1)
        totals = {r["model"]: r for r in await db.usage_totals("admin")}
        self.assertEqual(totals["m-a"]["requests"], 2)
        self.assertEqual(totals["m-a"]["input_tokens"], 150)
        self.assertEqual(totals["m-a"]["errors"], 1)
        self.assertEqual(totals["m-b"]["requests"], 1)
        overall = await db.usage_overall("admin")
        self.assertEqual(overall["requests"], 3)
        self.assertEqual(overall["input_tokens"], 157)
        self.assertEqual(overall["models"], 2)

    async def test_rows_are_scoped_to_their_owner(self):
        await db.usage_record("c1", "admin", "m", "anthropic", input_tokens=10)
        await db.usage_record("c1", "bob", "m", "anthropic", input_tokens=999)
        self.assertEqual((await db.usage_overall("admin"))["input_tokens"], 10)
        self.assertEqual((await db.usage_overall("bob"))["input_tokens"], 999)
        self.assertEqual(await db.usage_totals("carol"), [])

    async def test_incomplete_rows_are_refused(self):
        self.assertIsNone(await db.usage_record("", "admin", "m", "anthropic"))
        self.assertIsNone(await db.usage_record("c1", "", "m", "anthropic"))
        self.assertIsNone(await db.usage_record("c1", "admin", "", "anthropic"))
        self.assertEqual((await db.usage_overall("admin"))["requests"], 0)

    async def test_recent_orders_newest_first_and_joins_the_title(self):
        for i in range(3):
            await db.usage_record("c1", "admin", f"m{i}", "anthropic", input_tokens=i)
        recent = await db.usage_recent("admin", 10)
        self.assertEqual([r["model"] for r in recent], ["m2", "m1", "m0"])
        self.assertEqual(recent[0]["chat_title"], "teste")

    async def test_recent_survives_a_deleted_conversation(self):
        # A LEFT JOIN: usage already spent must not vanish from the log because
        # the conversation was removed afterwards.
        await db.usage_record("c1", "admin", "m", "anthropic", input_tokens=5)
        await db.chat_delete("c1", "admin")
        recent = await db.usage_recent("admin", 10)
        self.assertEqual(len(recent), 1)
        self.assertIsNone(recent[0]["chat_title"])

    async def test_recent_follows_a_renamed_conversation(self):
        await db.usage_record("c1", "admin", "m", "anthropic", input_tokens=5)
        await db.chat_update("c1", "admin", title="Renamed")
        self.assertEqual((await db.usage_recent("admin", 5))[0]["chat_title"], "Renamed")

    async def test_recent_limit_is_clamped(self):
        for i in range(5):
            await db.usage_record("c1", "admin", "m", "anthropic", input_tokens=i)
        self.assertEqual(len(await db.usage_recent("admin", 2)), 2)
        self.assertEqual(len(await db.usage_recent("admin", 0)), 1)
        self.assertLessEqual(len(await db.usage_recent("admin", 10_000)), 5)

    async def _record_at(self, days_ago: int, model: str) -> None:
        await db.usage_record("c1", "admin", model, "anthropic", input_tokens=1)
        await db.db_conn.execute(
            "UPDATE usage_events SET created_at = ? WHERE model = ?",
            (db._cutoff(days_ago), model),
        )
        await db.db_conn.commit()

    async def test_prune_boundary(self):
        await self._record_at(91, "old")
        await self._record_at(89, "fresh")
        removed = await db.usage_prune(90)
        self.assertEqual(removed, 1)
        self.assertEqual([r["model"] for r in await db.usage_totals("admin", None)],
                         ["fresh"])

    async def test_prune_disabled_keeps_everything(self):
        await self._record_at(500, "ancient")
        self.assertEqual(await db.usage_prune(0), 0)
        self.assertEqual(len(await db.usage_totals("admin", None)), 1)

    async def test_windowed_totals_exclude_older_rows(self):
        await self._record_at(40, "old")
        await self._record_at(2, "recent")
        self.assertEqual([r["model"] for r in await db.usage_totals("admin", 30)],
                         ["recent"])
        self.assertEqual(len(await db.usage_totals("admin", None)), 2)


# ── Component: the API contract ────────────────────────────────────────────────


class ComponentAPIQA(TemporaryDBMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.init_temp_db()
        await db.chat_create("c1", "teste", None, f"{self.tmp.name}/projects", "admin")

    async def asyncTearDown(self):
        await self.close_temp_db()

    def _req(self, query=None):
        return SimpleNamespace(
            method="GET",
            url=SimpleNamespace(path="/api/usage"),
            cookies={},
            headers={"accept": "*/*"},
            query_params=dict(query or {}),
            client=SimpleNamespace(host="127.0.0.1"),
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value={}),
        )

    async def _get(self, query=None):
        # The endpoint folds terminal spend in by reading ~/.claude, so without
        # this the test imports the operator's real transcripts into its own
        # temporary database -- 18,572 rows on this machine, which drowns every
        # assertion about what the test itself inserted. It also made a unit
        # test depend on the machine it ran on, and take 16 seconds.
        with patch.object(app, "_import_cli_usage", return_value=0):
            return json.loads((await app.handle_usage_get(self._req(query))).body)

    async def test_response_shape(self):
        body = await self._get()
        for key in ("days", "retention_days", "overall", "totals", "recent"):
            self.assertIn(key, body)

    async def test_empty_database_reports_zero_not_an_error(self):
        body = await self._get()
        self.assertEqual(body["overall"]["requests"], 0)
        self.assertEqual(body["totals"], [])
        self.assertEqual(body["recent"], [])

    async def test_cost_is_hidden_for_a_gateway_and_shown_for_the_official_api(self):
        await db.usage_record("c1", "admin", "vllm/Q", "anthropic-compatible",
                              input_tokens=10, cost_usd=0.078)
        await db.usage_record("c1", "admin", "claude-opus-5", "anthropic",
                              input_tokens=10, cost_usd=1.25)
        rows = {r["model"]: r for r in (await self._get())["totals"]}
        self.assertIsNone(rows["vllm/Q"]["cost_usd"])
        self.assertIn("Anthropic rates", rows["vllm/Q"]["cost_note"])
        self.assertAlmostEqual(rows["claude-opus-5"]["cost_usd"], 1.25)
        self.assertNotIn("cost_note", rows["claude-opus-5"])

    async def test_cost_is_hidden_for_proxy_rows(self):
        await db.usage_record("c1", "admin", "m", "proxy",
                              input_tokens=10, cost_usd=9.99)
        self.assertIsNone((await self._get())["totals"][0]["cost_usd"])
        self.assertIsNone((await self._get())["recent"][0]["cost_usd"])

    async def test_note_quotes_the_cli_when_it_reported_an_unknown_basis(self):
        await db.usage_record("c1", "admin", "m", "anthropic-compatible",
                              input_tokens=1, cost_usd=0.07, cost_basis="unknown")
        row = (await self._get())["totals"][0]
        self.assertIsNone(row["cost_usd"])
        self.assertEqual(row["cost_basis_unknown"], 1)
        self.assertIn("cost basis as unknown", row["cost_note"])

    async def test_note_falls_back_when_the_cli_said_nothing(self):
        await db.usage_record("c1", "admin", "m", "anthropic-compatible",
                              input_tokens=1, cost_usd=0.07)
        row = (await self._get())["totals"][0]
        self.assertEqual(row["cost_basis_unknown"], 0)
        self.assertIn("Anthropic rates", row["cost_note"])

    async def test_a_known_basis_does_not_claim_it_was_unknown(self):
        await db.usage_record("c1", "admin", "m", "anthropic-compatible",
                              input_tokens=1, cost_basis="billed")
        self.assertEqual((await self._get())["totals"][0]["cost_basis_unknown"], 0)

    async def test_cost_basis_survives_in_the_recent_log(self):
        await db.usage_record("c1", "admin", "m", "anthropic-compatible",
                              input_tokens=1, cost_basis="unknown")
        self.assertEqual((await self._get())["recent"][0]["cost_basis"], "unknown")

    async def test_an_unknown_basis_never_suppresses_a_trusted_cost(self):
        # cost_basis explains; base_url decides. An official-API row keeps its
        # cost even if the CLI happened to report an unknown basis.
        await db.usage_record("c1", "admin", "m", "anthropic",
                              input_tokens=1, cost_usd=2.50, cost_basis="unknown")
        self.assertAlmostEqual((await self._get())["totals"][0]["cost_usd"], 2.50)

    async def test_days_is_clamped_and_all_is_accepted(self):
        self.assertEqual((await self._get({"days": "7"}))["days"], 7)
        self.assertEqual((await self._get({"days": "all"}))["days"], 0)
        self.assertEqual((await self._get({"days": "0"}))["days"], 0)
        self.assertEqual((await self._get({"days": "999999"}))["days"], 3650)
        self.assertEqual((await self._get({"days": "-5"}))["days"], 1)
        self.assertEqual((await self._get({"days": "banana"}))["days"], 30)

    async def test_limit_is_clamped(self):
        for i in range(4):
            await db.usage_record("c1", "admin", f"m{i}", "anthropic", input_tokens=1)
        self.assertEqual(len((await self._get({"limit": "2"}))["recent"]), 2)
        self.assertLessEqual(len((await self._get({"limit": "99999"}))["recent"]), 4)
        self.assertGreaterEqual(len((await self._get({"limit": "nope"}))["recent"]), 1)

    async def test_one_user_cannot_see_another_users_usage(self):
        await db.usage_record("c1", "bob", "secret-model", "anthropic",
                              input_tokens=4242)
        body = await self._get()
        self.assertEqual(body["overall"]["requests"], 0)
        self.assertNotIn("secret-model", json.dumps(body))

    async def test_no_credential_appears_in_the_response(self):
        await db.ai_machine_create("m1", "GW", "llm.invalid", 443, "super-secret",
                                   "m", GATEWAY, None, "admin", "anthropic")
        await db.ai_machine_activate("m1", "admin")
        await db.usage_record("c1", "admin", "m", "anthropic-compatible",
                              input_tokens=1)
        self.assertNotIn("super-secret", json.dumps(await self._get()))


# ── Parity: every turn path records usage ─────────────────────────────────────


class ParityQA(TemporaryDBMixin, unittest.IsolatedAsyncioTestCase):
    """A usage row must land regardless of which turn path ran.

    Asserted per path, never inferred. Two production bugs came from checking
    the blocking path and assuming the streaming one matched.
    """

    async def asyncSetUp(self):
        await self.init_temp_db()
        await db.chat_create("c1", "teste", None, f"{self.tmp.name}/projects", "admin")
        await db.ai_machine_create("m1", "GW", "llm.invalid", 443, None, "m",
                                   GATEWAY, None, "admin", "anthropic")
        await db.ai_machine_activate("m1", "admin")
        runner._usage_by_chat.pop("c1", None)
        runner._models_by_chat.pop("c1", None)

    async def asyncTearDown(self):
        runner._usage_by_chat.pop("c1", None)
        await self.close_temp_db()

    async def _rows(self):
        return await db.usage_totals("admin", None)

    async def test_blocking_path_records_via_take_last_usage(self):
        # _execute_proxy / _collect_chunks stash the frame; the handler drains it.
        runner.record_usage_frame("c1", runner.usage_frame(RESULT_FRAME))
        await app._record_turn_usage("c1", "admin", runner.take_last_usage("c1"))
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "vllm/Qwen3.6-35B-A3B-NVFP4")
        self.assertEqual(rows[0]["input_tokens"], 15531)

    async def test_streaming_path_records_the_event_directly(self):
        # _do_proxy_stream / _do_direct_stream yield the event to the handler.
        await app._record_turn_usage("c1", "admin", runner.usage_frame(RESULT_FRAME))
        self.assertEqual(len(await self._rows()), 1)

    async def test_proxy_dispatch_passes_usage_through_to_the_stream(self):
        # The streaming proxy branch must list "usage" among forwarded types, or
        # the handler never sees it. Guards against the omitted-type class of bug.
        source = (
            __import__("pathlib").Path(runner.__file__).read_text()
        )
        marker = source.split("async def _do_proxy_stream", 1)[1]
        self.assertIn('"usage",', marker[:6000])

    async def test_both_normalisers_are_wired_into_their_result_branch(self):
        self.assertIn("usage", [f["type"] for f in
                                claude_proxy.normalise_claude_frame(RESULT_FRAME)])
        self.assertIn("usage", [e["type"] for e in
                                runner._normalise_cli_frame(RESULT_FRAME)])

    async def test_take_last_usage_is_single_shot(self):
        runner.record_usage_frame("c1", runner.usage_frame(RESULT_FRAME))
        self.assertTrue(runner.take_last_usage("c1"))
        self.assertEqual(runner.take_last_usage("c1"), {})

    async def test_a_turn_with_two_models_writes_two_rows(self):
        frame = runner.usage_frame({
            "type": "result",
            "total_cost_usd": 0.5,
            "modelUsage": {
                "claude-opus-5": {"inputTokens": 10, "outputTokens": 1},
                "vllm/Q": {"inputTokens": 20, "outputTokens": 2},
            },
        })
        await app._record_turn_usage("c1", "admin", frame)
        rows = await self._rows()
        self.assertEqual(len(rows), 2)
        # The turn cost is charged once, not once per model.
        self.assertAlmostEqual(sum(r["cost_usd"] for r in rows), 0.5)

    async def test_provider_is_captured_from_the_active_machine(self):
        await app._record_turn_usage("c1", "admin", runner.usage_frame(RESULT_FRAME))
        self.assertEqual((await self._rows())[0]["provider"], "anthropic-compatible")

    async def test_empty_frame_records_nothing(self):
        for frame in ({}, None, {"models": {}}):
            await app._record_turn_usage("c1", "admin", frame)
        self.assertEqual(await self._rows(), [])

    async def test_recording_never_raises_when_the_database_is_down(self):
        # Accounting must not turn a completed turn into a 500.
        await db.close()
        try:
            await app._record_turn_usage(
                "c1", "admin", runner.usage_frame(RESULT_FRAME)
            )
        finally:
            await db.init()


if __name__ == "__main__":
    unittest.main()


# ── Component: backend_kind on the machine API ────────────────────────────────


class BackendKindAPIQA(TemporaryDBMixin, unittest.IsolatedAsyncioTestCase):
    """The classification is served, so clients need not reimplement the rule.

    It was duplicated client-side once already; two copies of a provider +
    base_url rule agreeing is a coincidence, not a guarantee.
    """

    async def asyncSetUp(self):
        await self.init_temp_db()

    async def asyncTearDown(self):
        await self.close_temp_db()

    def _req(self):
        return SimpleNamespace(
            method="GET",
            url=SimpleNamespace(path="/api/machines"),
            cookies={},
            headers={"accept": "*/*"},
            query_params={},
            client=SimpleNamespace(host="127.0.0.1"),
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value={}),
        )

    async def _make(self, mid, provider, base_url):
        await db.ai_machine_create(mid, mid, "h.invalid", 443, None, "m",
                                   base_url, None, "admin", provider)

    async def test_list_labels_each_backend(self):
        await self._make("official", "anthropic", "https://api.anthropic.com")
        await self._make("gateway", "anthropic", GATEWAY)
        await self._make("proxied", "proxy", None)
        body = json.loads((await machine_routes.handle_machines_list(self._req())).body)
        kinds = {m["id"]: m["backend_kind"] for m in body["machines"]}
        self.assertEqual(kinds["official"], "anthropic")
        self.assertEqual(kinds["gateway"], "anthropic-compatible")
        self.assertEqual(kinds["proxied"], "proxy")

    async def test_get_labels_the_backend(self):
        await self._make("gateway", "anthropic", GATEWAY)
        body = json.loads((await machine_routes.handle_machine_get(self._req(), "gateway")).body)
        self.assertEqual(body["machine"]["backend_kind"], "anthropic-compatible")

    async def test_label_matches_what_usage_records(self):
        # The whole point of one source of truth: the badge on a backend card and
        # the provider on its usage rows must never disagree.
        await self._make("gateway", "anthropic", GATEWAY)
        await db.ai_machine_activate("gateway", "admin")
        await db.chat_create("c1", "t", None, f"{self.tmp.name}/projects", "admin")
        await app._record_turn_usage("c1", "admin", runner.usage_frame(RESULT_FRAME))
        body = json.loads((await machine_routes.handle_machines_list(self._req())).body)
        label = next(m["backend_kind"] for m in body["machines"] if m["id"] == "gateway")
        recorded = (await db.usage_totals("admin", None))[0]["provider"]
        self.assertEqual(label, recorded)

    async def test_label_never_leaks_the_key(self):
        await db.ai_machine_create("k", "k", "h.invalid", 443, "super-secret", "m",
                                   GATEWAY, None, "admin", "anthropic")
        for resp in (await machine_routes.handle_machines_list(self._req()),
                     await machine_routes.handle_machine_get(self._req(), "k")):
            self.assertNotIn("super-secret", resp.body.decode())
