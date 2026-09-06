"""QA: the orchestrator scheduler must be able to finish, and to say it failed.

`run_schedule_loop` runs `while self._running and not self.graph.all_done()`,
and `all_done()` counted only "done" and "blocked" as terminal. A task that
reached "failed" was neither, so the condition never cleared: the loop spun at
half-second intervals for ever with nothing runnable, the orchestrator never
reached a final state, and nothing was logged because nothing had gone wrong
as far as the code was concerned.

`any_failed()` -- the one predicate that would have ended it -- was defined and
called from nowhere in the codebase.

These tests drive the graph and the loop directly rather than asserting on the
source, because both defects are about what the code does at runtime and were
invisible in review.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import orchestrator


def graph_with(**statuses: str) -> orchestrator.TaskGraph:
    graph = orchestrator.TaskGraph()
    for task_id, status in statuses.items():
        graph.add_task(
            orchestrator.TaskNode(id=task_id, title=task_id, description="", model=None)
        )
        graph.update_status(task_id, status)
    return graph


class TerminalStateTests(unittest.TestCase):
    def test_a_failed_task_counts_as_finished(self):
        """Not "succeeded" -- finished. The loop has to be able to stop."""
        self.assertTrue(graph_with(t1="failed").all_done())

    def test_a_mixed_graph_with_a_failure_is_finished(self):
        graph = graph_with(t1="failed", t2="blocked", t3="done")
        self.assertTrue(graph.all_done(),
                        "the scheduler would spin for ever on this graph")

    def test_work_still_in_flight_is_not_finished(self):
        for status in ("pending", "ready", "running"):
            with self.subTest(status=status):
                self.assertFalse(graph_with(t1=status).all_done())

    def test_failure_is_reported_separately_from_completion(self):
        """all_done() says the run stopped; any_failed() says how it went."""
        graph = graph_with(t1="failed", t2="done")
        self.assertTrue(graph.all_done())
        self.assertTrue(graph.any_failed())
        self.assertFalse(graph_with(t1="done", t2="done").any_failed())

    def test_an_empty_graph_is_finished(self):
        self.assertTrue(orchestrator.TaskGraph().all_done())


class ReadyTaskTests(unittest.TestCase):
    def test_a_blocked_task_is_not_offered_as_ready(self):
        """It is blocked because a dependency failed; it cannot run.

        Without "blocked" in the skip list a blocked task with no dependencies
        of its own fell through to the no-dependencies branch, was flipped back
        to "ready" and returned as runnable. The scheduler happened to re-check
        the status before executing, so nothing ran -- but the function was
        wrong and any other caller would have run it.
        """
        graph = graph_with(t1="blocked")
        self.assertEqual(graph.get_ready_tasks(), [])
        self.assertEqual(graph.get_task("t1").status, "blocked",
                         "a blocked task was silently reset to ready")

    def test_terminal_and_running_tasks_are_not_offered(self):
        for status in ("done", "failed", "running", "blocked"):
            with self.subTest(status=status):
                self.assertEqual(graph_with(t1=status).get_ready_tasks(), [])

    def test_a_task_with_no_dependencies_is_ready(self):
        graph = graph_with(t1="pending")
        self.assertEqual(graph.get_ready_tasks(), ["t1"])


class ScheduleLoopTests(unittest.IsolatedAsyncioTestCase):
    """Assertions are on the *persisted* status, not the graph node.

    The engine updated a graph node called "orchestrator" that nothing ever
    creates, so those calls were no-ops and the status the interface reads was
    never written at all. Asserting on the graph would have passed against the
    broken code as readily as the fixed one.
    """

    def _engine(self, graph: orchestrator.TaskGraph) -> orchestrator.SupervisorEngine:
        engine = orchestrator.SupervisorEngine("sup-1", "admin")
        engine.graph = graph
        self.persisted: list[str] = []
        # A plain function, not a coroutine: patch.object replaces an async
        # method with an AsyncMock, which awaits the call itself -- a
        # side_effect that returns a coroutine would hand back an un-awaited
        # one instead of recording anything.
        patcher = patch.object(
            orchestrator.SupervisorEngine, "_set_status",
            side_effect=lambda status: self.persisted.append(status),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return engine

    async def test_it_terminates_on_a_failed_task(self):
        """The regression proper: this used to run until the process ended."""
        engine = self._engine(graph_with(t1="failed"))
        await asyncio.wait_for(engine.run_schedule_loop(), timeout=5)
        self.assertFalse(engine._running)

    async def test_a_failed_run_is_reported_as_an_error_not_as_done(self):
        engine = self._engine(graph_with(t1="failed", t2="done"))
        await asyncio.wait_for(engine.run_schedule_loop(), timeout=5)
        self.assertEqual(self.persisted[-1], "error")

    async def test_a_clean_run_is_reported_as_done(self):
        engine = self._engine(graph_with(t1="done", t2="done"))
        await asyncio.wait_for(engine.run_schedule_loop(), timeout=5)
        self.assertEqual(self.persisted[-1], "done")

    async def test_an_exception_inside_the_loop_is_logged_and_ends_it(self):
        """It runs as a task nobody awaits, so an escape is invisible."""
        engine = self._engine(graph_with(t1="pending"))
        with patch.object(engine, "_execute_task",
                          side_effect=RuntimeError("model unreachable")), \
                self.assertLogs("wc.orchestrator", level="ERROR") as logs:
            await asyncio.wait_for(engine.run_schedule_loop(), timeout=5)
        self.assertFalse(engine._running)
        self.assertEqual(self.persisted[-1], "error")
        self.assertIn("schedule_loop_failed", "\n".join(logs.output))
        self.assertIn("model unreachable", "\n".join(logs.output))

    async def test_a_stopped_run_is_not_reported_as_done(self):
        """Cancelling is not completing, and the badge must not claim it was."""
        engine = self._engine(graph_with(t1="pending"))

        async def stop_soon(_task_id, _prompt, _model):
            engine.stop()
            return ""

        with patch.object(engine, "_execute_task", side_effect=stop_soon):
            await asyncio.wait_for(engine.run_schedule_loop(), timeout=5)
        self.assertNotEqual(self.persisted[-1], "done")


class BackgroundTaskTests(unittest.IsolatedAsyncioTestCase):
    """A task nobody holds a reference to can be collected mid-run."""

    async def test_spawn_keeps_a_reference_while_it_runs(self):
        engine = orchestrator.SupervisorEngine("sup-2", "admin")
        started = asyncio.Event()

        async def work():
            started.set()
            await asyncio.sleep(0.2)

        task = engine.spawn(work())
        await started.wait()
        self.assertIn(task, engine._tasks,
                      "nothing holds the task: the loop keeps only weak refs")
        await task

    async def test_the_reference_is_released_when_it_finishes(self):
        """Holding them for ever would be a leak in the other direction."""
        engine = orchestrator.SupervisorEngine("sup-3", "admin")

        async def work():
            return None

        task = engine.spawn(work())
        await task
        await asyncio.sleep(0)          # let the done callbacks run
        self.assertNotIn(task, engine._tasks)

    async def test_a_failure_in_a_spawned_task_is_logged(self):
        """Otherwise it surfaces only as "Task exception was never retrieved"."""
        engine = orchestrator.SupervisorEngine("sup-4", "admin")

        async def boom():
            raise RuntimeError("planner exploded")

        with self.assertLogs("wc.orchestrator", level="ERROR") as logs:
            task = engine.spawn(boom())
            with self.assertRaises(RuntimeError):
                await task
            await asyncio.sleep(0)
        self.assertIn("supervisor_task_failed", "\n".join(logs.output))
        self.assertIn("planner exploded", "\n".join(logs.output))

    async def test_cancelling_is_not_reported_as_a_failure(self):
        engine = orchestrator.SupervisorEngine("sup-5", "admin")

        async def forever():
            await asyncio.sleep(60)

        task = engine.spawn(forever())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)          # would raise inside the callback if wrong


if __name__ == "__main__":
    unittest.main()
