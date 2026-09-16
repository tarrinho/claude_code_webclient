"""QA: spec 4.3-4.5 -- the three review gates and what a rejection moves.

The property that is easy to get backwards: a rejection escalates the
GENERATOR, not the reviewer. The reviewer stays put, because a reviewer
that climbs on every rejection is answering bad code by getting more
expensive.

The second property, easy to miss because it is a separate axis from the
first: the GATE itself climbs, but only when it rejects output produced at
the generator's own top rung (4.3, and 4.5's "the gate climbs too") --
rejecting the best generator available is evidence about the gate, not the
code. A mutation that makes the gate climb on every rejection, not just a
top-rung one, is the defect `GateRungClimbTests` exists to catch.

The third property, easiest to get backwards by omission than by
commission: spec 4.5 names TWO different triggers for human involvement,
in different words, and this file reads them as two different outcomes:
the 2-cycle cap ("the leaf fails with a human-flag" -- still a FAILED
leaf) versus surviving the gate's own top rung ("escalated to a human, not
recorded as a failed leaf" -- explicitly not a failure). Collapsing those
two into one outcome, or collapsing either of them into an ordinary
reviewer/QA failure, is the defect `GateExhaustionOutcomeTests` exists to
catch -- so its assertions check the actual outcome value returned, never
a substring of a reason string, and include cases on both sides of each
boundary.
"""
from __future__ import annotations

import unittest

import delegation_pipeline as pipeline


def _reject(gate):
    return pipeline.GateResult(gate=gate, passed=False, reason="nope")


def _ok(gate):
    return pipeline.GateResult(gate=gate, passed=True, reason="")


class GateEscalationTests(unittest.TestCase):
    def test_a_reviewer_rejection_moves_the_generator(self):
        self.assertEqual(
            pipeline.next_generator_rung(0, _reject(pipeline.GATE_REVIEWER)), 1)

    def test_a_qa_rejection_escalates_the_same_way(self):
        self.assertEqual(
            pipeline.next_generator_rung(0, _reject(pipeline.GATE_QA)), 1)

    def test_a_security_rejection_escalates_the_same_way(self):
        self.assertEqual(
            pipeline.next_generator_rung(0, _reject(pipeline.GATE_SECURITY)), 1)

    def test_a_pass_does_not_escalate(self):
        ok = pipeline.GateResult(gate=pipeline.GATE_REVIEWER, passed=True, reason="")
        self.assertEqual(pipeline.next_generator_rung(1, ok), 1)

    def test_escalation_stops_at_the_attempt_cap(self):
        """MAX_ATTEMPTS is 3, so the top rung index is 2 and a rejection there
        does not invent a fourth."""
        self.assertEqual(
            pipeline.next_generator_rung(2, _reject(pipeline.GATE_REVIEWER)), 2)


class GateRungClimbTests(unittest.TestCase):
    """4.3 / 4.5: the gate climbs one rung, but only on a rejection of
    output produced at the generator's OWN top rung -- never on an
    ordinary rejection at a lower generator rung, which is the reviewer
    "stays at Luna" case 4.3 already describes.
    """

    def test_the_gate_does_not_climb_one_rung_below_the_generators_top(self):
        """generator_rung=1, generator_max_rung=2: one below the top, so
        this is an ordinary rejection and the gate must not move."""
        self.assertEqual(
            pipeline.next_gate_rung(
                0, _reject(pipeline.GATE_SECURITY),
                generator_rung=1, generator_max_rung=2, max_rung=1,
            ),
            0,
        )

    def test_the_gate_climbs_when_the_generator_is_at_its_top_rung(self):
        self.assertEqual(
            pipeline.next_gate_rung(
                0, _reject(pipeline.GATE_SECURITY),
                generator_rung=2, generator_max_rung=2, max_rung=1,
            ),
            1,
        )

    def test_the_gate_stops_climbing_at_its_own_cap(self):
        """Already at max_rung=1; a further top-rung rejection must not
        invent rung 2."""
        self.assertEqual(
            pipeline.next_gate_rung(
                1, _reject(pipeline.GATE_SECURITY),
                generator_rung=2, generator_max_rung=2, max_rung=1,
            ),
            1,
        )

    def test_a_pass_does_not_climb_the_gate_either(self):
        self.assertEqual(
            pipeline.next_gate_rung(
                0, _ok(pipeline.GATE_SECURITY),
                generator_rung=2, generator_max_rung=2, max_rung=1,
            ),
            0,
        )


class SecurityCapTests(unittest.TestCase):
    def test_two_cycles_are_allowed(self):
        self.assertFalse(pipeline.security_exhausted(1))
        self.assertFalse(pipeline.security_exhausted(2))

    def test_the_third_is_a_human_failure(self):
        """4.5: a security fix can introduce a new vulnerability, so the cap
        is what prevents infinite recursion -- not the depth or node limits,
        which do not track cycles."""
        self.assertTrue(pipeline.security_exhausted(3))


