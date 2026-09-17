"""QA: spec 5.1 -- effective_deadline = per_type_baseline x size_factor x
model_speed_multiplier, and the multiplier's reference set is
LADDER-ELIGIBLE models only.

The multiplier is DERIVED from the benchmark table, never stored. Three
successive revisions of this arithmetic were wrong (7.40, 7.77, 7.39) while
the conclusion survived each time, which is why these tests reproduce the
number from the table rather than asserting a constant.

Separately: 5.1 is emphatic that the 1.0 reference comes from
ladder-eligible rows only, never every row -- a cost-excluded or unmeasured
model setting the reference would shrink every real rung's deadline. A
mutation of exactly that property went uncaught in an earlier task because
no fixture had a non-eligible row fast enough to expose it as the reference
by mistake. Every fixture below that computes a multiplier includes one
("ghost": unmeasured accuracy, far faster than the real rows).
"""
from __future__ import annotations

import unittest

import delegation_pipeline as pipeline
from tiered_delegation import (
    CapabilityRow,
    CapabilityTable,
    TIER0_BASELINE_CALIBRATION,
    baseline_task_ratio,
)

#: `_table()`'s coding reference. The baseline is derived against it, so every
#: expected figure below is `CODING_RATIO x 10.0 x size x multiplier` rather
#: than a fixed 90 -- see `CapabilityTable.baseline_deadline_s`.
CODING_RATIO = baseline_task_ratio("coding")          # 90.0 / 12.8 = 7.03125
CODING_BASELINE = CODING_RATIO * 10.0                 # 70.3125


def _table():
    return CapabilityTable([
        CapabilityRow("fast", "coding", 1.0, 24, 1.57, 10.0, 1_000_000),
        CapabilityRow("slow", "coding", 0.66, 44, 0.0, 30.0, 229_376),
        # Not ladder-eligible: accuracy is unmeasured (None). Far faster than
        # "fast". If the reference set ever widens to "every row", this row
        # -- not "fast" -- becomes the 1.0 reference and every assertion
        # below changes.
        CapabilityRow("ghost", "coding", None, None, 0.10, 0.001, 1_000_000),
    ])


class MultiplierTests(unittest.TestCase):
    def test_the_fastest_eligible_model_is_exactly_one(self):
        self.assertEqual(pipeline.speed_multiplier(_table(), "fast", "coding"), 1.0)

    def test_a_model_three_times_slower_is_three(self):
        self.assertEqual(pipeline.speed_multiplier(_table(), "slow", "coding"), 3.0)

    def test_a_faster_non_eligible_row_never_becomes_the_reference(self):
        """"ghost" is unmeasured and 10,000x faster than "fast". A reference
        set that is not filtered to ladder-eligible rows would divide by
        0.001 instead of 10.0, and "fast" would come out at 10000.0 instead
        of 1.0."""
        self.assertEqual(pipeline.speed_multiplier(_table(), "fast", "coding"), 1.0)
        self.assertEqual(pipeline.speed_multiplier(_table(), "ghost", "coding"), 0.0001)

    def test_changing_a_latency_changes_the_multiplier(self):
        """The derivation is the durable part. A multiplier that survives a
        latency change unchanged is a constant wearing a derivation's
        clothes."""
        table = CapabilityTable([
            CapabilityRow("fast", "coding", 1.0, 24, 1.57, 10.0, 1_000_000),
            CapabilityRow("slow", "coding", 0.66, 44, 0.0, 50.0, 229_376),
            CapabilityRow("ghost", "coding", None, None, 0.10, 0.001, 1_000_000),
        ])
        self.assertEqual(pipeline.speed_multiplier(table, "slow", "coding"), 5.0)

    def test_unknown_model_falls_back_to_one(self):
        self.assertEqual(
            pipeline.speed_multiplier(_table(), "nobody", "coding"), 1.0)

    def test_unmeasured_task_type_falls_back_to_one(self):
        self.assertEqual(
            pipeline.speed_multiplier(_table(), "fast", "reasoning"), 1.0)


