"""QA: the operator's actual path through Settings > Delegation, driven
end-to-end through the real route handlers against a real database.

Every other delegation test file sets a table up in the state it wants and
then asserts one property of it. None of them walks the SEQUENCE an operator
has to walk, and the sequence is where this design's behaviour actually lives:
spec 12's coverage rule (2026-09-17) means a task type cannot be flipped until
the gate types its stages 3-5 run on are themselves operational, so there is
now a required ORDER, and an order is not something a single-state fixture can
check.

The gap this closes is specific and was found by probing rather than by
reading. Constructing the sequence by hand immediately produced two things no
unit test had caught:

  * `_worst_case` was adding the gate terms to the gate types themselves,
    charging a gate for calling itself. `reviewer-gate` read 1,493s against a
    real 240s and `security-gate` 1,644s against 413s -- which is the only
    reason `security-gate` ever showed a ceiling warning. Fixed, and
    `test_a_gate_type_is_not_charged_for_calling_itself` holds it.
  * Turning a gate type OFF was not validated while turning one ON was, so a
    gate could be switched off under a live dependent, leaving a state that
    1.1 refuses at the next restart. Closed 2026-09-17; `GateTypeTeardownTests`
    now asserts the refusal, and that the rule stays satisfiable in both
    directions.

Everything here goes through `handle_operational_put` and
`handle_delegation_get` rather than calling `CapabilityTable` directly: a flip
that the validator would allow but the handler refuses (or vice versa) is
exactly the class of divergence these tests exist to catch, and it is
invisible from either side alone.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import db
import delegation_startup as ds
import tiered_delegation as td
from routes import delegation as delegation_routes


def _request(body=None, role="admin"):
    """Same shape as tests/test_qa_delegation_routes.py's helper --
    `_require_admin` reads `request.state.session`."""
    request = SimpleNamespace(
        state=SimpleNamespace(session={"user": "admin", "role": role}),
        query_params={},
    )
    if body is not None:
        request.json = AsyncMock(return_value=body)
    return request


class _FlowFixture(unittest.IsolatedAsyncioTestCase):
    """A throwaway database plus the helpers the flows below share."""

    #: The ordinary task type these flows drive. `coding` is deliberately NOT
    #: used: it carries a policy hold, so it could never complete a flip and
    #: every flow here would end in the same refusal for the wrong reason.
    TASK_TYPE = "long-context"
    MODEL = "vllm/Qwen3.6-35B-A3B-NVFP4"
    GATE_MODEL = "azure_ai/gpt-5.6-luna"

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
        await self._seed()

    async def _seed(self):
        """A complete, affordable, measured table for one ordinary type and
        both gate types -- the state an operator reaches after benchmarking,
        and the state from which the bootstrap sequence starts.

        Rows are seeded for BOTH gate types before any flip, because a gate
        type's own validation reads the other's rows for nothing now (see
        `test_a_gate_type_is_not_charged_for_calling_itself`) but a partial
        table would still make these flows fail for a data reason rather than
        an ordering one, and ordering is what they are about.
        """
        await db.delegation_row_set(
            self.MODEL, self.TASK_TYPE, accuracy=1.0, n=10,
            cost_per_1m_tokens=0.0, median_latency_s=12.0, max_context=229_376)
        for gate_type in td.GATE_CALLS:
            await db.delegation_row_set(
                self.GATE_MODEL, gate_type, accuracy=0.95, n=28,
                cost_per_1m_tokens=0.0285, median_latency_s=6.0,
                max_context=922_000)
        await self._serve(self.MODEL, self.GATE_MODEL)

    async def _serve(self, *model_ids):
        """Register a machine whose active list AND force-refreshed
        `models_list` name *model_ids*, so 1.1's model-resolution invariant
        sees them as live. Without this every rung fails resolution and the
        flows below would be testing the wrong refusal."""
        machine_id = "m-flow"
        await db.ai_machine_create(
            machine_id, "Flow Machine", "localhost", 0, None,
            model_ids[0], None, None, "tester", provider="claude_code")
        await db.ai_machine_set_models(machine_id, "tester", list(model_ids),
                                       model_ids[0])
        await db.ai_machine_set_models_list(
            machine_id, "tester",
            json.dumps([{"id": m} for m in model_ids]), db._now())

    async def _flip(self, task_type, operational=True):
        return await delegation_routes.handle_operational_put(
            _request({"task_type": task_type, "operational": operational}))

    async def _flip_fails(self, task_type, operational=True):
        """Attempt a flip that must be refused, returning the detail text."""
        with self.assertRaises(Exception) as ctx:
            await self._flip(task_type, operational)
        return str(getattr(ctx.exception, "detail", ctx.exception))

    async def _operational(self):
        return sorted(await db.delegation_operational_all())

    async def _blockers(self, task_type):
        body = json.loads(
            (await delegation_routes.handle_delegation_get(_request())).body)
        return body["blockers"][task_type]


class BootstrapOrderTests(_FlowFixture):
    """Spec 12's coverage rule, walked in the order an operator must walk it."""

    async def test_an_ordinary_type_cannot_be_flipped_before_its_gate_types(self):
        """Step 1 of the sequence, and the whole point of the rule: the data
        is complete and affordable, and the flip is still refused -- because
        the gate types its stages 3-5 run on have not cleared 1.1 themselves.
        """
        detail = await self._flip_fails(self.TASK_TYPE)
        for gate_type in td.GATE_CALLS:
            with self.subTest(gate_type=gate_type):
                self.assertIn(gate_type, detail)
        self.assertIn("not operational", detail)
        self.assertEqual(await self._operational(), [])

    async def test_flipping_one_gate_type_is_not_enough(self):
        """The half-finished state. An implementation that checked "some gate
        type is operational", or that was still written against a single
        `GATE_TASK_TYPE` constant, would let the flip through here -- and that
        is exactly the pre-split shape, so it is the regression most likely to
        be reintroduced."""
        await self._flip(td.REVIEWER_GATE_TASK_TYPE)
        detail = await self._flip_fails(self.TASK_TYPE)
        self.assertIn(td.SECURITY_GATE_TASK_TYPE, detail)
        self.assertNotIn(self.TASK_TYPE, await self._operational())

    async def test_the_full_sequence_succeeds(self):
        """The path end to end: both gates, then the ordinary type. This is
        the assertion that the rule is satisfiable at all -- a coverage rule
        nobody can get past is worse than no rule, and the exemption that
        makes it satisfiable (a gate does not run gates) is only visible from
        the sequence."""
        for gate_type in sorted(td.GATE_CALLS):
            await self._flip(gate_type)
        response = await self._flip(self.TASK_TYPE)
        self.assertTrue(json.loads(response.body)["ok"])
        self.assertEqual(
            await self._operational(),
            sorted({self.TASK_TYPE, *td.GATE_CALLS}))

    async def test_the_gate_types_can_be_flipped_in_either_order(self):
        """The rule must not impose an order BETWEEN the gates -- only
        between the gates and everything else. A check that required, say,
        the reviewer gate first would be an invented constraint."""
        for gate_type in sorted(td.GATE_CALLS, reverse=True):
            await self._flip(gate_type)
        await self._flip(self.TASK_TYPE)
        self.assertIn(self.TASK_TYPE, await self._operational())

    async def test_the_page_predicts_each_step_of_the_sequence(self):
        """9.2's blockers must agree with what a real flip does at every step,
        not only at the start. The page computes its blockers by running the
        same validation a flip runs; if the two ever diverged, an operator
        would be told a flip is available and then refused, or vice versa.
        """
        # before anything: blocked, and the reason names both gate types
        entry = await self._blockers(self.TASK_TYPE)
        self.assertTrue(entry["data"])
        for gate_type in td.GATE_CALLS:
            self.assertTrue(any(gate_type in d for d in entry["data"]),
                            entry["data"])

        # after one gate: still blocked, and now only the other is named
        await self._flip(td.REVIEWER_GATE_TASK_TYPE)
        entry = await self._blockers(self.TASK_TYPE)
        self.assertTrue(any(td.SECURITY_GATE_TASK_TYPE in d
                            for d in entry["data"]), entry["data"])
        self.assertFalse(any(td.REVIEWER_GATE_TASK_TYPE in d
                             for d in entry["data"]), entry["data"])

        # after both: clean, and the real flip agrees
        await self._flip(td.SECURITY_GATE_TASK_TYPE)
        entry = await self._blockers(self.TASK_TYPE)
        self.assertEqual(entry["data"], [])
        self.assertIsNone(entry["policy"])
        await self._flip(self.TASK_TYPE)
        self.assertIn(self.TASK_TYPE, await self._operational())

    async def test_a_refused_flip_writes_nothing(self):
        """A refusal must leave the stored state exactly as it was. A handler
        that stored first and validated second would pass every "it was
        refused" assertion and still corrupt the table."""
        before = await self._operational()
        await self._flip_fails(self.TASK_TYPE)
        self.assertEqual(await self._operational(), before)

    async def test_the_sequence_leaves_a_table_that_boots(self):
        """The end state has to survive a restart, which is the thing the
        whole 1.1 machinery exists for. `validate_or_die` is what runs at
        boot, and it is a different call path from the flip handler -- a rule
        enforced in one and not the other is how a deployment ends up unable
        to start after a settings change."""
        for gate_type in td.GATE_CALLS:
            await self._flip(gate_type)
        await self._flip(self.TASK_TYPE)
        table = await ds.validate_or_die()          # must not raise
        self.assertTrue(table.is_operational(self.TASK_TYPE))


