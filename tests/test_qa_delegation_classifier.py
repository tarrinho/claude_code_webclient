"""QA: the classifier of spec section 2.

Properties that are easy to get wrong and invisible when wrong: specificity
is the matched alternative's literal-character count (not the whole pattern's
length, not list order), ties resolve by matched span then table order, a
score is the MAXIMUM of matched patterns rather than the winning alternative's
own, mutates resolves to True on any disagreement (not a three-way ladder),
and every default -- including score, which the spec itself leaves unstated --
fails toward the expensive/safe branch.
"""
from __future__ import annotations

import unittest

import delegation_classifier as dc


class ClassifyTests(unittest.TestCase):
    def test_a_plain_read_is_long_context_and_does_not_mutate(self):
        result = dc.classify("read file config.py and summarize log output")
        self.assertEqual(result.task_type, "long-context")
        self.assertEqual(result.score, 1)
        self.assertEqual(result.mutates, dc.MUTATES_FALSE)

    def test_unmatched_text_defaults_to_comprehension_and_mutates(self):
        """Section 2.2 states only two defaults -- task_type and mutates --
        and both fail toward the expensive/safe branch. Score is a third
        default the spec does not state; this ruling supplies 3 (5.1's
        reference/mid-size score) rather than the table's minimum, because
        under-budgeting a deadline manufactures a false timeout escalation."""
        result = dc.classify("zzzz nothing matches this at all")
        self.assertEqual(result.task_type, "comprehension")
        self.assertEqual(result.mutates, dc.MUTATES_TRUE)
        self.assertEqual(result.score, dc.DEFAULT_SCORE)
        self.assertEqual(result.score, 3)

    def test_score_is_the_maximum_of_every_match_not_the_winner_s(self):
        """Section 2.1. Score feeds the size factor and therefore the deadline;
        under-budgeting manufactures a false timeout, which costs a rung.

        Input verified against the real PATTERNS table: `list.*directory`
        (literal 13, span 14, row 7, long-context, score 1) is the most
        specific matched alternative and wins task_type, but `orchestrate`
        (literal 11, span 11, row 1, planning, score 5) is also matched and
        carries the higher score. Returning the winner's own score (1)
        instead of the maximum across matched rows (5) fails this test. The
        same input also shows specificity beating list order from the other
        direction: the winning alternative sits in a *later* table row than
        the losing one.
        """
        result = dc.classify("list directory and orchestrate the agents")
        self.assertEqual(result.score, 5)
        self.assertEqual(result.task_type, "long-context")

    def test_a_mutates_conflict_resolves_to_true(self):
        """Section 2.1: if any two matched patterns disagree, the answer is True."""
        # "read file" is False; "fix typo" is True.
        result = dc.classify("read file and fix typo in it")
        self.assertEqual(result.mutates, dc.MUTATES_TRUE)

    def test_task_type_is_decided_by_specificity_not_list_order(self):
        """Input verified against the real PATTERNS table: `design.*system`
        (literal 12, span 13, row 0, planning) and `debug.*complex` (literal
        12, span 13, row 2, reasoning) tie on both literal count and span, so
        table order decides -- row 0 beats row 2. The two task types differ,
        so this assertion can actually fail, unlike an input where every
        matched alternative shares one task_type.
        """
        result = dc.classify("design system and debug complex")
        self.assertEqual(result.task_type, "planning")

    def test_specificity_is_literal_character_count_not_pattern_length(self):
        """Section 11, line 1057's mandated test: specificity is computed by
        asserting a hand-picked winner -- `refactor.*large` beats `quick` on
        "quick refactor large module" by literal-character count, and a tie
        falls to longer match span then table order.

        Both alternatives carry task_type "coding", so a test that only
        inspected classify().task_type could not observe which one won; the
        winning alternative is asserted directly.
        """
        self.assertEqual(dc.literal_character_count("refactor.*large"), 13)
        self.assertEqual(dc.literal_character_count("quick"), 5)
        self.assertEqual(dc.winning_alternative("quick refactor large module"), "refactor.*large")

    def test_specificity_tie_breaks_on_matched_span_before_table_order(self):
        """Input verified against the real PATTERNS table: `grep.*pattern`
        (literal 11, span 12, row 7, long-context) and `orchestrate` (literal
        11, span 11, row 1, planning) tie on literal count. The longer
        matched span (12 vs 11) decides in favour of the later row. If span
        were skipped and the tie fell straight to table order, row 1 would
        win instead and the result would be "planning".
        """
        result = dc.classify("orchestrate and grep pattern here")
        self.assertEqual(result.task_type, "long-context")

    def test_specificity_tie_breaks_on_table_order_as_a_last_resort(self):
        """Input verified against the real PATTERNS table: `refactor.*large`
        (literal 13, span 14, row 4, coding) and `list.*directory` (literal
        13, span 14, row 7, long-context) tie on both literal count and span,
        so the earlier row wins.
        """
        result = dc.classify("refactor large and list directory")
        self.assertEqual(result.task_type, "coding")


