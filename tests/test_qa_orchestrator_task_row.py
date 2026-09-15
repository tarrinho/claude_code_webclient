"""QA: what `_execute_task` writes to the orchestrator_tasks row.

Two fields were never written, and both were found by reading the only real
orchestrator run this deployment has: every task row carried an empty `model`,
and the two failed tasks carried an empty `result`. So the single run that
mattered left nothing to diagnose it with -- the error existed only in the log
file and in an in-memory ProgressEvent, and `orchestrator_progress` holds no
rows.

`db.orchestrator_task_update` has accepted `result` and `model` throughout. The
success path passed `result` but not `model`; the failure path passed neither.

Why each matters:

* `result` on failure -- a task that fails with no recorded reason cannot be
  told apart from one that failed for a different reason, and section 10 of the
  tiered-delegation spec wants every escalation traceable to what rejected it.
* `model` on both -- the whole point of tiered delegation is which rung ran the
  work. `usage_events` records it per turn, but the task row is where a person
  looks first, and it was empty.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import orchestrator


class _Node:
    """The fields `_execute_task` reads off a TaskNode."""

    def __init__(self) -> None:
        self.id = "t1"
        self.status = "pending"
        self.title = "t"
        self.description = "d"
        self.model = None
        self.result = None
        self.progress_pct = 0.0
        self.parent_id = None
        self.depends_on: list[str] = []


class _Graph:
    def __init__(self) -> None:
        self.node = _Node()

    def get_task(self, task_id):
        return self.node

    def update_status(self, task_id, status):
        self.node.status = status

    def update_result(self, task_id, result):
        self.node.result = result

    def update_progress(self, task_id, pct):
        self.node.progress_pct = pct


def _orch() -> "orchestrator.OrchestratorEngine":
    """An Orchestrator with just enough wired to reach the DB write."""
    o = orchestrator.OrchestratorEngine.__new__(orchestrator.OrchestratorEngine)
    o.orchestrator_id = "orch1"
    o.owner_id = "owner1"
    o.graph = _Graph()
    o.tracker = SimpleNamespace(record=lambda ev: None)
    return o


class TaskRowOnFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_records_the_error_and_the_model(self):
        """A failed task must say why it failed and what ran it.

        Both fields were empty on the two real failures in this database, which
        is why that run is undiagnosable.
        """
        o = _orch()
        updates: list[dict] = []

        async def _capture(**kwargs):
            updates.append(kwargs)
            return True

        fake_db = SimpleNamespace(
            orchestrator_task_update=_capture,
            orchestrator_clear_degraded=AsyncMock(return_value=None),
            orchestrator_mark_degraded=AsyncMock(return_value=None),
            orchestrator_message_add=AsyncMock(return_value=None),
            orchestrator_messages_append=AsyncMock(return_value=None),
        )
        fake_runner = SimpleNamespace(
            run_turn=AsyncMock(side_effect=RuntimeError("gateway said 429")),
            get_default_model=AsyncMock(return_value="some/model"),
        )

        with patch.dict("sys.modules", {"db": fake_db, "runner": fake_runner}), \
             patch.object(o, "_record_usage", AsyncMock(return_value=None), create=True):
            await o._execute_task("t1", "do the thing", "vllm/Qwen3.6-35B-A3B-NVFP4")

        failed = [u for u in updates if u.get("status") == "failed"]
        self.assertTrue(failed, "no failed status was written at all")
        row = failed[0]
        self.assertIn("429", str(row.get("result") or ""),
                      "the failure reason is not in the task row -- it exists only in the log")
        self.assertEqual(row.get("model"), "vllm/Qwen3.6-35B-A3B-NVFP4",
                         "the task row does not record which model ran it")


class TaskRowOnSuccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_records_the_model_alongside_the_result(self):
        """The done path already wrote `result`; `model` was missing.

        Every completed task in this database carries an empty model for that
        reason, including the one that succeeded.
        """
        o = _orch()
        updates: list[dict] = []

        async def _capture(**kwargs):
            updates.append(kwargs)
            return True

        fake_db = SimpleNamespace(
            orchestrator_task_update=_capture,
            orchestrator_clear_degraded=AsyncMock(return_value=None),
            orchestrator_mark_degraded=AsyncMock(return_value=None),
            orchestrator_message_add=AsyncMock(return_value=None),
            orchestrator_messages_append=AsyncMock(return_value=None),
        )
        fake_runner = SimpleNamespace(
            run_turn=AsyncMock(return_value=(["all done"], "sess1")),
            get_default_model=AsyncMock(return_value="some/model"),
        )

        with patch.dict("sys.modules", {"db": fake_db, "runner": fake_runner}), \
             patch.object(o, "_record_usage", AsyncMock(return_value=None), create=True):
            await o._execute_task("t1", "do the thing", "azure_ai/gpt-5.6-luna")

        done = [u for u in updates if u.get("status") == "done"]
        self.assertTrue(done, "no done status was written at all")
        self.assertEqual(done[0].get("model"), "azure_ai/gpt-5.6-luna",
                         "the task row does not record which model ran it")


if __name__ == "__main__":
    unittest.main()