class GateTypeTeardownTests(_FlowFixture):
    """The reverse direction, which the handler does NOT validate."""

    async def _fully_operational(self):
        for gate_type in td.GATE_CALLS:
            await self._flip(gate_type)
        await self._flip(self.TASK_TYPE)

    async def test_turning_a_gate_type_off_under_a_live_type_is_refused(self):
        """The hole this class documented until 2026-09-17, now closed.

        `handle_operational_put` validated only when flipping something ON, so
        turning a gate type OFF under a live dependent was accepted and left a
        stored state that 1.1 refuses -- the console kept running and then
        failed to start on its next restart, with nothing connecting the two
        events. Same trap the ceiling-enforcement endpoint already guarded
        against ("a settings write that bricks the boot path").

        Three assertions, and the third is the one that matters: refused, the
        flag unchanged, and the deployment still able to boot. A handler that
        stored first and raised afterwards would pass the first and brick the
        deployment anyway.
        """
        await self._fully_operational()
        detail = await self._flip_fails(td.REVIEWER_GATE_TASK_TYPE,
                                        operational=False)
        self.assertIn(self.TASK_TYPE, detail)
        self.assertIn(td.REVIEWER_GATE_TASK_TYPE, await self._operational())
        await ds.validate_or_die()                  # must still boot

    async def test_the_refusal_names_what_is_in_the_way(self):
        """A generic invariant failure would tell an operator nothing about
        what to do. The dependent type is named, because turning THAT off is
        the action that unblocks this one."""
        await self._fully_operational()
        detail = await self._flip_fails(td.REVIEWER_GATE_TASK_TYPE,
                                        operational=False)
        self.assertIn(self.TASK_TYPE, detail)
        self.assertIn("restart", detail)

    async def test_a_gate_type_can_be_turned_off_once_nothing_depends_on_it(self):
        """The rule must be satisfiable: turn the dependent off first and the
        gate type follows. A check that refused unconditionally would trap an
        operator in the operational state with no way back."""
        await self._fully_operational()
        await self._flip(self.TASK_TYPE, operational=False)
        await self._flip(td.REVIEWER_GATE_TASK_TYPE, operational=False)
        self.assertNotIn(td.REVIEWER_GATE_TASK_TYPE, await self._operational())
        await ds.validate_or_die()                  # must not raise

    async def test_a_gate_type_can_be_turned_off_when_only_gates_are_on(self):
        """Gate types do not depend on each other, so one may be turned off
        while the other stays on -- the exemption that keeps the rule from
        being unsatisfiable applies in this direction too."""
        for gate_type in td.GATE_CALLS:
            await self._flip(gate_type)
        await self._flip(td.REVIEWER_GATE_TASK_TYPE, operational=False)
        self.assertNotIn(td.REVIEWER_GATE_TASK_TYPE, await self._operational())


