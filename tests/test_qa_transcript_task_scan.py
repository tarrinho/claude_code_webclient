"""QA: finding Task-tool subagents in a transcript.

Subagents run inside the CLI process, so the console cannot hook their spawn
(CLAUDE.md §0). The transcript is the only place they are observable, as
`tool_use` blocks whose name is `Task`. Status comes from pairing a `tool_use`
with its `tool_result`, the same way _scan_questions_sync pairs questions.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import transcripts


def _write(records: list[dict]) -> Path:
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
    for record in records:
        tmp.write(json.dumps(record) + "\n")
    tmp.close()
    return Path(tmp.name)


def _task_use(tool_use_id, subagent_type, description, ts):
    return {"timestamp": ts, "message": {"content": [
        {"type": "tool_use", "id": tool_use_id, "name": "Task",
         "input": {"subagent_type": subagent_type, "description": description}},
    ]}}


def _task_result(tool_use_id, ts):
    return {"timestamp": ts, "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"},
    ]}}


class TaskScanTests(unittest.TestCase):

    def setUp(self):
        transcripts._task_scan_cache.clear()

    def test_an_unfinished_task_is_running(self):
        path = _write([_task_use("tu_1", "code-review", "review the diff",
                                 "2026-09-21T10:00:00Z")])
        self.addCleanup(path.unlink)
        got = transcripts._scan_tasks_sync(path)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["tool_use_id"], "tu_1")
        self.assertEqual(got[0]["agent_type"], "code-review")
        self.assertEqual(got[0]["description"], "review the diff")
        self.assertEqual(got[0]["status"], "running")
        self.assertEqual(got[0]["started_at"], "2026-09-21T10:00:00Z")
        self.assertIsNone(got[0]["ended_at"])

    def test_a_paired_task_is_done_and_carries_its_end_time(self):
        path = _write([
            _task_use("tu_1", "code-review", "review", "2026-09-21T10:00:00Z"),
            _task_result("tu_1", "2026-09-21T10:00:30Z"),
        ])
        self.addCleanup(path.unlink)
        got = transcripts._scan_tasks_sync(path)
        self.assertEqual(got[0]["status"], "done")
        self.assertEqual(got[0]["ended_at"], "2026-09-21T10:00:30Z")

    def test_a_result_arriving_before_its_use_is_still_paired(self):
        """Records are read in file order, and nothing guarantees a result
        cannot be read first after a compaction or a partial write."""
        path = _write([
            _task_result("tu_1", "2026-09-21T10:00:30Z"),
            _task_use("tu_1", "code-review", "review", "2026-09-21T10:00:00Z"),
        ])
        self.addCleanup(path.unlink)
        got = transcripts._scan_tasks_sync(path)
        self.assertEqual(got[0]["status"], "done")

    def test_other_tools_are_ignored(self):
        path = _write([{"message": {"content": [
            {"type": "tool_use", "id": "tu_9", "name": "Bash",
             "input": {"command": "ls"}}]}}])
        self.addCleanup(path.unlink)
        self.assertEqual(transcripts._scan_tasks_sync(path), [])

    def test_askuserquestion_is_not_a_subagent(self):
        """The other scanner's tool must not leak into this one."""
        path = _write([{"message": {"content": [
            {"type": "tool_use", "id": "tu_8", "name": "AskUserQuestion",
             "input": {"questions": []}}]}}])
        self.addCleanup(path.unlink)
        self.assertEqual(transcripts._scan_tasks_sync(path), [])

    def test_several_tasks_come_back_in_start_order(self):
        path = _write([
            _task_use("tu_1", "a", "first", "2026-09-21T10:00:00Z"),
            _task_use("tu_2", "b", "second", "2026-09-21T10:00:05Z"),
        ])
        self.addCleanup(path.unlink)
        got = transcripts._scan_tasks_sync(path)
        self.assertEqual([r["tool_use_id"] for r in got], ["tu_1", "tu_2"])

    def test_a_missing_file_is_empty_not_an_error(self):
        self.assertEqual(
            transcripts._scan_tasks_sync(Path("/nonexistent/x.jsonl")), [])

    def test_a_malformed_line_does_not_lose_the_rest(self):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
        tmp.write("{not json\n")
        tmp.write(json.dumps(
            _task_use("tu_1", "a", "d", "2026-09-21T10:00:00Z")) + "\n")
        tmp.close()
        path = Path(tmp.name)
        self.addCleanup(path.unlink)
        self.assertEqual(len(transcripts._scan_tasks_sync(path)), 1)

    def test_a_task_with_no_input_fields_still_yields_a_row(self):
        """A subagent that named neither type nor description is still a
        subagent; dropping it would hide real work."""
        path = _write([{"timestamp": "2026-09-21T10:00:00Z", "message": {
            "content": [{"type": "tool_use", "id": "tu_1", "name": "Task",
                         "input": {}}]}}])
        self.addCleanup(path.unlink)
        got = transcripts._scan_tasks_sync(path)
        self.assertEqual(len(got), 1)
        self.assertIsNone(got[0]["agent_type"])

    def test_the_cache_is_keyed_on_size_like_the_question_scan(self):
        path = _write([_task_use("tu_1", "a", "d", "2026-09-21T10:00:00Z")])
        self.addCleanup(path.unlink)
        transcripts._scan_tasks_sync(path)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(
                _task_use("tu_2", "b", "d2", "2026-09-21T10:00:05Z")) + "\n")
        self.assertEqual(len(transcripts._scan_tasks_sync(path)), 2,
                         "a grown file must be re-read, not served from cache")


if __name__ == "__main__":
    unittest.main()
