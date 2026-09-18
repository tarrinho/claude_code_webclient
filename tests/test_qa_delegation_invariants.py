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
from unittest.mock import patch

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

def _security_gate_rows(climb: bool = False, latency: float = 11.1):
    """`security-gate` rows, for the gate task type split out on 2026-09-17.

    Every fixture in this file needs them now: stage 5 runs on its own task
    type, so a table holding only `reviewer-gate` rows genuinely cannot time
    the security gate and `_worst_case` correctly refuses. The default latency
    matches luna's reviewer-gate figure so the arithmetic in tests written
    before the split -- which multiplied ONE gate latency by three -- still
    reproduces: 2 reviewer calls + 1 security call at the same latency is the
    same sum as 3 calls at that latency.

    `climb=True` adds a measured second rung, for the tests that are about the
    climb term itself.
    """
    rows = [_row("azure_ai/gpt-5.6-luna", "security-gate", 0.95, 28,
                 0.0285, latency, 922_000)]
    if climb:
        rows.append(_row("claude-sonnet-5", "security-gate", None, None,
                         1.5709, latency, 1_000_000))
    return rows


# Spec 2.6: the real reviewer-gate rows. Luna's 11.1s is the only measured
# gate latency in the table and is what 5.1's derivation divides by 12.8.
# Sonnet is the climb rung (spec 4.3/4.5) and its reviewer-gate latency is
# TBD -- so any worst-case path computed against this fixture is
# incomputable (spec 5.1, "Decided 2026-09-16"), on purpose. Used only where
# that incomputability is the point; tests about something else use
# GATE_SINGLE (no climb rung to be unmeasured) or GATE_CLIMB_MEASURED (a
# climb rung that is measured).
GATE_ROWS = [
    _row("azure_ai/gpt-5.6-luna", "reviewer-gate", 0.95, 28, 0.0285, 11.1,
         922_000),
    _row("claude-sonnet-5", "reviewer-gate", None, None, 1.5709, None,
         1_000_000),
] + _security_gate_rows(climb=False)

# One usable reviewer-gate model: there is no second rung to climb to, so
# the gate-climb term is correctly zero rather than missing. Every test that
# is not about gate-climbing itself uses this, so its numbers stay the ones
# the pre-climb formula already published.
GATE_SINGLE = [
    _row("azure_ai/gpt-5.6-luna", "reviewer-gate", 0.95, 28, 0.0285, 11.1,
         922_000),
] + _security_gate_rows(climb=False)

# Both reviewer-gate rungs measured, so a gate that climbs has a priced
# second call. Used only by tests about the climb term itself.
GATE_CLIMB_MEASURED = [
    _row("azure_ai/gpt-5.6-luna", "reviewer-gate", 0.95, 28, 0.0285, 11.1,
         922_000),
    _row("claude-sonnet-5", "reviewer-gate", None, None, 1.5709, 14.0,
         1_000_000),
] + _security_gate_rows(climb=False)


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
# [ generation (20.0 + 10.0 + 12.0)/10.0            = 4.20
#   reviewer-gate 2 calls x 3 attempts x 11.1/10.0  = 6.66
#   security-gate 1 call  x 3 attempts x 11.1/10.0  = 3.33 ]  = 14.19
# = 180 x 14.19
#
# The entry call is multiplied by MAX_ATTEMPTS since 2026-09-18: 4.3 and 4.5
# both say the gate re-reviews after the generator escalates, so a gate sees
# every attempt's output rather than only the first. Hand-computed here and
# independently in the module; the two agreeing is the check.
WIDGET_WORST_CASE_S = 2554.2


def _with_gates_operational(operational):
    """Add the gate task types to an operational set.

    Spec 12, decided 2026-09-17: a task type may not route through a gate type
    that has not itself cleared 1.1, so every table that flips an ordinary type
    operational must flip the gate types too. The fixtures below are about
    OTHER invariants, and threading two extra strings through every one of them
    would bury what each is testing. The rule itself has its own test --
    `GateTypeCoverageTests` -- which passes the operational set directly and
    would catch this helper masking a regression.
    """
    ops = set(operational)
    if ops and not ops <= set(td.GATE_CALLS):
        ops |= set(td.GATE_CALLS)
    return ops


def _table(rows, operational=()):
    return td.CapabilityTable(
        rows, operational=_with_gates_operational(operational))


