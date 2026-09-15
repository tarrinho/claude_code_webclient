"""QA: the capability table and the ladder generator.

Stage 1 of docs/superpowers/specs/2026-09-14-tiered-agent-delegation-spec-v3.md
-- sections 1.1 (startup validation), 2.6 (the benchmark table and ladder
eligibility), 2.7 (the cost ceiling) and 3 (ladder generation).

Deliberately pure. `tiered_delegation` reads no database, spawns no turn and
imports nothing from `app`, so every property below is exercised against a
table built in the test rather than against production data. That matters here
more than usual: section 2.6 ships mostly unmeasured, so a test that leaned on
the real table would be asserting today's measurement state and would start
failing the moment a benchmark lands.

What this stage does NOT do, so nobody reads more into a passing suite than is
there: no task type is flipped operational, nothing is routed, and
`ModelRouter.assign_model` is untouched. Section 12 of the spec carries three
open items, each with an explicit "do not", and the third forbids making
`coding` operational until gate-type validation is decided. This module is the
machinery those decisions will be applied to, not the decisions.
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


# The three coding rows that section 3.1 measured on 2026-09-15, which the spec
# says must generate `vllm -> luna -> sonnet` exactly.
CODING_MEASURED = [
    _row("vllm/Qwen3.6-35B-A3B-NVFP4", "coding", 0.66, 44, 0.0000, 26.8, 229_376),
    _row("azure_ai/gpt-5.6-luna", "coding", 1.00, 24, 0.0285, 12.8, 922_000),
    _row("claude-sonnet-5", "coding", 1.00, 24, 1.5709, 15.5, 1_000_000),
    # Present, priced, and never a rung: the backend answers 429 for it, so its
    # accuracy cannot be measured from this deployment (spec 3.1).
    _row("azure_ai/gpt-5.4-mini", "coding", None, None, 0.5261, None, 1_050_000),
]


class LadderEligibilityTests(unittest.TestCase):
    """Spec 2.6: one predicate, three conditions, used everywhere."""

    def test_a_measured_affordable_row_is_eligible(self):
        table = td.CapabilityTable(CODING_MEASURED)
        eligible = {r.model for r in table.ladder_eligible("coding")}
        self.assertIn("vllm/Qwen3.6-35B-A3B-NVFP4", eligible)
        self.assertIn("azure_ai/gpt-5.6-luna", eligible)
        self.assertIn("claude-sonnet-5", eligible)

    def test_an_unmeasured_row_is_not_a_candidate(self):
        """The spec is emphatic: TBD is not 'measured worse', it is not
        measured. Treating it as pass-through would let cheapest-first put an
        unmeasured model at rung 0."""
        table = td.CapabilityTable([
            _row("cheap/unmeasured", "planning", None, None, 0.0),
            _row("dear/measured", "planning", 0.9, 10, 9.9),
        ])
        self.assertEqual([r.model for r in table.ladder_eligible("planning")],
                         ["dear/measured"])

    def test_a_model_with_no_row_is_not_a_candidate(self):
        """This is the mechanism behind 'only coding and long-context start
        free' -- the free model holds rows for exactly those two types. It is
        not a separate rule."""
        table = td.CapabilityTable(CODING_MEASURED)
        self.assertEqual(table.ladder_eligible("planning"), [])

    def test_the_cost_ceiling_exclusion_is_a_decision_not_arithmetic(self):
        """Spec 2.7: mini is cheaper per token than sonnet, which the ladders
        keep, so no threshold derived from the numbers would exclude it. It is
        excluded by an operator decision recorded on 2026-09-15, and a
        cost-derived exclusion would reverse itself the moment rates moved."""
        table = td.CapabilityTable(CODING_MEASURED)
        self.assertNotIn("azure_ai/gpt-5.4-mini",
                         {r.model for r in table.ladder_eligible("coding")})
        # And it stays out even when measured, which is what makes it a
        # decision rather than a consequence of its TBD accuracy.
        measured_mini = td.CapabilityTable([
            _row("azure_ai/gpt-5.4-mini", "coding", 1.00, 99, 0.5261, 1.0),
        ])
        self.assertEqual(measured_mini.ladder_eligible("coding"), [])

    def test_exemption_and_ineligibility_are_the_same_fact(self):
        """Spec 1.1 states it twice on purpose: a row exempt from the
        completeness check is exactly a row excluded from ladder generation.
        If these ever diverge, an incomplete row could become a rung."""
        table = td.CapabilityTable(CODING_MEASURED)
        eligible = {r.model for r in table.ladder_eligible("coding")}
        exempt = {r.model for r in table.rows_for("coding")
                  if not table.is_ladder_eligible(r)}
        self.assertEqual(eligible & exempt, set())
        self.assertEqual(eligible | exempt,
                         {r.model for r in table.rows_for("coding")})


class LadderGenerationTests(unittest.TestCase):
    """Spec 3: eligible, then cheapest-first, then skip measured-worse."""

    def test_the_measured_coding_rows_generate_the_documented_ladder(self):
        """The spec's own worked example. Section 3.1: 'Walking the
        generator's rule over these rows gives vllm -> luna -> sonnet exactly
        as the snapshot shows.'"""
        table = td.CapabilityTable(CODING_MEASURED)
        self.assertEqual(
            table.ladder("coding"),
            ["vllm/Qwen3.6-35B-A3B-NVFP4", "azure_ai/gpt-5.6-luna",
             "claude-sonnet-5"],
        )

    def test_equal_accuracy_is_not_worse(self):
        """Named in 3.1 as the reason sonnet survives at rung 2: luna and
        sonnet both measure 100% on coding. A `<=` here would silently drop
        every tied rung and shorten the ladder."""
        table = td.CapabilityTable([
            _row("a/cheap", "x", 0.80, 10, 0.1),
            _row("b/dearer", "x", 0.80, 10, 0.2),
        ])
        self.assertEqual(table.ladder("x"), ["a/cheap", "b/dearer"])

    def test_a_dearer_but_worse_model_is_skipped(self):
        table = td.CapabilityTable([
            _row("a/cheap", "x", 0.80, 10, 0.1),
            _row("b/dearer-worse", "x", 0.50, 10, 0.2),
            _row("c/dearest-better", "x", 0.95, 10, 0.9),
        ])
        self.assertEqual(table.ladder("x"), ["a/cheap", "c/dearest-better"])

    def test_the_comparison_is_against_the_current_rung_not_the_best_so_far(self):
        """These differ once a model is skipped. Walking against 'best seen'
        would let a later model that beats the last *accepted* rung but not the
        best skipped one be dropped -- the ladder would depend on models it
        rejected."""
        table = td.CapabilityTable([
            _row("a", "x", 0.60, 10, 0.1),
            _row("b", "x", 0.90, 10, 0.2),
            _row("c", "x", 0.95, 10, 0.3),
        ])
        self.assertEqual(table.ladder("x"), ["a", "b", "c"])

    def test_an_all_tbd_task_type_has_an_empty_ladder(self):
        """Spec 2.6 states the consequence plainly, and calls it the intended
        bootstrap state rather than a defect."""
        table = td.CapabilityTable([
            _row("a", "voice", None, None, 0.1),
            _row("b", "voice", None, None, 0.2),
        ])
        self.assertEqual(table.ladder("voice"), [])


