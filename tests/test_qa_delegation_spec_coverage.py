# tests/test_qa_delegation_spec_coverage.py
"""QA: the spec 11 verification rows that no delivered test asserts in its
strong form.

Ten tasks shipped their own tests for release 0.19.0 and each one covers the
property it was asked to cover. Nobody audited the delivered suite against
section 11's verification table as a whole, and section 11 states, for almost
every row, BOTH the property and the weaker assertion that would not catch a
break in it. A test that asserts the weak form passes while the defect the row
names ships intact -- that is the class of gap this file exists to close.

Every test below names its section 11 row in its own docstring, so the mapping
from row to test survives the audit that produced it. The gaps closed here,
grouped:

  * the section 3 snapshot was asserted for `coding` alone -- eight of its nine
    rows, the voice ladder, the scoping of the free rung, and the "no row means
    not a candidate" mechanism were untested (rows 995, 996, 997, 1014, 1049,
    1065);
  * the per-type baseline map was asserted for the keys it holds, never for the
    keys it must NOT hold, and no test changed a deadline and checked the router
    followed (rows 998, 1000, 1002);
  * the classifier's score rule was asserted, but never that the score it
    produces reaches the size factor -- the binding score-5 case and the
    score-1-versus-score-4 case both budget a deadline (rows 1036, 1067);
  * the cost ceiling was asserted as arithmetic, never as an admissibility
    decision that depends on the RUNG rather than the model, and none of its
    three inputs (rate versus per-request size, expected task size,
    `LEAVES_PER_TREE`) was shown to move a conclusion (rows 1052, 1053, 1054,
    1056, 1057, 1064);
  * the worst-case path was reproduced on a table with nothing operational, so
    neither side of "it must load / it must stop loading" was exercised through
    the startup path (row 1022);
  * `median_latency_s` was used everywhere and never checked against the runs
    it claims to summarise (row 1024).

Nothing here flips `coding` operational -- spec section 12's third open item
forbids it, and the two rows that need an operational coding-shaped task type
use a synthetic type carrying `coding`'s measured numbers instead. A synthetic
type is absent from `TIER0_BASELINE_CALIBRATION`, so it takes the unknown-type
baseline of 90s and the prohibition is untouched. Note this is NO LONGER the
same figure as `coding`'s own baseline: since the 2026-09-17 derivation change
`coding` derives to 50.625s against the live table, so a synthetic coding-shaped
type is budgeted more generously than real `coding` would be. That is acceptable
here because these tests assert invariants rather than reproduce `coding`'s
numbers, but it is not the identity the earlier wording claimed.
"""
from __future__ import annotations

import json
import statistics
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db
import delegation_classifier as dc
import delegation_pipeline as pipeline
import delegation_startup as ds
from routes.db_users import setting_set
import tiered_delegation as td
from orchestrator import ModelRouter

REPO_ROOT = Path(__file__).resolve().parent.parent


def _row(model, task_type, accuracy=None, n=None, rate=0.0, latency=None,
         max_context=1_000_000):
    return td.CapabilityRow(
        model=model, task_type=task_type, accuracy=accuracy, n=n,
        cost_per_1m_tokens=rate, median_latency_s=latency,
        max_context=max_context,
    )


# One usable `reviewer-gate` row, as every table that computes a worst-case
# path needs at least one (stages 3-5 run on this task type, spec 3 / 4.3).
# A single row means there is no second, climbable rung (spec 4.3/4.5,
# 2026-09-16 amendment) -- deliberate here: none of this file's tests are
# about the gate-climb term, all of them are about the cost-ceiling
# invariant (2.7), and a second, unmeasured `reviewer-gate` row (the real
# state of section 2.6's own table) would make every worst-case path in this
# file incomputable and mask what these tests actually check. The climb term
# itself is covered in tests/test_qa_delegation_invariants.py.
GATE_ROWS = [
    _row("azure_ai/gpt-5.6-luna", "reviewer-gate", 0.95, 28, 0.0285, 11.1,
         922_000),
    _row("azure_ai/gpt-5.6-luna", "security-gate", 0.95, 28, 0.0285, 11.1,
         922_000),
]

VLLM = "vllm/Qwen3.6-35B-A3B-NVFP4"
LUNA = "azure_ai/gpt-5.6-luna"
SONNET = "claude-sonnet-5"
OPUS = "claude-opus-5"
MINI = "azure_ai/gpt-5.4-mini"

#: Spec 2.6's published rates, which every cost figure in section 2.7 is
#: computed from.
RATE = {VLLM: 0.0000, LUNA: 0.0285, MINI: 0.5261, SONNET: 1.5709, OPUS: 3.6082}


