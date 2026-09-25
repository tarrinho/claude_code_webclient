"""QA: every prospective check builds the table boot will build.

A knob flip and a row write are validated for exactly one reason -- to
predict what ``validate_or_die`` will say at the next restart. Predicting it
with a differently-shaped table does not predict it, and the failure is
silent in the worst direction: a write is refused for a breach that boot
would never see, and the message names a ladder the router does not use.

That is what four call sites did until 2026-09-25. Both enforcement
endpoints, the row-write check and the settings page's blockers built
``CapabilityTable(rows, operational=...)`` with **no pins**, while
``validate_or_die`` built it with them and then pin-safed it. Measured on
production data that day the disagreement was total for one task type:
``multi-turn``'s generated ladder is 3,850s and its pinned ladder 2,675s
against a 3,400s ceiling, so the ceiling-enforcement knob refused a flip that
boot would have accepted.

``routes/delegation.py``'s own comment had asserted the invariant these tests
check for as long as it had been false: "if the startup check, this endpoint
and the seed script ever disagreed, a table would pass through one and be
refused by another, which is how an operator ends up with a deployment that
will not boot after its next restart."
"""
from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import config
import db
import delegation_startup as ds
from routes import delegation as delegation_routes
from tiered_delegation import LATENCY_CEILING_S


#: A leaf type whose GENERATED ladder breaches the ceiling and whose PINNED
#: ladder does not -- the shape that makes the two tables disagree. The slow
#: model is cheaper, so cost ordering puts it on the generated ladder; see
#: test_qa_delegation_ladder_latency.py for why nothing prevents that.
TASK = "multi-turn"
FAST_MODEL = "test/fast"
SLOW_CHEAP_MODEL = "test/slow-cheap"
GATE_MODEL = "test/gate"