class EffectiveCostTests(unittest.TestCase):
    """Spec 2.7: per token, normalised to the task type's expected size."""

    def test_cost_is_a_rate_times_a_size_never_a_per_request_figure(self):
        table = td.CapabilityTable(CODING_MEASURED)
        cost = table.effective_cost_per_task("claude-sonnet-5", "coding")
        expected = 1.5709 * td.expected_tokens("coding") / 1_000_000
        self.assertAlmostEqual(cost, expected, places=9)

    def test_ladder_order_does_not_depend_on_expected_tokens(self):
        """The spec never fixes a number for expected_tokens(task_type). That
        is an ambiguity, and this is the resolution: it is a per-task-type
        constant, so it scales every model's cost by the same factor and
        cannot reorder them. It therefore matters for the budget invariants
        and not at all for the generator.

        Pinned as a property rather than assumed, because if expected_tokens
        ever became model-dependent this silently stops holding -- and that is
        precisely the per-request unit error 2.7 exists to correct, where a
        size that varied by model made mini look dearer than sonnet.
        """
        table = td.CapabilityTable(CODING_MEASURED)
        baseline = table.ladder("coding")
        for size in (1, 500, 12_000, 91_696, 10_000_000):
            with self.subTest(expected_tokens=size):
                self.assertEqual(
                    table.ladder("coding", expected_tokens=size), baseline)

    def test_an_unpriced_model_is_ineligible_not_free(self):
        """Spec 2.7: 'still ineligible, still not estimated into eligibility,
        and still not treated as free'. Zero is a real rate for the self-hosted
        model, so None has to mean unpriced and must not collapse to 0.0."""
        table = td.CapabilityTable([
            _row("free/self-hosted", "x", 0.5, 10, 0.0),
            td.CapabilityRow(model="unpriced/x", task_type="x", accuracy=0.99,
                             n=10, cost_per_1m_tokens=None,
                             median_latency_s=1.0, max_context=1000),
        ])
        self.assertEqual(table.ladder("x"), ["free/self-hosted"])


