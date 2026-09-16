"""QA: the three spec 1.1 invariants that were enforced nowhere.

`CapabilityTable.validate()` answered three of section 1.1's six invariants --
no blank fields, no empty ladder, every rung backed by a row. The other three
were named as "stage 2" in a docstring and implemented in no module, so a task
type could be flipped operational while

  * naming a rung that resolves to no model in the combo box (1.1 / 9.3),
  * carrying a worst-case path above the combined latency ceiling (1.1 / 5.1),
  * carrying a ladder whose expected tree cost exceeds `BUDGET_USD` (1.1 / 2.7),

and the system would start and route it. This file exists to catch all three,
in both directions: a table that breaks the invariant must produce the problem
string, and a table that satisfies it must produce none. Only the failing
direction would also pass for an implementation that reports a problem always.

Nothing here is operational except the synthetic task types the fixtures flip
on purpose. `coding` is never flipped -- section 12 forbids it until the
gate-type dependency is decided -- so the one place the real `coding` rows
appear (reproducing 5.1's published 1,243.125s figure) computes the worst-case
path on a table where no type is operational at all.

Every figure asserted below is arithmetic over the spec's own constants, done
by hand in the test and independently in the module; the two agreeing is the
check.
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


# Spec 2.6/3.1, measured 2026-09-15 -- the rows 5.1's worked figure is built
# from. Used only to reproduce that figure; `coding` is never flipped
# operational anywhere in this file.
CODING_MEASURED = [
    _row("vllm/Qwen3.6-35B-A3B-NVFP4", "coding", 0.66, 44, 0.0000, 26.8, 229_376),
    _row("azure_ai/gpt-5.6-luna", "coding", 1.00, 24, 0.0285, 12.8, 922_000),
    _row("claude-sonnet-5", "coding", 1.00, 24, 1.5709, 15.5, 1_000_000),
    _row("azure_ai/gpt-5.4-mini", "coding", None, None, 0.5261, None, 1_050_000),
]

# Spec 2.6: the reviewer-gate rows. Luna's 11.1s is the only measured gate
# latency in the table and is what 5.1's derivation divides by 12.8.
GATE_ROWS = [
    _row("azure_ai/gpt-5.6-luna", "reviewer-gate", None, None, 0.0285, 11.1,
         922_000),
    _row("claude-sonnet-5", "reviewer-gate", None, None, 1.5709, None,
         1_000_000),
]


def _widget_rows(task_type="widget"):
    """A complete, affordable, fast synthetic type.

    Latencies are round numbers so the expected worst case can be computed in
    the test by hand: reference 10.0, rungs 20.0/10.0/12.0.
    """
    return [
        _row("vllm/free", task_type, 0.66, 20, 0.0000, 20.0, 229_376),
        _row("azure_ai/gpt-5.6-luna", task_type, 1.00, 24, 0.0285, 10.0, 922_000),
        _row("claude-sonnet-5", task_type, 1.00, 24, 1.5709, 12.0, 1_000_000),
    ]


# 90 (unknown type -> longest baseline) x 2.0 (score 5) x
# [ (20.0 + 10.0 + 12.0)/10.0 + 3 x (11.1/10.0) ] = 180 x 7.53
WIDGET_WORST_CASE_S = 1355.4


def _table(rows, operational=()):
    return td.CapabilityTable(rows, operational=operational)


class ModelResolutionTests(unittest.TestCase):
    """Spec 1.1 invariant 2: every rung resolves in the model combo box (9.3)."""

    def test_a_complete_table_reports_no_resolution_problem(self):
        problems = _table(_widget_rows() + GATE_ROWS,
                          operational={"widget"}).validate()
        self.assertEqual(problems, [])

    def test_a_bare_model_outside_the_combo_box_fails(self):
        """The defect this catches is a typo in an Anthropic model id, which is
        the only class of name the built-in list can adjudicate."""
        rows = [r for r in _widget_rows() if r.model != "claude-sonnet-5"]
        rows.append(_row("claude-sonet-5", "widget", 1.00, 24, 1.5709, 12.0))
        problems = _table(rows + GATE_ROWS, operational={"widget"}).validate()
        self.assertTrue(any("claude-sonet-5" in p and "widget" in p
                            for p in problems), problems)

    def test_a_backend_qualified_id_resolves_without_the_builtin_list(self):
        """`azure_ai/gpt-5.6-luna` is in no combo-box fallback list, and spec
        1.2 records it as resolving. An implementation that demanded
        membership in `config.KNOWN_MODELS` would fail it."""
        problems = _table(_widget_rows() + GATE_ROWS,
                          operational={"widget"}).validate()
        self.assertFalse([p for p in problems if "azure_ai/gpt-5.6-luna" in p],
                         problems)

    def test_a_supplied_combo_box_is_authoritative(self):
        """Passing the live list must actually be consulted: sonnet is in the
        built-in fallback and must still fail when the caller says it is not
        served."""
        problems = _table(_widget_rows() + GATE_ROWS,
                          operational={"widget"}).validate(
            known_models=["vllm/free", "azure_ai/gpt-5.6-luna"])
        self.assertTrue(any("claude-sonnet-5" in p for p in problems), problems)

    def test_a_supplied_combo_box_that_covers_every_rung_passes(self):
        problems = _table(_widget_rows() + GATE_ROWS,
                          operational={"widget"}).validate(
            known_models=["vllm/free", "azure_ai/gpt-5.6-luna",
                          "claude-sonnet-5"])
        self.assertEqual(problems, [])

    def test_a_malformed_pair_fails(self):
        """9.3: a routing decision is a backend-and-model pair. Half of one is
        not a resolvable name."""
        rows = [_row("azure_ai/", "widget", 1.00, 24, 0.0, 10.0)]
        problems = _table(rows + GATE_ROWS, operational={"widget"}).validate()
        self.assertTrue(any("azure_ai/" in p for p in problems), problems)


class WorstCasePathTests(unittest.TestCase):
    """Spec 1.1 invariant 5 / 5.1: the ceiling fits the budget."""

    def test_reproduces_the_spec_worked_figure(self):
        """5.1's own derivation, on 2.6's measured rows:

            generation (26.8 + 12.8 + 15.5) / 12.8 = 4.3047
            gates      3 x (11.1 / 12.8)           = 2.6016
                                             sum   = 6.90625
            90 x 2.0 x 6.90625                     = 1,243.125s

        5.1 states the exact sum and the exact product, and says this check is
        "the one it must reproduce". An earlier revision printed 1,242s, which
        is `180 x 6.90` -- the sum rounded to two places before multiplying; it
        was corrected once this check recomputed it. Both are under the 1,500s
        ceiling, so no decision moved, and no constant here was bent to land on
        either number.

        The 1.0 reference is asserted too, because it is the input the whole
        derivation hangs on and the one 5.1 records changing identity: measured
        over the two hard tasks alone sonnet was faster, over all six luna is,
        and `vllm` at 26.8s is in the ladder without ever being the reference.
        """
        table = _table(CODING_MEASURED + GATE_ROWS)  # nothing operational
        self.assertAlmostEqual(table.latency_reference_s("coding"), 12.8,
                               places=3)
        self.assertAlmostEqual(table.worst_case_path_s("coding"), 1243.125,
                               places=3)

    def test_an_unknown_task_type_takes_the_longest_baseline(self):
        """5.1: "An unknown type must receive the longest deadline, never the
        shortest." Same rows under a known short-baseline type and under an
        unlisted one; the unlisted one must get 90, not 45."""
        known = _table(_widget_rows("long-context") + GATE_ROWS)
        unknown = _table(_widget_rows("widget") + GATE_ROWS)
        self.assertAlmostEqual(known.worst_case_path_s("long-context"),
                               677.7, places=3)
        self.assertAlmostEqual(unknown.worst_case_path_s("widget"),
                               WIDGET_WORST_CASE_S, places=3)

    def test_a_path_over_the_ceiling_is_reported(self):
        """Free rungs on both ends, so cost cannot be what fails: a rung ten
        times slower than the reference blows the ceiling on latency alone.
        180 x (1.0 + 10.0 + 3 x 1.11) = 2,579.4s."""
        rows = [
            _row("vllm/fast", "widget", 0.90, 20, 0.0, 10.0),
            _row("vllm/slow", "widget", 0.90, 20, 0.0, 100.0),
        ]
        table = _table(rows + GATE_ROWS, operational={"widget"})
        self.assertAlmostEqual(table.worst_case_path_s("widget"), 2579.4,
                               places=3)
        self.assertTrue(any("ceiling" in p and "widget" in p
                            for p in table.validate()), table.validate())

    def test_a_path_under_the_ceiling_is_not_reported(self):
        """The same table with the slow rung at 20.0s instead of 100.0s:
        180 x (1.0 + 2.0 + 3 x 1.11) = 1,139.4s, and no problem."""
        rows = [
            _row("vllm/fast", "widget", 0.90, 20, 0.0, 10.0),
            _row("vllm/slow", "widget", 0.90, 20, 0.0, 20.0),
        ]
        table = _table(rows + GATE_ROWS, operational={"widget"})
        self.assertAlmostEqual(table.worst_case_path_s("widget"), 1139.4,
                               places=3)
        self.assertEqual(table.validate(), [])

    def test_the_gate_multiplier_comes_from_the_reviewer_gate_row(self):
        """5.1 pins this: a gate uses the `reviewer-gate` row when the model
        has one. Doubling only that row must move the worst case by
        3 x (11.1/10.0) x 180 = 599.4s. An implementation using a gate
        multiplier of 1.00 -- the mistake 5.1 records an earlier revision
        making -- would not move at all."""
        slow_gate = [
            _row("azure_ai/gpt-5.6-luna", "reviewer-gate", None, None, 0.0285,
                 22.2, 922_000),
        ]
        base = _table(_widget_rows() + GATE_ROWS).worst_case_path_s("widget")
        slowed = _table(_widget_rows() + slow_gate).worst_case_path_s("widget")
        self.assertAlmostEqual(base, WIDGET_WORST_CASE_S, places=3)
        self.assertAlmostEqual(slowed, 1954.8, places=3)

    def test_the_gate_falls_back_to_the_leaf_task_type_row(self):
        """5.1: "falling back to the leaf's task-type row when it does not"
        have a reviewer-gate row. Luna's widget latency is the reference
        itself, so all three gates come out at 1.00 and the sum is 7.2."""
        gate_without_latency = [
            _row("azure_ai/gpt-5.6-luna", "reviewer-gate", None, None, 0.0285,
                 None, 922_000),
        ]
        table = _table(_widget_rows() + gate_without_latency,
                       operational={"widget"})
        self.assertAlmostEqual(table.worst_case_path_s("widget"), 1296.0,
                               places=3)
        self.assertEqual(table.validate(), [])

    def test_a_gate_with_no_row_at_all_is_a_problem_not_a_crash(self):
        """Absent data is a reason a type cannot be operational, not a
        silent skip and not a traceback."""
        table = _table(_widget_rows(), operational={"widget"})
        self.assertIsNone(table.worst_case_path_s("widget"))
        problems = table.validate()
        self.assertTrue(any("no reviewer-gate row at all" in p and "widget" in p
                            for p in problems), problems)

    def test_the_reference_is_the_fastest_ladder_eligible_row_only(self):
        """5.1: "A model that is cost-excluded or unmeasured can never be a
        rung, so letting it set the 1.0 reference would shrink the deadline of
        every model that *can* be a rung."

        Two rows faster than every rung, neither of them able to be one: mini
        is excluded from every ladder by operator decision (2.7), and a row
        with no measured accuracy is not ladder-eligible (2.6). The reference
        must stay at luna's 10.0s, and the worst case must not move.
        """
        never_a_rung = [
            _row("azure_ai/gpt-5.4-mini", "widget", 0.99, 20, 0.5261, 2.0),
            _row("vllm/unmeasured", "widget", None, None, 0.0, 1.0),
        ]
        table = _table(_widget_rows() + never_a_rung + GATE_ROWS,
                       operational={"widget"})
        self.assertEqual(table.ladder("widget"),
                         ["vllm/free", "azure_ai/gpt-5.6-luna",
                          "claude-sonnet-5"])
        self.assertAlmostEqual(table.latency_reference_s("widget"), 10.0,
                               places=3)
        self.assertAlmostEqual(table.worst_case_path_s("widget"),
                               WIDGET_WORST_CASE_S, places=3)
        self.assertEqual(table.validate(), [])

    def test_unusable_gate_rows_are_not_reported_as_missing_ones(self):
        """The repair differs, so the sentence has to. An operator looking at
        two reviewer-gate rows must not be told the table holds none: what
        needs fixing is the blank rate on the rows in front of them."""
        unpriced = [
            _row("azure_ai/gpt-5.6-luna", "reviewer-gate", None, None, None,
                 11.1, 922_000),
        ]
        table = _table(_widget_rows() + unpriced, operational={"widget"})
        self.assertIsNone(table.worst_case_path_s("widget"))
        problems = table.validate()
        self.assertTrue(any("azure_ai/gpt-5.6-luna" in p
                            and "no cost_per_1m_tokens" in p
                            for p in problems), problems)
        self.assertFalse([p for p in problems if "no reviewer-gate row at all" in p],
                         problems)

    def test_a_cost_excluded_gate_row_says_so(self):
        """`azure_ai/gpt-5.4-mini` is excluded from every ladder by operator
        decision (2.7), so a table whose only gate row is mini has no gate
        model -- and the message must name the exclusion, not a missing row."""
        excluded_only = [
            _row("azure_ai/gpt-5.4-mini", "reviewer-gate", None, None, 0.5261,
                 11.1, 1_050_000),
        ]
        table = _table(_widget_rows() + excluded_only, operational={"widget"})
        problems = table.validate()
        self.assertTrue(any("azure_ai/gpt-5.4-mini" in p and "excluded" in p
                            for p in problems), problems)

    def test_generation_is_truncated_to_max_attempts(self):
        """A four-rung ladder still runs at most MAX_ATTEMPTS generations, so
        only three rung multipliers are summed: 180 x (3 x 1.0 + 3 x 1.11) =
        1,139.4s, not the 1,319.4s a fourth rung would add."""
        rows = [
            _row("vllm/a", "widget", 0.50, 20, 0.0, 10.0),
            _row("vllm/b", "widget", 0.60, 20, 0.0, 10.0),
            _row("vllm/c", "widget", 0.70, 20, 0.0, 10.0),
            _row("vllm/d", "widget", 0.80, 20, 0.0, 10.0),
        ]
        table = _table(rows + GATE_ROWS)
        self.assertEqual(len(table.ladder("widget")), 4)
        self.assertAlmostEqual(table.worst_case_path_s("widget"), 1139.4,
                               places=3)

    def test_a_rung_without_a_latency_is_a_problem_not_a_crash(self):
        rows = [
            _row("vllm/fast", "widget", 0.90, 20, 0.0, 10.0),
            _row("vllm/blank", "widget", 0.90, 20, 0.0, None),
        ]
        table = _table(rows + GATE_ROWS, operational={"widget"})
        self.assertIsNone(table.worst_case_path_s("widget"))
        # Asserted on "worst-case" rather than on the column name: the
        # pre-existing completeness check also names `median_latency_s` for
        # this row, so a test matching only that would pass with invariant 5
        # unimplemented.
        self.assertTrue(any("worst-case" in p and "vllm/blank" in p
                            for p in table.validate()), table.validate())


class TreeCostTests(unittest.TestCase):
    """Spec 1.1 invariant 6 / 2.7: the ladder fits `BUDGET_USD`."""

    def test_reproduces_the_spec_rung_cost_table(self):
        """2.7's published table, every row of it. The rung-2 column is what
        pins P(reach rung 2): the table is computed from the exact 1/6 (one
        leaf of six, n=6), which 2.7's input table now states outright. An
        earlier revision of it printed 0.17, and at 0.17 sonnet's rung 2 would
        be $0.635 against the published $0.623 -- so this loop is what caught
        the rounding and is what keeps it caught."""
        for rate, rung0, rung1, rung2 in [
            (0.0000, 0.000, 0.000, 0.000),
            (0.0285, 0.068, 0.034, 0.011),
            (0.5261, 1.251, 0.626, 0.209),
            (1.5709, 3.736, 1.868, 0.623),
            (3.6082, 8.582, 4.291, 1.430),
        ]:
            self.assertAlmostEqual(td.rung_cost_usd(0, rate), rung0, places=3)
            self.assertAlmostEqual(td.rung_cost_usd(1, rate), rung1, places=3)
            self.assertAlmostEqual(td.rung_cost_usd(2, rate), rung2, places=3)

    def test_an_unaffordable_ladder_is_reported(self):
        """1.2's ready-made case: luna at rung 0 ($0.068) and sonnet at rung 1
        ($1.868) is $1.936 against a $1.00 tree budget. The latencies are close
        together on purpose so the latency invariant does not fire and mask
        which check caught it."""
        rows = [
            _row("azure_ai/gpt-5.6-luna", "widget", 0.90, 20, 0.0285, 10.0),
            _row("claude-sonnet-5", "widget", 0.95, 20, 1.5709, 10.5),
        ]
        table = _table(rows + GATE_ROWS, operational={"widget"})
        self.assertAlmostEqual(table.tree_cost_usd("widget"), 1.9359, places=3)
        problems = table.validate()
        self.assertTrue(any("widget" in p and "BUDGET_USD" in p
                            for p in problems), problems)
        self.assertFalse([p for p in problems if "ceiling" in p], problems)

    def test_the_unaffordable_ladder_names_the_costliest_rung(self):
        """Spec 11: the budget-overrun problem string must name the offending
        rung, not just the task type and the total -- an operator should not
        have to recompute what `_tree_cost` already knew. Luna at rung 0
        contributes $0.068 and sonnet at rung 1 contributes $1.868 of the
        $1.9359 total, so sonnet is the rung to name."""
        rows = [
            _row("azure_ai/gpt-5.6-luna", "widget", 0.90, 20, 0.0285, 10.0),
            _row("claude-sonnet-5", "widget", 0.95, 20, 1.5709, 10.5),
        ]
        table = _table(rows + GATE_ROWS, operational={"widget"})
        problems = table.validate()
        budget_problems = [p for p in problems if "BUDGET_USD" in p]
        self.assertTrue(budget_problems, problems)
        self.assertTrue(
            all("claude-sonnet-5" in p for p in budget_problems),
            budget_problems,
        )

    def test_an_affordable_ladder_is_not_reported(self):
        """The same shape with a cheaper top rung: $0.068 at rung 0 plus
        $0.595 at rung 1 is $0.662, and no problem. Only the rate changed, so
        an implementation that reports a budget problem unconditionally fails
        here while passing the test above."""
        rows = [
            _row("azure_ai/gpt-5.6-luna", "widget", 0.90, 20, 0.0285, 10.0),
            _row("azure_ai/mid", "widget", 0.95, 20, 0.5000, 10.5),
        ]
        table = _table(rows + GATE_ROWS, operational={"widget"})
        self.assertAlmostEqual(table.tree_cost_usd("widget"), 0.6624, places=3)
        self.assertEqual(table.validate(), [])

    def test_the_spec_coding_ladder_fits_the_budget(self):
        """2.7: `coding` is one of only two ladders that fit unchanged.
        0 + 0.5 x 0.0285 + (1/6) x 1.5709, over 40 x 59,460 tokens."""
        table = _table(CODING_MEASURED + GATE_ROWS)  # nothing operational
        self.assertAlmostEqual(table.tree_cost_usd("coding"), 0.6566, places=3)

    def test_a_fourth_rung_is_not_priced_as_free(self):
        """2.7 publishes reach probabilities for three rungs. A deeper ladder
        is refused rather than truncated, because a rung nobody can price is
        indistinguishable from one that costs nothing."""
        rows = [
            _row("vllm/a", "widget", 0.50, 20, 0.0, 10.0),
            _row("vllm/b", "widget", 0.60, 20, 0.0, 10.0),
            _row("vllm/c", "widget", 0.70, 20, 0.0, 10.0),
            _row("vllm/d", "widget", 0.80, 20, 0.0, 10.0),
        ]
        table = _table(rows + GATE_ROWS, operational={"widget"})
        self.assertIsNone(table.tree_cost_usd("widget"))
        self.assertTrue(any("widget" in p and "reach probabilit" in p
                            for p in table.validate()), table.validate())


class AllSixTogetherTests(unittest.TestCase):
    """1.1: the error lists every broken invariant, not the first."""

    def test_every_broken_invariant_is_reported_at_once(self):
        rows = [
            # unresolvable bare name, 10x the reference latency, and sonnet's
            # rate at rung 1 -- one row breaking all three new invariants.
            _row("vllm/fast", "widget", 0.90, 20, 0.0, 10.0),
            _row("claude-sonet-5", "widget", 0.90, 20, 1.5709, 100.0),
        ]
        problems = _table(rows + GATE_ROWS, operational={"widget"}).validate()
        self.assertTrue(any("claude-sonet-5" in p for p in problems), problems)
        self.assertTrue(any("ceiling" in p for p in problems), problems)
        self.assertTrue(any("BUDGET_USD" in p for p in problems), problems)

    def test_a_non_operational_type_is_exempt_from_all_three(self):
        """The bootstrap exemption still holds: these checks are what a type
        is submitted to by being flipped, not a rule for the whole table."""
        rows = [
            _row("claude-sonet-5", "widget", 0.90, 20, 1.5709, 100.0),
        ]
        self.assertEqual(_table(rows).validate(), [])


if __name__ == "__main__":
    unittest.main()