# ── Section 3's snapshot, as a fully-measured fixture ────────────────────────
#
# Spec 11 (line 1049): "build a fully-measured fixture, regenerate, and assert
# it equals section 3. The shipped table is mostly TBD, so section 3 is
# unreachable from it by design (2.6); a test that regenerated from live data
# would assert the snapshot is wrong."
#
# So the accuracies below are INVENTED where 2.6 holds TBD, chosen to be
# non-decreasing in cost order for each task type -- which is the only thing
# section 3's ladder shapes require of them. Rates, models and the rows that
# exist per task type are 2.6's own. Two rows are deliberately left unmeasured
# because measuring them would change the ladder section 3 publishes:
#
#   * luna's `comprehension` row. 2.6 holds it TBD and 2.7 names measuring it
#     as the cheapest way to give comprehension an affordable ladder -- but
#     luna is the cheapest comprehension row, so a measured luna would take
#     rung 0 and section 3's `sonnet -> opus` would not regenerate.
#   * every `reasoning` row. Section 3 sets no reasoning rung pending luna's
#     benchmark. See `test_the_reasoning_hold_is_not_enforced_by_the_generator`
#     for what 2.6's own published sonnet row does to that, which is a finding
#     rather than something this fixture can paper over.
SECTION_3_FIXTURE = [
    # coding -- 2.6/3.1's measured rows, unchanged.
    _row(VLLM, "coding", 0.66, 44, RATE[VLLM], 26.8, 229_376),
    _row(LUNA, "coding", 1.00, 24, RATE[LUNA], 12.8, 922_000),
    _row(SONNET, "coding", 1.00, 24, RATE[SONNET], 15.5, 1_000_000),
    _row(MINI, "coding", None, None, RATE[MINI], None, 1_050_000),
    # long-context
    _row(VLLM, "long-context", 1.00, 10, RATE[VLLM], 12.0, 229_376),
    _row(LUNA, "long-context", 1.00, 6, RATE[LUNA], 13.0, 922_000),
    _row(SONNET, "long-context", 1.00, 6, RATE[SONNET], 14.0, 1_000_000),
    # multi-turn
    _row(LUNA, "multi-turn", 0.90, 6, RATE[LUNA], 13.0, 922_000),
    _row(SONNET, "multi-turn", 0.95, 6, RATE[SONNET], 14.0, 1_000_000),
    # planning
    _row(LUNA, "planning", 0.90, 6, RATE[LUNA], 13.0, 922_000),
    _row(SONNET, "planning", 0.95, 6, RATE[SONNET], 14.0, 1_000_000),
    # comprehension -- luna stays TBD (see above), sonnet and opus tie at 1.00
    # so opus survives as rung 1 (equal accuracy is not "worse", spec 3).
    _row(LUNA, "comprehension", None, None, RATE[LUNA], None, 922_000),
    _row(SONNET, "comprehension", 1.00, 2, RATE[SONNET], 14.0, 1_000_000),
    _row(OPUS, "comprehension", 1.00, 2, RATE[OPUS], 15.0, 1_000_000),
    # voice -- luna then sonnet, and 2.6 holds NO opus voice row at all.
    _row(LUNA, "voice", 0.90, 6, RATE[LUNA], 13.0, 922_000),
    _row(SONNET, "voice", 0.95, 6, RATE[SONNET], 14.0, 1_000_000),
    # reasoning -- unmeasured except mini, which is cost-excluded (2.7).
    _row(LUNA, "reasoning", None, None, RATE[LUNA], None, 922_000),
    _row(MINI, "reasoning", 0.86, None, RATE[MINI], None, 1_050_000),
    _row(OPUS, "reasoning", None, None, RATE[OPUS], None, 1_000_000),
    # split-decision
    _row(SONNET, "split-decision", 0.95, 6, RATE[SONNET], 14.0, 1_000_000),
    # reviewer-gate
    _row(LUNA, "reviewer-gate", 0.90, 9, RATE[LUNA], 11.1, 922_000),
    _row(SONNET, "reviewer-gate", 0.95, 6, RATE[SONNET], 14.0, 1_000_000),
]

#: Spec 3's table, verbatim, as the generator must reproduce it. `reasoning` is
#: carried as the empty ladder its row describes ("TBD -- Luna must be
#: benchmarked on reasoning before a rung is set").
SECTION_3_LADDERS = {
    "coding": [VLLM, LUNA, SONNET],
    "long-context": [VLLM, LUNA, SONNET],
    "multi-turn": [LUNA, SONNET],
    "planning": [LUNA, SONNET],
    "comprehension": [SONNET, OPUS],
    "voice": [LUNA, SONNET],
    "reasoning": [],
    "split-decision": [SONNET],
    "reviewer-gate": [LUNA, SONNET],
}


