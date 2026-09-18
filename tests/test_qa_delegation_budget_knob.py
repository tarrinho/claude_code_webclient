"""QA: the budget invariant is enforceable, not unconditional.

Spec 1.1's "the ladder fits the budget" refused a flip outright whenever a
task type's expected tree cost exceeded `BUDGET_USD`. That made the check
unskippable, and asking for a cost-blocked type to become flippable -- which
is what was asked on 2026-09-17 for `comprehension` ($3.366 against $1.00) --
could only be satisfied by letting an operator store a state the startup check
then refuses. Measured against a copy of production on 2026-09-18:

    BOOT WOULD FAIL (DelegationConfigError):
      comprehension: its ladder's expected tree cost is $3.366, above
      BUDGET_USD ($1.00) -- rung claude-sonnet-5 is the costliest (spec 2.7)

`routes/delegation.py` already carries the scar of that shape, where
`long-context` "was accepted, and left a stored state that 1.1 refuses".

So the budget check gets the knob 5.1's latency ceiling already has, and for
the same class of reason. The latency ceiling is off by default because it is
derived from nothing while nothing is operational; the budget figure is off by
default because **one of its inputs is assumed rather than measured**:
`LEAVES_PER_TREE = 40` is `MAX_NODES` used as an upper bound, and
`tiered_delegation.py` says so outright -- "the one input on this list that is
assumed, never measured". Spec 1.2 explains it cannot be measured here, because
no orchestrator task has ever decomposed, so no tree has ever been formed.

That direction matters and is the whole argument. An over-estimated leaf count
inflates every tree cost, so the check refuses task types that would in fact
fit. A hard refusal computed from a deliberately conservative guess is the
"check that cries wolf" this codebase already warns about, and the answer it
already chose for the neighbouring invariant is a knob plus a visible warning,
never a silent drop.

What does NOT change, and is asserted below: the breach is still computed, still
reported, and still shown. Off means "does not block", never "not measured".
"""
from __future__ import annotations

import unittest

import tiered_delegation as td


def _row(model, task_type, accuracy=None, n=None, rate=0.0,
         latency=None, max_context=1_000_000):
    return td.CapabilityRow(
        model=model, task_type=task_type, accuracy=accuracy, n=n,
        cost_per_1m_tokens=rate, median_latency_s=latency,
        max_context=max_context,
    )


def _gates():
    """Both gate types, measured and operational-ready.

    Every non-gate type's validation requires them (spec 12, decided
    2026-09-17), so a fixture that omits them fails for a reason that has
    nothing to do with the budget.
    """
    return [
        _row(m, g, 0.95, 28, 0.0000, 5.0, 1_000_000)
        for g in sorted(td.GATE_CALLS) for m in ("claude-sonnet-5",)
    ]


def _over_budget_type(task_type="comprehension"):
    """One ladder-eligible rung dear enough to exceed BUDGET_USD.

    The rate is DERIVED from `td.BUDGET_USD` rather than written as a literal,
    and that is the whole point of the helper. It used to carry
    `claude-sonnet-5`'s real 1.5709/1M, chosen because it put `comprehension`
    at $3.366 against the $1.00 cap of the day. The cap then moved to $3.50 and
    again to $5.25, and at both of those $3.366 fits -- so the fixture stopped
    describing an over-budget ladder and these tests started asserting that an
    affordable ladder is affordable. The same trap caught
    `test_a_ladder_can_still_be_refused_on_cost` on 2026-09-18, which is why
    that test derives its rate too.

    `REACH_PROBABILITY[0]` is 1.0, so a single rung at rung 0 costs the full
    `LEAVES_PER_TREE * TOKENS_PER_LEAF` at this rate; 1.2x the budget puts it
    20% over whatever the budget currently is.
    """
    per_unit = (td.LEAVES_PER_TREE * td.TOKENS_PER_LEAF
                * td.REACH_PROBABILITY[0] / 1_000_000)
    return [_row("claude-sonnet-5", task_type, 1.00, 10,
                 td.BUDGET_USD / per_unit * 1.2, 12.0, 1_000_000)]


class BudgetEnforcementKnobTests(unittest.TestCase):

    def _table(self, task_type="comprehension"):
        return td.CapabilityTable(
            _over_budget_type(task_type) + _gates(),
            operational=set(sorted(td.GATE_CALLS)) | {task_type},
        )

    def test_the_default_is_not_to_block(self):
        """The property the request needs: a cost-blocked type can be flipped
        and the service still starts."""
        problems = self._table().validate({"claude-sonnet-5"})
        budget = [p for p in problems if "BUDGET_USD" in p]
        self.assertEqual(budget, [], f"budget blocked by default: {problems}")

    def test_enforcing_it_blocks_again(self):
        """The knob has to be a knob in both directions, or it is a deletion
        of the invariant wearing a setting's clothes."""
        problems = self._table().validate({"claude-sonnet-5"},
                                          enforce_budget=True)
        self.assertTrue([p for p in problems if "BUDGET_USD" in p], problems)

    def test_the_breach_is_still_reported_when_not_enforced(self):
        """Off must mean "does not block", never "not measured". This is what
        the settings page renders the dollar warning from, so if it went quiet
        the icon would have nothing to show and the operator would see an
        over-budget type as unremarkable."""
        breaches = self._table().budget_breaches()
        self.assertIn("comprehension", breaches)
        self.assertIn("BUDGET_USD", breaches["comprehension"])
        self.assertIn("claude-sonnet-5", breaches["comprehension"],
                      "spec 11 requires the costliest rung to be named")

    def test_a_type_within_budget_is_not_reported_as_a_breach(self):
        """The guard against an icon on every card."""
        cheap = td.CapabilityTable(
            [_row("azure_ai/gpt-5.6-luna", "planning", 1.00, 10, 0.0285, 12.0,
                  922_000)] + _gates(),
            operational=set(sorted(td.GATE_CALLS)) | {"planning"},
        )
        self.assertNotIn("planning", cheap.budget_breaches())
        self.assertEqual(
            [p for p in cheap.validate({"azure_ai/gpt-5.6-luna",
                                        "claude-sonnet-5"},
                                       enforce_budget=True)
             if "BUDGET_USD" in p], [])

    def test_an_incomputable_cost_still_blocks_whatever_the_knob_says(self):
        """The same distinction the latency knob draws: missing data and a
        breach are different failures, and only the breach is negotiable. An
        unpriced rung must never be read as an affordable one."""
        unpriced = td.CapabilityTable(
            [td.CapabilityRow(model="mystery/model", task_type="comprehension",
                              accuracy=1.0, n=10, cost_per_1m_tokens=None,
                              median_latency_s=12.0, max_context=1_000)]
            + _gates(),
            operational=set(sorted(td.GATE_CALLS)) | {"comprehension"},
        )
        problems = unpriced.validate({"mystery/model", "claude-sonnet-5"})
        self.assertTrue(problems, "an unpriced ladder must not validate clean")

    def test_breaches_only_cover_operational_types(self):
        """A non-operational type is exempt from 1.1 entirely, so surfacing it
        as a breach would put a warning icon on a card that is not being
        judged yet."""
        table = td.CapabilityTable(
            _over_budget_type() + _gates(),
            operational=set(sorted(td.GATE_CALLS)),   # comprehension NOT in it
        )
        self.assertNotIn("comprehension", table.budget_breaches())


if __name__ == "__main__":
    unittest.main()
