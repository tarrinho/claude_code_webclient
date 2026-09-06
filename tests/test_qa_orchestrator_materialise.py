"""QA: turning a parsed plan into graph nodes and task rows.

`SupervisorEngine._materialise_plan` was lifted out of `_run_planner_turn`,
which was 199 lines doing four jobs: run the planning turn, validate its output,
build the graph, and start the scheduler. This is the third, and it is the one
with its own failure semantics — a row that cannot be written must not stop the
tasks that can.

**The extraction is why this file exists.** Ruff reported `db` undefined in the
new method, because `orchestrator.py` imports `db` inside functions (a
module-level import is a cycle: `db` imports `orchestrator` at load time) and the
lifted block had left its import behind. The suite reported 191 passing while
that stood — so nothing in it drove this path with tasks present. A decomposition
whose new function is never called is not verified by the tests passing around
it.

Two defects the loop already carries scars from, both pinned below:

* Every orchestrator's `PlanParser` numbers tasks from 1, so every plan produces a
  `t001`, and `orchestrator_tasks.id` is a global primary key. The second
  orchestrator's writes failed on the UNIQUE constraint, were swallowed by a bare
  warning, and its task list stayed empty at 0% while the work actually ran.
* That bare warning is why it took a live run to notice.
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import config
import db
import orchestrator


class MaterialisePlanTests(unittest.IsolatedAsyncioTestCase):
    """The path the extraction created, driven for real."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        self.sup = uuid.uuid4().hex
        await db.orchestrator_create(self.sup, "Release", "", "alice")
        self.engine = orchestrator.OrchestratorEngine(self.sup, "alice")

    @staticmethod
    def _tasks(*specs):
        return [
            orchestrator.ParsedTask(
                id=tid, title=title, description=desc,
                model=model, depends_on=list(deps),
            )
            for tid, title, desc, model, deps in specs
        ]

    async def test_it_runs_at_all(self):
        """The assertion the 191-test suite could not make.

        `db` was undefined in this method after the lift, and every one of those
        tests passed regardless — because none of them called it with tasks. If
        this raises NameError, the extraction was never exercised.
        """
        await self.engine._materialise_plan(
            self._tasks(("t001", "Do the thing", "a description", None, ()))
        )
        rows = await db.orchestrator_tasks_get(self.sup, "alice")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Do the thing")

    async def test_no_tasks_is_a_no_op(self):
        await self.engine._materialise_plan([])
        self.assertEqual(await db.orchestrator_tasks_get(self.sup, "alice"), [])

    async def test_row_ids_are_namespaced_so_two_supervisors_can_both_have_t001(self):
        """The collision that made a task list stay empty at 0%.

        PlanParser numbers from 1 within a plan, so every orchestrator emits a
        `t001`, and `orchestrator_tasks.id` is a global primary key. Without the
        namespace the second write fails the UNIQUE constraint.
        """
        other = uuid.uuid4().hex
        await db.orchestrator_create(other, "Second", "", "alice")
        engine2 = orchestrator.OrchestratorEngine(other, "alice")
        plan = self._tasks(("t001", "First", "d", None, ()))
        await self.engine._materialise_plan(plan)
        await engine2._materialise_plan(self._tasks(("t001", "Second", "d", None, ())))

        mine = await db.orchestrator_tasks_get(self.sup, "alice")
        theirs = await db.orchestrator_tasks_get(other, "alice")
        self.assertEqual(len(mine), 1, "the first orchestrator's task is missing")
        self.assertEqual(len(theirs), 1,
                         "the second orchestrator's task was lost to an id collision")
        self.assertNotEqual(mine[0]["id"], theirs[0]["id"])
        for row in (mine[0], theirs[0]):
            self.assertNotEqual(row["id"], "t001", "the raw plan id was written")

    async def test_dependencies_are_namespaced_to_match(self):
        """Or a task waits for an id that was never written, forever."""
        await self.engine._materialise_plan(self._tasks(
            ("t001", "First", "d", None, ()),
            ("t002", "Second", "d", None, ("t001",)),
        ))
        rows = {r["title"]: r for r in
                await db.orchestrator_tasks_get(self.sup, "alice")}
        self.assertEqual(len(rows), 2)
        ids = {r["id"] for r in rows.values()}
        import json
        deps = json.loads(rows["Second"]["depends_on"])
        self.assertEqual(len(deps), 1)
        self.assertIn(deps[0], ids,
                      "the dependency points at an id no row carries")

    async def test_one_unwritable_row_does_not_abandon_the_rest(self):
        """The per-task try is inside the loop on purpose.

        Moving it out would drop every remaining task on the first bad row,
        which is the opposite of what it is for. Verified by failing the middle
        write rather than by reading the source.
        """
        real = db.orchestrator_task_create
        calls = {"n": 0}

        async def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated write failure")
            return await real(**kwargs)

        with patch.object(db, "orchestrator_task_create", AsyncMock(side_effect=flaky)):
            await self.engine._materialise_plan(self._tasks(
                ("t001", "One", "d", None, ()),
                ("t002", "Two", "d", None, ()),
                ("t003", "Three", "d", None, ()),
            ))
        titles = {r["title"] for r in
                  await db.orchestrator_tasks_get(self.sup, "alice")}
        self.assertEqual(titles, {"One", "Three"},
                         "a single failed write took the later tasks with it")

    async def test_a_failed_write_is_logged_with_its_reason(self):
        """It was a bare warning with no exception attached, which is why a task
        list that silently stayed empty while the work ran took a live run to
        notice at all."""
        async def always_fail(**kwargs):
            raise RuntimeError("simulated write failure")

        with patch.object(db, "orchestrator_task_create",
                          AsyncMock(side_effect=always_fail)), \
             self.assertLogs("wc.orchestrator", level="ERROR") as logs:
            await self.engine._materialise_plan(
                self._tasks(("t001", "One", "d", None, ()))
            )
        blob = "\n".join(logs.output)
        self.assertIn("supervisor_task_create failed", blob)
        self.assertIn("simulated write failure", blob,
                      "the reason must travel with the log line")

    async def test_the_graph_gets_the_same_nodes_the_database_does(self):
        """Two stores, one plan. They drifted once and the UI believed the
        emptier of the two."""
        await self.engine._materialise_plan(self._tasks(
            ("t001", "One", "d", None, ()),
            ("t002", "Two", "d", None, ()),
        ))
        rows = await db.orchestrator_tasks_get(self.sup, "alice")
        self.assertEqual(
            {r["id"] for r in rows}, set(self.engine.graph.tasks),
            "the in-memory graph and the task rows disagree",
        )


if __name__ == "__main__":
    unittest.main()