class Section3SnapshotTests(unittest.TestCase):
    """Spec 11 rows 995, 996, 997, 1014, 1049, 1065.

    The delivered suite asserts one of section 3's nine rows (`coding`) and
    nothing about the other eight. Row 995 is explicit that this is not enough:
    "testing only the types named in prose -- assert all nine rows ...
    including the three that break positional order".
    """

    def setUp(self):
        self.table = td.CapabilityTable(SECTION_3_FIXTURE)

    def test_a_measured_fixture_regenerates_every_row_of_section_3(self):
        """Rows 995 and 1049. All nine task types, regenerated from the table
        rather than compared against a hand-written snapshot -- and the three
        rows that break positional order (comprehension starting at sonnet,
        split-decision holding one rung, reviewer-gate being a gate type) are
        in the dict like any other."""
        for task_type, expected in SECTION_3_LADDERS.items():
            with self.subTest(task_type=task_type):
                self.assertEqual(self.table.ladder(task_type), expected)

    def test_voice_has_two_rungs_and_opus_is_not_one_of_them(self):
        """Row 996: "assert voice has a rung 0 and rung 1 and that Opus is not
        a voice rung at any accuracy". Voice is latency-bound (spec 3), so the
        ladder stops at sonnet."""
        ladder = self.table.ladder("voice")
        self.assertEqual(len(ladder), 2)
        self.assertEqual(ladder[0], LUNA)
        self.assertEqual(ladder[1], SONNET)
        self.assertNotIn(OPUS, ladder)

    def test_comprehension_is_sonnet_then_opus_and_has_no_third_rung(self):
        """Row 1014: "assert rung 0 is sonnet, rung 1 is opus, rung 2 is
        absent; rung 2 is empty by design". Asserting only that comprehension
        uses sonnet would pass for a one-rung ladder and for a three-rung
        one."""
        ladder = self.table.ladder("comprehension")
        self.assertEqual(ladder, [SONNET, OPUS])
        self.assertEqual(len(ladder), 2)

    def test_the_free_model_is_attempt_one_for_coding_and_long_context_only(self):
        """Row 997: "assert it is attempt 1 for `coding` and `long-context`
        and attempt 1 for nothing else". Asserting the free model appears
        somewhere would pass for a table that put it at rung 0 of all nine."""
        starts_free = {t for t, rungs in
                       ((t, self.table.ladder(t)) for t in SECTION_3_LADDERS)
                       if rungs and rungs[0] == VLLM}
        self.assertEqual(starts_free, {"coding", "long-context"})
        for task_type in SECTION_3_LADDERS:
            if task_type not in ("coding", "long-context"):
                with self.subTest(task_type=task_type):
                    self.assertNotIn(VLLM, self.table.ladder(task_type))

    def test_adding_a_measured_free_row_makes_it_planning_s_rung_zero(self):
        """Row 1065: "add a measured `vllm/*` planning row in the fixture and
        assert it becomes planning's rung 0, proving absence is what excluded
        it". The delivered suite asserts only the negative half -- that a model
        with no row is not a candidate -- which passes equally for an
        implementation holding a hardcoded list of free-tier task types."""
        self.assertEqual(self.table.ladder("planning"), [LUNA, SONNET])
        widened = td.CapabilityTable(
            SECTION_3_FIXTURE
            + [_row(VLLM, "planning", 0.80, 6, RATE[VLLM], 20.0, 229_376)])
        self.assertEqual(widened.ladder("planning"), [VLLM, LUNA, SONNET])

    def test_the_reasoning_hold_is_not_enforced_by_the_generator(self):
        """Row 995, reasoning. Section 3 sets no reasoning rung, and 2.6 adds a
        blocking constraint against Opus taking the entry rung. Neither is
        code: with 2.6's own published `claude-sonnet-5` reasoning row (75%,
        n=2) the generator hands back a one-rung ladder. Asserted as the
        behaviour it is, so the divergence between section 3's snapshot and
        section 2.6's data is visible rather than folded into a fixture."""
        published = td.CapabilityTable(
            [r for r in SECTION_3_FIXTURE if r.task_type != "reasoning"]
            + [_row(LUNA, "reasoning", None, None, RATE[LUNA], None, 922_000),
               _row(MINI, "reasoning", 0.86, None, RATE[MINI], None, 1_050_000),
               _row(SONNET, "reasoning", 0.75, 2, RATE[SONNET], 14.0, 1_000_000),
               _row(OPUS, "reasoning", None, None, RATE[OPUS], None, 1_000_000)])
        self.assertEqual(published.ladder("reasoning"), [SONNET])


class Tier0DeadlineTests(unittest.TestCase):
    """Spec 11 rows 998 and 1000."""

    def test_tier0_deadline_holds_coding_and_long_context_and_nothing_else(self):
        """Row 998: "asserting the two present keys -- also assert the other
        types are absent". A map that grew a third entry would keep every
        present-key assertion green while silently giving that type a shorter
        deadline than the unknown-type rule grants it."""
        self.assertEqual(set(td.TIER0_BASELINE_CALIBRATION),
                         {"coding", "long-context"})
        for task_type in ("multi-turn", "planning", "comprehension", "voice",
                          "reasoning", "split-decision", "reviewer-gate"):
            with self.subTest(task_type=task_type):
                self.assertNotIn(task_type, td.TIER0_BASELINE_CALIBRATION)

    def test_every_calibration_entry_publishes_its_reference(self):
        """The reference beside each published baseline is what makes the
        figure re-derivable when the fastest model changes. An entry carrying
        a bare number would silently reintroduce the fixed map this replaced,
        and the failure it caused is not visible until a faster model is
        measured -- which is exactly how it shipped."""
        for task_type, entry in td.TIER0_BASELINE_CALIBRATION.items():
            with self.subTest(task_type=task_type):
                published, reference = entry
                self.assertGreater(published, 0)
                self.assertGreater(reference, 0)
                self.assertAlmostEqual(td.baseline_task_ratio(task_type),
                                       published / reference)

    def test_the_deadline_follows_the_map_rather_than_a_hardcoded_number(self):
        """Row 1000: "hardcoding the number in router and test so both agree
        and neither tracks the map -- change a value in the test and assert the
        router follows"."""
        table = td.CapabilityTable(
            [_row("solo", "coding", 1.0, 6, 0.0, 10.0, 1_000_000)])
        ratio = td.baseline_task_ratio("coding")
        self.assertEqual(
            pipeline.effective_deadline(table, "solo", "coding", score=3),
            ratio * 10.0)
        with patch.dict(td.TIER0_BASELINE_CALIBRATION, {"coding": (123.0, 1.0)}):
            self.assertEqual(
                pipeline.effective_deadline(table, "solo", "coding", score=3),
                1230.0)

    def test_the_deadline_follows_the_reference_rather_than_the_published_value(self):
        """The 2026-09-17 defect in one assertion: the published seconds stay
        put while the table's fastest ladder-eligible model changes, and the
        deadline must move anyway. A baseline read straight out of the map
        keeps the published 90.0 here and is what put `coding`'s worst case at
        2,278.1s -- 778.1s over the 1,500s ceiling -- with no model having got
        slower. (778.1s is the overage; 879.9s, which an earlier revision of
        this docstring quoted as the overage, is the increase over the previous
        1,398.2s worst case. The two are different quantities.)"""
        ratio = td.baseline_task_ratio("coding")
        for reference in (10.0, 5.0, 40.0):
            with self.subTest(reference=reference):
                table = td.CapabilityTable(
                    [_row("solo", "coding", 1.0, 6, 0.0, reference, 1_000_000)])
                self.assertEqual(table.baseline_deadline_s("coding"),
                                 ratio * reference)

    def test_the_unknown_type_baseline_follows_a_changed_map(self):
        """Row 999 / 1000 together: the longest baseline must move when the map
        does. A module that returned a literal 90 would pass every delivered
        test and fail here."""
        table = td.CapabilityTable(
            [_row("solo", "widget", 1.0, 6, 0.0, 10.0, 1_000_000)])
        self.assertEqual(
            pipeline.effective_deadline(table, "solo", "widget", score=3), 90.0)
        with patch.dict(td.TIER0_BASELINE_CALIBRATION,
                        {"long-context": (300.0, 4.2)}):
            self.assertEqual(table.unknown_type_baseline_s(), 300.0)
            self.assertEqual(
                pipeline.effective_deadline(table, "solo", "widget", score=3),
                300.0)

    def test_the_unknown_type_baseline_never_falls_below_a_published_value(self):
        """The derived baselines join the same maximum as the published ones,
        so a fleet fast enough to derive `coding` down to 35s must NOT drag the
        unmeasured type's deadline down with it. "We have not measured this"
        must not become a timeout (5.1)."""
        table = td.CapabilityTable([
            _row("solo", "widget", 1.0, 6, 0.0, 10.0, 1_000_000),
            _row("quick", "coding", 1.0, 6, 0.0, 5.0, 1_000_000),
        ])
        self.assertLess(table.baseline_deadline_s("coding"), 90.0)
        self.assertEqual(table.unknown_type_baseline_s(), 90.0)

    def test_changing_a_median_latency_changes_the_deadline(self):
        """Row 1002: "assert ... that changing a `median_latency_s` in the
        table changes the deadline without any other edit". The delivered suite
        asserts the multiplier moves; the deadline is the quantity the spec
        names, and a multiplier that is computed and then discarded would keep
        those tests green."""
        def table(slow_latency):
            return td.CapabilityTable([
                _row("fast", "coding", 1.0, 6, 0.0, 10.0, 1_000_000),
                _row("slow", "coding", 1.0, 6, 0.0, slow_latency, 1_000_000),
            ])
        ratio = td.baseline_task_ratio("coding")
        # "fast" at 10.0 is the 1.0 reference in both tables, and it cancels
        # out of the product: the deadline is the ratio times "slow"'s OWN
        # measured latency, which is the property deriving both halves from
        # one reference buys.
        self.assertEqual(
            pipeline.effective_deadline(table(20.0), "slow", "coding", score=3),
            ratio * 20.0)
        self.assertEqual(
            pipeline.effective_deadline(table(50.0), "slow", "coding", score=3),
            ratio * 50.0)


