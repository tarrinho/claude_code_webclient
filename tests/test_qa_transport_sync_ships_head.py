"""QA: a transport receives committed content, never the working tree.

transport_sync's manifest is commit-derived and that is its stated security
property -- "a request can trigger *that* a sync happens, but never *what*
gets synced". The manifest was never the problem. `apply_plan` then did

    sftp.put(str(_REPO_ROOT / rel_path), remote_path)

which reads the bytes off disk. So the file *list* came from git and the file
*contents* came from whatever happened to be saved, and `last_synced_sha` was
advanced to `head_sha` afterwards -- recording that the transport matches a
commit it may never have received.

Six sessions share this checkout, so at any moment the working tree is a blend
of several half-finished changes. The same defect in the QA runbook's §14 sync
shipped an uncommitted `UnboundLocalError` to the test node and produced 20
failures against a defect that existed in no commit (fixed 2026-09-12 in
3acb266). Here the blast radius is larger: these files are what a transport
*runs*, and 031e337 showed the relay's behaviour depends directly on what is
deployed at `remote_path`.

Each test builds a throwaway git repo and points `_REPO_ROOT` at it, so the
real checkout is never read or modified.
"""
from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import transport_sync


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout


class _FakeSFTP:
    """Records what would have been written, without a network."""

    def __init__(self) -> None:
        self.written: dict[str, bytes] = {}
        self.removed: list[str] = []

    # paramiko's SFTPClient surface, only the parts apply_plan uses.
    def put(self, localpath, remotepath):
        self.written[remotepath] = Path(localpath).read_bytes()

    def putfo(self, fileobj, remotepath, **kwargs):
        self.written[remotepath] = fileobj.read()

    def remove(self, remotepath):
        self.removed.append(remotepath)

    def stat(self, path):
        raise FileNotFoundError(path)

    def mkdir(self, path):
        return None

    def close(self):
        return None


class SyncShipsCommittedContentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@example.invalid")
        _git(self.repo, "config", "user.name", "t")

        self.tracked = self.repo / "module.py"
        self.tracked.write_text("COMMITTED = 1\n")
        _git(self.repo, "add", "module.py")
        _git(self.repo, "commit", "-qm", "initial")
        self.head = _git(self.repo, "rev-parse", "HEAD").strip()

        self._patch = patch.object(transport_sync, "_REPO_ROOT", self.repo)
        self._patch.start()
        self.addCleanup(self._patch.stop)

        self.sftp = _FakeSFTP()

        async def _open_sftp(machine_id):
            return self.sftp

        import tunnel_manager_ssh
        self._sftp_patch = patch.object(
            tunnel_manager_ssh, "open_sftp", _open_sftp)
        self._sftp_patch.start()
        self.addCleanup(self._sftp_patch.stop)

    async def _push_all(self):
        plan = await transport_sync.compute_plan("")
        await transport_sync.apply_plan("machine-1", "/remote", plan)
        return plan

    async def test_an_uncommitted_edit_is_not_shipped(self):
        """The defect, stated as the thing a reader most needs to believe."""
        self.tracked.write_text("COMMITTED = 1\nUNCOMMITTED_EDIT = True\n")
        await self._push_all()
        sent = self.sftp.written["/remote/module.py"]
        self.assertNotIn(b"UNCOMMITTED_EDIT", sent,
                         "the working tree reached the transport")
        self.assertEqual(sent, b"COMMITTED = 1\n")

    async def test_what_is_shipped_matches_the_sha_that_gets_recorded(self):
        """apply_plan's content and the pointer the caller stores afterwards
        must describe the same commit, or `last_synced_sha` is a claim about a
        state that never existed on the far host."""
        self.tracked.write_text("drifted\n")
        plan = await self._push_all()
        from_git = await asyncio.to_thread(
            lambda: subprocess.run(
                ["git", "-C", str(self.repo), "show", f"{plan.head_sha}:module.py"],
                capture_output=True, check=True,
            ).stdout
        )
        self.assertEqual(self.sftp.written["/remote/module.py"], from_git)
        self.assertEqual(plan.head_sha, self.head)

    async def test_an_untracked_file_is_never_shipped(self):
        """Already true via the manifest; asserted so a future "just copy the
        directory" simplification cannot quietly widen what leaves this host."""
        (self.repo / "secret.env").write_text("TOKEN=hunter2\n")
        await self._push_all()
        self.assertNotIn("/remote/secret.env", self.sftp.written)

    async def test_a_deleted_working_copy_still_ships(self):
        """Content comes from the commit, so a file missing from disk is not a
        transfer failure. Under the old read-from-disk behaviour this raised
        and aborted the whole sync partway through."""
        self.tracked.unlink()
        await self._push_all()
        self.assertEqual(self.sftp.written["/remote/module.py"], b"COMMITTED = 1\n")

    async def test_binary_content_survives_the_round_trip(self):
        """Reading committed bytes must not go through a text decode."""
        blob = bytes(range(256))
        (self.repo / "data.bin").write_bytes(blob)
        _git(self.repo, "add", "data.bin")
        _git(self.repo, "commit", "-qm", "binary")
        await self._push_all()
        self.assertEqual(self.sftp.written["/remote/data.bin"], blob)


if __name__ == "__main__":
    unittest.main()