class DeadlineTests(unittest.TestCase):
    def test_a_mid_sized_task_on_the_fastest_model_is_the_baseline(self):
        self.assertEqual(
            pipeline.effective_deadline(_table(), "fast", "coding", score=3),
            CODING_BASELINE)

    def test_the_size_factor_scales_it(self):
        """Score 3 is the reference point; 1 halves and 5 doubles."""
        self.assertEqual(
            pipeline.effective_deadline(_table(), "fast", "coding", score=1),
            CODING_BASELINE * 0.5)
        self.assertEqual(
            pipeline.effective_deadline(_table(), "fast", "coding", score=5),
            CODING_BASELINE * 2.0)

    def test_all_three_factors_compose(self):
        """baseline x size 2.0 x multiplier 3.0."""
        self.assertEqual(
            pipeline.effective_deadline(_table(), "slow", "coding", score=5),
            CODING_BASELINE * 2.0 * 3.0)

    def test_the_baseline_follows_the_reference_rather_than_a_fixed_number(self):
        """5.1 defines the baseline as the type's cost "on the fastest model
        measured for it", so it must move when that model does. A fixed
        seconds map kept the baseline at its luna-era 90s while the
        multipliers renormalised onto a faster reference, which is what put
        `coding`'s worst case over the ceiling on 2026-09-17 with no model
        having got slower.

        Same fixture, reference halved: every deadline must halve with it.
        """
        halved = CapabilityTable([
            CapabilityRow("fast", "coding", 1.0, 24, 1.57, 5.0, 1_000_000),
            CapabilityRow("slow", "coding", 0.66, 44, 0.0, 15.0, 229_376),
            CapabilityRow("ghost", "coding", None, None, 0.10, 0.001, 1_000_000),
        ])
        self.assertEqual(
            pipeline.effective_deadline(halved, "fast", "coding", score=3),
            CODING_BASELINE / 2)

    def test_the_reference_cancels_so_the_deadline_tracks_measured_latency(self):
        """The property the derivation buys: `ratio x reference x (latency /
        reference)` is `ratio x latency`, so a model's own deadline depends on
        its OWN measured latency and not on how fast its neighbours are.

        "slow" is 30.0s in both tables below; only its neighbours differ.
        """
        faster_neighbours = CapabilityTable([
            CapabilityRow("fast", "coding", 1.0, 24, 1.57, 2.0, 1_000_000),
            CapabilityRow("slow", "coding", 0.66, 44, 0.0, 30.0, 229_376),
        ])
        self.assertAlmostEqual(
            pipeline.effective_deadline(_table(), "slow", "coding", score=3),
            pipeline.effective_deadline(faster_neighbours, "slow", "coding",
                                        score=3))
        self.assertAlmostEqual(
            pipeline.effective_deadline(_table(), "slow", "coding", score=3),
            CODING_RATIO * 30.0)

    def test_a_task_type_with_no_baseline_gets_the_longest_not_the_shortest(self):
        """5.1: an unknown type must receive the LONGEST deadline. Defaulting
        to the shortest turns "we have not measured this" into a timeout.

        `_table()` holds no `reasoning` rows, so the derivation has no
        reference and the unknown-type rule applies. The longest is taken over
        the derived baselines AND the published ones, so it can never fall
        below what 5.1 prints: here `coding` derives to 70.3125 against a
        published 90.0, and 90.0 must win.
        """
        published = [b for b, _ in TIER0_BASELINE_CALIBRATION.values()]
        self.assertEqual(
            pipeline.effective_deadline(_table(), "fast", "reasoning", score=3),
            float(max(published)))

    def test_unknown_type_deadline_is_not_the_shortest(self):
        """Belt and suspenders on the same invariant, with the two published
        baselines pinned to differ so a defaulting-to-shortest bug cannot
        agree with the correct answer by coincidence."""
        published = [b for b, _ in TIER0_BASELINE_CALIBRATION.values()]
        self.assertNotEqual(min(published), max(published))  # guards the test
        result = pipeline.effective_deadline(_table(), "fast", "reasoning", score=3)
        self.assertNotEqual(result, float(min(published)))

    def test_a_calibrated_type_with_no_reference_keeps_its_published_baseline(self):
        """A table whose only row for the type is unmeasured has no 1.0
        reference, so nothing can be derived. The fallback is that type's own
        PUBLISHED baseline -- not a derived figure, and not the unknown-type
        maximum: the type is calibrated, it just has no data today.

        The type under test is `long-context` and that is the whole point of
        the test. An earlier version used `coding`, whose published 90.0 IS the
        unknown-type maximum, so the correct implementation and one that fell
        straight through to `unknown_type_baseline_s()` returned the same
        number and the test could not fail. `long-context` publishes 45.0
        against a maximum of 90.0, so the two answers differ.
        """
        published = TIER0_BASELINE_CALIBRATION["long-context"][0]
        table = CapabilityTable([
            CapabilityRow("ghost", "long-context", None, None, 0.10, 0.001,
                          1_000_000),
        ])
        # Guards the test itself: if these two ever coincide, this has gone
        # back to being the assertion that could not fail.
        self.assertNotEqual(published, table.unknown_type_baseline_s())
        self.assertEqual(
            pipeline.effective_deadline(table, "ghost", "long-context", score=3),
            published)

    def test_the_unknown_type_baseline_is_not_moved_by_a_slow_calibrated_fleet(self):
        """The direction that went untested when the unknown-type baseline
        briefly consulted the table: a derived baseline is `ratio x reference`
        and nothing bounds `reference`, so one slow calibrated type could set
        the deadline for every type nobody has measured.

        `coding`'s ratio is 7.03125, so a sole row at 600s derives a baseline
        of 4,218.75s. The unknown type must still get the published 90.0.
        """
        for latency in (12.8, 60.0, 600.0):
            with self.subTest(coding_latency=latency):
                table = CapabilityTable([
                    CapabilityRow("m", "coding", 1.0, 12, 0.0, latency,
                                  1_000_000),
                    CapabilityRow("w", "widget", 1.0, 12, 0.0, 10.0, 1_000_000),
                ])
                self.assertEqual(table.unknown_type_baseline_s(), 90.0)
                self.assertEqual(
                    pipeline.effective_deadline(table, "w", "widget", score=3),
                    90.0)
        # The clamp must not reach the calibrated type itself: `coding` still
        # derives, which is what the whole 2026-09-17 change is for.
        slow = CapabilityTable(
            [CapabilityRow("m", "coding", 1.0, 12, 0.0, 600.0, 1_000_000)])
        self.assertEqual(slow.baseline_deadline_s("coding"),
                         CODING_RATIO * 600.0)


