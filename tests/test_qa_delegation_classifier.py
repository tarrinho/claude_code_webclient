"""QA: the classifier of spec section 2.

Three properties that are easy to get wrong and invisible when wrong:
specificity beats list order, a score is the MAXIMUM of matched patterns
rather than the winning pattern's own, and both defaults fail toward the
expensive branch.
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
        """Section 2.2: both defaults fail toward the expensive/safe branch."""
        result = dc.classify("zzzz nothing matches this at all")
        self.assertEqual(result.task_type, "comprehension")
        self.assertEqual(result.mutates, dc.MUTATES_TRUE)

    def test_score_is_the_maximum_of_every_match_not_the_winner_s(self):
        """Section 2.1. Score feeds the size factor and therefore the deadline;
        under-budgeting manufactures a false timeout, which costs a rung."""
        # "simple" scores 1; "orchestrate" scores 5. Both match.
        result = dc.classify("a simple change to orchestrate the agents")
        self.assertEqual(result.score, 5)

    def test_a_mutates_conflict_resolves_to_true(self):
        """Section 2.1: if any two matched patterns disagree, the answer is True."""
        # "read file" is False; "fix typo" is True.
        result = dc.classify("read file and fix typo in it")
        self.assertEqual(result.mutates, dc.MUTATES_TRUE)

    def test_task_type_is_decided_by_specificity_not_list_order(self):
        """The most specific matching pattern wins. 'migrate database' is a
        longer, more specific pattern than 'simple'."""
        result = dc.classify("simple migrate database task")
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