class GateSelfChargeTests(_FlowFixture):
    """The defect the flow probe found: a gate charged for calling itself."""

    async def test_a_gate_type_is_not_charged_for_calling_itself(self):
        """Stages 3-5 ARE the gates, so a gate leaf is its own one or two
        model calls and nothing more. `_worst_case` was adding the gate terms
        to every task type including the gate types, which charged each gate
        for a full set of gate calls on top of its own work.

        The check is structural rather than a fixed number: a gate type's
        worst case must be its generation rungs alone, so it cannot exceed
        what an ordinary type with the SAME rungs would cost -- an ordinary
        type pays for the gates as well.
        """
        for gate_type in td.GATE_CALLS:
            await self._flip(gate_type)
        table = await ds.validate_or_die()

        gate_worst = table.worst_case_path_s(td.REVIEWER_GATE_TASK_TYPE)
        self.assertIsNotNone(gate_worst)

        # The same single rung, under an ordinary task type, pays the gate
        # terms on top -- so it must come out strictly larger.
        await db.delegation_row_set(
            self.GATE_MODEL, "widget", accuracy=0.95, n=28,
            cost_per_1m_tokens=0.0285, median_latency_s=6.0,
            max_context=922_000)
        table = await ds.validate_or_die()
        ordinary_worst = table.worst_case_path_s("widget")
        self.assertIsNotNone(ordinary_worst)
        self.assertLess(gate_worst, ordinary_worst)

    async def test_a_gate_types_own_rows_are_what_time_it(self):
        """Changing the OTHER gate type's latency must not move this one.
        Before the fix it did, because both gate terms were summed into every
        type's path."""
        for gate_type in td.GATE_CALLS:
            await self._flip(gate_type)
        before = (await ds.validate_or_die()).worst_case_path_s(
            td.REVIEWER_GATE_TASK_TYPE)

        await db.delegation_row_set(
            self.GATE_MODEL, td.SECURITY_GATE_TASK_TYPE, accuracy=0.95, n=28,
            cost_per_1m_tokens=0.0285, median_latency_s=60.0,
            max_context=922_000)
        after = (await ds.validate_or_die()).worst_case_path_s(
            td.REVIEWER_GATE_TASK_TYPE)
        self.assertAlmostEqual(before, after, places=9)


