"""QA: `max_context` must not disagree with itself across task types.

A context window is a property of the MODEL, so the same model measured on
`coding` and on `planning` has the same window. A row set where it differs is
not a window at all -- it is some per-run quantity written into the column by
mistake.

Both cases below are real, measured on this deployment on 2026-09-20 while
rechecking every model against its authoritative source:

    claude-haiku-4-5-20251001   24, 10, 20      across six task types
    claude-fable-5              13900 - 14062   across six task types

They passed every other 1.1 invariant, because every other one asks only
whether the column is non-null -- `24` is non-null. They were shown on the
settings page as measured fact for as long as they sat there. The real
figures are 200000 (Claude Haiku 4.5) and 1000000 (Claude Fable 5).

These tests pin the invariant, not the values: the fixtures use the numbers
that actually shipped, so a regression reproduces the original defect rather
than an invented one.
"""
from __future__ import annotations

import unittest

from tiered_delegation import CapabilityRow, CapabilityTable


def _row(model, task_type, max_context, accuracy=1.0, cost=1.0):
    """A ladder-eligible row, varying only what each case is about."""
    return CapabilityRow(
        model=model,
        task_type=task_type,
        accuracy=accuracy,
        n=12,
        cost_per_1m_tokens=cost,
        median_latency_s=5.0,
        max_context=max_context,
    )


def _problems(rows, operational=None):
    """Problems for *rows*.

    Defaults to marking every task type present as operational, because
    the check is scoped to those -- a fixture that forgot would assert
    nothing and pass for the wrong reason.
    """
    if operational is None:
        operational = {r.task_type for r in rows}
    table = CapabilityTable(rows, operational=operational)
    return table._max_context_problems()


class MaxContextConsistencyTests(unittest.TestCase):

    def test_one_value_across_task_types_is_clean(self):
        rows = [_row("m", "coding", 200_000), _row("m", "planning", 200_000)]
        self.assertEqual(_problems(rows), [])

    def test_the_haiku_defect_is_caught(self):
        """The values that actually shipped: 24, 10 and 20."""
        rows = [
            _row("claude-haiku-4-5-20251001", "coding", 24),
            _row("claude-haiku-4-5-20251001", "comprehension", 10),
            _row("claude-haiku-4-5-20251001", "multi-turn", 20),
        ]
        problems = _problems(rows)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("claude-haiku-4-5-20251001", problems[0])
        self.assertIn("max_context disagrees with itself", problems[0])

    def test_the_fable_defect_is_caught(self):
        """Values ~14000 that varied per task type -- a plausibility floor
        would have missed these, which is why the invariant is consistency."""
        rows = [
            _row("claude-fable-5", "coding", 13_900),
            _row("claude-fable-5", "comprehension", 14_062),
            _row("claude-fable-5", "reasoning", 13_926),
        ]
        problems = _problems(rows)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("claude-fable-5", problems[0])

    def test_the_message_names_every_value_and_where_it_sits(self):
        """A report that says only "inconsistent" makes the reader go
        digging; the fix needs to know which rows to correct."""
        rows = [
            _row("m", "coding", 24),
            _row("m", "planning", 10),
        ]
        message = _problems(rows)[0]
        self.assertIn("10 on planning", message)
        self.assertIn("24 on coding", message)

    def test_each_model_is_reported_once(self):
        rows = [
            _row("a", "coding", 1), _row("a", "planning", 2),
            _row("b", "coding", 3), _row("b", "planning", 4),
        ]
        problems = _problems(rows)
        self.assertEqual(len(problems), 2, problems)

    def test_a_clean_model_is_not_reported_beside_a_dirty_one(self):
        rows = [
            _row("dirty", "coding", 1), _row("dirty", "planning", 2),
            _row("clean", "coding", 200_000), _row("clean", "planning", 200_000),
        ]
        problems = _problems(rows)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("dirty", problems[0])

    def test_none_is_left_to_the_missing_columns_check(self):
        """"Not measured" is a different fault, reported elsewhere. Counting
        it here would report one problem twice."""
        rows = [_row("m", "coding", None), _row("m", "planning", 200_000)]
        self.assertEqual(_problems(rows), [])

    def test_all_none_is_clean_here(self):
        rows = [_row("m", "coding", None), _row("m", "planning", None)]
        self.assertEqual(_problems(rows), [])

    def test_zero_is_filler_not_a_window(self):
        """Several fixtures use 0 for a column their case does not care
        about. Zero carries as much information as None -- reporting it would
        flag an impossible model in tests that are about something else."""
        rows = [_row("m", "coding", 229_376), _row("m", "reviewer-gate", 0)]
        self.assertEqual(_problems(rows), [])

    def test_a_negative_value_is_skipped_for_the_same_reason(self):
        rows = [_row("m", "coding", 200_000), _row("m", "planning", -1)]
        self.assertEqual(_problems(rows), [])

    def test_zero_does_not_mask_a_real_disagreement(self):
        """Skipping 0 must not swallow two genuine values that differ."""
        rows = [
            _row("m", "coding", 24),
            _row("m", "planning", 10),
            _row("m", "voice", 0),
        ]
        problems = _problems(rows)
        self.assertEqual(len(problems), 1, problems)
        self.assertNotIn("0 on voice", problems[0])

    def test_a_single_row_cannot_disagree(self):
        self.assertEqual(_problems([_row("m", "coding", 24)]), [])

    def test_an_empty_table_is_clean(self):
        self.assertEqual(_problems([]), [])

    def test_two_models_may_legitimately_differ_from_each_other(self):
        """The invariant is within a model, never across models."""
        rows = [
            _row("haiku", "coding", 200_000),
            _row("fable", "coding", 1_000_000),
        ]
        self.assertEqual(_problems(rows), [])