class ScoreReachesTheBudgetTests(unittest.TestCase):
    """Spec 11 rows 1036 and 1067: the classifier's score is only interesting
    because it budgets a deadline, and no delivered test carries a score from
    `classify()` through to `effective_deadline`."""

    def setUp(self):
        self.table = td.CapabilityTable(
            [_row("solo", "coding", 1.0, 6, 0.0, 10.0, 1_000_000)])

    def test_a_coding_task_matching_the_score_five_pattern_is_budgeted_at_two_x(self):
        """Row 1036: "assert a task matching both a coding pattern and the
        score-5 planning pattern is classified `coding` at score 5 and budgeted
        at 2.0x". `refactor.*large` (13 literals) wins task_type over
        `orchestrate` (11), while `orchestrate`'s score 5 wins the score -- and
        5, not 4, is what makes 1,243s the binding worst case (spec 5.1)."""
        decision = dc.classify("refactor large module and orchestrate the agents")
        self.assertEqual(decision.task_type, "coding")
        self.assertEqual(decision.score, 5)
        self.assertEqual(td.SIZE_FACTOR[decision.score], 2.0)
        self.assertEqual(td.BINDING_SIZE_FACTOR, 2.0)
        baseline = pipeline.effective_deadline(
            self.table, "solo", "coding", score=3)
        budgeted = pipeline.effective_deadline(
            self.table, "solo", decision.task_type, score=decision.score)
        self.assertEqual(budgeted, 2.0 * baseline)

    def test_a_score_one_and_score_four_match_is_budgeted_at_one_and_a_half(self):
        """Row 1067: "assert a text matching score 1 and score 4 patterns
        yields score 4, and that its size factor is 1.5 not 0.5". `quick`
        (score 1) and `refactor.*large` (score 4) both match and both carry
        task_type `coding`, so only the score distinguishes them -- and a
        size factor of 0.5 would hand the largest task the smallest budget."""
        decision = dc.classify("quick refactor large module")
        self.assertEqual(decision.task_type, "coding")
        self.assertEqual(decision.score, 4)
        self.assertEqual(td.SIZE_FACTOR[decision.score], 1.5)
        self.assertNotEqual(td.SIZE_FACTOR[decision.score], 0.5)
        baseline = pipeline.effective_deadline(
            self.table, "solo", "coding", score=3)
        budgeted = pipeline.effective_deadline(
            self.table, "solo", decision.task_type, score=decision.score)
        self.assertEqual(budgeted, 1.5 * baseline)
        # The invariant the row is really about: the larger task gets the
        # larger budget, never the score-1 half-baseline.
        self.assertGreater(budgeted, pipeline.effective_deadline(
            self.table, "solo", "coding", score=1))