class MutatesVocabularyTests(unittest.TestCase):
    def test_the_three_values_are_distinct(self):
        """Section 2.3 is three-valued. Folding side_effecting_read into
        either neighbour is the mistake this guards."""
        values = {dc.MUTATES_FALSE, dc.MUTATES_SIDE_EFFECTING_READ, dc.MUTATES_TRUE}
        self.assertEqual(len(values), 3)

    def test_every_pattern_declares_a_known_mutates_value(self):
        known = {dc.MUTATES_FALSE, dc.MUTATES_SIDE_EFFECTING_READ, dc.MUTATES_TRUE}
        for pattern, _score, _task_type, mutates in dc.PATTERNS:
            self.assertIn(mutates, known, pattern)

    def test_resolve_mutates_unanimous_side_effecting_read_is_preserved(self):
        """No production PATTERNS row declares side_effecting_read yet, so
        this drives the resolution helper directly: unanimous agreement on
        side_effecting_read must not be pulled up to True."""
        result = dc.resolve_mutates([dc.MUTATES_SIDE_EFFECTING_READ, dc.MUTATES_SIDE_EFFECTING_READ])
        self.assertEqual(result, dc.MUTATES_SIDE_EFFECTING_READ)

    def test_resolve_mutates_disagreement_resolves_to_true_not_side_effecting_read(self):
        """Section 2.1, literally: any disagreement is True. A False/
        side_effecting_read disagreement must not settle on
        side_effecting_read -- that was the old, now-removed ranking."""
        result = dc.resolve_mutates([dc.MUTATES_FALSE, dc.MUTATES_SIDE_EFFECTING_READ])
        self.assertEqual(result, dc.MUTATES_TRUE)


class OrdinaryCodingWorkTests(unittest.TestCase):
    """The 2026-09-18 row: ordinary coding work must reach `coding`.

    Until it existed, `coding` matched only a test suite, a large refactor, a
    database migration and trivia -- everything else fell through to
    DEFAULT_TASK_TYPE, which is `comprehension`. That left 4.1's cost thesis
    unreachable in practice: `coding` is the only type with an oracle (4.2)
    and one of two that start free, and it was receiving almost none of the
    work it exists for.
    """

    ORDINARY = (
        "fix the bug in the auth middleware",
        "implement a retry decorator",
        "add a function to parse the config",
        "refactor this module",
        "debug why the login fails",
        "write a function to sort rows",
    )

    def test_ordinary_coding_work_is_coding(self):
        for text in self.ORDINARY:
            with self.subTest(text=text):
                self.assertEqual(dc.classify(text).task_type, "coding")

    def test_it_did_not_steal_its_neighbours(self):
        """2.1 resolves a multi-match by literal-character specificity for the
        TYPE and by highest score for the SIZE. A bare `debug` or `implement`
        must not outrank the more specific patterns that existed first --
        which is how a broad new row breaks a classifier."""
        for text, expected in (
            ("debug complex error chain", "reasoning"),
            ("implement multiple coordinated agents", "planning"),
            ("architect a new system", "planning"),
            ("summarize this log file", "long-context"),
            ("explain the concept of currying", "comprehension"),
            ("research the api documentation", "comprehension"),
        ):
            with self.subTest(text=text):
                self.assertEqual(dc.classify(text).task_type, expected)

    def test_the_score_rows_still_win_on_size(self):
        """Same type, different size: the score-4 and score-1 rows must still
        beat the new score-3 one, or the size factor (5.1) silently flattens."""
        self.assertEqual(dc.classify("refactor large module").score, 4)
        self.assertEqual(dc.classify("quick fix").score, 1)
        self.assertEqual(dc.classify("refactor this module").score, 3)

    def test_comprehension_is_still_the_default_for_non_coding_text(self):
        """The new row must not make everything coding. Text matching no
        pattern still lands on DEFAULT_TASK_TYPE."""
        for text in ("the weather is nice today", "hello", "zzzz qqqq"):
            with self.subTest(text=text):
                self.assertEqual(dc.classify(text).task_type,
                                 dc.DEFAULT_TASK_TYPE)
