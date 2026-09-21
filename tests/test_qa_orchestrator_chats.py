"""QA: schema groundwork for running each orchestrator task as a real chat.

Two additive columns and a new status value:

* ``orchestrator_tasks.chat_id`` -- which chat actually ran a given task.
* ``orchestrators.work_dir`` -- the run's shared workspace on disk, the
  mechanism by which one task's artefacts reach the next task in the same
  run (see db._ensure_orchestrator_columns for the full rationale).
* ``"blocked"`` as a valid task status string (already used by
  orchestrator.py's own scheduling logic; there is no CHECK constraint on
  the column, so nothing further is required for it to be accepted).

Later tasks in this design add more classes to this file -- see
docs/superpowers/sdd/2026-09-20-orchestrator-chats-all-the-way-down-design/.
Fixture setup lives inside this class rather than as a module-level fixture
so a later class here does not have to fight it or reuse it by accident.
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from unittest.mock import patch

import config
import db


class OrchestratorTaskChatIdSchemaTests(unittest.IsolatedAsyncioTestCase):
    """Same fixture shape as tests/test_qa_orchestrator_degraded.py: a fresh,
    throwaway database created by patching config.DB_PATH/PROJECTS_ROOT to a
    temp directory before db.init(), never the production database.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_tasks_table_has_a_chat_id_column(self):
        cur = await db.db_conn.execute("PRAGMA table_info(orchestrator_tasks)")
        cols = {r["name"] for r in await cur.fetchall()}
        self.assertIn("chat_id", cols)

    async def test_runs_table_has_a_work_dir_column(self):
        """The run's shared directory: without it the tasks cannot pass
        files to one another, which is the point of one workspace per run."""
        cur = await db.db_conn.execute("PRAGMA table_info(orchestrators)")
        cols = {r["name"] for r in await cur.fetchall()}
        self.assertIn("work_dir", cols)

    async def test_migration_is_idempotent(self):
        await db.close()
        await db.init()
        cur = await db.db_conn.execute("PRAGMA table_info(orchestrator_tasks)")
        names = [r["name"] for r in await cur.fetchall()]
        self.assertEqual(names.count("chat_id"), 1)
        cur = await db.db_conn.execute("PRAGMA table_info(orchestrators)")
        names = [r["name"] for r in await cur.fetchall()]
        self.assertEqual(names.count("work_dir"), 1)

    async def test_a_task_can_record_the_chat_that_runs_it(self):
        # chat_create raises ValueError for the literal owner_id "admin", so
        # every owner id here is UUID-shaped instead.
        owner_id = uuid.uuid4().hex
        orch = uuid.uuid4().hex
        await db.orchestrator_create(orch, "run", None, owner_id)
        await db.orchestrator_task_create(orch, "t1", "Research", None)
        updated = await db.orchestrator_task_update(
            orch, "t1", owner_id, chat_id="chat-abc"
        )
        self.assertTrue(updated)
        task = await db.orchestrator_task_get(orch, "t1", owner_id)
        self.assertEqual(task["chat_id"], "chat-abc")

    async def test_a_task_without_chat_id_set_reads_back_none(self):
        owner_id = uuid.uuid4().hex
        orch = uuid.uuid4().hex
        await db.orchestrator_create(orch, "run", None, owner_id)
        await db.orchestrator_task_create(orch, "t1", "Research", None)
        task = await db.orchestrator_task_get(orch, "t1", owner_id)
        self.assertIsNone(task["chat_id"])

    async def test_the_run_can_record_its_work_dir(self):
        owner_id = uuid.uuid4().hex
        orch = uuid.uuid4().hex
        await db.orchestrator_create(orch, "run", None, owner_id)
        updated = await db.orchestrator_update(
            orch, owner_id, work_dir="/tmp/some-run-workspace"
        )
        self.assertTrue(updated)
        run = await db.orchestrator_get(orch, owner_id)
        self.assertEqual(run["work_dir"], "/tmp/some-run-workspace")

    async def test_blocked_is_accepted_as_a_task_status(self):
        """No CHECK constraint on orchestrator_tasks.status -- 'blocked' is
        already written by orchestrator.py's own scheduling logic. Pinned
        here as a status value that must keep round-tripping cleanly."""
        owner_id = uuid.uuid4().hex
        orch = uuid.uuid4().hex
        await db.orchestrator_create(orch, "run", None, owner_id)
        await db.orchestrator_task_create(orch, "t1", "Research", None)
        updated = await db.orchestrator_task_update(
            orch, "t1", owner_id, status="blocked"
        )
        self.assertTrue(updated)
        task = await db.orchestrator_task_get(orch, "t1", owner_id)
        self.assertEqual(task["status"], "blocked")


class OrchestratorCreateTaskChatTests(unittest.IsolatedAsyncioTestCase):
    """orchestrator.create_task_chat: the chat that actually runs one task,
    inside the run's shared workspace directory. Same throwaway-database
    fixture shape as OrchestratorTaskChatIdSchemaTests above.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_task_chats_of_one_run_share_its_workspace(self):
        import orchestrator

        owner_id = uuid.uuid4().hex
        orch = uuid.uuid4().hex
        # The parent chat only needs to exist as an id here: chat_update's
        # parent_chat_id column carries no foreign-key constraint, and this
        # test is about the two task chats, not the parent chat itself.
        parent_chat_id = uuid.uuid4().hex
        await db.orchestrator_create(orch, "run", None, owner_id)
        await db.orchestrator_task_create(orch, "t1", "One", None)
        await db.orchestrator_task_create(orch, "t2", "Two", None)

        a = await orchestrator.create_task_chat(
            orch, "t1", "One", "/tmp/run-ws", owner_id, parent_chat_id,
        )
        b = await orchestrator.create_task_chat(
            orch, "t2", "Two", "/tmp/run-ws", owner_id, parent_chat_id,
        )

        self.assertNotEqual(a, b)
        chat_a = await db.chat_get(a, owner_id)
        chat_b = await db.chat_get(b, owner_id)
        # The point of the shared directory: artefacts flow between tasks.
        self.assertEqual(chat_a["work_dir"], "/tmp/run-ws")
        self.assertEqual(chat_b["work_dir"], "/tmp/run-ws")
        self.assertEqual(chat_a["parent_chat_id"], parent_chat_id)
        self.assertEqual(chat_b["parent_chat_id"], parent_chat_id)
        # and each task now knows its own chat
        task_a = await db.orchestrator_task_get(orch, "t1", owner_id)
        task_b = await db.orchestrator_task_get(orch, "t2", owner_id)
        self.assertEqual(task_a["chat_id"], a)
        self.assertEqual(task_b["chat_id"], b)


if __name__ == "__main__":
    unittest.main()