class MaxContextReachesValidateTests(unittest.TestCase):
    """The check has to run from `validate`, or nothing enforces it.

    `validate` is what the startup check, the row-write endpoint and the seed
    script all call, and spec 1.1 requires the same code path and the same
    error text at every moment a table is validated.
    """

    def test_validate_reports_it(self):
        rows = [
            _row("m", "coding", 24),
            _row("m", "planning", 10),
        ]
        table = CapabilityTable(rows, operational=("coding", "planning"))
        problems = table.validate(
            known_models=None, enforce_latency_ceiling=False,
            enforce_budget=False)
        self.assertTrue(
            any("max_context disagrees with itself" in p for p in problems),
            problems)

    def test_a_dormant_type_is_not_reported_until_it_is_flipped(self):
        """Scoped to operational types, like the "no blank fields" rule.

        Nothing is lost by that: the flip itself runs validation, so the same
        rows are refused at the moment they would begin to route."""
        rows = [_row("m", "coding", 24), _row("m", "planning", 10)]
        dormant = CapabilityTable(rows, operational=())
        self.assertEqual(
            [p for p in dormant.validate(
                known_models=None, enforce_latency_ceiling=False,
                enforce_budget=False) if "max_context" in p], [])

        flipped = CapabilityTable(rows, operational=("coding", "planning"))
        self.assertTrue(
            any("max_context disagrees with itself" in p
                for p in flipped.validate(
                    known_models=None, enforce_latency_ceiling=False,
                    enforce_budget=False)))

    def test_a_consistent_table_adds_no_problem(self):
        rows = [
            _row("m", "coding", 200_000),
            _row("m", "planning", 200_000),
        ]
        table = CapabilityTable(rows, operational=("coding", "planning"))
        problems = table.validate(
            known_models=None, enforce_latency_ceiling=False,
            enforce_budget=False)
        self.assertEqual(
            [p for p in problems if "max_context" in p], [])


if __name__ == "__main__":
    unittest.main()
