"""QA: spec 9.2's enforcement knob for 5.1's combined latency ceiling, and
5.1's per-stage runtime check that the knob governs.

Two halves, and they fail in opposite directions, so both are asserted here:

  * The knob is OFF by default. A test suite that only ever exercised the
    blocking path would pass against an implementation that ignored the knob
    entirely and always blocked -- which is the behaviour that shipped before
    the knob existed.
  * "Off" must mean NOT BLOCKING, never NOT MEASURED. Spec 5.1 asks for the
    rate of ceiling cuts to be monitored (section 10), and that rate is
    exactly what is wanted while nothing is being stopped. A knob that
    suppressed the measurement along with the block would pass every "it
    loads" assertion and destroy the evidence needed to decide whether to
    turn it on.

The runtime half (`ceiling_decision`) is a pure function, like the rest of
delegation_pipeline: nothing runs leaves yet, so the decision is built ahead
of the stage loop that will call it -- the same order the gate rules were
built in.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import db
import delegation_pipeline as pipeline
import delegation_startup as ds
import tiered_delegation as td
from routes import delegation as delegation_routes
from routes.db_users import setting_set


class CeilingDefaultTests(unittest.TestCase):
    """The default is off, asserted directly rather than inferred."""

    def test_the_knob_is_off_by_default(self):
        self.assertFalse(td.CEILING_ENFORCEMENT_DEFAULT)

    def test_the_settings_key_is_stable(self):
        """The key is stored in the database, so renaming it silently turns
        every deployment's setting back to the default. Pinned here so a
        rename has to be a deliberate edit to this assertion."""
        self.assertEqual(td.CEILING_ENFORCEMENT_SETTING,
                         "delegation_enforce_latency_ceiling")

    def test_validate_defaults_to_not_enforcing(self):
        """`validate()` called with no knob argument must take the default,
        not the blocking behaviour. Belt and braces on the call sites that
        do not pass it."""
        table = _over_ceiling_table()
        self.assertFalse([p for p in table.validate() if "ceiling" in p])


def _over_ceiling_table():
    """One operational type whose worst-case path is far over the ceiling.

    Free rungs on both ends so the tree-cost invariant cannot be what fails:
    the only thing wrong with this table is latency.
    """
    rows = [
        td.CapabilityRow("vllm/fast", "widget", 0.90, 20, 0.0, 10.0, 1_000_000),
        td.CapabilityRow("vllm/slow", "widget", 0.90, 20, 0.0, 100.0, 1_000_000),
        td.CapabilityRow("azure_ai/gpt-5.6-luna", "reviewer-gate", None, None,
                         0.0285, 11.1, 922_000),
    ]
    return td.CapabilityTable(rows, operational={"widget"})


class CeilingDecisionTests(unittest.TestCase):
    """5.1's per-stage check: evaluated BEFORE a stage starts, never during."""

    def test_a_projection_under_the_ceiling_proceeds(self):
        d = pipeline.ceiling_decision(100.0, 90.0, enforce=True)
        self.assertTrue(d.proceed)
        self.assertFalse(d.exceeded)
        self.assertIsNone(d.signal)
        self.assertIsNone(d.reason)
        self.assertEqual(d.projected_total_s, 190.0)

    def test_a_projection_exactly_on_the_ceiling_still_runs(self):
        """5.1: the leaf stops when the projection "would exceed" the
        ceiling. Exactly on it does not exceed it, so the stage runs. A `>=`
        here would cut a leaf that fits, which is the failure 5.1's whole
        "never interrupt a stage in flight" argument is trying to avoid."""
        d = pipeline.ceiling_decision(
            td.LATENCY_CEILING_S - 90.0, 90.0, enforce=True)
        self.assertEqual(d.projected_total_s, float(td.LATENCY_CEILING_S))
        self.assertTrue(d.proceed)
        self.assertFalse(d.exceeded)

    def test_one_second_over_the_ceiling_stops_the_leaf_when_enforcing(self):
        d = pipeline.ceiling_decision(
            td.LATENCY_CEILING_S - 89.0, 90.0, enforce=True)
        self.assertFalse(d.proceed)
        self.assertTrue(d.exceeded)
        self.assertEqual(d.signal, pipeline.LATENCY_CEILING_EXHAUSTED)
        self.assertIn("ceiling", d.reason)

    def test_the_same_projection_proceeds_when_not_enforcing(self):
        """The knob's whole job, in one assertion pair: identical inputs,
        `exceeded` identical, `proceed` opposite."""
        args = (td.LATENCY_CEILING_S - 89.0, 90.0)
        off = pipeline.ceiling_decision(*args, enforce=False)
        on = pipeline.ceiling_decision(*args, enforce=True)
        self.assertEqual(off.exceeded, on.exceeded)
        self.assertEqual(off.projected_total_s, on.projected_total_s)
        self.assertEqual(off.reason, on.reason)
        self.assertTrue(off.proceed)
        self.assertFalse(on.proceed)

    def test_not_enforcing_still_records_the_breach(self):
        """"Off" means not blocking, never not measured. Without `exceeded`
        and `reason` surviving the off path there is nothing for section 10
        to monitor, and the decision to turn the knob on would be taken with
        no evidence."""
        d = pipeline.ceiling_decision(2_000.0, 90.0, enforce=False)
        self.assertTrue(d.proceed)
        self.assertTrue(d.exceeded)
        self.assertIsNotNone(d.reason)
        self.assertIn(str(td.LATENCY_CEILING_S), d.reason)

    def test_a_proceeding_leaf_never_carries_the_terminating_signal(self):
        """6.1's signal means the leaf STOPPED. Emitting it for a leaf that
        carries on would make the log say the opposite of what happened, and
        6.1 is explicit that this signal must stay distinguishable from a
        deadline expiry."""
        d = pipeline.ceiling_decision(2_000.0, 90.0, enforce=False)
        self.assertTrue(d.proceed)
        self.assertIsNone(d.signal)

    def test_the_signal_is_the_spec_string_and_not_a_leaf_failure(self):
        """6.1 lists this apart from the escalating signals and apart from
        the three terminal outcomes: a ceiling exhaustion is not a failed
        leaf, not a human flag, and not an escalation."""
        self.assertEqual(pipeline.LATENCY_CEILING_EXHAUSTED,
                         "latency_ceiling_exhausted")
        for other in (pipeline.LEAF_FAILED, pipeline.FAILED_HUMAN_FLAGGED,
                      pipeline.ESCALATED_TO_HUMAN):
            self.assertNotEqual(pipeline.LATENCY_CEILING_EXHAUSTED, other)

    def test_the_decision_defaults_to_not_enforcing(self):
        self.assertTrue(pipeline.ceiling_decision(2_000.0, 90.0).proceed)

    def test_a_custom_ceiling_is_honoured(self):
        """The ceiling is derived (5.1) and is recomputed whenever a ladder or
        a measured latency changes, so the check must take it as a parameter
        rather than reading one baked-in number."""
        self.assertFalse(
            pipeline.ceiling_decision(100.0, 50.0, enforce=True,
                                      ceiling_s=120.0).proceed)
        self.assertTrue(
            pipeline.ceiling_decision(100.0, 50.0, enforce=True,
                                      ceiling_s=200.0).proceed)

    def test_negative_durations_are_caller_errors(self):
        for elapsed, nxt in ((-1.0, 10.0), (10.0, -1.0)):
            with self.subTest(elapsed=elapsed, next=nxt):
                with self.assertRaises(ValueError):
                    pipeline.ceiling_decision(elapsed, nxt)


