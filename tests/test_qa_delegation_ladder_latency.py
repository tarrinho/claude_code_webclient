"""QA: the ladder generator orders on cost and cannot see latency.

This is a characterization test, not an aspiration. It records what
``generated_ladder`` does today so the blind spot is visible in the test
suite rather than rediscovered from a breached ceiling -- which is how it
was found on 2026-09-25.

The finding, in production numbers. ``multi-turn`` was the only task type
over the 2,900s combined latency ceiling, at 3,850s. The cause was one rung:
``azure_ai/gpt-5-mini`` at $0.4375 and **58.69s**, chosen over
``claude-sonnet-5`` at $0.4769 and **7.8s** with the same measured accuracy
of 1.0. A 9% cost saving bought at 7.5 times the latency, and nothing in the
generator could weigh that, because it sorts on cost with an accuracy ratchet
and has no latency term at all. Repinning that single rung took the worst
case to 2,675s.

If somebody adds a latency term, ``test_a_cheaper_far_slower_model_still_wins``
fails. That failure is the point: it is the signal to come and read this
file, the ``CEILING_ENFORCEMENT_SETTING`` docstring, and spec 5.1 before
deciding what the new ordering should be. It is not a bug report.
"""
from __future__ import annotations

import unittest

from tiered_delegation import (
    CapabilityRow, CapabilityTable, LATENCY_CEILING_S,
)


def _row(model, task_type, accuracy, cost, latency):
    """One fully-measured row. `n` and `max_context` are filled because an
    incomplete row is ladder-ineligible for a different reason entirely, and
    these tests are about ordering, not eligibility."""
    return CapabilityRow(
        model=model, task_type=task_type, accuracy=accuracy, n=12,
        cost_per_1m_tokens=cost, median_latency_s=latency,
        max_context=200_000,
    )


#: The production shape, reduced to the three rows that decide the ordering.
#: Names and figures are the real ones so the test and the incident describe
#: the same thing.
TASK = "multi-turn"
CHEAP_FAST = _row("azure_ai/gpt-5.6-luna", TASK, 0.917, 0.037, 13.5)
CHEAP_SLOW = _row("azure_ai/gpt-5-mini", TASK, 1.0, 0.4375, 58.69)
DEARER_FAST = _row("claude-sonnet-5", TASK, 1.0, 0.4769, 7.8)

#: The worst-case path charges the two gate types as well as the leaf, and
#: refuses to compute at all when the table names no model for them. They are
#: here to make the figure computable; their own latencies are small and equal
#: across every case below, so they never decide a comparison.
GATE_ROWS = [
    _row("azure_ai/gpt-5.6-luna", "reviewer-gate", 1.0, 0.037, 8.0),
    _row("azure_ai/gpt-5.6-luna", "security-gate", 1.0, 0.037, 8.0),
]


class LadderOrderingTests(unittest.TestCase):
    def setUp(self):
        self.rows = [CHEAP_FAST, CHEAP_SLOW, DEARER_FAST] + GATE_ROWS
        self.table = CapabilityTable(self.rows, operational=[TASK])

    def _pinned_to_the_fast_model(self):
        return CapabilityTable(
            self.rows, operational=[TASK],
            pins={TASK: [CHEAP_FAST.model, DEARER_FAST.model]})

    def test_a_cheaper_far_slower_model_still_wins(self):
        """Cost decides; 50.9 seconds of extra latency does not enter it.

        Asserted as an ORDER rather than as "the fast model is absent". With
        only three rows nothing is truncated, so both appear; in production
        there were seven and the attempt budget dropped the faster one
        entirely. The order is what both cases share, and it is the thing the
        generator actually decides.
        """
        ladder = self.table.generated_ladder(TASK)
        self.assertIn(CHEAP_SLOW.model, ladder)
        self.assertIn(DEARER_FAST.model, ladder)
        self.assertLess(
            ladder.index(CHEAP_SLOW.model), ladder.index(DEARER_FAST.model),
            "the 58.69s model is tried before the 7.8s one at equal accuracy",
        )

    def test_the_two_candidates_really_are_a_latency_trade(self):
        """Guards the fixture, not the code.

        If these figures are ever 'tidied' so the slow model is also cheaper
        by a wide margin, or slower by a little, the test above stops
        demonstrating anything. The trade has to stay sharp: a few percent of
        cost against several times the latency.
        """
        self.assertLess(CHEAP_SLOW.cost_per_1m_tokens,
                        DEARER_FAST.cost_per_1m_tokens)
        self.assertLess(
            (DEARER_FAST.cost_per_1m_tokens - CHEAP_SLOW.cost_per_1m_tokens)
            / DEARER_FAST.cost_per_1m_tokens, 0.15,
            "the cost gap is meant to be small",
        )
        self.assertGreater(
            CHEAP_SLOW.median_latency_s / DEARER_FAST.median_latency_s, 5.0,
            "the latency gap is meant to be large",
        )
        self.assertEqual(CHEAP_SLOW.accuracy, DEARER_FAST.accuracy,
                         "equal accuracy is what makes the choice pure cost")

    def test_the_choice_is_what_breaches_the_ceiling(self):
        """The consequence, which is the only reason the ordering matters.

        Asserted as a comparison between the two ladders rather than against
        a fixed number: the absolute figures move whenever the worst-case
        formula is amended, and that has happened seven times. What must stay
        true is that the slow rung costs materially more worst-case time than
        the fast one.
        """
        with_slow = self.table.worst_case_path_s(TASK)
        with_fast = self._pinned_to_the_fast_model().worst_case_path_s(TASK)
        self.assertIsNotNone(with_slow)
        self.assertIsNotNone(with_fast)
        self.assertGreater(
            with_slow, with_fast * 1.5,
            "the slow rung is supposed to dominate the worst-case path",
        )

    def test_a_pin_can_buy_the_time_back(self):
        """The remedy actually applied on 2026-09-25: repin, do not re-derive.

        Pinning is the only lever that reaches this today, because the
        generator cannot be asked to prefer the faster model.
        """
        pinned = self._pinned_to_the_fast_model()
        self.assertEqual(pinned.ladder(TASK),
                         [CHEAP_FAST.model, DEARER_FAST.model])
        self.assertLess(pinned.worst_case_path_s(TASK), LATENCY_CEILING_S)


if __name__ == "__main__":
    unittest.main()