class PinAgreementTests(unittest.IsolatedAsyncioTestCase):
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

        # Latencies chosen so the generated ladder clears the ceiling only
        # when the slow rung is pinned away. The exact seconds do not matter
        # to the assertions; the ORDER of the two worst cases does.
        await self._row(FAST_MODEL, TASK, accuracy=1.0, cost=1.0, latency=8.0)
        await self._row(SLOW_CHEAP_MODEL, TASK, accuracy=1.0, cost=0.5,
                        latency=120.0)
        for gate in ("reviewer-gate", "security-gate"):
            await self._row(GATE_MODEL, gate, accuracy=1.0, cost=0.5,
                            latency=5.0)

    async def _row(self, model, task_type, *, accuracy, cost, latency):
        await db.delegation_row_set(
            model, task_type, accuracy=accuracy, n=12,
            cost_per_1m_tokens=cost, median_latency_s=latency,
            max_context=200_000,
        )

    async def _worst(self, pins):
        rows = ds.rows_to_capability(await db.delegation_rows_all())
        table, _dropped = ds.boot_shaped_table(rows, [TASK], pins)
        return table.worst_case_path_s(TASK)

    async def test_the_fixture_really_does_split_the_two_tables(self):
        """Guards the fixture. Without this the tests below can pass
        vacuously, because a pin that changes nothing agrees with everything.
        """
        generated = await self._worst({})
        pinned = await self._worst({TASK: [FAST_MODEL]})
        self.assertIsNotNone(generated)
        self.assertIsNotNone(pinned)
        self.assertGreater(generated, LATENCY_CEILING_S,
                           "the generated ladder is meant to breach")
        self.assertLess(pinned, LATENCY_CEILING_S,
                        "the pinned ladder is meant to clear")

    async def test_boot_validation_sees_the_pin(self):
        """The reference behaviour the others have to match.

        The gate types are flipped operational and the model list is stubbed
        because `validate_or_die` checks all six of 1.1's invariants, not just
        the ceiling -- a leaf may not route through a gate type that has not
        itself cleared them (spec 12), and every rung must appear in the live
        combo box (spec 9.3). Neither has anything to do with pins; without
        them this fails for three reasons that are not the one under test.
        """
        for task_type in (TASK, "reviewer-gate", "security-gate"):
            await db.delegation_operational_set(task_type, True)
        await db.delegation_pin_set(TASK, [FAST_MODEL])
        models = frozenset({FAST_MODEL, SLOW_CHEAP_MODEL, GATE_MODEL})
        # Plain values, not coroutines: `patch.object` detects an `async def`
        # target and substitutes an AsyncMock, which awaits to `return_value`
        # as-is. Handing it a coroutine makes the await yield the coroutine
        # object itself.
        with patch.object(ds, "live_known_models", return_value=models), \
                patch.object(ds, "ceiling_enforcement_enabled",
                             return_value=True):
            table = await ds.validate_or_die()
        self.assertEqual(table.ladder(TASK), [FAST_MODEL])

    async def test_the_prospective_table_matches_boot(self):
        """Same builder, so this is a wiring check rather than a claim about
        two implementations agreeing -- which is the point of there being one
        builder.
        """
        await db.delegation_operational_set(TASK, True)
        await db.delegation_pin_set(TASK, [FAST_MODEL])
        table, _dropped = await ds.prospective_table()
        self.assertEqual(table.ladder(TASK), [FAST_MODEL])
        self.assertLess(table.worst_case_path_s(TASK), LATENCY_CEILING_S)

    async def test_a_pin_that_clears_the_ceiling_is_honoured(self):
        """The production case, reduced. Before the fix the prospective table
        reported the generated ladder's breach and the flip was refused."""
        await db.delegation_operational_set(TASK, True)
        await db.delegation_pin_set(TASK, [FAST_MODEL])
        table, _dropped = await ds.prospective_table()
        problems = table.validate(known_models=None,
                                  enforce_latency_ceiling=True)
        ceiling_problems = [p for p in problems if "latency ceiling" in p]
        self.assertEqual(ceiling_problems, [],
                         "the pinned ladder clears the ceiling; "
                         "validation must not report the generated one")

    async def test_a_pin_that_would_add_a_problem_is_dropped(self):
        """The safety half, and the reason a pin can be trusted here at all.

        `without_unusable_pins` drops any pin whose problems are not a subset
        of the same type's problems without it, so honouring pins cannot be
        used to pin a breach into acceptance -- it can only ever remove one.
        """
        await db.delegation_operational_set(TASK, True)
        await db.delegation_pin_set(TASK, [SLOW_CHEAP_MODEL])
        table, dropped = await ds.prospective_table()
        self.assertTrue(dropped, "a breaching pin should have been dropped")
        self.assertNotEqual(table.ladder(TASK), [SLOW_CHEAP_MODEL])

    def _admin_request(self, body):
        """`_require_admin` reads `request.state.session`, not an attribute."""
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            query_params={},
        )
        request.json = AsyncMock(return_value=body)
        return request

    async def test_the_ceiling_knob_endpoint_honours_the_pin(self):
        """The wiring, not the builder.

        The builder tests above pass whether or not the ENDPOINT calls it --
        verified by reverting the endpoint and watching them stay green. This
        one fails, which is the only reason it earns a place next to them.
        """
        for task_type in (TASK, "reviewer-gate", "security-gate"):
            await db.delegation_operational_set(task_type, True)
        await db.delegation_pin_set(TASK, [FAST_MODEL])
        models = frozenset({FAST_MODEL, SLOW_CHEAP_MODEL, GATE_MODEL})
        with patch.object(delegation_routes, "live_known_models",
                          return_value=models):
            response = await delegation_routes.handle_ceiling_enforcement_put(
                self._admin_request({"enabled": True}))
        self.assertEqual(json.loads(response.body)["enabled"], True)
        self.assertTrue(await ds.ceiling_enforcement_enabled())

    async def test_the_ceiling_knob_endpoint_still_refuses_a_real_breach(self):
        """The other direction, so the fix cannot be read as "stop checking".

        No pin, so the breaching generated ladder is what both boot and this
        endpoint see, and the flip must be refused.
        """
        for task_type in (TASK, "reviewer-gate", "security-gate"):
            await db.delegation_operational_set(task_type, True)
        models = frozenset({FAST_MODEL, SLOW_CHEAP_MODEL, GATE_MODEL})
        with patch.object(delegation_routes, "live_known_models",
                          return_value=models):
            with self.assertRaises(HTTPException) as caught:
                await delegation_routes.handle_ceiling_enforcement_put(
                    self._admin_request({"enabled": True}))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("latency ceiling", caught.exception.detail)
        self.assertFalse(await ds.ceiling_enforcement_enabled())

    async def test_no_pin_still_validates_the_generated_ladder(self):
        """Nothing about this change relaxes the unpinned case."""
        await db.delegation_operational_set(TASK, True)
        table, _dropped = await ds.prospective_table()
        problems = table.validate(known_models=None,
                                  enforce_latency_ceiling=True)
        self.assertTrue([p for p in problems if "latency ceiling" in p],
                        "an unpinned breaching ladder must still be reported")



if __name__ == "__main__":
    unittest.main()
