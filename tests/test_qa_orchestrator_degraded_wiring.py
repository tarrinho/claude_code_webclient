"""QA: every DB write in orchestrator.py that can silently fail marks the run
degraded, and every one that can silently start working again clears it.

See docs/superpowers/specs/2026-09-04-orchestrator-observability-design.md's
failure-policy table. _record_usage's own wiring is covered in
tests/test_qa_supervisor_usage.py; this file covers the other five sites.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import orchestrator


class CallSiteTests(unittest.TestCase):
    """Asserted on source, the same way test_qa_supervisor_usage.py's
    CallSiteTests class asserts _record_usage's call sites -- driving all of
    these live needs a real CLI turn, and a missed call site is silent: it
    looks exactly like a orchestrator that happened not to hit that failure."""

    @classmethod
    def setUpClass(cls):
        cls.source = Path(orchestrator.__file__).read_text(encoding="utf-8")

    def _body(self, name):
        start = self.source.index(f"    async def {name}(")
        rest = self.source[start + 10:]
        ends = [
            offset for offset in (
                rest.find("\n    async def "), rest.find("\n    def "),
                rest.find("\nclass "),
            ) if offset != -1
        ]
        return rest[:min(ends)] if ends else rest

    def test_persist_progress_marks_and_clears(self):
        body = self._body("_persist_progress")
        self.assertIn('orchestrator_mark_degraded(self.orchestrator_id, "progress"', body)
        self.assertIn('orchestrator_clear_degraded(self.orchestrator_id, "progress")', body)

    def test_set_status_marks_and_clears(self):
        body = self._body("_set_status")
        self.assertIn('orchestrator_mark_degraded(self.orchestrator_id, "status"', body)
        self.assertIn('orchestrator_clear_degraded(self.orchestrator_id, "status")', body)

    def test_materialise_plan_marks_per_task_and_clears_once_for_the_whole_plan(self):
        body = self._body("_materialise_plan")
        self.assertIn('orchestrator_mark_degraded(self.orchestrator_id, "task_create"', body)
        self.assertIn('orchestrator_clear_degraded(self.orchestrator_id, "task_create")', body)

    def test_execute_task_success_path_marks_and_clears_message_and_status(self):
        body = self._body("_execute_task")
        boundary = body.index("except Exception as exc:")
        before = body[:boundary]
        self.assertIn('orchestrator_mark_degraded(self.orchestrator_id, "task_message"', before)
        self.assertIn('orchestrator_clear_degraded(self.orchestrator_id, "task_message"', before)
        self.assertIn('orchestrator_mark_degraded(self.orchestrator_id, "task_status_done"', before)
        self.assertIn('orchestrator_clear_degraded(self.orchestrator_id, "task_status_done"', before)

    def test_execute_task_failure_path_marks_and_clears_status(self):
        body = self._body("_execute_task")
        boundary = body.index("except Exception as exc:")
        after = body[boundary:]
        self.assertIn('orchestrator_mark_degraded(self.orchestrator_id, "task_status_failed"', after)
        self.assertIn('orchestrator_clear_degraded(self.orchestrator_id, "task_status_failed"', after)


if __name__ == "__main__":
    unittest.main()