class GateExhaustionOutcomeTests(unittest.TestCase):
    """4.5's two distinct human-involvement triggers, kept as two distinct
    outcome values, plus reviewer/QA getting neither.
    """

    def test_reviewer_exhaustion_is_an_ordinary_failed_leaf(self):
        self.assertEqual(
            pipeline.gate_exhaustion_outcome(
                pipeline.GATE_REVIEWER, cycles=3, gate_rung=1, gate_max_rung=1),
            pipeline.LEAF_FAILED,
        )

    def test_qa_exhaustion_is_an_ordinary_failed_leaf(self):
        self.assertEqual(
            pipeline.gate_exhaustion_outcome(
                pipeline.GATE_QA, cycles=3, gate_rung=1, gate_max_rung=1),
            pipeline.LEAF_FAILED,
        )

    def test_security_inside_both_caps_is_an_ordinary_failed_leaf(self):
        self.assertEqual(
            pipeline.gate_exhaustion_outcome(
                pipeline.GATE_SECURITY, cycles=1, gate_rung=0, gate_max_rung=1),
            pipeline.LEAF_FAILED,
        )

    def test_security_past_the_cycle_cap_alone_fails_with_a_human_flag(self):
        """The pre-existing 2-cycle-cap rule: still a FAILED leaf, with a
        human alerted alongside it -- gate_rung has not reached its cap,
        so this is NOT the gate's-own-top-rung addition."""
        self.assertEqual(
            pipeline.gate_exhaustion_outcome(
                pipeline.GATE_SECURITY, cycles=3, gate_rung=0, gate_max_rung=1),
            pipeline.FAILED_HUMAN_FLAGGED,
        )

    def test_security_at_its_own_top_rung_escalates_without_failing(self):
        """4.5's addition: surviving the gate's own top rung is escalated
        to a human and explicitly NOT recorded as a failed leaf, even
        while still inside the 2-cycle cap."""
        self.assertEqual(
            pipeline.gate_exhaustion_outcome(
                pipeline.GATE_SECURITY, cycles=1, gate_rung=1, gate_max_rung=1),
            pipeline.ESCALATED_TO_HUMAN,
        )

    def test_the_gate_top_rung_outcome_wins_when_both_caps_are_exhausted(self):
        """4.5 introduces the gate-top-rung rule as "the addition" on top
        of the pre-existing cycle cap, phrased more strongly ("not
        recorded as a failed leaf" vs "fails ... with a human-flag") --
        so when both conditions hold, the addition's outcome applies, not
        the older cycle-cap one."""
        self.assertEqual(
            pipeline.gate_exhaustion_outcome(
                pipeline.GATE_SECURITY, cycles=3, gate_rung=1, gate_max_rung=1),
            pipeline.ESCALATED_TO_HUMAN,
        )

    def test_the_three_outcomes_are_pairwise_distinct(self):
        outcomes = {
            pipeline.LEAF_FAILED,
            pipeline.FAILED_HUMAN_FLAGGED,
            pipeline.ESCALATED_TO_HUMAN,
        }
        self.assertEqual(len(outcomes), 3)


class UnrecognisedGateTests(unittest.TestCase):
    """F6: an unrecognised gate name must be a loud, explicit error -- never
    a silent fall-through to the reviewer/QA path. Before this fix,
    `gate_exhaustion_outcome("Security", ...)` (wrong case) or a stage
    number returned `LEAF_FAILED` -- the safe-looking answer that turns off
    4.5's human-escalation rule with no error and no failing test.
    """

    def test_constructing_a_gateresult_with_an_unknown_name_raises(self):
        with self.assertRaises(ValueError):
            pipeline.GateResult(gate="Security", passed=False, reason="nope")

    def test_a_stage_number_is_not_a_gate_name(self):
        with self.assertRaises(ValueError):
            pipeline.GateResult(gate=5, passed=False, reason="nope")

    def test_a_near_miss_gate_name_is_not_silently_accepted(self):
        with self.assertRaises(ValueError):
            pipeline.GateResult(gate="security_gate", passed=False, reason="nope")

    def test_gate_exhaustion_outcome_rejects_an_unknown_gate_directly(self):
        """`gate_exhaustion_outcome` takes a bare string, not a `GateResult`,
        so it must validate independently -- a caller can reach it without
        ever constructing a `GateResult`."""
        with self.assertRaises(ValueError):
            pipeline.gate_exhaustion_outcome(
                "Security", cycles=3, gate_rung=1, gate_max_rung=1)

    def test_an_unknown_gate_does_not_return_leaf_failed(self):
        """The exact regression this fix closes: an unrecognised gate must
        not quietly resolve to the reviewer/QA outcome."""
        try:
            outcome = pipeline.gate_exhaustion_outcome(
                "security_gate", cycles=3, gate_rung=1, gate_max_rung=1)
        except ValueError:
            pass
        else:
            self.fail(
                f"expected ValueError, got a returned outcome: {outcome!r}")
