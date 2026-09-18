"""QA: the settings page's endpoints -- spec 9.2.

The load-bearing property is that a write which would break an invariant of an
operational task type is REFUSED, not stored. The table is live-editable and
the ladders regenerate from it, so a check that ran only at startup would let
an operator break routing at 15:00 and discover it at the next restart.

Two of the scenarios below (the "allowed" flip, and the unknown-column
rejection) go beyond the values sketched in the task brief:

* the "flip succeeds" scenario needs a *reviewer-gate* row and a model id
  that actually resolves against `config.KNOWN_MODELS`, because
  `CapabilityTable.validate()` (already committed, tiered_delegation.py)
  checks model resolution and the worst-case-path's gate latency for every
  operational type -- a fixture with no reviewer-gate row and a fake model
  id cannot pass those checks no matter how the route is written, so a test
  that used them would never be satisfiable by a spec-correct implementation.
* the unknown-column test exercises the "validate and constrain what the
  write endpoint accepts" requirement directly at the route layer, rather
  than relying on `db.delegation_row_set`'s own `ValueError` to leak out as
  an unhandled 500.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import db
import routes.delegation as delegation_routes
import tiered_delegation
from routes import db_delegation


def _request(role="admin", body=None, query=None):
    request = SimpleNamespace(
        state=SimpleNamespace(session={"user": "admin", "role": role}),
        query_params=query or {},
    )
    if body is not None:
        request.json = AsyncMock(return_value=body)
    return request


async def _ordinary_operational():
    """The operational set with the gate task types removed.

    Spec 12's coverage rule (2026-09-17) means every fixture that flips an
    ordinary type operational must flip `reviewer-gate` and `security-gate`
    too, so a bare equality against `delegation_operational_all()` would now
    be asserting the fixture's own setup rather than what the request under
    test did. Subtracting them keeps the assertion exactly as strong about the
    types these tests are actually about -- it still catches a flip that
    should not have happened.
    """
    return await db.delegation_operational_all() - set(
        tiered_delegation.GATE_CALLS)


class DelegationRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def _seed_machine_serving(self, *model_ids: str) -> str:
        """Register a machine whose active list AND force-refreshed
        `models_list` name *model_ids*, so `routes.machines.known_backend_models`
        -- and therefore `delegation_startup.live_known_models`, which both
        route handlers now call -- reports them as live, complete included:
        completeness requires a populated `models_list`, which only a real
        force-refresh (`ai_machine_set_models_list`, normally reached through
        the Backends UI's `?force=1`) ever writes. Without it the machine
        counts as incomplete and the resolution check falls back to a
        shape-only pass for backend-qualified ids -- which would let these
        tests stay green even if strict membership broke.

        `db.ai_machine_create` returns its write timestamp, not the id --
        reuse the id passed in for the follow-up update.
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

    def test_the_three_column_lists_agree(self):
        """`routes.delegation._EDITABLE`, `routes.db_delegation._COLUMNS` and
        `tiered_delegation._REQUIRED_COLUMNS` are three independent copies of
        the same five names. Adding a sixth column to one and not the others
        would silently reintroduce the partial-write nulling bug this file
        already regression-tests: a column missing from `_EDITABLE` is never
        offered to the merge in `handle_row_put`, so it would be nulled on
        every write the same way `n`/`cost_per_1m_tokens`/etc. used to be."""
        self.assertEqual(set(delegation_routes._EDITABLE), set(db_delegation._COLUMNS))
        self.assertEqual(set(delegation_routes._EDITABLE),
                          set(tiered_delegation._REQUIRED_COLUMNS))

    async def test_the_list_reports_nothing_operational(self):
        response = await delegation_routes.handle_delegation_get(_request())
        body = json.loads(response.body)
        self.assertEqual(body["operational"], [])

    async def test_the_config_overview_surfaces_9_1_5_10_and_2_7(self):
        """Spec 9.2's first sentence: the page also surfaces the kill switch
        (9.1), the 5/10 tunables, and the cost ceiling (2.7), read only. The
        values that do exist as code must match tiered_delegation exactly
        (not a copied-by-hand number that can drift); values with no single
        source in this release (the kill switch, per-gate MAX_ATTEMPTS,
        MAX_DEPTH, MAX_CHILDREN, MAX_SUBAGENTS_PER_LEAF, the circuit-breaker
        threshold, the spot-check rate, the free-tier target) must say so
        rather than showing an invented number."""
        response = await delegation_routes.handle_delegation_get(_request())
        body = json.loads(response.body)
        cfg = body["config"]

        self.assertFalse(cfg["kill_switch"]["available"])
        self.assertTrue(cfg["kill_switch"]["note"])

        caps = cfg["attempts_and_caps"]
        self.assertEqual(caps["max_attempts_generation"]["value"],
                          tiered_delegation.MAX_ATTEMPTS)
        self.assertEqual(caps["max_nodes_per_tree"]["value"],
                          tiered_delegation.LEAVES_PER_TREE)
        self.assertEqual(caps["combined_latency_ceiling_s"]["value"],
                          tiered_delegation.LATENCY_CEILING_S)
        for no_source in ("max_attempts_per_gate", "max_depth",
                          "max_children_per_node", "max_subagents_per_leaf"):
            self.assertIsNone(caps[no_source]["value"])
            self.assertTrue(caps[no_source]["note"])

        cost = cfg["cost_ceiling"]
        self.assertEqual(cost["budget_usd_per_tree"]["value"],
                          tiered_delegation.BUDGET_USD)
        self.assertEqual(set(cost["excluded_models"]["value"]),
                          set(tiered_delegation.EXCLUDED_MODELS))

        obs = cfg["observability"]
        for no_source in ("circuit_breaker_threshold", "human_spot_check_rate",
                          "free_tier_target"):
            self.assertIsNone(obs[no_source]["value"])
            self.assertTrue(obs[no_source]["note"])

    async def test_a_row_can_be_written_and_read_back(self):
        await delegation_routes.handle_row_put(_request(body={
            "model": "m", "task_type": "coding", "accuracy": 0.9, "n": 4,
            "cost_per_1m_tokens": 1.0, "median_latency_s": 2.0,
            "max_context": 1000}))
        body = json.loads((await delegation_routes.handle_delegation_get(_request())).body)
        self.assertEqual(len(body["rows"]), 1)

    async def test_a_non_admin_cannot_write(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_row_put(_request(role="user", body={
                "model": "m", "task_type": "coding"}))
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_a_non_admin_cannot_flip_operational(self):
        """The flip endpoint is the highest-privilege action in this change --
        it submits a task type to production routing -- and had no test of
        its own admin gate; only handle_row_put's was covered. Uses a task
        type other than `coding` so this stays a pure statement of "the admin
        check runs" -- see test_a_non_admin_flipping_coding_gets_403_not_400
        for the case where the coding policy guard and the admin check both
        apply."""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_operational_put(_request(role="user", body={
                "task_type": "long-context", "operational": True}))
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_a_non_admin_flipping_coding_gets_403_not_400(self):
        """Where the admin check and the coding policy guard both apply, the
        admin check must win: a non-admin gets 403 (you may not do this),
        never 400 (this is not allowed for this task type) -- the latter
        would leak the guard's existence, and its reason, to someone not
        entitled to it. This is only true if the coding guard sits strictly
        after `_require_admin` in `handle_operational_put`."""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_operational_put(_request(role="user", body={
                "task_type": "coding", "operational": True}))
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_a_non_dict_row_body_is_refused_with_400(self):
        """A bare list or string body must not reach the first `.get()` call
        and surface as an unhandled 500."""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_row_put(_request(body=["not", "an", "object"]))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_a_non_dict_operational_body_is_refused_with_400(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_operational_put(_request(body="x"))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_a_non_string_model_field_is_refused_with_400_not_500(self):
        """`_require_json_object` catches a non-object body one level up, but
        a body like `{"model": 5, "task_type": "coding"}` is a dict, so it
        passes that check and used to reach `(data.get("model") or
        "").strip()`, which raises `AttributeError` on an int -- an unhandled
        500, not a 400."""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_row_put(_request(body={
                "model": 5, "task_type": "coding"}))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_a_non_string_task_type_field_on_the_flip_endpoint_is_refused_with_400_not_500(self):
        """Same defect class, the other call site (`handle_operational_put`).
        A previous fix in this branch covered two endpoints with a single
        test and the uncovered one was silently green when broken -- so this
        is its own test, not a variation appended to the row-put one."""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_operational_put(_request(body={
                "task_type": 5, "operational": True}))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_a_non_numeric_value_is_refused_and_the_page_stays_readable(self):
        """Reproduced defect: a PUT with accuracy="abc" used to return 200 and
        store TEXT (SQLite REAL affinity does not coerce it), and every later
        GET raised TypeError from CapabilityTable.ladder's sort key -- a
        permanent 500 on the only page that could fix the cell. The write must
        be refused, and a normal GET afterwards must still succeed."""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_row_put(_request(body={
                "model": "m", "task_type": "coding", "accuracy": "abc"}))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("accuracy", str(ctx.exception.detail))
        response = await delegation_routes.handle_delegation_get(_request())
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertEqual(body["rows"], [])

    async def test_a_dict_value_is_also_refused(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_row_put(_request(body={
                "model": "m", "task_type": "coding", "cost_per_1m_tokens": {"a": 1}}))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_an_unknown_column_is_refused_not_passed_through(self):
        """'Validate and constrain what the write endpoint accepts. Unknown
        column names must be refused, not passed through.' A typo in a column
        name (or a probing client) must get a 400 naming the problem, not a
        write of the four columns it did recognise."""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_row_put(_request(body={
                "model": "m", "task_type": "coding", "accuracy": 0.9,
                "bogus_column": 1}))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("bogus_column", str(ctx.exception.detail))
        body = json.loads((await delegation_routes.handle_delegation_get(_request())).body)
        self.assertEqual(body["rows"], [])

    async def test_a_partial_write_does_not_null_the_other_columns(self):
        """`db.delegation_row_set` is a full-row UPSERT with no partial-column
        mode (an omitted keyword is stored as None), but the settings page
        edits one cell at a time (delegation.js's `_saveRow` sends exactly one
        column per PUT). The route must merge onto the stored row, or every
        single-cell edit would blank the other four columns."""
        await delegation_routes.handle_row_put(_request(body={
            "model": "m", "task_type": "coding", "accuracy": 0.9, "n": 4,
            "cost_per_1m_tokens": 1.0, "median_latency_s": 2.0,
            "max_context": 1000}))
        await delegation_routes.handle_row_put(_request(body={
            "model": "m", "task_type": "coding", "accuracy": 0.95}))
        body = json.loads((await delegation_routes.handle_delegation_get(_request())).body)
        self.assertEqual(len(body["rows"]), 1)
        row = body["rows"][0]
        self.assertEqual(row["accuracy"], 0.95)
        self.assertEqual(row["n"], 4)
        self.assertEqual(row["cost_per_1m_tokens"], 1.0)
        self.assertEqual(row["median_latency_s"], 2.0)
        self.assertEqual(row["max_context"], 1000)

    async def test_a_row_write_that_would_break_an_already_operational_type_is_refused(self):
        """The scoping rule (spec 1.1) is that a write is checked against the
        invariants of task types *already* operational -- exercised here by
        flipping one operational first, then writing a row that blanks a
        column a ladder-eligible row of that type needs."""
        from fastapi import HTTPException
        await db.delegation_row_set("claude-sonnet-5", "long-context",
                                    accuracy=1.0, n=10, cost_per_1m_tokens=0.0,
                                    median_latency_s=12.0, max_context=229376)
        await db.delegation_row_set("claude-sonnet-5", "reviewer-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        # Stage 5's own task type since the 2026-09-17 gate split. Same model
        # and latency as the reviewer row, so every figure these tests were
        # written against still reproduces.
        await db.delegation_row_set("claude-sonnet-5", "security-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        # Spec 12's coverage rule (2026-09-17): an ordinary task type may not
        # route through a gate type that has not itself cleared 1.1.
        for gate_type in tiered_delegation.GATE_CALLS:
            await db.delegation_operational_set(gate_type, True)
        await delegation_routes.handle_operational_put(_request(body={
            "task_type": "long-context", "operational": True}))

        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_row_put(_request(body={
                "model": "claude-sonnet-5", "task_type": "long-context",
                "median_latency_s": None}))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("long-context", str(ctx.exception.detail))
        # And the stored row is untouched by the refused write.
        body = json.loads((await delegation_routes.handle_delegation_get(_request())).body)
        row = next(r for r in body["rows"] if r["task_type"] == "long-context")
        self.assertEqual(row["median_latency_s"], 12.0)

    async def test_flipping_a_type_operational_on_incomplete_data_is_refused(self):
        """9.2: the flip re-runs 1.1's validation immediately and refuses with
        the offending column named, rather than accepting it and failing at the
        next restart. Uses `long-context`, not `coding` -- `coding` is now
        blocked unconditionally (spec 12), and using it here would test that
        guard instead of the invariant-validation path this test is about."""
        from fastapi import HTTPException
        await db.delegation_row_set("claude-sonnet-5", "long-context",
                                    accuracy=0.9, n=4, cost_per_1m_tokens=1.0,
                                    median_latency_s=None, max_context=1000)
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_operational_put(_request(body={
                "task_type": "long-context", "operational": True}))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("long-context", str(ctx.exception.detail))

    async def test_flipping_a_complete_type_operational_is_allowed(self):
        # A real, resolvable model id (spec 1.1's model-resolution invariant)
        # and a reviewer-gate row (5.1's gate latency, used by the worst-case
        # check) are both required for validate() to clear -- see the module
        # docstring.
        await db.delegation_row_set("claude-sonnet-5", "long-context",
                                    accuracy=1.0, n=10, cost_per_1m_tokens=0.0,
                                    median_latency_s=12.0, max_context=229376)
        await db.delegation_row_set("claude-sonnet-5", "reviewer-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        # Stage 5's own task type since the 2026-09-17 gate split. Same model
        # and latency as the reviewer row, so every figure these tests were
        # written against still reproduces.
        await db.delegation_row_set("claude-sonnet-5", "security-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        # Spec 12's coverage rule (2026-09-17): an ordinary task type may not
        # route through a gate type that has not itself cleared 1.1.
        for gate_type in tiered_delegation.GATE_CALLS:
            await db.delegation_operational_set(gate_type, True)
        await delegation_routes.handle_operational_put(_request(body={
            "task_type": "long-context", "operational": True}))
        self.assertEqual(await _ordinary_operational(), {"long-context"})

    async def test_a_refused_flip_leaves_the_stored_state_alone(self):
        """'the stored value is left as it was' -- a rejected write that half
        applied would be worse than one that failed outright. Uses
        `long-context` for the same reason as the test above."""
        from fastapi import HTTPException
        await db.delegation_row_set("claude-sonnet-5", "long-context",
                                    accuracy=0.9, n=4, cost_per_1m_tokens=1.0,
                                    median_latency_s=None, max_context=1000)
        with self.assertRaises(HTTPException):
            await delegation_routes.handle_operational_put(_request(body={
                "task_type": "long-context", "operational": True}))
        self.assertEqual(await _ordinary_operational(), set())

    async def test_a_row_write_introducing_an_unserved_backend_qualified_rung_is_refused(self):
        """`handle_row_put`'s validate() call uses the live model list
        (`delegation_startup.live_known_models`), the same one startup uses --
        not just config.KNOWN_MODELS' bare-Anthropic-id fallback. A
        backend-qualified rung (`vllm/...`) that no machine serves is refused
        here exactly as `validate_or_die` would refuse it at the next boot,
        rather than passing on shape alone."""
        from fastapi import HTTPException
        await db.delegation_row_set("claude-sonnet-5", "long-context",
                                    accuracy=1.0, n=10, cost_per_1m_tokens=0.0,
                                    median_latency_s=12.0, max_context=229376)
        await db.delegation_row_set("claude-sonnet-5", "reviewer-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        # Stage 5's own task type since the 2026-09-17 gate split. Same model
        # and latency as the reviewer row, so every figure these tests were
        # written against still reproduces.
        await db.delegation_row_set("claude-sonnet-5", "security-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        # Spec 12's coverage rule (2026-09-17): an ordinary task type may not
        # route through a gate type that has not itself cleared 1.1.
        for gate_type in tiered_delegation.GATE_CALLS:
            await db.delegation_operational_set(gate_type, True)
        await delegation_routes.handle_operational_put(_request(body={
            "task_type": "long-context", "operational": True}))

        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_row_put(_request(body={
                "model": "vllm/NotAModel", "task_type": "long-context",
                "accuracy": 1.0, "n": 10, "cost_per_1m_tokens": 0.0,
                "median_latency_s": 12.0, "max_context": 229376}))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("vllm/NotAModel", str(ctx.exception.detail))
        self.assertIn("is not in the model combo box", str(ctx.exception.detail))

    async def test_a_row_write_introducing_a_machine_served_backend_qualified_rung_is_accepted(self):
        """The other side of the same wiring: a backend-qualified rung a
        seeded machine actually serves is accepted, so the check is really
        consulting the live list rather than refusing every non-bare id."""
        await self._seed_machine_serving("vllm/Qwen3.6-35B-A3B-NVFP4")
        await db.delegation_row_set("claude-sonnet-5", "long-context",
                                    accuracy=1.0, n=10, cost_per_1m_tokens=0.0,
                                    median_latency_s=12.0, max_context=229376)
        await db.delegation_row_set("claude-sonnet-5", "reviewer-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        # Stage 5's own task type since the 2026-09-17 gate split. Same model
        # and latency as the reviewer row, so every figure these tests were
        # written against still reproduces.
        await db.delegation_row_set("claude-sonnet-5", "security-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        # Spec 12's coverage rule (2026-09-17): an ordinary task type may not
        # route through a gate type that has not itself cleared 1.1.
        for gate_type in tiered_delegation.GATE_CALLS:
            await db.delegation_operational_set(gate_type, True)
        await delegation_routes.handle_operational_put(_request(body={
            "task_type": "long-context", "operational": True}))

        response = await delegation_routes.handle_row_put(_request(body={
            "model": "vllm/Qwen3.6-35B-A3B-NVFP4", "task_type": "long-context",
            "accuracy": 1.0, "n": 10, "cost_per_1m_tokens": 0.0,
            "median_latency_s": 12.0, "max_context": 229376}))
        self.assertTrue(json.loads(response.body)["ok"])

    async def test_flipping_operational_with_an_unserved_backend_qualified_rung_is_refused(self):
        """Same wiring, the other call site: `handle_operational_put` must
        refuse a flip whose only ladder rung is a backend-qualified id no
        machine serves, for the same "does not resolve" reason
        `validate_or_die` would give at the next boot."""
        from fastapi import HTTPException
        await db.delegation_row_set("vllm/NotAModel", "long-context",
                                    accuracy=1.0, n=10, cost_per_1m_tokens=0.0,
                                    median_latency_s=12.0, max_context=229376)
        await db.delegation_row_set("claude-sonnet-5", "reviewer-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        # Stage 5's own task type since the 2026-09-17 gate split. Same model
        # and latency as the reviewer row, so every figure these tests were
        # written against still reproduces.
        await db.delegation_row_set("claude-sonnet-5", "security-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        # Spec 12's coverage rule (2026-09-17): an ordinary task type may not
        # route through a gate type that has not itself cleared 1.1.
        for gate_type in tiered_delegation.GATE_CALLS:
            await db.delegation_operational_set(gate_type, True)
        with self.assertRaises(HTTPException) as ctx:
            await delegation_routes.handle_operational_put(_request(body={
                "task_type": "long-context", "operational": True}))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("vllm/NotAModel", str(ctx.exception.detail))
        self.assertIn("is not in the model combo box", str(ctx.exception.detail))
        self.assertEqual(await _ordinary_operational(), set())

    async def test_flipping_operational_with_a_machine_served_backend_qualified_rung_is_accepted(self):
        await self._seed_machine_serving("vllm/Qwen3.6-35B-A3B-NVFP4")
        await db.delegation_row_set("vllm/Qwen3.6-35B-A3B-NVFP4", "long-context",
                                    accuracy=1.0, n=10, cost_per_1m_tokens=0.0,
                                    median_latency_s=12.0, max_context=229376)
        await db.delegation_row_set("claude-sonnet-5", "reviewer-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        # Stage 5's own task type since the 2026-09-17 gate split. Same model
        # and latency as the reviewer row, so every figure these tests were
        # written against still reproduces.
        await db.delegation_row_set("claude-sonnet-5", "security-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        # Spec 12's coverage rule (2026-09-17): an ordinary task type may not
        # route through a gate type that has not itself cleared 1.1.
        for gate_type in tiered_delegation.GATE_CALLS:
            await db.delegation_operational_set(gate_type, True)
        await delegation_routes.handle_operational_put(_request(body={
            "task_type": "long-context", "operational": True}))
        self.assertEqual(await _ordinary_operational(), {"long-context"})

    async def test_coding_can_now_be_flipped_operational(self):
        """`coding`'s hold was lifted by operator decision on 2026-09-18.

        This test asserted the opposite until then, and deliberately used the
        same complete row shape as the long-context "allowed" case so that a
        pass meant the GUARD was being tested rather than incomplete data.
        It now asserts the flip succeeds on that same shape -- so if the hold
        is ever reinstated, this fails loudly rather than silently passing for
        the wrong reason.
        """
        await db.delegation_row_set("claude-sonnet-5", "coding",
                                    accuracy=1.0, n=10, cost_per_1m_tokens=0.0,
                                    median_latency_s=12.0, max_context=229376)
        await db.delegation_row_set("claude-sonnet-5", "reviewer-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        await db.delegation_row_set("claude-sonnet-5", "security-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        for gate_type in tiered_delegation.GATE_CALLS:
            await db.delegation_operational_set(gate_type, True)
        await delegation_routes.handle_operational_put(_request(body={
            "task_type": "coding", "operational": True}))
        self.assertIn("coding", await db.delegation_operational_all())

    async def test_coding_is_not_in_the_policy_hold_map(self):
        """The hold is lifted in the map itself, not merely worked around."""
        self.assertNotIn("coding",
                         delegation_routes._OPERATIONAL_FLIP_BLOCKED)

    async def test_flipping_coding_off_is_not_blocked(self):
        """The guard is specifically about *flipping to* operational -- it
        must not reject `operational: false` for `coding`, which can never
        have been operational in this release anyway but must not become a
        special case that raises on the way out too."""
        await delegation_routes.handle_operational_put(_request(body={
            "task_type": "coding", "operational": False}))
        self.assertEqual(await _ordinary_operational(), set())

    async def test_coding_is_never_flipped_operational_by_this_module(self):
        """Spec 12 forbids flipping `coding` operational until the gate-type
        validation question is decided. Nothing in this route module may do
        it -- not as a default, not as a side effect of another call."""
        self.assertEqual(await _ordinary_operational(), set())
        await delegation_routes.handle_delegation_get(_request())
        self.assertEqual(await _ordinary_operational(), set())

    async def test_reasoning_can_now_be_flipped_operational(self):
        """Amendment b782e4d's hold was lifted 2026-09-18. It held `reasoning`
        "until its 75% n=2 accuracy figure is either re-measured or enforced
        in code" -- and it was re-measured, at n=6 for five models, so the
        hold was blocking on a condition already met.

        Same complete row shape the blocked version used, so a reinstated hold
        fails here loudly rather than passing for the wrong reason."""
        await db.delegation_row_set("claude-sonnet-5", "reasoning",
                                    accuracy=1.0, n=10, cost_per_1m_tokens=0.0,
                                    median_latency_s=12.0, max_context=229376)
        for gate in ("reviewer-gate", "security-gate"):
            await db.delegation_row_set("claude-sonnet-5", gate,
                                        accuracy=0.95, n=28,
                                        cost_per_1m_tokens=0.0,
                                        median_latency_s=5.0, max_context=0)
        for gate_type in tiered_delegation.GATE_CALLS:
            await db.delegation_operational_set(gate_type, True)
        await delegation_routes.handle_operational_put(_request(body={
            "task_type": "reasoning", "operational": True}))
        self.assertIn("reasoning", await db.delegation_operational_all())

    async def test_no_policy_hold_remains(self):
        """Both holds lifted, so the map is empty. Asserted directly so adding
        one is deliberate, and so an empty map is not mistaken for the
        mechanism having been removed."""
        self.assertEqual(dict(delegation_routes._OPERATIONAL_FLIP_BLOCKED), {})

    async def test_the_policy_channel_still_works_when_a_hold_exists(self):
        """The mechanism must keep working with no live hold to exercise it.

        Patched rather than real: every real hold is now lifted, so without
        this the policy path would be code that only a future decision
        exercises -- for the first time, in production."""
        await db.delegation_row_set("claude-sonnet-5", "planning", accuracy=1.0)
        with patch.dict(delegation_routes._OPERATIONAL_FLIP_BLOCKED,
                        {"planning": "held for a test"}, clear=False):
            body = json.loads(
                (await delegation_routes.handle_delegation_get(_request())).body)
            entry = body["blockers"]["planning"]
            self.assertIsNotNone(entry["policy"])
            self.assertIn("held for a test", entry["policy"])
            self.assertTrue(entry["policy"].startswith("planning: "))

    async def test_flipping_reasoning_off_is_not_blocked(self):
        """The guard is specifically about *flipping to* operational -- it
        must not reject `operational: false` for `reasoning`."""
        await delegation_routes.handle_operational_put(_request(body={
            "task_type": "reasoning", "operational": False}))
        self.assertEqual(await _ordinary_operational(), set())

    async def test_reasoning_is_never_flipped_operational_by_this_module(self):
        """Same statement as test_coding_is_never_flipped_operational_by_this_module,
        for the type spec amendment b782e4d added the hold for."""
        self.assertEqual(await _ordinary_operational(), set())
        await delegation_routes.handle_delegation_get(_request())
        self.assertEqual(await _ordinary_operational(), set())

    async def test_no_task_type_reports_a_policy_blocker(self):
        """Both holds lifted 2026-09-18. Asserted for BOTH previously-held
        types, so reinstating either fails here rather than silently changing
        what the page says."""
        await db.delegation_row_set("claude-sonnet-5", "coding", accuracy=1.0)
        await db.delegation_row_set("claude-sonnet-5", "reasoning", accuracy=1.0)
        body = json.loads(
            (await delegation_routes.handle_delegation_get(_request())).body)
        for task_type in ("coding", "reasoning"):
            with self.subTest(task_type=task_type):
                self.assertIsNone(body["blockers"][task_type]["policy"])

    async def test_get_computes_a_data_blocker_for_an_incomplete_non_operational_type(self):
        """`long-context` has no policy hold, but a row missing
        `median_latency_s` fails 1.1's "no blank fields" invariant the same
        way `test_flipping_a_type_operational_on_incomplete_data_is_refused`
        proves at the flip endpoint -- the GET response must surface the
        identical problem without the operator having to attempt the flip
        first."""
        await db.delegation_row_set("claude-sonnet-5", "long-context",
                                    accuracy=0.9, n=4, cost_per_1m_tokens=1.0,
                                    median_latency_s=None, max_context=1000)
        response = await delegation_routes.handle_delegation_get(_request())
        body = json.loads(response.body)
        entry = body["blockers"]["long-context"]
        self.assertIsNone(entry["policy"])
        self.assertTrue(entry["data"], "expected at least one data blocker")
        self.assertTrue(
            any("median_latency_s" in p for p in entry["data"]),
            entry["data"])
        self.assertTrue(all(p.startswith("long-context:") for p in entry["data"]))

    async def test_get_reports_no_blockers_for_an_operational_type(self):
        """A type that already cleared the flip must not show a stale or
        invented blocker -- `{"policy": None, "data": []}`, not an empty
        warning box rendered from leftover state."""
        await db.delegation_row_set("claude-sonnet-5", "long-context",
                                    accuracy=1.0, n=10, cost_per_1m_tokens=0.0,
                                    median_latency_s=12.0, max_context=229376)
        await db.delegation_row_set("claude-sonnet-5", "reviewer-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        # Stage 5's own task type since the 2026-09-17 gate split. Same model
        # and latency as the reviewer row, so every figure these tests were
        # written against still reproduces.
        await db.delegation_row_set("claude-sonnet-5", "security-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        # Spec 12's coverage rule (2026-09-17): an ordinary task type may not
        # route through a gate type that has not itself cleared 1.1.
        for gate_type in tiered_delegation.GATE_CALLS:
            await db.delegation_operational_set(gate_type, True)
        await delegation_routes.handle_operational_put(_request(body={
            "task_type": "long-context", "operational": True}))
        response = await delegation_routes.handle_delegation_get(_request())
        body = json.loads(response.body)
        self.assertEqual(body["blockers"]["long-context"],
                          {"policy": None, "data": [], "warnings": [],
                          "over_budget": False})

    async def test_get_reports_no_blockers_for_a_clean_non_operational_type(self):
        """A non-operational type with complete, valid data and no policy
        hold -- the same shape used for the "flip succeeds" fixture -- must
        also report cleanly even though it has not been flipped yet: a data
        blocker only exists when validate() would actually refuse the flip."""
        await db.delegation_row_set("claude-sonnet-5", "long-context",
                                    accuracy=1.0, n=10, cost_per_1m_tokens=0.0,
                                    median_latency_s=12.0, max_context=229376)
        await db.delegation_row_set("claude-sonnet-5", "reviewer-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        # Stage 5's own task type since the 2026-09-17 gate split. Same model
        # and latency as the reviewer row, so every figure these tests were
        # written against still reproduces.
        await db.delegation_row_set("claude-sonnet-5", "security-gate",
                                    accuracy=0.95, n=28,
                                    cost_per_1m_tokens=0.0,
                                    median_latency_s=5.0, max_context=0)
        # Spec 12's coverage rule (2026-09-17): an ordinary task type may not
        # route through a gate type that has not itself cleared 1.1.
        for gate_type in tiered_delegation.GATE_CALLS:
            await db.delegation_operational_set(gate_type, True)
        response = await delegation_routes.handle_delegation_get(_request())
        body = json.loads(response.body)
        self.assertEqual(body["blockers"]["long-context"],
                          {"policy": None, "data": [], "warnings": [],
                          "over_budget": False})


if __name__ == "__main__":
    unittest.main()
