"""QA: spec 9.1's one global kill switch, and above all its OFF path.

9.1 does not merely permit an off switch, it specifies the testing:

    "Rollback must be clean. Flipping back to off is one action leaving no side
    effects... The off path is tested and confirmed clean before the switch is
    ever turned on in production."

So the centre of this file is `OffPathIsCleanTests`: with the switch off the
design writes nothing, validates nothing, and -- the assertion that matters
most -- a capability table that WOULD refuse the boot no longer does. A switch
that leaves the startup check running is not an off switch for the failure this
subsystem actually has.

The polarity is the other half. This switch defaults ON, the inverse of the two
enforcement knobs, so its reader takes `!= "0"` where theirs take `== "1"`.
Copying the wrong shape would disable delegation on every deployment whose
settings row is absent, which is every deployment until somebody touches it.
That is silent, so it is asserted directly rather than left to the reader.
"""
from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import db
import delegation_startup as ds
import orchestrator
import routes.delegation as dr
import tiered_delegation as td
from routes.db_users import setting_set


def _request(body=None, role="admin"):
    request = SimpleNamespace(
        state=SimpleNamespace(session={"user": "admin", "role": role}),
        query_params={},
    )
    if body is not None:
        request.json = AsyncMock(return_value=body)
    return request


class _SwitchCase(unittest.IsolatedAsyncioTestCase):
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
        p = patch.object(ds, "live_known_models", self._no_live_models)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    async def _no_live_models():
        return None

    async def _seed_unbootable(self):
        """An operational task type whose worst-case path cannot be computed.

        Missing `median_latency_s` is MISSING DATA, not a breach, so
        `validate()` reports it unconditionally -- no enforcement knob gates
        it. That makes this the sharpest possible test of the kill switch: a
        table that refuses the boot no matter how every other knob is set.
        """
        for gate in sorted(td.GATE_CALLS):
            await db.delegation_row_set(
                "claude-sonnet-5", gate, accuracy=0.95, n=28,
                cost_per_1m_tokens=0.0, median_latency_s=5.0,
                max_context=1_000_000)
            await db.delegation_operational_set(gate, True)
        await db.delegation_row_set(
            "claude-sonnet-5", "widget", accuracy=1.0, n=10,
            cost_per_1m_tokens=0.0, median_latency_s=None, max_context=1_000_000)
        await db.delegation_operational_set("widget", True)


class PolarityTests(_SwitchCase):
    """Default ON, and only an explicit "0" turns it off."""

    async def test_an_absent_row_means_on(self):
        self.assertTrue(await ds.delegation_enabled())
        self.assertTrue(td.DELEGATION_ENABLED_DEFAULT)

    async def test_an_explicit_zero_means_off(self):
        await setting_set(td.DELEGATION_ENABLED_SETTING, "0")
        self.assertFalse(await ds.delegation_enabled())

    async def test_one_means_on(self):
        await setting_set(td.DELEGATION_ENABLED_SETTING, "1")
        self.assertTrue(await ds.delegation_enabled())

    async def test_an_unparseable_value_means_on_not_off(self):
        """The inverse of the enforcement knobs, and deliberately so: a corrupt
        settings row must not be able to silently disable a subsystem. There it
        must not be able to silently START blocking; here it must not be able
        to silently STOP recording."""
        for junk in ("", "  ", "yes", "true", "off", "None"):
            with self.subTest(value=junk):
                await setting_set(td.DELEGATION_ENABLED_SETTING, junk)
                self.assertTrue(await ds.delegation_enabled(),
                                f"{junk!r} disabled delegation")

    async def test_its_polarity_is_the_opposite_of_the_enforcement_knobs(self):
        """Asserted as a relationship, not two separate facts, because the
        risk is somebody copying one shape onto the other."""
        self.assertTrue(td.DELEGATION_ENABLED_DEFAULT)
        self.assertFalse(td.CEILING_ENFORCEMENT_DEFAULT)
        self.assertFalse(td.BUDGET_ENFORCEMENT_DEFAULT)
        await setting_set(td.DELEGATION_ENABLED_SETTING, "junk")
        await setting_set(td.CEILING_ENFORCEMENT_SETTING, "junk")
        self.assertTrue(await ds.delegation_enabled())
        self.assertFalse(await ds.ceiling_enforcement_enabled())