class CeilingEnforcementSettingTests(unittest.IsolatedAsyncioTestCase):
    """The stored setting, read through the one reader every caller uses."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def test_a_missing_row_is_the_default(self):
        self.assertEqual(await ds.ceiling_enforcement_enabled(),
                         td.CEILING_ENFORCEMENT_DEFAULT)

    async def test_one_turns_it_on(self):
        await setting_set(td.CEILING_ENFORCEMENT_SETTING, "1")
        self.assertTrue(await ds.ceiling_enforcement_enabled())

    async def test_zero_turns_it_off_again(self):
        await setting_set(td.CEILING_ENFORCEMENT_SETTING, "1")
        await setting_set(td.CEILING_ENFORCEMENT_SETTING, "0")
        self.assertFalse(await ds.ceiling_enforcement_enabled())

    async def test_a_malformed_value_falls_back_to_off_not_on(self):
        """A corrupt settings row must not be able to refuse startup. Falling
        back to ON would let one bad byte in the database stop the console
        from booting, which is strictly worse than not enforcing a limit
        nothing is currently routing against."""
        for raw in ("", "true", "yes", "01", "  ", "None"):
            with self.subTest(raw=raw):
                await setting_set(td.CEILING_ENFORCEMENT_SETTING, raw)
                self.assertFalse(await ds.ceiling_enforcement_enabled())

    async def test_surrounding_whitespace_is_tolerated(self):
        await setting_set(td.CEILING_ENFORCEMENT_SETTING, " 1 ")
        self.assertTrue(await ds.ceiling_enforcement_enabled())


class CeilingEnforcementEndpointTests(unittest.IsolatedAsyncioTestCase):
    """PUT /api/delegation/ceiling-enforcement."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    def _request(self, body=None):
        """Same shape as tests/test_qa_delegation_routes.py's `_request` --
        `_require_admin` reads `request.state.session`, not a bare attribute."""
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            query_params={},
        )
        if body is not None:
            request.json = AsyncMock(return_value=body)
        return request

    async def _put(self, body):
        return await delegation_routes.handle_ceiling_enforcement_put(
            self._request(body))

    async def test_turning_it_on_and_off_round_trips(self):
        response = await self._put({"enabled": True})
        self.assertEqual(json.loads(response.body)["enabled"], True)
        self.assertTrue(await ds.ceiling_enforcement_enabled())
        response = await self._put({"enabled": False})
        self.assertEqual(json.loads(response.body)["enabled"], False)
        self.assertFalse(await ds.ceiling_enforcement_enabled())

    async def test_a_missing_enabled_field_is_refused(self):
        """Not defaulted to False. A client that sent the wrong field name
        must not read as a successful request to turn enforcement off."""
        with self.assertRaises(Exception) as ctx:
            await self._put({})
        self.assertIn("enabled", str(ctx.exception))

    async def test_the_get_payload_reports_the_state(self):
        body = json.loads(
            (await delegation_routes.handle_delegation_get(
                self._request())).body)
        self.assertIn("ceiling_enforcement", body)
        self.assertEqual(body["ceiling_enforcement"]["enabled"], False)
        self.assertEqual(body["ceiling_enforcement"]["default"], False)
        self.assertEqual(body["ceiling_enforcement"]["setting"],
                         td.CEILING_ENFORCEMENT_SETTING)
        await self._put({"enabled": True})
        body = json.loads(
            (await delegation_routes.handle_delegation_get(
                self._request())).body)
        self.assertEqual(body["ceiling_enforcement"]["enabled"], True)

    async def test_turning_it_on_is_refused_when_that_would_break_the_boot(self):
        """Enabling enforcement can invalidate a type that is ALREADY
        operational. Storing the flag first would leave a deployment that
        refuses to start on its next restart -- a settings write that bricks
        the boot path. The refusal must also leave the flag unchanged, which
        is asserted separately: a handler that raised after writing would
        pass the first assertion and still brick the deployment.
        """
        await db.delegation_row_set(
            "vllm/fast", "widget", accuracy=0.9, n=20, cost_per_1m_tokens=0.0,
            median_latency_s=10.0, max_context=1_000_000)
        await db.delegation_row_set(
            "vllm/slow", "widget", accuracy=0.9, n=20, cost_per_1m_tokens=0.0,
            median_latency_s=400.0, max_context=1_000_000)
        await db.delegation_row_set(
            "azure_ai/gpt-5.6-luna", "reviewer-gate", accuracy=None, n=None,
            cost_per_1m_tokens=0.0285, median_latency_s=11.1,
            max_context=922_000)
        await db.delegation_operational_set("widget", True)

        with self.assertRaises(Exception) as ctx:
            await self._put({"enabled": True})
        self.assertIn("ceiling", str(ctx.exception))
        self.assertFalse(await ds.ceiling_enforcement_enabled())

    async def test_turning_it_off_is_never_validated(self):
        """Relaxing a blocking invariant cannot break another one, so the off
        path must work even on a table that could never satisfy the check.
        An implementation that validated both directions would trap an
        operator in the enforcing state with no way back."""
        await db.delegation_row_set(
            "vllm/slow", "widget", accuracy=0.9, n=20, cost_per_1m_tokens=0.0,
            median_latency_s=9_000.0, max_context=1_000_000)
        await db.delegation_row_set(
            "azure_ai/gpt-5.6-luna", "reviewer-gate", accuracy=None, n=None,
            cost_per_1m_tokens=0.0285, median_latency_s=11.1,
            max_context=922_000)
        await db.delegation_operational_set("widget", True)
        await setting_set(td.CEILING_ENFORCEMENT_SETTING, "1")

        response = await self._put({"enabled": False})
        self.assertEqual(json.loads(response.body)["enabled"], False)
        self.assertFalse(await ds.ceiling_enforcement_enabled())


if __name__ == "__main__":
    unittest.main()
