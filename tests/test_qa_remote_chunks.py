"""QA: qa_remote's chunked remote execution, status tagging, and the
streamed event sequence.

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §6.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import config
import db
import qa_remote

ROOT = Path(__file__).resolve().parents[1]


def _exec_result(text: str, rc: int = 0):
    stdout = MagicMock()
    stdout.read.return_value = text.encode("utf-8")
    stdout.channel.recv_exit_status.return_value = rc
    stderr = MagicMock()
    stderr.read.return_value = b""
    return (MagicMock(), stdout, stderr)


class ChunkFileListParityTests(unittest.TestCase):
    """run-suite-chunked.sh and qa_remote.py must never define "a chunk"
    two different ways -- pinned by checking both sides use the same
    literal fragments, not by re-deriving one from the other."""

    def setUp(self):
        self.source = (ROOT / "bin" / "run-suite-chunked.sh").read_text()

    def test_collect_fragment_matches_the_local_script(self):
        self.assertIn(qa_remote._COLLECT_CMD_FRAGMENT, self.source)

    def test_browser_grep_fragment_matches_the_local_script(self):
        self.assertIn(qa_remote._BROWSER_GREP_FRAGMENT, self.source)

    def test_group_size_matches_the_local_script(self):
        self.assertIn(f'-eq {qa_remote._CHUNK_GROUP_SIZE}', self.source)


class CollectChunksTests(unittest.IsolatedAsyncioTestCase):
    async def test_groups_plain_files_and_separates_browser_files(self):
        files = "\n".join(f"tests/test_{i}.py" for i in range(1, 8))  # 7 plain

        async def fake_exec(machine_id, cmd, timeout):
            if "playwright" in cmd:
                return _exec_result("tests/test_browser_x.py\n")
            return _exec_result(files + "\ntests/test_browser_x.py\n")

        with patch("tunnel_manager_ssh.exec_command", fake_exec):
            plain_chunks, browser_files = await qa_remote._collect_chunks("m1")

        self.assertEqual(browser_files, ["tests/test_browser_x.py"])
        self.assertEqual(len(plain_chunks), 2)  # 6 + 1, group size 6
        self.assertEqual(len(plain_chunks[0]), 6)
        self.assertEqual(len(plain_chunks[1]), 1)


class RunChunkTests(unittest.IsolatedAsyncioTestCase):
    async def test_passed_when_pytest_exits_zero(self):
        with (
            patch("qa_remote._check_capacity", AsyncMock(return_value=(True, 900))),
            patch("tunnel_manager_ssh.exec_command",
                  AsyncMock(return_value=_exec_result("2 passed\n", rc=0))),
        ):
            result = await qa_remote._run_chunk(
                "m1", "plain-01", ["tests/test_a.py"], timeout=600, floor_mb=700)
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.returncode, 0)

    async def test_test_failure_when_pytest_exits_nonzero(self):
        with (
            patch("qa_remote._check_capacity", AsyncMock(return_value=(True, 900))),
            patch("tunnel_manager_ssh.exec_command",
                  AsyncMock(return_value=_exec_result("1 failed\n", rc=1))),
        ):
            result = await qa_remote._run_chunk(
                "m1", "plain-01", ["tests/test_a.py"], timeout=600, floor_mb=700)
        self.assertEqual(result.status, "test_failure")
        self.assertEqual(result.returncode, 1)

    async def test_transport_error_when_exec_command_raises(self):
        with (
            patch("qa_remote._check_capacity", AsyncMock(return_value=(True, 900))),
            patch("tunnel_manager_ssh.exec_command",
                  AsyncMock(side_effect=RuntimeError("ssh dropped"))),
        ):
            result = await qa_remote._run_chunk(
                "m1", "plain-01", ["tests/test_a.py"], timeout=600, floor_mb=700)
        self.assertEqual(result.status, "transport_error")
        self.assertIsNone(result.returncode)

    async def test_capacity_refused_before_running_pytest_at_all(self):
        exec_mock = AsyncMock()
        with (
            patch("qa_remote._check_capacity", AsyncMock(return_value=(False, 300))),
            patch("tunnel_manager_ssh.exec_command", exec_mock),
        ):
            result = await qa_remote._run_chunk(
                "m1", "plain-01", ["tests/test_a.py"], timeout=600, floor_mb=700)
        self.assertEqual(result.status, "capacity_refused")
        exec_mock.assert_not_awaited()


class ExecuteEventStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        await db.ssh_transport_create(
            "t1", "One", "admin", "one.example.net", "kali", "~/.ssh/id_ed25519")
        qa_remote._RUN_LOCKS.clear()
        self.addCleanup(qa_remote._RUN_LOCKS.clear)

    async def _prepared(self):
        transport = await db.ssh_transport_get("t1", "admin")
        return qa_remote.Prepared(transport=transport, machine_id="m1", floor_mb=700)

    async def test_events_arrive_incrementally_not_only_at_the_end(self):
        """The whole reason execute() is a generator and not a coroutine
        returning one blocking result: a caller can observe sync-done before
        the first chunk has even started, not only after the entire run."""
        sync_result = {"ok": True, "files_changed": 1, "reason": "", "head_sha": "sha1"}
        with (
            patch("qa_remote._sync", AsyncMock(return_value=sync_result)),
            patch("qa_remote._collect_chunks",
                  AsyncMock(return_value=([["tests/test_a.py"]], []))),
            patch("qa_remote._run_chunk",
                  AsyncMock(return_value=qa_remote.ChunkResult(
                      "plain-01", "passed", "1 passed", 0))),
        ):
            events = []
            async for event in qa_remote.execute(await self._prepared()):
                events.append(event["type"])
                if event["type"] == "sync-done":
                    # Proven mid-stream: the lock is already held here, well
                    # before run-done, which is what "for the run's whole
                    # duration" (spec §5) means operationally.
                    self.assertTrue(qa_remote._run_lock("m1").locked())

        self.assertEqual(
            events, ["sync-start", "sync-done", "chunk-start", "chunk-result", "run-done"])

    async def test_lock_is_released_after_the_run_even_on_failure(self):
        with patch("qa_remote._sync", AsyncMock(side_effect=RuntimeError("boom"))):
            with self.assertRaises(RuntimeError):
                async for _ in qa_remote.execute(await self._prepared()):
                    pass
        self.assertFalse(qa_remote._run_lock("m1").locked())

    async def test_sync_failure_stops_before_any_chunk_runs(self):
        sync_result = {"ok": False, "files_changed": 0, "reason": "boom", "head_sha": ""}
        collect_mock = AsyncMock()
        with (
            patch("qa_remote._sync", AsyncMock(return_value=sync_result)),
            patch("qa_remote._collect_chunks", collect_mock),
        ):
            events = [e async for e in qa_remote.execute(await self._prepared())]
        collect_mock.assert_not_awaited()
        self.assertEqual(events[-1], {"type": "run-done", "ok": False, "reason": "boom"})

    async def test_run_done_totals_count_each_status(self):
        sync_result = {"ok": True, "files_changed": 0, "reason": "", "head_sha": "sha1"}
        results = [
            qa_remote.ChunkResult("plain-01", "passed", "", 0),
            qa_remote.ChunkResult("plain-02", "test_failure", "", 1),
        ]
        with (
            patch("qa_remote._sync", AsyncMock(return_value=sync_result)),
            patch("qa_remote._collect_chunks", AsyncMock(
                return_value=([["a.py"], ["b.py"]], []))),
            patch("qa_remote._run_chunk", AsyncMock(side_effect=results)),
        ):
            events = [e async for e in qa_remote.execute(await self._prepared())]
        done = events[-1]
        self.assertEqual(done["type"], "run-done")
        self.assertFalse(done["ok"])
        self.assertEqual(done["totals"]["passed"], 1)
        self.assertEqual(done["totals"]["test_failure"], 1)

    async def test_locked_transport_is_refused_immediately_not_queued(self):
        """Task 3's resolve_transport only checks lock.locked() before
        returning success -- it never acquires. A second concurrent caller
        must therefore be refused by execute() itself, not silently block
        waiting for the first run to finish (spec §5)."""
        lock = qa_remote._run_lock("m1")
        await lock.acquire()
        try:
            with patch("qa_remote._sync", AsyncMock()) as sync_mock:
                events = [e async for e in qa_remote.execute(await self._prepared())]
            sync_mock.assert_not_awaited()
        finally:
            lock.release()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "run-done")
        self.assertFalse(events[0]["ok"])
        self.assertIn("already has a QA run in progress", events[0]["reason"])


if __name__ == "__main__":
    unittest.main()
