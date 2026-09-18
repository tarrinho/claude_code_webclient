# tests/test_qa_benchmark_cell.py
"""QA: one benchmark cell, against a fake subprocess.

Nothing here spawns a model. `parse_bench_payload` is pure so the three
outcomes the sweep must survive -- success, failure, timeout -- are table
tests, and `run_cell` is exercised against a fake `asyncio.create_subprocess_exec`.

The timeout case is asserted to produce status 'failed' and NO median, because
spec 7 is explicit that a truncated run is not a measurement: vllm's reasoning
cell produced an n=2 median of 356s under the cap, which is an artefact and
would be a lie in the capability table.
"""
from __future__ import annotations

import unittest
from unittest import mock

import benchmark_cell


class ParseTests(unittest.TestCase):
    def _payload(self, summary):
        return {"summary": summary}

    def test_a_successful_cell_aggregates_its_tasks(self):
        payload = self._payload({
            "m1|coding-1|cli": {"repeats": 3, "pass_rate": 1.0,
                                "total_s_median": 12.0, "errors": 0},
            "m1|coding-2|cli": {"repeats": 3, "pass_rate": 1.0,
                                "total_s_median": 14.0, "errors": 0},
        })
        result = benchmark_cell.parse_bench_payload(payload, "coding")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.accuracy, 1.0)
        self.assertEqual(result.n, 6)
        self.assertEqual(result.median_latency_s, 13.0)

    def test_every_task_erroring_is_a_failed_cell(self):
        payload = self._payload({
            "m1|coding-1|cli": {"repeats": 3, "pass_rate": 0.0,
                                "total_s_median": None, "errors": 3},
        })
        result = benchmark_cell.parse_bench_payload(payload, "coding")
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.median_latency_s)

    def test_an_empty_summary_is_a_failed_cell(self):
        result = benchmark_cell.parse_bench_payload(self._payload({}), "coding")
        self.assertEqual(result.status, "failed")
        self.assertIn("no tasks", result.error)

    def test_partial_errors_still_measure(self):
        """One task erroring must not discard the two that worked."""
        payload = self._payload({
            "m1|coding-1|cli": {"repeats": 3, "pass_rate": 1.0,
                                "total_s_median": 12.0, "errors": 0},
            "m1|coding-2|cli": {"repeats": 3, "pass_rate": 0.0,
                                "total_s_median": None, "errors": 3},
        })
        result = benchmark_cell.parse_bench_payload(payload, "coding")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.median_latency_s, 12.0)
        self.assertEqual(result.accuracy, 0.5)


class TasksForTests(unittest.TestCase):
    def test_tasks_for_returns_real_task_ids(self):
        """Regression test for the .name bug (Task has no such field; the
        dataclass field is .id, per bench/tasks.py:40). Without this test,
        _tasks_for would raise AttributeError on every real call and that
        would only surface when a real sweep ran, since every other test in
        this file drives run_cell through a patched _run_subprocess and never
        reaches _tasks_for's real bench.tasks import with a real filter."""
        from bench.tasks import BY_ID

        ids = benchmark_cell._tasks_for("coding")
        self.assertTrue(ids, "expected at least one coding task id")
        for task_id in ids:
            self.assertIn(task_id, BY_ID)


class TimeoutBudgetTests(unittest.TestCase):
    def test_coding_gets_a_bigger_derived_budget_than_reasoning(self):
        """coding has far more tasks than reasoning (bench/tasks.py), so a
        budget that scales with the work must give coding a bigger timeout.
        This fails against any fixed constant -- which was the bug: a single
        1200s default was too small for coding's 12 tasks * 3 repeats = 36
        runs while being needlessly huge for reasoning's 2 tasks."""
        coding_tasks = benchmark_cell._tasks_for("coding")
        reasoning_tasks = benchmark_cell._tasks_for("reasoning")
        coding_budget = benchmark_cell._timeout_for(coding_tasks, repeats=3)
        reasoning_budget = benchmark_cell._timeout_for(reasoning_tasks,
                                                        repeats=3)
        self.assertGreater(len(coding_tasks), len(reasoning_tasks))
        self.assertGreater(coding_budget, reasoning_budget)

    def test_derived_budget_covers_every_run_at_the_harness_cap(self):
        """Below the MAX_CELL_BUDGET_S ceiling, the budget must be at least
        runs * per-run cap, or it can fire on a cell that is working normally
        -- every run within the harness's own per-run timeout
        (bench/transports.py TIMEOUT_S). Uses reasoning rather than coding:
        coding's own derived value now exceeds the cap (see
        TimeoutBudgetCapTests), so it is the wrong task_type to assert
        uncapped derivation against."""
        tasks = benchmark_cell._tasks_for("reasoning")
        repeats = 3
        budget = benchmark_cell._timeout_for(tasks, repeats)
        per_run_cap = 1000.0  # bench/transports.py's own default
        self.assertLess(budget, benchmark_cell.MAX_CELL_BUDGET_S)
        self.assertGreaterEqual(budget, len(tasks) * repeats * per_run_cap)


class TimeoutBudgetCapTests(unittest.TestCase):
    def test_a_coding_cell_is_capped_at_the_ceiling(self):
        """coding's raw derived value (12 tasks * 3 repeats * 1000s + margin
        = 36,120s, just over ten hours) is not a usable backstop: the nightly
        job only checks whether the box is busy between cells, never during
        one (spec 8.3), so an uncapped budget is also the longest the sweep
        can be deaf to that check. A `return MAX_CELL_BUDGET_S` stub would
        pass this assertion alone, which is why it is paired below with a
        task_type that must NOT be capped."""
        tasks = benchmark_cell._tasks_for("coding")
        budget = benchmark_cell._timeout_for(tasks, repeats=3)
        self.assertEqual(budget, benchmark_cell.MAX_CELL_BUDGET_S)

    def test_a_reasoning_cell_is_not_capped(self):
        """reasoning's small task count must keep its own tighter, derived
        budget rather than being handed the two-hour ceiling meant for a
        pathological cell. Paired with the coding test above so a flat
        `return MAX_CELL_BUDGET_S` implementation fails here."""
        tasks = benchmark_cell._tasks_for("reasoning")
        budget = benchmark_cell._timeout_for(tasks, repeats=3)
        self.assertLess(budget, benchmark_cell.MAX_CELL_BUDGET_S)


class TimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_timeout_is_a_failure_carrying_the_cap(self):
        async def _hang(*args, **kwargs):
            raise TimeoutError()
        with mock.patch.object(benchmark_cell, "_run_subprocess", _hang):
            result = await benchmark_cell.run_cell("m1", "coding",
                                                   timeout_s=900.0)
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.median_latency_s)
        self.assertIn("900", result.error)
