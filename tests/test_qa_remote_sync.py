"""QA: qa_remote's sync step calls transport_sync unmodified, against the
QA path and the QA pointer -- never the production ones.

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §2.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import config
import db
import qa_remote


class SyncIsolationTests(unittest.IsolatedAsyncioTestCase):
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
        await db.ssh_transport_set_last_synced_sha("t1", "prodsha-untouched")

    async def _prepared(self):
        transport = await db.ssh_transport_get("t1", "admin")
        return qa_remote.Prepared(transport=transport, machine_id="m1", floor_mb=700)

    async def test_sync_calls_the_qa_path_and_qa_pointer(self):
        fake = {"ok": True, "files_changed": 2, "reason": "", "head_sha": "qasha1"}
        with patch("transport_sync.sync_transport", AsyncMock(return_value=fake)) as mocked:
            await qa_remote._sync(await self._prepared())
        mocked.assert_awaited_once_with("m1", qa_remote.QA_REMOTE_PATH, "")

    async def test_success_advances_only_the_qa_pointer(self):
        fake = {"ok": True, "files_changed": 1, "reason": "", "head_sha": "qasha2"}
        with patch("transport_sync.sync_transport", AsyncMock(return_value=fake)):
            await qa_remote._sync(await self._prepared())

        row = await db.ssh_transport_get("t1", "admin")
        self.assertEqual(row["last_qa_synced_sha"], "qasha2")
        self.assertEqual(
            row["last_synced_sha"], "prodsha-untouched",
            "a QA sync must never read or write the production sync pointer")

    async def test_failure_does_not_advance_the_qa_pointer(self):
        fake = {"ok": False, "files_changed": 0, "reason": "boom", "head_sha": ""}
        with patch("transport_sync.sync_transport", AsyncMock(return_value=fake)):
            await qa_remote._sync(await self._prepared())

        row = await db.ssh_transport_get("t1", "admin")
        self.assertEqual(row["last_qa_synced_sha"], "")

    async def test_second_sync_passes_the_previously_advanced_qa_sha(self):
        first = {"ok": True, "files_changed": 1, "reason": "", "head_sha": "qasha-a"}
        with patch("transport_sync.sync_transport", AsyncMock(return_value=first)):
            await qa_remote._sync(await self._prepared())

        second = {"ok": True, "files_changed": 0, "reason": "already up to date",
                  "head_sha": "qasha-a"}
        with patch("transport_sync.sync_transport", AsyncMock(return_value=second)) as mocked:
            await qa_remote._sync(await self._prepared())
        mocked.assert_awaited_once_with("m1", qa_remote.QA_REMOTE_PATH, "qasha-a")


if __name__ == "__main__":
    unittest.main()