class GateAxisTests(unittest.TestCase):
    """Spec 11 rows 1008 and 1035."""

    def test_a_rejection_moves_the_generator_and_leaves_the_gate_where_it_is(self):
        """Row 1008: "asserting a rejection escalates *something* -- assert the
        generator moved and the reviewer did not". The delivered tests assert
        each axis in its own test with its own arguments; the property is about
        one rejection moving one axis and not the other, so both are asserted
        here for a single `GateResult`."""
        rejection = pipeline.GateResult(gate=pipeline.GATE_REVIEWER, passed=False,
                                        reason="nope")
        self.assertEqual(pipeline.next_generator_rung(0, rejection), 1)
        self.assertEqual(
            pipeline.next_gate_rung(0, rejection, generator_rung=0,
                                    generator_max_rung=2, max_rung=1),
            0)

    def test_the_gate_multiplier_ignores_a_non_reference_leaf_latency(self):
        """Row 1035: "assert a `reviewer-gate` multiplier is derived from the
        `reviewer-gate` rows, and that moving a `coding` latency does not
        change it". The delivered suite asserts the first half. The second is
        what tells a gate multiplier apart from a generation multiplier that
        happens to be computed on the gate model's row."""
        def table(other_leaf_latency):
            return td.CapabilityTable([
                _row(LUNA, "widget", 1.0, 6, RATE[LUNA], 10.0, 922_000),
                _row(SONNET, "widget", 1.0, 6, RATE[SONNET],
                     other_leaf_latency, 1_000_000),
                _row(LUNA, "reviewer-gate", None, None, RATE[LUNA], 11.1,
                     922_000),
            ])
        expected = 90 * 1.0 * (11.1 / 10.0)
        first, reason = pipeline.gate_effective_deadline(
            table(12.0), "widget", score=3)
        self.assertIsNone(reason)
        self.assertAlmostEqual(first, expected)
        second, reason = pipeline.gate_effective_deadline(
            table(60.0), "widget", score=3)
        self.assertIsNone(reason)
        self.assertAlmostEqual(second, expected)


class ReadOnlyFloorTests(unittest.TestCase):
    """Spec 11 rows 1016 and 1017: stage 3 is the read-only floor.

    The delivered stage tests construct a `Classification` by hand, so the one
    case row 1016 names -- the `read.*file` pattern, which is score 1 and
    `mutates=False` at once, and therefore meets the trivial bypass and the
    read-only rule in the same leaf -- is never produced by the classifier the
    pipeline actually consumes. A classifier that stopped scoring that pattern
    at 1, or stopped marking it read-only, would leave every stage test green
    while the case row 1016 calls "the one an unscoped bypass leaves with no
    verification at all" changed shape.
    """

    def test_the_read_file_pattern_is_trivial_and_read_only_and_still_runs_stage_three(self):
        """Row 1016: "assert the `read.*file` pattern, which is score 1 and
        `mutates=False` at once, still runs stage 3"."""
        decision = dc.classify("read file config.py")
        self.assertEqual(decision.score, pipeline.TRIVIAL_SCORE)
        self.assertEqual(decision.mutates, dc.MUTATES_FALSE)
        stages = pipeline.stages_for(decision, files_changed=0)
        self.assertEqual(stages, [1, 2, 3])
        self.assertIn(3, stages)

    def test_stage_three_appears_in_every_read_only_combination(self):
        """Row 1017: "enumerate every combination of trivial / non-trivial,
        prose / executable output, and blast radius, and assert stage 3 appears
        in every `mutates=False` row of 4.8's table". Asserting each rule
        separately is what the row warns against: the rules interact, and only
        read-only's last-applied restore keeps stage 3 in the intersection.

        `side_effecting_read` is included here too, and split into its own
        branch below (reversing a 2026-09-16 ruling): a NON-trivial one still
        takes the full five stages, same as `True` (4.7 does not exempt it),
        but a TRIVIAL one keeps stage 3 -- the trivial bypass (4.8) is scoped
        by whether an oracle can still check the output, not by the
        read/write alignment 4.7 uses for the non-trivial case, and a trivial
        side_effecting_read's output may be prose the oracle cannot check."""
        for mutates in (dc.MUTATES_FALSE, dc.MUTATES_SIDE_EFFECTING_READ,
                        dc.MUTATES_TRUE):
            for score in (1, 3, 5):
                for files_changed in (0, pipeline.MAX_FILES_TRIVIAL,
                                      pipeline.MAX_FILES_TRIVIAL + 1, 99):
                    with self.subTest(mutates=mutates, score=score,
                                      files_changed=files_changed):
                        stages = pipeline.stages_for(
                            dc.Classification("coding", score, mutates),
                            files_changed=files_changed)
                        trivial = score <= pipeline.TRIVIAL_SCORE and \
                            files_changed <= pipeline.MAX_FILES_TRIVIAL
                        if mutates == dc.MUTATES_FALSE:
                            self.assertEqual(stages, [1, 2, 3])
                        elif mutates == dc.MUTATES_SIDE_EFFECTING_READ:
                            if trivial:
                                self.assertEqual(stages, [1, 2, 3])
                            else:
                                self.assertEqual(stages, [1, 2, 3, 4, 5])
                        elif trivial:
                            self.assertEqual(stages, [1, 2])
                        else:
                            self.assertEqual(stages, [1, 2, 3, 4, 5])


