"""QA: qa_remote's sync step calls transport_sync unmodified, against the
QA path and the QA pointer -- never the production ones.

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §2.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import config
import db
import qa_remote


def _exec_home(text: str):
    stdout = MagicMock()
    stdout.read.return_value = text.encode("utf-8")
    return (MagicMock(), stdout, MagicMock())


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

    async def test_sync_resolves_home_and_uses_it_for_the_sftp_bound_call(self):
        """SFTP does not do bash-style '~' expansion -- the tilde form would
        put/mkdir against a literal '~' directory at the SFTP root, not the
        real checkout at $HOME/wc-qa-checkout. $HOME is resolved live via
        exec_command (which does get shell expansion) and only the
        resolved absolute path is handed to the SFTP-bound sync_transport
        call."""
        async def fake_exec(machine_id, cmd, timeout):
            self.assertEqual(cmd, "echo $HOME")
            return _exec_home("/home/kali\n")

        fake_sync = {"ok": True, "files_changed": 1, "reason": "", "head_sha": "qasha1"}
        with (
            patch("tunnel_manager_ssh.exec_command", AsyncMock(side_effect=fake_exec)),
            patch("transport_sync.sync_transport", AsyncMock(return_value=fake_sync)) as mocked,
        ):
            await qa_remote._sync(await self._prepared())
        mocked.assert_awaited_once_with("m1", "/home/kali/wc-qa-checkout", "")

    async def test_sync_falls_back_to_the_tilde_form_when_home_cannot_be_read(self):
        """A failed $HOME read must not hard-fail the whole run over an
        unrelated hiccup -- it falls back to today's existing (already
        broken for SFTP, but no worse than before this fix) tilde form."""
        fake_sync = {"ok": True, "files_changed": 1, "reason": "", "head_sha": "qasha1"}
        with (
            patch("tunnel_manager_ssh.exec_command",
                  AsyncMock(side_effect=RuntimeError("tunnel not connected"))),
            patch("transport_sync.sync_transport", AsyncMock(return_value=fake_sync)) as mocked,
        ):
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