class ModelResolutionTests(unittest.TestCase):
    """Spec 1.1 invariant 2: every rung resolves in the model combo box (9.3)."""

    def test_a_complete_table_reports_no_resolution_problem(self):
        problems = _table(_widget_rows() + GATE_SINGLE,
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
        problems = _table(_widget_rows() + GATE_SINGLE,
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

    def test_the_spec_worked_figure_is_now_a_lower_bound_not_the_worst_case(self):
        """Spec 5.1, "Decided 2026-09-16: the formula above is incomplete, and
        1,243.125s is a lower bound". The 2026-09-15 derivation --

            generation (26.8 + 12.8 + 15.5) / 12.8 = 4.3047
            gates      3 x (11.1 / 12.8)           = 2.6016
                                             sum   = 6.90625
            90 x 2.0 x 6.90625                     = 1,243.125s

        -- summed three gate calls, each run once, at the gate's entry rung.
        4.3/4.5 let a gate climb (`luna -> sonnet`) for a second call, and
        section 2.6 records sonnet's `reviewer-gate` `median_latency_s` as
        TBD -- "Luna's gate row is measured at 11.1s; sonnet's is not
        measured at all." So `coding`'s full worst-case path is not merely
        unequal to 1,243.125s, it cannot be computed at all: this is what
        it now means for a task type to fail 1.1's "ceiling fits the budget"
        invariant when reviewer-gate is this incomplete, and it is why
        section 1.2's row for `coding` changed from "pass" to "not yet
        answerable".

        The 1.0 reference is asserted too, because it is unaffected by any
        of this -- it comes from `coding`'s own ladder-eligible rows, not
        from `reviewer-gate` -- and staying at 12.8 is how this test shows
        the climb term, not the reference, is what broke.
        """
        table = _table(CODING_MEASURED + GATE_ROWS)  # nothing operational
        self.assertAlmostEqual(table.latency_reference_s("coding"), 12.8,
                               places=3)
        self.assertIsNone(table.worst_case_path_s("coding"))

    def test_an_unresolved_climb_rung_is_a_startup_problem_not_a_pass(self):
        """The consequence of the above for 1.1: an operational task type
        whose gate can climb but whose climb rung is unmeasured must be
        refused at startup, not silently compared against the ceiling with
        an understated number. `coding` itself is never flipped operational
        here (spec 12 forbids it) -- a differently-named type carrying the
        same measured rows and the same real, TBD-climb reviewer-gate table
        stands in for it."""
        table = _table(
            [td.CapabilityRow(model=r.model, task_type="coding-shaped-2",
                              accuracy=r.accuracy, n=r.n,
                              cost_per_1m_tokens=r.cost_per_1m_tokens,
                              median_latency_s=r.median_latency_s,
                              max_context=r.max_context)
             for r in CODING_MEASURED]
            + GATE_ROWS,
            operational={"coding-shaped-2"})
        problems = table.validate()
        self.assertTrue(
            any("coding-shaped-2" in p and "claude-sonnet-5" in p
                and "climb" in p for p in problems),
            problems)

    def test_an_unknown_task_type_takes_the_longest_baseline(self):
        """5.1: "An unknown type must receive the longest deadline, never the
        shortest." Same rows under a calibrated type and under an unlisted
        one: the calibrated one derives its baseline against the table's own
        reference, the unlisted one takes the longest published baseline (90),
        never the shortest (45). `GATE_SINGLE` is used because this test is
        about the baseline, not about gate climbing -- a second, unmeasured
        gate rung would make the path incomputable and mask the property under
        test."""
        known = _table(_widget_rows("long-context") + GATE_SINGLE)
        unknown = _table(_widget_rows("widget") + GATE_SINGLE)
        # Reference 10.0, so long-context derives to ratio x 10.0 rather than
        # sitting at the published 45.0.
        derived = td.baseline_task_ratio("long-context") * 10.0
        self.assertAlmostEqual(known.baseline_deadline_s("long-context"),
                               derived, places=9)
        self.assertAlmostEqual(known.worst_case_path_s("long-context"),
                               derived * 2.0 * 14.19, places=6)
        self.assertEqual(unknown.baseline_deadline_s("widget"), 90.0)
        self.assertAlmostEqual(unknown.worst_case_path_s("widget"),
                               WIDGET_WORST_CASE_S, places=3)

    def test_an_unknown_type_is_not_dragged_down_by_a_fast_calibrated_type(self):
        """The direction that matters now the baseline is derived: a fleet
        fast enough to derive a calibrated type BELOW its published figure
        must not shorten the unmeasured type's deadline with it. Rows here are
        five times faster than `_widget_rows`, deriving `coding` well under
        90 -- and `widget` must still get 90."""
        fast = [_row(r.model, "coding", r.accuracy, r.n, r.cost_per_1m_tokens,
                     r.median_latency_s / 5.0, r.max_context)
                for r in _widget_rows()]
        table = _table(fast + _widget_rows("widget") + GATE_SINGLE)
        self.assertLess(table.baseline_deadline_s("coding"), 90.0)
        self.assertEqual(table.baseline_deadline_s("widget"), 90.0)

    def test_a_path_over_the_ceiling_is_reported(self):
        """Free rungs on both ends, so cost cannot be what fails: a rung ten
        times slower than the reference blows the ceiling on latency alone.
        180 x (1.0 + 10.0 + 3 x 1.11) = 2,579.4s. `GATE_SINGLE`: no climb
        rung to be unmeasured, so the only thing that can fail is latency."""
        rows = [
            _row("vllm/fast", "widget", 0.90, 20, 0.0, 10.0),
            _row("vllm/slow", "widget", 0.90, 20, 0.0, 100.0),
        ]
        table = _table(rows + GATE_SINGLE, operational={"widget"})
        self.assertAlmostEqual(table.worst_case_path_s("widget"), 3778.2,
                               places=3)
        enforced = table.validate(enforce_latency_ceiling=True)
        self.assertTrue(any("ceiling" in p and "widget" in p
                            for p in enforced), enforced)

    def test_the_ceiling_blocks_only_when_enforcement_is_on(self):
        """9.2's knob, off by default. The SAME table: the breach is computed
        and reported through `latency_ceiling_breaches` either way, and
        reaches `validate` -- and so blocks -- only when enforcement is on.

        A knob that suppressed the measurement as well as the block would make
        "off" mean "not checked", and 5.1 wants the breach rate visible
        precisely while nothing is being stopped by it."""
        rows = [
            _row("vllm/fast", "widget", 0.90, 20, 0.0, 10.0),
            _row("vllm/slow", "widget", 0.90, 20, 0.0, 100.0),
        ]
        table = _table(rows + GATE_SINGLE, operational={"widget"})

        # measured regardless of the knob
        breaches = table.latency_ceiling_breaches()
        self.assertIn("widget", breaches)
        self.assertIn("ceiling", breaches["widget"])

        # off (the default) -- no ceiling problem, and the default really is
        # off rather than the call below merely agreeing with it by accident
        self.assertFalse(td.CEILING_ENFORCEMENT_DEFAULT)
        for problems in (table.validate(),
                         table.validate(enforce_latency_ceiling=False)):
            self.assertFalse([p for p in problems if "ceiling" in p], problems)

        # on
        self.assertTrue(
            [p for p in table.validate(enforce_latency_ceiling=True)
             if "ceiling" in p])

    def test_an_incomputable_path_blocks_whatever_the_knob_says(self):
        """The knob gates the COMPARISON, never the missing data. A rung with
        no measured latency cannot be timed at all, and "we do not know how
        long this takes" is not relaxed by deciding not to enforce a limit --
        5.1's ceiling and 1.1's no-blank-fields rule are different failures."""
        rows = [
            _row("vllm/fast", "widget", 0.90, 20, 0.0, 10.0),
            _row("vllm/untimed", "widget", 0.90, 20, 0.0, None),
        ]
        table = _table(rows + GATE_SINGLE, operational={"widget"})
        for enforce in (False, True):
            with self.subTest(enforce=enforce):
                problems = table.validate(enforce_latency_ceiling=enforce)
                self.assertTrue(
                    any("median_latency_s" in p for p in problems), problems)

    def test_a_path_under_the_ceiling_is_not_reported(self):
        """The same table with the slow rung at 20.0s instead of 100.0s:
        180 x (1.0 + 2.0 + 3 x 1.11) = 1,139.4s, and no problem."""
        rows = [
            _row("vllm/fast", "widget", 0.90, 20, 0.0, 10.0),
            _row("vllm/slow", "widget", 0.90, 20, 0.0, 20.0),
        ]
        table = _table(rows + GATE_SINGLE, operational={"widget"})
        self.assertAlmostEqual(table.worst_case_path_s("widget"), 2338.2,
                               places=3)
        self.assertEqual(table.validate(), [])

    def test_the_gate_multiplier_comes_from_the_reviewer_gate_row(self):
        """5.1 pins this: a gate uses the `reviewer-gate` row when the model
        has one. Doubling only that row must move the worst case by
        3 x (11.1/10.0) x 180 = 599.4s. An implementation using a gate
        multiplier of 1.00 -- the mistake 5.1 records an earlier revision
        making -- would not move at all. `GATE_SINGLE` on both sides: this
        test is about the entry rung's own latency, not the climb term."""
        slow_gate = [
            _row("azure_ai/gpt-5.6-luna", "reviewer-gate", None, None, 0.0285,
                 22.2, 922_000),
        ] + _security_gate_rows(climb=False)
        base = _table(_widget_rows() + GATE_SINGLE).worst_case_path_s("widget")
        slowed = _table(_widget_rows() + slow_gate).worst_case_path_s("widget")
        self.assertAlmostEqual(base, WIDGET_WORST_CASE_S, places=3)
        # Since the 2026-09-17 split this moves 2 calls, not 3: the reviewer
        # gate backs 4.3's reviewer and 4.4's QA, and 4.5's security gate is
        # untouched at 11.1s. That the delta is 2x rather than 3x is the
        # assertion -- it is what proves the security call is timed against
        # its OWN row instead of the reviewer's.
        # x3: the entry call happens once per generation attempt.
        self.assertAlmostEqual(
            slowed - base, 2 * 3 * (22.2 - 11.1) / 10.0 * 180.0, places=3)

    def test_the_gate_climb_term_is_added_when_the_climb_rung_is_measured(self):
        """Spec 4.3/4.5, 2026-09-16 amendment: each of the three gates can
        make a second call, at the rung above its entry, if it climbs -- and
        the worst case must assume it does. `GATE_CLIMB_MEASURED` adds a
        measured sonnet reviewer-gate row (14.0s) above `GATE_SINGLE`'s luna
        (11.1s) with nothing else changed, so the whole difference is the
        climb term:

        Since the 2026-09-17 split only the REVIEWER gate gains a climb rung
        here, so the rise is 2 calls' worth, not 3 -- and that asymmetry is
        the point: the security gate still has one usable model, so its climb
        term is correctly zero rather than borrowed from the reviewer's
        table."""
        single = _table(_widget_rows() + GATE_SINGLE).worst_case_path_s("widget")
        climbable = _table(
            _widget_rows() + GATE_CLIMB_MEASURED).worst_case_path_s("widget")
        self.assertAlmostEqual(single, WIDGET_WORST_CASE_S, places=3)
        self.assertAlmostEqual(
            climbable - single, 2 * (14.0 / 10.0) * 180.0, places=3)

    def test_a_measured_but_unresolved_climb_rung_is_a_problem_not_zero(self):
        """The negative of the test above, on the exact real-spec fixture:
        a second reviewer-gate model exists (sonnet), so the gate can climb
        and the worst case must assume it does -- but sonnet's own
        `reviewer-gate` `median_latency_s` is TBD, so the climb term cannot
        be priced. The path must come back `None` with a problem naming the
        climb model, not a number that silently omits the climb (which
        `test_the_gate_multiplier_comes_from_the_reviewer_gate_row`'s
        `GATE_SINGLE` case already shows is a real, different, zero-cost
        outcome that only applies when there is no second model at all)."""
        table = _table(_widget_rows() + GATE_ROWS, operational={"widget"})
        self.assertIsNone(table.worst_case_path_s("widget"))
        problems = table.validate()
        self.assertTrue(
            any("claude-sonnet-5" in p and "climb" in p and "widget" in p
                for p in problems), problems)

    def test_the_gate_falls_back_to_the_leaf_task_type_row(self):
        """5.1: "falling back to the leaf's task-type row when it does not"
        have a reviewer-gate row. Luna's widget latency is the reference
        itself, so all three gates come out at 1.00 and the sum is 7.2."""
        gate_without_latency = [
            _row("azure_ai/gpt-5.6-luna", "reviewer-gate", None, None, 0.0285,
                 None, 922_000),
            _row("azure_ai/gpt-5.6-luna", "security-gate", None, None, 0.0285,
                 None, 922_000),
        ]
        table = _table(_widget_rows() + gate_without_latency,
                       operational={"widget"})
        self.assertAlmostEqual(table.worst_case_path_s("widget"), 2376.0,
                               places=3)
        # Scoped to `widget`'s own problems. The gate rows here deliberately
        # carry no latency -- that absence is what makes the fallback the only
        # path that can produce a figure -- so the gate task types cannot
        # themselves be clean, and asserting a globally empty problem list
        # would be asserting something this fixture is built not to have.
        self.assertEqual([p for p in table.validate()
                          if p.startswith("widget:")], [])

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
        table = _table(_widget_rows() + never_a_rung + GATE_SINGLE,
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
        """At most MAX_ATTEMPTS generations run, so at most three rung
        multipliers are summed: 180 x (3 x 1.0 + 3 x 1.11) = 1,139.4s, not the
        1,319.4s a fourth rung would add.

        Since 2026-09-17 the cap is applied by `ladder()` itself rather than
        by `_worst_case` slicing a longer list, so the ladder IS three rungs
        here. The worst-case figure is unchanged, which is the point: the two
        halves of the module now agree about which rungs exist instead of one
        ignoring a rung the other refused to price."""
        rows = [
            _row("vllm/a", "widget", 0.50, 20, 0.0, 10.0),
            _row("vllm/b", "widget", 0.60, 20, 0.0, 10.0),
            _row("vllm/c", "widget", 0.70, 20, 0.0, 10.0),
            _row("vllm/d", "widget", 0.80, 20, 0.0, 10.0),
        ]
        table = _table(rows + GATE_SINGLE)
        self.assertEqual(len(table.ladder("widget")), td.MAX_ATTEMPTS)
        self.assertAlmostEqual(table.worst_case_path_s("widget"), 2338.2,
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


def _rate_over_budget() -> float:
    """A rung-1 rate whose tree cost exceeds BUDGET_USD, whatever it is.

    The fixtures below used sonnet's literal 1.5709, which was unaffordable
    against a $1.00 budget and affordable against the $3.50 one set on
    2026-09-18 -- so the "unaffordable ladder" tests silently stopped
    describing an unaffordable ladder. Derived from the constant instead.

    rung 1 is reached with probability REACH_PROBABILITY[1], so the rate
    needed to clear the budget on that rung alone is the budget divided by
    (leaves x tokens x P), with a 20% margin so rounding cannot land it under.
    """
    per_unit = (td.LEAVES_PER_TREE * td.TOKENS_PER_LEAF
                * td.REACH_PROBABILITY[1] / 1_000_000)
    return td.BUDGET_USD / per_unit * 1.2


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
            _row("claude-sonnet-5", "widget", 0.95, 20, _rate_over_budget(), 10.5),
        ]
        table = _table(rows + GATE_ROWS, operational={"widget"})
        self.assertGreater(table.tree_cost_usd("widget"), td.BUDGET_USD)
        # `enforce_budget=True` because this asserts the invariant, not the
        # knob's default. Since BUDGET_ENFORCEMENT_DEFAULT is False, a bare
        # validate() suppresses the breach and this test would assert that an
        # unaffordable ladder is tolerated -- which is a different claim, and
        # one `BudgetEnforcementKnobTests` already makes on purpose.
        problems = table.validate(enforce_budget=True)
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
            _row("claude-sonnet-5", "widget", 0.95, 20, _rate_over_budget(), 10.5),
        ]
        table = _table(rows + GATE_ROWS, operational={"widget"})
        problems = table.validate(enforce_budget=True)
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
        table = _table(rows + GATE_SINGLE, operational={"widget"})
        self.assertAlmostEqual(table.tree_cost_usd("widget"), 0.6624, places=3)
        self.assertEqual(table.validate(), [])

    def test_the_spec_coding_ladder_fits_the_budget(self):
        """2.7: `coding` is one of only two ladders that fit unchanged.
        0 + 0.5 x 0.0285 + (1/6) x 1.5709, over 40 x 59,460 tokens."""
        table = _table(CODING_MEASURED + GATE_ROWS)  # nothing operational
        self.assertAlmostEqual(table.tree_cost_usd("coding"), 0.6566, places=3)

    def test_an_unpriceable_rung_is_never_priced_as_free(self):
        """2.7 publishes reach probabilities for `MAX_ATTEMPTS` rungs, and a
        rung nobody can price must never be indistinguishable from one that
        costs nothing.

        `ladder()` now caps itself at MAX_ATTEMPTS, so this can no longer be
        reached by a ladder simply being long -- which is why the assertion is
        on `rung_cost_usd` directly. The guard protects the relationship that
        replaced the old failure: MAX_ATTEMPTS attempts need MAX_ATTEMPTS
        published probabilities, and raising the budget without publishing one
        must refuse."""
        with self.assertRaises(ValueError):
            td.rung_cost_usd(len(td.REACH_PROBABILITY), 1.0)
        with self.assertRaises(ValueError):
            td.rung_cost_usd(-1, 1.0)

    def test_the_attempt_budget_never_outruns_the_published_probabilities(self):
        """The invariant the truncation rests on. If MAX_ATTEMPTS were ever
        raised past the number of published reach probabilities, every ladder
        would become unpriceable at once -- so this asserts the relationship
        rather than either number."""
        self.assertLessEqual(td.MAX_ATTEMPTS, len(td.REACH_PROBABILITY))

    def test_a_capped_ladder_is_priced_over_the_rungs_that_can_run(self):
        """Truncation must not quietly make a ladder cheaper by hiding rungs.
        The four-rung table below prices exactly as the three rungs that
        survive: a free rung 0, and two more that are also free here, so the
        total is 0 -- and, crucially, computable rather than refused."""
        rows = [
            _row("vllm/a", "widget", 0.50, 20, 0.0, 10.0),
            _row("vllm/b", "widget", 0.60, 20, 0.0, 10.0),
            _row("vllm/c", "widget", 0.70, 20, 0.0, 10.0),
            _row("vllm/d", "widget", 0.80, 20, 0.0, 10.0),
        ]
        table = _table(rows + GATE_ROWS, operational={"widget"})
        self.assertEqual(len(table.ladder("widget")), td.MAX_ATTEMPTS)
        self.assertEqual(table.tree_cost_usd("widget"), 0.0)
        self.assertFalse([p for p in table.validate()
                          if "reach probabilit" in p], table.validate())


class LadderTruncationTests(unittest.TestCase):
    """Spec 5's attempt budget, applied to spec 3's generated ladder.

    A ladder longer than MAX_ATTEMPTS contains rungs that can never run. WHICH
    rungs are dropped is the whole question: plain truncation would remove the
    top rung, and because spec 3's walk produces non-decreasing accuracies the
    top rung is always the accuracy ceiling.
    """

    def test_the_top_rung_survives_because_it_is_the_accuracy_ceiling(self):
        """The failure plain `[:MAX_ATTEMPTS]` would cause. Here the only
        model that reaches 100% is the most expensive one, so truncating from
        the end would leave a ladder that tops out at 60%."""
        rows = [
            _row("vllm/free", "widget", 0.50, 20, 0.0000, 10.0),
            _row("azure_ai/gpt-5.6-luna", "widget", 0.55, 20, 0.0285, 10.0),
            _row("claude-sonnet-5", "widget", 0.60, 20, 1.5709, 10.0),
            _row("claude-opus-5", "widget", 1.00, 20, 3.6082, 10.0),
        ]
        ladder = _table(rows + GATE_SINGLE).ladder("widget")
        self.assertEqual(len(ladder), td.MAX_ATTEMPTS)
        self.assertEqual(ladder[-1], "claude-opus-5")

    def test_rung_zero_survives_because_it_is_the_cost_thesis(self):
        """4.1's free start. 5.1 rejected dropping the free rung once already,
        under its option 2, for exactly this reason -- it fixes a number by
        abandoning the thesis the design is built on."""
        rows = [
            _row("vllm/free", "widget", 0.50, 20, 0.0000, 10.0),
            _row("azure_ai/gpt-5.6-luna", "widget", 0.55, 20, 0.0285, 10.0),
            _row("claude-sonnet-5", "widget", 0.60, 20, 1.5709, 10.0),
            _row("claude-opus-5", "widget", 1.00, 20, 3.6082, 10.0),
        ]
        ladder = _table(rows + GATE_SINGLE).ladder("widget")
        self.assertEqual(ladder[0], "vllm/free")

    def test_a_redundant_rung_is_dropped_before_an_improving_one(self):
        """What a four-rung ladder is actually made of. `dup` matches its
        predecessor's accuracy at the same price, so it buys an attempt and no
        capability; `better` improves. The redundant one goes even though it
        is cheaper, because cheapness is not what an ESCALATION rung is for."""
        rows = [
            _row("vllm/free", "widget", 0.50, 20, 0.0000, 10.0),
            _row("azure_ai/dup", "widget", 0.50, 20, 0.0285, 10.0),
            _row("azure_ai/better", "widget", 0.80, 20, 0.0286, 10.0),
            _row("claude-opus-5", "widget", 1.00, 20, 3.6082, 10.0),
        ]
        ladder = _table(rows + GATE_SINGLE).ladder("widget")
        self.assertEqual(
            ladder, ["vllm/free", "azure_ai/better", "claude-opus-5"])

    def test_with_no_redundant_rung_the_lowest_accuracy_middle_goes(self):
        """Strictly increasing accuracies, so no rung is redundant. The one
        contributing least between the two ends is the lowest-accuracy middle
        rung, and that is the one dropped."""
        rows = [
            _row("vllm/a", "widget", 0.50, 20, 0.0, 10.0),
            _row("vllm/b", "widget", 0.60, 20, 0.0, 10.0),
            _row("vllm/c", "widget", 0.70, 20, 0.0, 10.0),
            _row("vllm/d", "widget", 0.80, 20, 0.0, 10.0),
        ]
        ladder = _table(rows + GATE_SINGLE).ladder("widget")
        self.assertEqual(ladder, ["vllm/a", "vllm/c", "vllm/d"])

    def test_redundancy_beats_low_accuracy_when_the_two_rules_disagree(self):
        """The case that separates "drop the redundant rung" from "drop the
        lowest-accuracy rung", which agree on most tables and not on this one.

        Both middle rungs measure 0.90. `first` IMPROVES on rung 0 (0.50), so
        it is the rung that earns its attempt; `dup` merely repeats `first`.
        A lowest-accuracy rule sees a tie at 0.90 and drops whichever comes
        first, removing the improving rung and keeping the redundant one. This
        is the shape `coding` actually had -- luna and terra tied at 100% above
        a 66% free rung -- so getting it backwards produces a ladder that
        escalates from the free model to a repeat of the rung it just failed.
        """
        rows = [
            _row("vllm/free", "widget", 0.50, 20, 0.0000, 10.0),
            _row("azure_ai/a-first", "widget", 0.90, 20, 0.0285, 10.0),
            _row("azure_ai/b-dup", "widget", 0.90, 20, 0.0286, 10.0),
            _row("claude-opus-5", "widget", 1.00, 20, 3.6082, 10.0),
        ]
        ladder = _table(rows + GATE_SINGLE).ladder("widget")
        self.assertEqual(
            ladder, ["vllm/free", "azure_ai/a-first", "claude-opus-5"])
        self.assertNotIn("azure_ai/b-dup", ladder)

    def test_a_ladder_within_the_budget_is_untouched(self):
        """The rule must not fire on ladders that already fit, or it would
        start editing spec 3's published shapes. Equal accuracy is KEPT by
        spec 3 ("a `<=` here would drop every tied rung"), and that stays true
        for a three-rung ladder even though a tie is what truncation targets
        first when there are four."""
        rows = [
            _row("vllm/free", "widget", 0.50, 20, 0.0000, 10.0),
            _row("azure_ai/gpt-5.6-luna", "widget", 1.00, 20, 0.0285, 10.0),
            _row("claude-sonnet-5", "widget", 1.00, 20, 1.5709, 10.0),
        ]
        self.assertEqual(
            _table(rows + GATE_SINGLE).ladder("widget"),
            ["vllm/free", "azure_ai/gpt-5.6-luna", "claude-sonnet-5"])

    def test_the_real_coding_rows_regenerate_the_published_ladder(self):
        """The end-to-end case this change exists for. With terra measured,
        `coding` generated four rungs and became unpriceable; capped, it is
        `vllm -> luna -> sonnet` again -- exactly what spec 3 publishes and
        what 2.7 calls "correct and survives unchanged"."""
        rows = CODING_MEASURED + [
            _row("azure_ai/gpt-5.6-terra", "coding", 1.00, 18, 0.0285, 7.2,
                 922_000),
        ]
        self.assertEqual(
            _table(rows + GATE_SINGLE).ladder("coding"),
            ["vllm/Qwen3.6-35B-A3B-NVFP4", "azure_ai/gpt-5.6-luna",
             "claude-sonnet-5"])


class GatePricingTests(unittest.TestCase):
    """2.7's cost model, applied to a GATE ladder rather than a generation
    ladder (2026-09-17).

    A gate call is not a leaf. Pricing one with the other's constants is what
    made `security-gate` look unaffordable, and the three constants that
    differ -- per-call size, attempt budget, reach probability -- each move the
    answer on their own.
    """

    def _gate_rows(self):
        """Three security-gate rungs, improving with cost, as measured."""
        return [
            _row("azure_ai/gpt-5.6-luna", "security-gate", 0.857, 28,
                 0.0285, 5.83, 922_000),
            _row("claude-sonnet-5", "security-gate", 0.929, 28,
                 1.5709, 4.635, 1_000_000),
            _row("claude-opus-5", "security-gate", 0.964, 28,
                 3.6082, 4.8, 1_000_000),
        ]

    def test_a_gate_call_is_priced_at_its_own_measured_size(self):
        """`GATE_TOKENS_PER_CALL` (13,883, measured over 168 calls) against
        `TOKENS_PER_LEAF` (59,460, a measured generation leaf). Using the leaf
        figure overprices a gate ladder by 4.28x."""
        self.assertLess(td.GATE_TOKENS_PER_CALL, td.TOKENS_PER_LEAF)
        table = _table(self._gate_rows())
        cost = table.tree_cost_usd("security-gate")
        as_a_leaf = cost * td.TOKENS_PER_LEAF / td.GATE_TOKENS_PER_CALL
        # Stated as the RATIO rather than against BUDGET_USD: the budget moved
        # from $1.00 to $3.50 on 2026-09-18 and the "as a leaf it would be
        # unaffordable" framing stopped holding, even though the 4.28x
        # overstatement this test is about did not change at all.
        self.assertAlmostEqual(as_a_leaf / cost,
                               td.TOKENS_PER_LEAF / td.GATE_TOKENS_PER_CALL,
                               places=6)
        self.assertGreater(as_a_leaf, cost * 4)
        self.assertLess(cost, td.BUDGET_USD)

    def test_a_gate_ladder_is_capped_at_its_own_attempt_budget(self):
        """4.3 gives a gate ONE climb, so a gate makes at most two calls and a
        third gate rung is as unreachable as a fourth generation rung. The
        three rungs above are capped to two, keeping both ends."""
        self.assertLess(td.GATE_MAX_ATTEMPTS, td.MAX_ATTEMPTS)
        ladder = _table(self._gate_rows()).ladder("security-gate")
        self.assertEqual(len(ladder), td.GATE_MAX_ATTEMPTS)
        self.assertEqual(ladder,
                         ["azure_ai/gpt-5.6-luna", "claude-opus-5"])

    def test_a_generation_ladder_keeps_the_longer_budget(self):
        """The cap must be per-kind, not global: capping generation at 2 would
        remove a rung the pipeline really does run."""
        self.assertEqual(
            len(_table(_widget_rows() + GATE_SINGLE).ladder("widget")),
            td.MAX_ATTEMPTS)

    def test_the_gate_reach_probability_is_the_provisional_one(self):
        """The one figure in this chain that is borrowed rather than measured
        (`GATE_REACH_PROBABILITY_IS_PROVISIONAL`). It is 2.7's P(the generator
        reaches its top rung), used as an upper bound because a gate can only
        climb once the generator has got there -- so it OVERprices, and a gate
        type that fits under it fits under the true figure too.

        Asserted rather than left implicit so that replacing it with a
        measured number is a deliberate edit here, not a silent one."""
        self.assertTrue(td.GATE_REACH_PROBABILITY_IS_PROVISIONAL)
        self.assertEqual(len(td.GATE_REACH_PROBABILITY), td.GATE_MAX_ATTEMPTS)
        self.assertEqual(td.GATE_REACH_PROBABILITY[0], 1.0)
        self.assertAlmostEqual(td.GATE_REACH_PROBABILITY[1],
                               td.REACH_PROBABILITY[-1])
        # Strictly cheaper than a generation ladder's rung 1, which is the
        # whole claim: a gate climbs far less often than a generator escalates.
        self.assertLess(td.GATE_REACH_PROBABILITY[1], td.REACH_PROBABILITY[1])

    def test_the_gate_reach_probability_actually_reaches_the_cost(self):
        """A constant that is defined and then not used would pass every
        assertion above. Halving it must halve the climb rung's contribution,
        and rung 0 is unaffected because its probability is 1.0 either way."""
        table = _table(self._gate_rows())
        base = table.tree_cost_usd("security-gate")
        with patch.object(td, "GATE_REACH_PROBABILITY", (1.0, 1.0 / 12.0)):
            halved = _table(self._gate_rows()).tree_cost_usd("security-gate")
        rung0 = (td.LEAVES_PER_TREE * td.GATE_TOKENS_PER_CALL
                 * 1.0 * 0.0285 / 1_000_000)
        self.assertAlmostEqual(halved - rung0, (base - rung0) / 2, places=9)


class GateTypeCoverageTests(unittest.TestCase):
    """Spec 12, decided 2026-09-17: a task type may not route through a gate
    type that has not itself cleared 1.1.

    These pass the operational set to `CapabilityTable` DIRECTLY rather than
    through `_table`, because `_with_gates_operational` exists to keep the
    other fixtures readable and would mask exactly this rule.
    """

    def _rows(self):
        return _widget_rows() + GATE_SINGLE

    def test_a_type_whose_gate_types_are_not_operational_is_refused(self):
        """The hole 12 records: every 1.1 invariant is per task type, so
        before this `coding` could clear all six while `reviewer-gate`, which
        its stages 3-5 run on, cleared none."""
        table = td.CapabilityTable(self._rows(), operational={"widget"})
        problems = table.validate()
        for gate_type in td.GATE_CALLS:
            with self.subTest(gate_type=gate_type):
                self.assertTrue(
                    any(p.startswith("widget:") and gate_type in p
                        and "not operational" in p for p in problems),
                    problems)

    def test_the_same_table_passes_once_the_gate_types_are_operational(self):
        """The other direction, and the one that proves the rule is about the
        FLAG rather than about the data: identical rows, only the operational
        set differs."""
        table = td.CapabilityTable(
            self._rows(), operational={"widget", *td.GATE_CALLS})
        self.assertEqual(table.validate(), [])

    def test_every_gate_type_is_required_not_just_the_first(self):
        """Flipping one gate type on must not satisfy the rule for the other.
        A loop that broke on its first success, or a check written against a
        single `GATE_TASK_TYPE` constant, would pass with only the reviewer
        gate operational -- which is exactly the pre-split shape."""
        table = td.CapabilityTable(
            self._rows(),
            operational={"widget", td.REVIEWER_GATE_TASK_TYPE})
        problems = table.validate()
        self.assertTrue(
            any(td.SECURITY_GATE_TASK_TYPE in p and "not operational" in p
                for p in problems), problems)

    def test_a_gate_type_is_exempt_from_the_rule(self):
        """A gate does not run gates. Requiring gate types to depend on each
        other would make the pair unsatisfiable: neither could ever be the
        first one flipped."""
        table = td.CapabilityTable(
            self._rows(), operational=set(td.GATE_CALLS))
        self.assertFalse([p for p in table.validate()
                          if "not operational" in p], table.validate())


class AllSixTogetherTests(unittest.TestCase):
    """1.1: the error lists every broken invariant, not the first."""

    def test_every_broken_invariant_is_reported_at_once(self):
        rows = [
            # unresolvable bare name, 10x the reference latency, and sonnet's
            # rate at rung 1 -- one row breaking all three new invariants.
            _row("vllm/fast", "widget", 0.90, 20, 0.0, 10.0),
            _row("claude-sonet-5", "widget", 0.90, 20, _rate_over_budget(),
                 100.0),
        ]
        problems = _table(rows + GATE_SINGLE, operational={"widget"}).validate(
            enforce_latency_ceiling=True, enforce_budget=True)
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