class CostCeilingPositionTests(unittest.TestCase):
    """Spec 11 rows 1052, 1053, 1054, 1056, 1057 and 1064: the ceiling is a
    check on expected tree cost, so it depends on the rung and on three inputs.
    The delivered suite reproduces 2.7's published arithmetic and asserts one
    affordable and one unaffordable ladder; none of the inputs is moved, and no
    test shows the same model changing answer with its position."""

    def test_the_ceiling_ranks_by_rate_not_by_historical_request_size(self):
        """Row 1052: the mini/Sonnet inversion. `dear-per-request` charges
        0.5261/1M against `cheap-per-request`'s 1.5709/1M, but its historical
        requests average 28,538 tokens against 928 -- so per REQUEST it looks
        3x dearer (2.7's table: $0.01501 against $0.00146) while per TOKEN it
        is 2.99x cheaper. The ladder must follow the rate."""
        rows = [
            _row("azure_ai/dear-per-request", "widget", 0.90, 6, 0.5261, 10.0),
            _row(SONNET, "widget", 0.90, 6, 1.5709, 10.0),
        ]
        table = td.CapabilityTable(rows)
        per_token = table.effective_cost_per_task("azure_ai/dear-per-request",
                                                  "widget")
        sonnet_per_token = table.effective_cost_per_task(SONNET, "widget")
        self.assertLess(per_token, sonnet_per_token)
        # The per-request figures 2.7 measured, which order the other way.
        self.assertGreater(0.01501, 0.00146)
        self.assertEqual(table.ladder("widget"),
                         ["azure_ai/dear-per-request", SONNET])

    def test_the_same_model_costs_more_on_a_larger_task_type(self):
        """Row 1053: "assert `effective_cost_per_task` multiplies the rate by
        the task type's expected tokens, so the same model costs more on a
        larger task type". `EXPECTED_TOKENS` ships empty, so every type takes
        the same default -- a test on one task type cannot tell a rate times a
        size from a bare rate."""
        rows = [
            _row(SONNET, "small-type", 0.90, 6, RATE[SONNET], 10.0),
            _row(SONNET, "large-type", 0.90, 6, RATE[SONNET], 10.0),
        ]
        table = td.CapabilityTable(rows)
        with patch.dict(td.EXPECTED_TOKENS, {"large-type": 120_000}):
            small = table.effective_cost_per_task(SONNET, "small-type")
            large = table.effective_cost_per_task(SONNET, "large-type")
            self.assertAlmostEqual(
                small, RATE[SONNET] * td.DEFAULT_EXPECTED_TOKENS / 1_000_000)
            self.assertAlmostEqual(
                large, RATE[SONNET] * 120_000 / 1_000_000)
            self.assertAlmostEqual(large, small * 10)

    def test_the_same_model_is_admissible_at_rung_two_and_refused_at_rung_zero(self):
        """Row 1054: "assert the same model is admissible at rung 2 and refused
        at rung 0, because reach probability differs; a scalar threshold cannot
        express this and must fail the test". Sonnet costs $3.736 as rung 0 and
        $0.623 as rung 2, against a $1.00 tree budget."""
        at_rung_zero = td.CapabilityTable(
            [_row(SONNET, "widget", 1.00, 6, RATE[SONNET], 10.0, 1_000_000)]
            + GATE_ROWS, operational={"widget", *td.GATE_CALLS})
        self.assertAlmostEqual(at_rung_zero.tree_cost_usd("widget"), 3.7358,
                               places=3)
        problems = at_rung_zero.validate()
        self.assertTrue(any("BUDGET_USD" in p and "widget" in p
                            for p in problems), problems)

        at_rung_two = td.CapabilityTable([
            _row("vllm/a", "widget", 0.90, 6, 0.0, 10.0, 229_376),
            _row("vllm/b", "widget", 0.95, 6, 0.0, 10.0, 229_376),
            _row(SONNET, "widget", 1.00, 6, RATE[SONNET], 10.0, 1_000_000),
        ] + GATE_ROWS, operational={"widget", *td.GATE_CALLS})
        self.assertEqual(at_rung_two.ladder("widget"),
                         ["vllm/a", "vllm/b", SONNET])
        self.assertAlmostEqual(at_rung_two.tree_cost_usd("widget"), 0.6226,
                               places=3)
        self.assertEqual(at_rung_two.validate(), [])

    def test_opus_is_refused_at_every_published_rung(self):
        """Row 1056: "asserting opus is merely expensive -- assert it is
        refused at every rung of every ladder on the measured inputs". $8.582,
        $4.291 and $1.430 against a $1.00 budget, the last of which is the one
        an "it is only dear at rung 0" reading would miss."""
        for rung in range(len(td.REACH_PROBABILITY)):
            with self.subTest(rung=rung):
                self.assertGreater(td.rung_cost_usd(rung, RATE[OPUS]),
                                   td.BUDGET_USD)
        deepest = td.CapabilityTable([
            _row("vllm/a", "widget", 0.90, 6, 0.0, 10.0, 229_376),
            _row("vllm/b", "widget", 0.95, 6, 0.0, 10.0, 229_376),
            _row(OPUS, "widget", 1.00, 6, RATE[OPUS], 10.0, 1_000_000),
        ] + GATE_ROWS, operational={"widget", *td.GATE_CALLS})
        self.assertEqual(deepest.ladder("widget")[2], OPUS)
        self.assertTrue(any("BUDGET_USD" in p for p in deepest.validate()),
                        deepest.validate())

    def test_lowering_leaves_per_tree_makes_sonnet_admissible_at_rung_one(self):
        """Row 1057: "hardcoding 40 -- assert lowering it to 10 makes sonnet
        admissible at rung 1 with no other edit, proving the assumption is what
        drives the exclusions". `LEAVES_PER_TREE` is the one pure assumption in
        2.7 and it scales every figure linearly."""
        table = td.CapabilityTable([
            _row(LUNA, "widget", 0.90, 6, RATE[LUNA], 10.0, 922_000),
            _row(SONNET, "widget", 0.95, 6, RATE[SONNET], 10.5, 1_000_000),
        ] + GATE_ROWS, operational={"widget", *td.GATE_CALLS})
        self.assertEqual(table.ladder("widget"), [LUNA, SONNET])
        self.assertAlmostEqual(table.tree_cost_usd("widget"), 1.9359, places=3)
        self.assertTrue(any("BUDGET_USD" in p for p in table.validate()))

        with patch.object(td, "LEAVES_PER_TREE", 10):
            self.assertAlmostEqual(table.tree_cost_usd("widget"), 0.4840,
                                   places=3)
            self.assertEqual(table.validate(), [])

    def test_a_type_whose_every_rung_is_excluded_goes_non_operational_and_falls_back(self):
        """Row 1064: "assert a task type whose every rung is excluded goes
        non-operational and falls back, rather than routing to an excluded
        model". Mini is measured, priced and complete here -- only 2.7's
        operator exclusion keeps it out -- so an implementation that applied
        the exclusion to eligibility but not to routing would return it."""
        table = td.CapabilityTable(
            [_row(MINI, "long-context", 1.00, 20, RATE[MINI], 10.0, 1_050_000)]
            + GATE_ROWS, operational={"long-context", *td.GATE_CALLS})
        self.assertEqual(table.ladder("long-context"), [])
        self.assertTrue(any("ladder is empty" in p and "long-context" in p
                            for p in table.validate()), table.validate())

        router = ModelRouter({"rules": []})
        with self.assertLogs("wc.orchestrator", level="WARNING") as captured:
            chosen = router.assign_model("read file", "list directory",
                                         complexity=1, table=table)
        self.assertEqual(chosen, config.ANTHROPIC_MODEL)
        self.assertNotIn(MINI, chosen)
        self.assertTrue(any("long-context" in line for line in captured.output),
                        captured.output)


