"""QA: spec 4.3-4.5 -- the three review gates and what a rejection moves.

The property that is easy to get backwards: a rejection escalates the
GENERATOR, not the reviewer. The reviewer stays put, because a reviewer
that climbs on every rejection is answering bad code by getting more
expensive.

The second property, easier to get backwards by omission than by
commission: a security rejection that survives the gate's own top rung
(spec 4.5) is escalated to a HUMAN, not recorded as a failed leaf. That is
a different outcome from an ordinary reviewer/QA exhaustion, not the same
failure wearing a different label -- so the tests below check the actual
outcome value returned, never a substring of a reason string, and include
a case (security within its cap) where the naive "gate == security" shortcut
would wrongly escalate too.
"""
from __future__ import annotations

import unittest

import delegation_pipeline as pipeline


def _reject(gate):
    return pipeline.GateResult(gate=gate, passed=False, reason="nope")


class GateEscalationTests(unittest.TestCase):
    def test_a_reviewer_rejection_moves_the_generator(self):
        self.assertEqual(pipeline.next_generator_rung(0, _reject("reviewer")), 1)

    def test_a_qa_rejection_escalates_the_same_way(self):
        self.assertEqual(pipeline.next_generator_rung(0, _reject("qa")), 1)

    def test_a_security_rejection_escalates_the_same_way(self):
        self.assertEqual(pipeline.next_generator_rung(0, _reject("security")), 1)

    def test_a_pass_does_not_escalate(self):
        ok = pipeline.GateResult(gate="reviewer", passed=True, reason="")
        self.assertEqual(pipeline.next_generator_rung(1, ok), 1)

    def test_escalation_stops_at_the_attempt_cap(self):
        """MAX_ATTEMPTS is 3, so the top rung index is 2 and a rejection there
        does not invent a fourth."""
        self.assertEqual(pipeline.next_generator_rung(2, _reject("reviewer")), 2)


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
    """4.5's addition: security exhaustion is not just another failed leaf.

    A false reject is indistinguishable from a true one without judgement,
    and discarding correct work silently is the worse of the two errors --
    so the security gate's own cap produces a different outcome value than
    a reviewer or QA gate that keeps rejecting the generator's top rung.
    """

    def test_security_exhaustion_escalates_to_a_human(self):
        self.assertEqual(
            pipeline.gate_exhaustion_outcome("security", 3),
            pipeline.ESCALATED_TO_HUMAN,
        )

    def test_reviewer_exhaustion_is_an_ordinary_failed_leaf(self):
        self.assertEqual(
            pipeline.gate_exhaustion_outcome("reviewer", 3),
            pipeline.LEAF_FAILED,
        )

    def test_qa_exhaustion_is_an_ordinary_failed_leaf(self):
        self.assertEqual(
            pipeline.gate_exhaustion_outcome("qa", 3),
            pipeline.LEAF_FAILED,
        )

    def test_security_within_its_own_cap_is_not_escalated(self):
        """cycles=2 is still allowed (security_exhausted(2) is False), so a
        gate-name-only shortcut ("gate == 'security' -> escalate") must not
        fire here -- the cycle count has to matter too."""
        self.assertEqual(
            pipeline.gate_exhaustion_outcome("security", 2),
            pipeline.LEAF_FAILED,
        )

    def test_the_two_outcomes_are_distinct_values(self):
        self.assertNotEqual(pipeline.ESCALATED_TO_HUMAN, pipeline.LEAF_FAILED)