class GateDeadlineTests(unittest.TestCase):
    """5.1: a model-backed gate's deadline uses the LEAF's own task type and
    score for baseline and size factor, but the GATE model's multiplier --
    the reviewer-gate row (or the leaf task type's own row as fallback),
    against the leaf task type's own reference. Spec's worked example:
    reference 12.8 (luna, coding), gate row 11.1 (luna, reviewer-gate),
    multiplier 11.1/12.8.
    """

    def test_gate_deadline_uses_the_gate_row_and_the_leaf_reference(self):
        table = CapabilityTable([
            CapabilityRow("azure_ai/gpt-5.6-luna", "coding",
                          1.0, 24, 1.0, 12.8, 1_000_000),
            CapabilityRow("azure_ai/gpt-5.6-luna", "reviewer-gate",
                          1.0, 9, 1.0, 11.1, 1_000_000),
        ])
        # 12.8 is `coding`'s calibration reference, so the derived baseline is
        # exactly the published 90.0 here and the spec's worked example is
        # reproduced unchanged.
        expected = 90.0 * 1.0 * (11.1 / 12.8)
        value, reason = pipeline.gate_effective_deadline(table, "coding", score=3)
        self.assertIsNone(reason)
        self.assertAlmostEqual(value, expected)

    def test_gate_deadline_falls_back_to_the_leaf_task_type_row(self):
        """A reviewer-gate row exists for the gate model but has no measured
        median_latency_s yet -- 5.1's fallback: use the leaf's own
        task-type row for the same model instead."""
        table = CapabilityTable([
            CapabilityRow("solo", "reviewer-gate", 1.0, 6, 1.0, None, 1_000_000),
            CapabilityRow("solo", "coding", 1.0, 6, 1.0, 20.0, 1_000_000),
        ])
        # Reference 20.0, so the baseline derives to CODING_RATIO x 20.0
        # rather than the published 90.0.
        expected = (CODING_RATIO * 20.0) * 1.0 * (20.0 / 20.0)
        value, reason = pipeline.gate_effective_deadline(table, "coding", score=3)
        self.assertIsNone(reason)
        self.assertAlmostEqual(value, expected)

    def test_gate_deadline_reports_when_no_gate_model_can_be_named(self):
        """No reviewer-gate row anywhere in the table -- there is no model
        to time the gate against. `coding` itself has a valid, ladder-
        eligible row, so `latency_reference_s("coding")` succeeds and
        cannot be the guard that fires; only the missing-gate-row guard
        can produce this failure, and the reason string must say so by
        naming "reviewer-gate" -- a bare "it failed" would not tell one
        guard apart from the other."""
        table = CapabilityTable([
            CapabilityRow("solo", "coding", 1.0, 6, 1.0, 20.0, 1_000_000),
        ])
        value, reason = pipeline.gate_effective_deadline(table, "coding", score=3)
        self.assertIsNone(value)
        self.assertIn("reviewer-gate", reason)


if __name__ == "__main__":
    unittest.main()