class MedianLatencyProvenanceTests(unittest.TestCase):
    """Spec 11 row 1024: "asserting the cell has a value -- assert it equals
    the median of that model's recorded runs. The 2026-09-15 values were single
    samples (vllm 33.2 where the median was 45.8), and the column feeds both
    the deadline and the ceiling, so a sample here corrupts two derived
    quantities silently."

    Nothing in the delivered suite compares the column against the runs it
    claims to summarise; every test takes 26.8 / 12.8 / 15.5 as given, which is
    precisely the state that let three single samples ship under a median's
    name.
    """

    #: Spec 2.6 / 3.1's coding column, and its provenance: the runs recorded in
    #: these four files, less the `floor-add` control (3.1: "n=20 plus a floor
    #: control"), which is not a coding task.
    RUN_FILES = (
        "bench/coding_accuracy_20260915.json",
        "bench/coding_vllm_n20_20260915.json",
        "bench/coding_edits_20260915.json",
        "bench/coding_edits_sonnet_20260915.json",
    )
    PUBLISHED = {VLLM: (26.8, 44), LUNA: (12.8, 24), SONNET: (15.5, 24)}

    def _recorded(self):
        latencies: dict[str, list[float]] = {}
        for name in self.RUN_FILES:
            path = REPO_ROOT / name
            self.assertTrue(path.exists(), f"{name} is the column's provenance")
            for run in json.loads(path.read_text(encoding="utf-8"))["runs"]:
                if run.get("task") == "floor-add":
                    continue                      # the control, not a coding task
                if run.get("total_s") is None:
                    continue
                latencies.setdefault(run["model"], []).append(run["total_s"])
        return latencies

    def test_the_published_coding_latencies_are_medians_of_the_recorded_runs(self):
        recorded = self._recorded()
        for model, (published, n) in self.PUBLISHED.items():
            with self.subTest(model=model):
                runs = recorded[model]
                self.assertEqual(len(runs), n,
                                 "the n column is the number of runs behind it")
                self.assertAlmostEqual(statistics.median(runs), published,
                                       places=1)

    def test_a_single_sample_would_not_reproduce_the_published_value(self):
        """The failure this row records, reproduced: lifting one run's
        `total_s` out of the sample gives a different number, so a column
        holding a sample cannot be told from one holding a median by looking at
        it. Asserted on `vllm`, whose spread is widest."""
        runs = self._recorded()[VLLM]
        median = statistics.median(runs)
        first, last = runs[0], runs[-1]
        self.assertNotAlmostEqual(first, median, places=1)
        self.assertNotAlmostEqual(last, median, places=1)


def _latency_over_the_ceiling() -> float:
    """A free-rung latency large enough to breach whatever the ceiling is.

    Was a literal 60.0, chosen against a 1,500s ceiling. When the ceiling was
    raised to 2,900s on 2026-09-18 that stopped breaching, and the tests below
    asserted a refusal that no longer happened -- they passed only because
    `assertRaises` was the thing that failed, which is luck rather than
    coverage. Scaled off the constant so the breach survives the next change.
    """
    return 60.0 * (td.LATENCY_CEILING_S / 1_500) * 1.2