class OperationalFlagTests(unittest.TestCase):
    """Spec 1.1: the bootstrap exemption, and what it is scoped to."""

    def test_task_types_default_to_non_operational(self):
        table = td.CapabilityTable(CODING_MEASURED)
        self.assertFalse(table.is_operational("coding"))

    def test_a_non_operational_type_may_hold_tbd_in_any_column(self):
        """Without this the system could never start for the first time."""
        table = td.CapabilityTable([_row("a", "voice", None, None, 0.1)])
        self.assertEqual(table.validate(), [])

    def test_completeness_is_scoped_to_ladder_eligible_rows(self):
        """Decided 2026-09-15. mini's coding row is incomplete and cannot be
        measured from this deployment; under the broad rule coding could never
        go operational, blocked by a model that is not a rung and cannot
        become one."""
        table = td.CapabilityTable(CODING_MEASURED, operational={"coding"})
        self.assertEqual(table.validate(), [])

    def test_an_incomplete_eligible_row_fails_validation(self):
        """The guard that must survive the scoping: a row that *is* a rung has
        to be complete, or a deadline is derived from a missing latency."""
        rows = list(CODING_MEASURED)
        rows[1] = _row("azure_ai/gpt-5.6-luna", "coding", 1.00, 24, 0.0285,
                       None, 922_000)          # measured, eligible, no latency
        table = td.CapabilityTable(rows, operational={"coding"})
        problems = table.validate()
        self.assertTrue(problems)
        joined = " ".join(problems)
        self.assertIn("coding", joined)
        self.assertIn("median_latency_s", joined,
                      "the error must name the column, per spec 1.1")

    def test_an_operational_type_with_an_empty_ladder_fails(self):
        table = td.CapabilityTable(
            [_row("a", "voice", None, None, 0.1)], operational={"voice"})
        self.assertTrue(any("ladder" in p for p in table.validate()))

    def test_validation_only_considers_operational_types(self):
        """Editing a non-operational type's rows stays free -- that is the
        bootstrap path, and gating it would make the table impossible to fill
        in."""
        rows = CODING_MEASURED + [_row("b", "voice", None, None, 0.1)]
        table = td.CapabilityTable(rows, operational={"coding"})
        self.assertEqual(table.validate(), [])


if __name__ == "__main__":
    unittest.main()