class CeilingKnobFlowTests(_FlowFixture):
    """9.2's enforcement knob, driven against a live operational type."""

    async def test_enforcement_can_be_turned_on_when_everything_fits(self):
        for gate_type in td.GATE_CALLS:
            await self._flip(gate_type)
        await self._flip(self.TASK_TYPE)
        response = await delegation_routes.handle_ceiling_enforcement_put(
            _request({"enabled": True}))
        self.assertTrue(json.loads(response.body)["enabled"])
        await ds.validate_or_die()                  # must still boot

    async def test_enforcement_is_refused_when_a_live_type_is_over(self):
        """The interaction that matters: an operational type is over the
        ceiling, and turning enforcement on would make the deployment refuse
        to start. The endpoint must refuse the SETTING rather than accept it
        and break the next boot -- and the flip that put the type there was
        legitimate, because enforcement was off when it happened.
        """
        for gate_type in td.GATE_CALLS:
            await self._flip(gate_type)
        await self._flip(self.TASK_TYPE)
        # make the live type far too slow, through the same handler an
        # operator would use
        await delegation_routes.handle_row_put(_request({
            "model": self.MODEL, "task_type": self.TASK_TYPE,
            "median_latency_s": 4000.0,
        }))
        with self.assertRaises(Exception) as ctx:
            await delegation_routes.handle_ceiling_enforcement_put(
                _request({"enabled": True}))
        self.assertIn("ceiling", str(getattr(ctx.exception, "detail",
                                             ctx.exception)))
        self.assertFalse(await ds.ceiling_enforcement_enabled())
        await ds.validate_or_die()                  # and it still boots


if __name__ == "__main__":
    unittest.main()
