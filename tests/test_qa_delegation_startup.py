# tests/test_qa_delegation_startup.py
"""QA: spec 1.1's startup validation, and its bootstrap exemption.

The exemption is the part worth testing hardest. Section 2.6 ships mostly
unmeasured, so a check that refused every TBD would mean the system could
never start a first time -- and a validation gate that cannot be satisfied
gets satisfied with junk values instead.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch

import config
import db
import delegation_startup as ds
import tiered_delegation as td


class StartupValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def _row(self, model, task_type, **kw):
        defaults = dict(accuracy=None, n=None, cost_per_1m_tokens=None,
                        median_latency_s=None, max_context=None)
        defaults.update(kw)
        await db.delegation_row_set(model, task_type, **defaults)

    async def _seed_machine_serving(self, *model_ids: str) -> str:
        """Register a machine whose active list AND force-refreshed
        `models_list` name *model_ids*, so `routes.machines.known_backend_models`
        reports them as live -- the DB-only stand-in for "the combo box
        actually offers this", complete included: F1's completeness rule
        requires a populated `models_list`, which only a real force-refresh
        (`ai_machine_set_models_list`, normally reached through the
        Backends UI's `?force=1`) ever writes.

        `db.ai_machine_create` returns its write timestamp, not the id --
        the id passed in is the one to reuse for the follow-up update.
        """
        machine_id = f"m-{model_ids[0]}"
        await db.ai_machine_create(
            machine_id, "Test Machine", "localhost", 0, None,
            model_ids[0], None, None, "tester", provider="claude_code",
        )
        await db.ai_machine_set_models(machine_id, "tester", list(model_ids),
                                       model_ids[0])
        await db.ai_machine_set_models_list(
            machine_id, "tester",
            json.dumps([{"id": m} for m in model_ids]), db._now(),
        )
        return machine_id

    async def test_an_empty_table_starts_fine(self):
        """The bootstrap case: nothing measured, nothing operational."""
        table = await ds.validate_or_die()
        self.assertEqual(table.ladder("coding"), [])

    async def test_a_non_operational_type_may_hold_tbd(self):
        await self._row("m", "coding", cost_per_1m_tokens=1.0)
        await ds.validate_or_die()  # must not raise

    async def test_an_operational_type_with_a_tbd_ladder_row_refuses_to_start(self):
        await self._row("m", "coding", accuracy=0.9, n=4,
                        cost_per_1m_tokens=1.0, median_latency_s=None,
                        max_context=1000)
        await db.delegation_operational_set("coding", True)
        with self.assertRaises(ds.DelegationConfigError) as ctx:
            await ds.validate_or_die()
        self.assertIn("coding", str(ctx.exception))

    async def test_an_operational_type_with_no_eligible_row_refuses_to_start(self):
        """'At least one ladder-eligible row must exist' -- a type whose rows
        are all TBD has an empty ladder, and an empty ladder cannot route."""
        await self._row("m", "coding", cost_per_1m_tokens=1.0)
        await db.delegation_operational_set("coding", True)
        with self.assertRaises(ds.DelegationConfigError):
            await ds.validate_or_die()

    async def _gate_rows(self):
        """Section 2.6's real two `reviewer-gate` rows.

        Any table that flips a type operational needs at least one: stages
        3-5 run on this task type (spec 3, 4.3), so 1.1's "the ceiling fits
        the budget" invariant has no gate multiplier to compute without one.
        Luna's 11.1s is the measured gate latency 5.1's own derivation
        divides by. Neither row carries an accuracy -- that is why
        `reviewer-gate` itself is not operational (1.2), and the latency
        invariant deliberately does not require one.

        Sonnet is the climb rung (spec 4.3/4.5), and its `reviewer-gate`
        latency is unmeasured here, matching section 2.6's real, current
        state -- so a type validated against this fixture cannot compute a
        worst-case path (2026-09-16 amendment) and cannot start. Use
        `_gate_rows_single` for a test that needs the worst case to actually
        resolve.
        """
        await self._row("azure_ai/gpt-5.6-luna", "reviewer-gate",
                        accuracy=0.95, n=28,
                        cost_per_1m_tokens=0.0285, median_latency_s=11.1,
                        max_context=922_000)
        await self._row("claude-sonnet-5", "reviewer-gate",
                        cost_per_1m_tokens=1.5709, max_context=1_000_000)
        await self._security_gate_row()

    async def _security_gate_row(self):
        """Stage 5's own task type, split out from `reviewer-gate` on
        2026-09-17.

        Every fixture that flips a type operational now needs one: 4.5's
        security gate runs on this type, so without a row 1.1's ceiling
        invariant has no multiplier for a third of its gate calls and
        correctly refuses. The latency matches luna's reviewer-gate figure so
        the arithmetic in tests written before the split -- which multiplied
        ONE gate latency by three -- still reproduces.
        """
        await self._row("azure_ai/gpt-5.6-luna", "security-gate",
                        accuracy=0.95, n=28,
                        cost_per_1m_tokens=0.0285, median_latency_s=11.1,
                        max_context=922_000)
        # Spec 12, decided 2026-09-17: an ordinary task type may not route
        # through a gate type that has not itself cleared 1.1, so every
        # fixture that flips a type operational must flip the gate types too.
        for gate_type in td.GATE_CALLS:
            await db.delegation_operational_set(gate_type, True)

    async def _gate_rows_single(self):
        """One usable rung on each gate task type: no second, climbable rung,
        so a type validated against this fixture computes its worst-case path
        exactly as the pre-2026-09-16 formula did.
        """
        await self._row("azure_ai/gpt-5.6-luna", "reviewer-gate",
                        accuracy=0.95, n=28,
                        cost_per_1m_tokens=0.0285, median_latency_s=11.1,
                        max_context=922_000)
        await self._security_gate_row()

    async def test_a_complete_operational_type_starts(self):
        """The one end-to-end check that a complete operational type boots
        through the database path, so all six invariants pass together.

        The model is a real section 2.6 id rather than `m`: 1.1's model
        resolution invariant asks the combo box (9.3) whether a rung resolves,
        and a placeholder name resolves to nothing.

        A machine actually serving it is seeded too: `validate_or_die` now
        wires the live model list (`routes.machines.known_backend_models`)
        into this check rather than the `config.KNOWN_MODELS` fallback, and
        that fallback holds bare Anthropic ids only -- it would never contain
        a `vllm/...` id, live or not.
        """
        await self._row("vllm/Qwen3.6-35B-A3B-NVFP4", "long-context",
                        accuracy=1.0, n=10, cost_per_1m_tokens=0.0,
                        median_latency_s=12.0, max_context=229376)
        # Luna is served too: since spec 12's coverage rule the gate types
        # are operational here, so THEIR rungs go through 1.1's model
        # resolution check as well.
        await self._seed_machine_serving("vllm/Qwen3.6-35B-A3B-NVFP4",
                                         "azure_ai/gpt-5.6-luna")
        await self._gate_rows_single()
        await db.delegation_operational_set("long-context", True)
        table = await ds.validate_or_die()
        self.assertEqual(table.ladder("long-context"),
                         ["vllm/Qwen3.6-35B-A3B-NVFP4"])
        # The baseline is derived against this table's own 1.0 reference
        # (12.0s, the only rung), not read out of the published 45.0:
        #   (45.0 / 4.2) x 12.0 = 128.571s
        # x 2.0 (score 5) x [ 12.0/12.0 + 3 x (11.1/12.0) ] = 970.714s, under
        # the 1,500s ceiling, and the free rung costs nothing -- so the start
        # above is a real pass of all six invariants rather than a vacuous one.
        baseline = td.baseline_task_ratio("long-context") * 12.0
        self.assertAlmostEqual(table.baseline_deadline_s("long-context"),
                               baseline, places=9)
        # Each gate's ENTRY call is charged once per generation attempt since
        # 2026-09-18 (4.3/4.5: the gate re-reviews after the generator
        # escalates); the climb stays once. Two gate types back 2 and 1 of
        # stages 3-5's calls, and this fixture gives both the same 11.1s rung
        # with no climb:
        #     1 generation rung + (2 + 1) calls x MAX_ATTEMPTS x 11.1/12.0
        self.assertAlmostEqual(
            table.worst_case_path_s("long-context"),
            baseline * 2.0 * (1.0 + 3 * td.MAX_ATTEMPTS * (11.1 / 12.0)),
            places=6)
        self.assertLess(table.worst_case_path_s("long-context"),
                        td.LATENCY_CEILING_S)
        self.assertAlmostEqual(table.tree_cost_usd("long-context"), 0.0,
                               places=6)

    async def test_a_backend_qualified_rung_no_machine_serves_now_fails(self):
        """Closes the gap two reviews flagged: with the live model list
        wired in, a backend-qualified rung (`vllm/...`) is checked against
        real machines, not just its own shape. Before this change,
        `validate()` ran with `known_models=None` and any non-empty
        `backend/model` string passed unconditionally.
        """
        await self._row("vllm/NotAModel", "reasoning", accuracy=0.9, n=4,
                        cost_per_1m_tokens=0.0, median_latency_s=12.0,
                        max_context=229376)
        await self._gate_rows()
        await db.delegation_operational_set("reasoning", True)
        with self.assertRaises(ds.DelegationConfigError) as ctx:
            await ds.validate_or_die()
        self.assertIn("vllm/NotAModel", str(ctx.exception))

    async def test_an_unreadable_live_model_list_degrades_instead_of_refusing_to_boot(self):
        """"This model does not resolve" and "I could not find out which
        models exist" must not share a code path. A DB failure fetching the
        live list is the second one: `validate_or_die` must still start,
        falling back to the shape-only check rather than raising -- and
        rather than treating the failure as "the combo box has nothing in
        it", which would refuse to start for a reason that has nothing to do
        with the configured data.
        """
        await self._row("vllm/SomeGatewayModel", "reasoning", accuracy=0.9,
                        n=4, cost_per_1m_tokens=0.0, median_latency_s=12.0,
                        max_context=229376)
        await self._gate_rows_single()
        await db.delegation_operational_set("reasoning", True)
        with patch("routes.machines.known_backend_models",
                   side_effect=RuntimeError("db unavailable")):
            table = await ds.validate_or_die()  # must not raise
        self.assertEqual(table.ladder("reasoning"), ["vllm/SomeGatewayModel"])

    async def test_an_operational_types_unmeasured_climb_rung_refuses_to_start(self):
        """Spec 5.1's 2026-09-16 amendment, through the real startup path:
        `reviewer-gate` holding its real two rows (luna measured, sonnet's
        climb latency TBD) makes every operational type's worst-case path
        incomputable, not merely a smaller number -- so `validate_or_die`
        must refuse to start rather than let an understated figure pass the
        ceiling check."""
        await self._row("vllm/SomeGatewayModel", "reasoning", accuracy=0.9,
                        n=4, cost_per_1m_tokens=0.0, median_latency_s=12.0,
                        max_context=229376)
        await self._gate_rows()
        await db.delegation_operational_set("reasoning", True)
        with self.assertRaises(ds.DelegationConfigError) as ctx:
            await ds.validate_or_die()
        message = str(ctx.exception)
        self.assertIn("reasoning", message)
        self.assertIn("claude-sonnet-5", message)
        self.assertIn("climb", message)

    async def test_a_never_force_refreshed_machine_does_not_refuse_boot(self):
        """F1's ruling, through the real startup path: a machine registered
        but never force-refreshed on the Backends UI (`ai_machine_create`
        only, no `ai_machine_set_models`) leaves `known_backend_models`'s
        view incomplete -- it has default `model`/`active_models` for that
        machine but no positive evidence of what else it serves. A rung on
        a *different* backend, absent from that partial view, must not be
        refused for it: an unknown is not a negative, and rejecting a
        served model here is unrepairable (the settings page needs the app
        running). Before the fix this raised `DelegationConfigError`
        because `vllm/SomeGatewayModel` is not among the unprobed machine's
        default model/active list.
        """
        await db.ai_machine_create(
            "m-unprobed", "Unprobed Machine", "localhost", 0, None,
            "vllm/never-probed", None, None, "tester", provider="claude_code",
        )
        await self._row("vllm/SomeGatewayModel", "reasoning", accuracy=0.9,
                        n=4, cost_per_1m_tokens=0.0, median_latency_s=12.0,
                        max_context=229376)
        await self._gate_rows_single()
        await db.delegation_operational_set("reasoning", True)
        table = await ds.validate_or_die()  # must not raise
        self.assertEqual(table.ladder("reasoning"), ["vllm/SomeGatewayModel"])

    async def test_a_never_force_refreshed_machine_logs_which_one_to_refresh(self):
        """The operator-facing half of the same fix: falling back must say
        which machine to force-refresh, not just that it fell back."""
        await db.ai_machine_create(
            "m-unprobed", "Unprobed Machine", "localhost", 0, None,
            "vllm/never-probed", None, None, "tester", provider="claude_code",
        )
        with self.assertLogs("wc.app", level="WARNING") as ctx:
            known = await ds.live_known_models()
        self.assertIsNone(known)
        self.assertTrue(any("Unprobed Machine" in m for m in ctx.output))

    async def test_known_backend_models_is_complete_once_force_refreshed(self):
        from routes.machines import known_backend_models
        await self._seed_machine_serving("vllm/Qwen3.6-35B-A3B-NVFP4")
        result = await known_backend_models()
        self.assertTrue(result.complete)
        self.assertEqual(result.incomplete_machines, ())
        self.assertIn("vllm/Qwen3.6-35B-A3B-NVFP4", result.ids)

    async def test_known_backend_models_is_vacuously_complete_with_no_machines(self):
        """An empty `ai_machines` table has no machine to be incomplete
        about -- completeness must not default to False just because
        nothing has been configured yet, or the bootstrap case (no machines,
        nothing operational) would degrade for no reason."""
        from routes.machines import known_backend_models
        result = await known_backend_models()
        self.assertTrue(result.complete)

    async def test_the_error_names_every_broken_invariant_not_just_the_first(self):
        """'The error lists every broken invariant so the operator can fix the
        data before deployment' -- one at a time means one restart each."""
        await self._row("a", "coding", accuracy=0.9, n=4,
                        cost_per_1m_tokens=1.0, median_latency_s=None,
                        max_context=1000)
        await self._row("b", "reasoning", accuracy=0.9, n=4,
                        cost_per_1m_tokens=1.0, median_latency_s=None,
                        max_context=1000)
        await db.delegation_operational_set("coding", True)
        await db.delegation_operational_set("reasoning", True)
        with self.assertRaises(ds.DelegationConfigError) as ctx:
            await ds.validate_or_die()
        message = str(ctx.exception)
        self.assertIn("coding", message)
        self.assertIn("reasoning", message)


if __name__ == "__main__":
    unittest.main()