class CeilingVersusAttemptBudgetTests(unittest.IsolatedAsyncioTestCase):
    """Spec 11 row 1022: "assert startup FAILS for an operational task type
    whose computed worst case exceeds the ceiling. Both sides: an operational
    `coding` type at score 5 (1,242s) must LOAD against the 1,500s ceiling, and
    raising any ladder-eligible `median_latency_s` enough to push the sum past
    1,500 must stop it loading."

    The delivered suite reproduces 1,243.125s on a table where nothing is
    operational, so neither side of "loads / does not load" is exercised
    through the startup path at all.

    `coding` is NOT flipped operational here -- spec 12 forbids it. The
    synthetic type below carries `coding`'s measured rows, and because it is
    absent from `TIER0_BASELINE_CALIBRATION` it takes the unknown-type baseline
    of 90s, with nothing routed and no prohibition touched. The 90s is no longer
    `coding`'s own baseline -- since 2026-09-17 that derives against the live
    reference -- so this reproduces the 2026-09-15 arithmetic on a fixed
    baseline, which is what the startup path under test needs, not `coding`'s
    current figure.
    """

    TASK_TYPE = "coding-shaped"

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

    async def _seed(self, vllm_latency):
        for model, accuracy, n, rate, latency, context in (
            (VLLM, 0.66, 44, RATE[VLLM], vllm_latency, 229_376),
            (LUNA, 1.00, 24, RATE[LUNA], 12.8, 922_000),
            (SONNET, 1.00, 24, RATE[SONNET], 15.5, 1_000_000),
        ):
            await db.delegation_row_set(
                model, self.TASK_TYPE, accuracy=accuracy, n=n,
                cost_per_1m_tokens=rate, median_latency_s=latency,
                max_context=context)
        await db.delegation_row_set(
            LUNA, "reviewer-gate", accuracy=0.95, n=28,
            cost_per_1m_tokens=RATE[LUNA], median_latency_s=11.1,
            max_context=922_000)
        # Stage 5's own task type since the 2026-09-17 split. Same latency as
        # the reviewer gate, so 2 reviewer calls + 1 security call reproduce
        # the arithmetic this test was written against (one figure x 3).
        await db.delegation_row_set(
            LUNA, "security-gate", accuracy=0.95, n=28,
            cost_per_1m_tokens=RATE[LUNA], median_latency_s=11.1,
            max_context=922_000)
        # Spec 12's coverage rule (2026-09-17): the gate types must be
        # operational before an ordinary type may route through them.
        for gate_type in td.GATE_CALLS:
            await db.delegation_operational_set(gate_type, True)
        await db.delegation_operational_set(self.TASK_TYPE, True)
        # `validate_or_die` (delegation_startup.py) now wires the live model
        # list into 1.1's resolution check instead of the config.KNOWN_MODELS
        # fallback (bare Anthropic ids only), so the backend-qualified rungs
        # here -- VLLM and LUNA -- need a machine that actually serves them or
        # this class's own "must not raise" assertion fails for a reason that
        # has nothing to do with what it tests. SONNET is a bare id and
        # already resolves through config.KNOWN_MODELS.
        # `db.ai_machine_create` returns its write timestamp, not the id --
        # reuse the id passed in for the follow-up update.
        machine_id = "m-coding-shaped"
        await db.ai_machine_create(
            machine_id, "Test Machine", "localhost", 0, None, VLLM,
            None, None, "tester", provider="claude_code",
        )
        await db.ai_machine_set_models(machine_id, "tester", [VLLM, LUNA], VLLM)

    async def test_a_coding_shaped_type_at_its_binding_score_loads(self):
        await self._seed(26.8)
        table = await ds.validate_or_die()          # must not raise
        self.assertEqual(table.ladder(self.TASK_TYPE), [VLLM, LUNA, SONNET])
        self.assertAlmostEqual(table.worst_case_path_s(self.TASK_TYPE),
                               2179.6875, places=3)
        self.assertLess(table.worst_case_path_s(self.TASK_TYPE),
                        td.LATENCY_CEILING_S)

    async def test_raising_a_ladder_eligible_latency_stops_it_loading(self):
        """The other side, with one column changed and nothing else: 60.0s for
        the free rung puts the sum at 9.50 and the worst case at 1,710s.

        Enforcement is turned ON here, because this is the test of the ceiling
        INVARIANT. 9.2's knob is off by default (see
        `td.CEILING_ENFORCEMENT_SETTING`), and the off case is
        `test_the_same_table_loads_with_the_ceiling_knob_off` below -- the two
        together are what stop this from reading as either "the ceiling never
        blocks" or "the knob does nothing"."""
        await self._seed(_latency_over_the_ceiling())
        await setting_set(td.CEILING_ENFORCEMENT_SETTING, "1")
        with self.assertRaises(ds.DelegationConfigError) as ctx:
            await ds.validate_or_die()
        message = str(ctx.exception)
        self.assertIn(self.TASK_TYPE, message)
        self.assertIn("ceiling", message)
        self.assertIn(str(td.LATENCY_CEILING_S), message)

    async def test_the_same_table_loads_with_the_ceiling_knob_off(self):
        """Identical table, knob off (the default): it must LOAD, and the
        breach must still be visible rather than silently dropped.

        Two assertions, and the second is the one that matters. A knob that
        merely skipped the check would pass the first on its own while making
        "off" mean "not measured" -- so the breach is also asserted to come
        back from `latency_ceiling_breaches`, which is what the settings page
        and the boot log both read."""
        await self._seed(_latency_over_the_ceiling())
        self.assertFalse(td.CEILING_ENFORCEMENT_DEFAULT)
        table = await ds.validate_or_die()          # must not raise
        breaches = table.latency_ceiling_breaches()
        self.assertIn(self.TASK_TYPE, breaches)
        self.assertIn("ceiling", breaches[self.TASK_TYPE])
        self.assertIn(str(td.LATENCY_CEILING_S), breaches[self.TASK_TYPE])

    async def test_an_explicit_off_value_is_honoured_like_a_missing_row(self):
        """A stored "0" and no row at all must behave identically. A reader
        that treated "any stored value" as on would turn the act of switching
        enforcement OFF into switching it on."""
        await self._seed(_latency_over_the_ceiling())
        await setting_set(td.CEILING_ENFORCEMENT_SETTING, "0")
        await ds.validate_or_die()                  # must not raise
        self.assertFalse(await ds.ceiling_enforcement_enabled())


if __name__ == "__main__":
    unittest.main()
