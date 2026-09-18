"""QA: the shadow record captures which model a task actually ran on.

`delegation_routing_decision.actual_model` holds what the PLAN named
(`ParsedTask.model`). The first production row, written 2026-09-18, showed why
that is not enough on its own:

    shadow_model  vllm/Qwen3.6-35B-A3B-NVFP4
    actual_model  NULL                 <- the plan named no model
    task row      model = vllm/Qwen3.6-35B-A3B-NVFP4   <- but it ran on one

Plans almost never name a model, so `actual_model` is NULL on almost every row
and the learn pass is left comparing the classifier against nobody. `ran_model`
is resolved in `_execute_task` -- `runner.get_default_model` when the plan named
none -- and is what makes "did the ladder agree with what the system chose" a
query rather than a guess.

The tests below are about the RECORDING rule, not about any particular model:
which rows are updated, which are deliberately left alone, and that a failure
here cannot take a task down with it.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class _DecisionDbCase(unittest.IsolatedAsyncioTestCase):
    """A throwaway database per test. Never production: db.init() migrates."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = str(Path(self._tmp.name) / "wc.db")
        os.environ["WC_DB_PATH"] = path
        os.environ["WC_SESSION_DB_PATH"] = str(Path(self._tmp.name) / "s.db")
        import config
        import db
        self._config_patch = patch.object(config, "DB_PATH", path)
        self._config_patch.start()
        self.addCleanup(self._config_patch.stop)
        self.db = db
        await db.init()
        self.addCleanup(lambda: None)

    async def asyncTearDown(self):
        await self.db.close()

    async def _record(self, task_id: str, **over):
        fields = dict(
            task_table="orchestrator_tasks", task_id=task_id,
            task_type="coding", score=3, mutates="true", source="ladder",
            shadow_model="azure_ai/gpt-5.6-luna", actual_model=None,
            ladder='["azure_ai/gpt-5.6-luna"]',
        )
        fields.update(over)
        return await self.db.delegation_decision_record(**fields)

    async def _rows(self):
        cur = await self.db.db_conn.execute(
            "SELECT task_id, actual_model, ran_model FROM "
            "delegation_routing_decision ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


class RanModelColumnTests(_DecisionDbCase):

    async def test_the_column_exists_on_a_fresh_database(self):
        cur = await self.db.db_conn.execute(
            "PRAGMA table_info(delegation_routing_decision)")
        names = {r["name"] for r in await cur.fetchall()}
        self.assertIn("ran_model", names)

    async def test_a_recorded_decision_starts_with_no_ran_model(self):
        """It is not known yet. A decision is written when the plan is
        materialised; the task has not run."""
        await self._record("t001")
        self.assertEqual([r["ran_model"] for r in await self._rows()], [None])

    async def test_noting_the_model_fills_it_in(self):
        await self._record("t001")
        updated = await self.db.delegation_decision_note_ran_model(
            "orchestrator_tasks", "t001", "claude-sonnet-5")
        self.assertEqual(updated, 1)
        self.assertEqual([r["ran_model"] for r in await self._rows()],
                         ["claude-sonnet-5"])

    async def test_it_leaves_actual_model_alone(self):
        """The two columns answer different questions and must not be
        conflated: a plan that named a model still records what it named,
        whatever the task then ran on."""
        await self._record("t001", actual_model="claude-opus-5")
        await self.db.delegation_decision_note_ran_model(
            "orchestrator_tasks", "t001", "claude-sonnet-5")
        row = (await self._rows())[0]
        self.assertEqual(row["actual_model"], "claude-opus-5")
        self.assertEqual(row["ran_model"], "claude-sonnet-5")

    async def test_a_second_execution_does_not_overwrite_the_first(self):
        """The first execution is the one the ladder was asked about. Without
        the `ran_model IS NULL` guard a retry would silently rewrite history,
        and the row would describe the retry rather than the decision."""
        await self._record("t001")
        await self.db.delegation_decision_note_ran_model(
            "orchestrator_tasks", "t001", "first-model")
        updated = await self.db.delegation_decision_note_ran_model(
            "orchestrator_tasks", "t001", "second-model")
        self.assertEqual(updated, 0)
        self.assertEqual([r["ran_model"] for r in await self._rows()],
                         ["first-model"])

    async def test_it_updates_every_row_for_a_re_planned_task(self):
        """The schema deliberately allows a second decision for one task id --
        a re-plan is data, not a conflict -- so this cannot assume one row."""
        await self._record("t001")
        await self._record("t001", shadow_model="claude-sonnet-5")
        updated = await self.db.delegation_decision_note_ran_model(
            "orchestrator_tasks", "t001", "claude-sonnet-5")
        self.assertEqual(updated, 2)

    async def test_it_touches_no_other_task(self):
        await self._record("t001")
        await self._record("t002")
        await self.db.delegation_decision_note_ran_model(
            "orchestrator_tasks", "t001", "claude-sonnet-5")
        by_id = {r["task_id"]: r["ran_model"] for r in await self._rows()}
        self.assertEqual(by_id["t002"], None)

    async def test_an_unknown_task_updates_nothing_and_does_not_raise(self):
        """The hook fires for every task, including ones no decision was
        recorded for -- a task created before this shipped, or one whose
        record failed. That must be a no-op, not an error."""
        await self._record("t001")
        updated = await self.db.delegation_decision_note_ran_model(
            "orchestrator_tasks", "nosuchtask", "claude-sonnet-5")
        self.assertEqual(updated, 0)


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    """The column is added to databases that already have the table, which is
    every database that matters -- CREATE TABLE IF NOT EXISTS is a no-op there.
    """

    async def test_an_existing_table_without_the_column_gains_it(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "old.db")

        # A database carrying the pre-migration shape, built by hand.
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE delegation_routing_decision ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, task_table TEXT NOT NULL,"
            " task_id TEXT NOT NULL, task_type TEXT NOT NULL,"
            " score INTEGER NOT NULL, mutates TEXT NOT NULL,"
            " source TEXT NOT NULL, shadow_model TEXT NOT NULL,"
            " actual_model TEXT, ladder TEXT, decided_at TEXT NOT NULL)")
        con.execute(
            "INSERT INTO delegation_routing_decision (task_table, task_id,"
            " task_type, score, mutates, source, shadow_model, decided_at)"
            " VALUES ('orchestrator_tasks','old1','coding',3,'true','ladder',"
            " 'azure_ai/gpt-5.6-luna','2026-09-18T00:00:00Z')")
        con.commit()
        con.close()

        os.environ["WC_DB_PATH"] = path
        os.environ["WC_SESSION_DB_PATH"] = str(Path(tmp.name) / "s.db")
        import config
        import db
        with patch.object(config, "DB_PATH", path):
            await db.init()
            try:
                cur = await db.db_conn.execute(
                    "PRAGMA table_info(delegation_routing_decision)")
                names = {r["name"] for r in await cur.fetchall()}
                self.assertIn(
                    "ran_model", names,
                    "an existing table must gain the column; CREATE TABLE IF "
                    "NOT EXISTS cannot add it")
                cur = await db.db_conn.execute(
                    "SELECT task_id, ran_model FROM delegation_routing_decision")
                rows = [dict(r) for r in await cur.fetchall()]
                self.assertEqual(rows, [{"task_id": "old1", "ran_model": None}],
                                 "the existing row must survive the migration")
            finally:
                await db.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