class OffPathIsCleanTests(_SwitchCase):
    """9.1: "the off path is tested and confirmed clean"."""

    async def test_a_table_that_would_refuse_the_boot_does_refuse_it_when_on(self):
        """The control. Without this, the test below passes against a fixture
        that was never unbootable in the first place."""
        await self._seed_unbootable()
        with self.assertRaises(ds.DelegationConfigError):
            await ds.validate_or_die()

    async def test_startup_skips_validation_entirely_when_off(self):
        """The assertion 9.1 is really asking for: off means a broken table
        cannot stop the console starting."""
        await self._seed_unbootable()
        await setting_set(td.DELEGATION_ENABLED_SETTING, "0")
        called = []

        async def _spy():
            called.append(True)
            raise AssertionError("validate_or_die ran with delegation off")

        with patch.object(ds, "validate_or_die", _spy):
            if await ds.delegation_enabled():
                await ds.validate_or_die()
        self.assertEqual(called, [], "validate_or_die ran with delegation off")

    async def test_the_recorder_writes_nothing_when_off(self):
        await setting_set(td.DELEGATION_ENABLED_SETTING, "0")
        await db.orchestrator_create("orch-1234abcd", "T", None, "tester")
        engine = orchestrator.OrchestratorEngine("orch-1234abcd", "tester")
        await engine._materialise_plan([
            orchestrator.ParsedTask(id="t001", title="Implement the parser"),
            orchestrator.ParsedTask(id="t002", title="Summarise the document"),
        ])
        self.assertEqual(await db.delegation_decisions_recent(), [])

    async def test_the_tasks_are_still_created_when_off(self):
        """Off disables the DESIGN, not the orchestrator. A kill switch that
        also stopped task creation would be a far worse failure than the one it
        exists to prevent."""
        await setting_set(td.DELEGATION_ENABLED_SETTING, "0")
        await db.orchestrator_create("orch-1234abcd", "T", None, "tester")
        engine = orchestrator.OrchestratorEngine("orch-1234abcd", "tester")
        await engine._materialise_plan(
            [orchestrator.ParsedTask(id="t001", title="Implement the parser")])
        rows = await db.orchestrator_tasks_get("orch-1234abcd", "tester")
        self.assertEqual(len(rows), 1)

    async def test_turning_it_back_on_restores_recording(self):
        """"Rollback must be clean" runs both ways: off then on must leave the
        design working, not half-initialised."""
        await setting_set(td.DELEGATION_ENABLED_SETTING, "0")
        await db.orchestrator_create("orch-1234abcd", "T", None, "tester")
        engine = orchestrator.OrchestratorEngine("orch-1234abcd", "tester")
        await engine._materialise_plan(
            [orchestrator.ParsedTask(id="t001", title="Implement the parser")])
        self.assertEqual(await db.delegation_decisions_recent(), [])

        await setting_set(td.DELEGATION_ENABLED_SETTING, "1")
        await engine._materialise_plan(
            [orchestrator.ParsedTask(id="t002", title="Implement the lexer")])
        self.assertEqual(len(await db.delegation_decisions_recent()), 1)

    async def test_off_leaves_no_partial_state_behind(self):
        """9.1: "no leaf stuck mid-pipeline, no orphaned sub-agent, no dangling
        config". The only durable state this design writes today is the
        decision table, so "clean" means exactly that it is untouched."""
        await setting_set(td.DELEGATION_ENABLED_SETTING, "0")
        before = await db.delegation_decisions_recent()
        await db.orchestrator_create("orch-1234abcd", "T", None, "tester")
        engine = orchestrator.OrchestratorEngine("orch-1234abcd", "tester")
        await engine._materialise_plan(
            [orchestrator.ParsedTask(id="t001", title="Implement it")])
        self.assertEqual(await db.delegation_decisions_recent(), before)


class EndpointTests(_SwitchCase):
    async def test_turning_off_is_never_refused(self):
        """Even against a table that fails 1.1. An off switch that can be
        refused is not an off switch."""
        await self._seed_unbootable()
        response = await dr.handle_enabled_put(_request({"enabled": False}))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(await ds.delegation_enabled())

    async def test_turning_on_against_a_broken_table_is_refused(self):
        """Storing the flag first would leave a deployment that refuses to
        start at the next restart, with nothing connecting it to this click."""
        await self._seed_unbootable()
        await setting_set(td.DELEGATION_ENABLED_SETTING, "0")
        with self.assertRaises(dr.HTTPException) as caught:
            await dr.handle_enabled_put(_request({"enabled": True}))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("refuse to start", caught.exception.detail)
        self.assertFalse(await ds.delegation_enabled(),
                         "the flag was stored despite the refusal")

    async def test_turning_on_against_a_sound_table_succeeds(self):
        """The inverse, so the refusal above cannot pass by the endpoint being
        broken outright."""
        await setting_set(td.DELEGATION_ENABLED_SETTING, "0")
        response = await dr.handle_enabled_put(_request({"enabled": True}))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(await ds.delegation_enabled())

    async def test_a_missing_enabled_field_is_400(self):
        with self.assertRaises(dr.HTTPException) as caught:
            await dr.handle_enabled_put(_request({}))
        self.assertEqual(caught.exception.status_code, 400)

    async def test_a_non_admin_is_refused(self):
        with self.assertRaises(dr.HTTPException) as caught:
            await dr.handle_enabled_put(_request({"enabled": False}, role="user"))
        self.assertEqual(caught.exception.status_code, 403)

    async def test_the_payload_reports_the_live_state(self):
        await setting_set(td.DELEGATION_ENABLED_SETTING, "0")
        body = await _payload()
        self.assertIn("enabled", body)
        self.assertFalse(body["enabled"]["enabled"])
        self.assertTrue(body["enabled"]["default"])
        self.assertEqual(body["enabled"]["setting"],
                         td.DELEGATION_ENABLED_SETTING)

    async def test_the_config_block_no_longer_claims_it_is_unimplemented(self):
        """`_config_overview` said "not implemented in this release". A stale
        note that contradicts a shipped feature is how a reader concludes the
        switch does not exist and adds a second one."""
        body = await _payload()
        kill = body["config"]["kill_switch"]
        self.assertTrue(kill["available"])
        self.assertNotIn("not implemented", kill["note"])
        self.assertEqual(kill["setting"], td.DELEGATION_ENABLED_SETTING)


async def _payload():
    import json
    response = await dr.handle_delegation_get(_request())
    return json.loads(response.body.decode())


if __name__ == "__main__":
    unittest.main()
