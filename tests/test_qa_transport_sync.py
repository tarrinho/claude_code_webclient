"""QA: transport_sync.py -- the sync engine, and two real bugs found by
testing it against a live host before this suite existed.

Design: docs/superpowers/specs/2026-09-09-transport-project-sync-design.md

1. _sftp_makedirs_sync dropped the leading '/' on an absolute remote path
   (an earlier `current or part` fell back to the bare part on the first
   iteration), so every stat/mkdir landed relative to the SFTP session's
   own default directory instead of the intended absolute one. It did not
   raise -- it silently created a stray directory tree under the session's
   home instead, confirmed live against pentester.tail850c40.ts.net.
2. git ls-files includes gitlink entries (mode 160000 -- a committed
   worktree in this exact repo, .claude/worktrees/db-modularize) that are
   not regular files. sftp.put() on one tries to open a directory as a
   file and fails.
"""
from __future__ import annotations

import unittest
import unittest.mock

import transport_sync as ts


def _fake_proc(stdout: bytes, returncode: int = 0, stderr: bytes = b""):
    proc = unittest.mock.AsyncMock()
    proc.communicate = unittest.mock.AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


class SafeRelativeTests(unittest.TestCase):
    def test_ordinary_relative_path_passes(self):
        self.assertEqual(ts._safe_relative("db.py"), "db.py")
        self.assertEqual(ts._safe_relative("routes/chats.py"), "routes/chats.py")

    def test_parent_traversal_is_rejected(self):
        with self.assertRaises(ts.SyncError):
            ts._safe_relative("../etc/passwd")

    def test_nested_parent_traversal_is_rejected(self):
        with self.assertRaises(ts.SyncError):
            ts._safe_relative("routes/../../etc/passwd")

    def test_absolute_path_is_rejected(self):
        with self.assertRaises(ts.SyncError):
            ts._safe_relative("/etc/passwd")

    def test_empty_path_is_rejected(self):
        with self.assertRaises(ts.SyncError):
            ts._safe_relative("")


class MakedirsSyncTests(unittest.TestCase):
    """Regression test for the exact bug found live: the leading '/' must
    survive into every stat()/mkdir() call, not just the first."""

    def test_absolute_path_components_keep_their_leading_slash(self):
        sftp = unittest.mock.MagicMock()
        sftp.stat.side_effect = FileNotFoundError()

        ts._sftp_makedirs_sync(sftp, "/home/u/wc-proxy/a/b")

        checked = [call.args[0] for call in sftp.stat.call_args_list]
        # The bug's exact symptom: a version that drops the slash checks
        # "home", "home/u", ... instead of "/home", "/home/u", ...
        self.assertEqual(
            checked, ["/home", "/home/u", "/home/u/wc-proxy", "/home/u/wc-proxy/a",
                      "/home/u/wc-proxy/a/b"],
        )
        for path in checked:
            self.assertTrue(path.startswith("/"), f"{path!r} lost its leading slash")

    def test_existing_directories_are_not_recreated(self):
        sftp = unittest.mock.MagicMock()
        sftp.stat.return_value = object()  # exists -- no FileNotFoundError

        ts._sftp_makedirs_sync(sftp, "/home/u/wc-proxy")

        sftp.mkdir.assert_not_called()

    def test_root_and_dot_are_no_ops(self):
        sftp = unittest.mock.MagicMock()
        ts._sftp_makedirs_sync(sftp, "/")
        ts._sftp_makedirs_sync(sftp, ".")
        ts._sftp_makedirs_sync(sftp, "")
        sftp.stat.assert_not_called()
        sftp.mkdir.assert_not_called()


class ComputePlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_sync_pushes_the_full_manifest_minus_gitlinks(self):
        async def fake_run_git(*args):
            if args[0] == "rev-parse":
                return "abc123\n"
            if args[0] == "ls-files" and args[-1] == "-s":
                return (
                    "100644 deadbeef 0\tdb.py\n"
                    "100644 deadbeef 0\troutes/chats.py\n"
                    "160000 cafebabe 0\t.claude/worktrees/db-modularize\n"
                )
            if args[0] == "ls-files":
                return "db.py\nroutes/chats.py\n.claude/worktrees/db-modularize\n"
            raise AssertionError(f"unexpected git call: {args}")

        with unittest.mock.patch.object(ts, "_run_git", fake_run_git):
            plan = await ts.compute_plan("")

        self.assertEqual(sorted(plan.to_push), ["db.py", "routes/chats.py"])
        self.assertEqual(plan.to_delete, [])
        self.assertEqual(plan.head_sha, "abc123")

    async def test_same_sha_is_a_no_op(self):
        async def fake_run_git(*args):
            if args[0] == "rev-parse":
                return "abc123\n"
            raise AssertionError(f"unexpected git call for a no-op sync: {args}")

        with unittest.mock.patch.object(ts, "_run_git", fake_run_git):
            plan = await ts.compute_plan("abc123")

        self.assertEqual(plan.to_push, [])
        self.assertEqual(plan.to_delete, [])

    async def test_diff_separates_added_modified_and_deleted(self):
        async def fake_run_git(*args):
            if args[0] == "rev-parse":
                return "def456\n"
            if args[0] == "ls-files" and "-s" in args:
                return ""  # no gitlinks in this diff
            if args[0] == "diff":
                return (
                    "A\tnew_file.py\n"
                    "M\tdb.py\n"
                    "D\told_file.py\n"
                )
            raise AssertionError(f"unexpected git call: {args}")

        with unittest.mock.patch.object(ts, "_run_git", fake_run_git):
            plan = await ts.compute_plan("abc123")

        self.assertEqual(sorted(plan.to_push), ["db.py", "new_file.py"])
        self.assertEqual(plan.to_delete, ["old_file.py"])

    async def test_rename_is_delete_old_plus_push_new(self):
        async def fake_run_git(*args):
            if args[0] == "rev-parse":
                return "def456\n"
            if args[0] == "ls-files" and "-s" in args:
                return ""
            if args[0] == "diff":
                return "R100\told_name.py\tnew_name.py\n"
            raise AssertionError(f"unexpected git call: {args}")

        with unittest.mock.patch.object(ts, "_run_git", fake_run_git):
            plan = await ts.compute_plan("abc123")

        self.assertEqual(plan.to_push, ["new_name.py"])
        self.assertEqual(plan.to_delete, ["old_name.py"])

    async def test_a_diff_touching_a_gitlink_excludes_it(self):
        async def fake_run_git(*args):
            if args[0] == "rev-parse":
                return "def456\n"
            if args[0] == "ls-files" and "-s" in args:
                return "160000 cafebabe 0\t.claude/worktrees/db-modularize\n"
            if args[0] == "diff":
                return "M\t.claude/worktrees/db-modularize\nM\tdb.py\n"
            raise AssertionError(f"unexpected git call: {args}")

        with unittest.mock.patch.object(ts, "_run_git", fake_run_git):
            plan = await ts.compute_plan("abc123")

        self.assertEqual(plan.to_push, ["db.py"])


class ApplyPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_pushes_and_deletes_the_right_files(self):
        sftp = unittest.mock.MagicMock()
        sftp.stat.return_value = object()  # every directory already exists

        async def fake_open_sftp(machine_id):
            return sftp

        plan = ts.SyncPlan(to_push=["db.py"], to_delete=["old.py"], head_sha="abc123")

        # apply_plan now reads each file's bytes from the commit rather than
        # from disk, so head_sha reaches `git show`. This test is about which
        # paths move, not what is in them, and "abc123" is not a real object --
        # stubbed so it stays a test about the plan.
        async def fake_committed_bytes(sha, rel_path):
            return b"contents of " + rel_path.encode()

        with unittest.mock.patch("tunnel_manager_ssh.open_sftp", fake_open_sftp), \
             unittest.mock.patch.object(ts, "_committed_bytes", fake_committed_bytes):
            changed = await ts.apply_plan("m1", "/home/u/wc-proxy", plan)

        self.assertEqual(changed, 2)
        sftp.putfo.assert_called_once()
        sftp.remove.assert_called_once_with("/home/u/wc-proxy/old.py")
        sftp.close.assert_called_once()

    async def test_deleting_an_already_gone_file_is_not_an_error(self):
        sftp = unittest.mock.MagicMock()
        sftp.remove.side_effect = FileNotFoundError()

        async def fake_open_sftp(machine_id):
            return sftp

        plan = ts.SyncPlan(to_push=[], to_delete=["already_gone.py"], head_sha="abc123")

        with unittest.mock.patch("tunnel_manager_ssh.open_sftp", fake_open_sftp):
            changed = await ts.apply_plan("m1", "/home/u/wc-proxy", plan)

        self.assertEqual(changed, 1)

    async def test_no_live_tunnel_raises_sync_error_not_a_crash(self):
        async def fake_open_sftp(machine_id):
            raise RuntimeError("tunnel not connected")

        plan = ts.SyncPlan(to_push=["db.py"], to_delete=[], head_sha="abc123")

        with unittest.mock.patch("tunnel_manager_ssh.open_sftp", fake_open_sftp):
            with self.assertRaises(ts.SyncError):
                await ts.apply_plan("m1", "/home/u/wc-proxy", plan)


class SyncTransportTests(unittest.IsolatedAsyncioTestCase):
    """sync_transport is the top-level orchestrator routes/transports.py
    calls -- these pin its return shape and the no-silent-advancement rule
    at the boundary the caller actually uses."""

    async def test_success_reports_files_changed_and_head_sha(self):
        plan = ts.SyncPlan(to_push=["db.py"], to_delete=[], head_sha="abc123")

        async def fake_compute_plan(sha):
            return plan

        async def fake_apply_plan(machine_id, remote_path, p):
            return 1

        with (
            unittest.mock.patch.object(ts, "compute_plan", fake_compute_plan),
            unittest.mock.patch.object(ts, "apply_plan", fake_apply_plan),
        ):
            result = await ts.sync_transport("m1", "/home/u/wc-proxy", "")

        self.assertTrue(result["ok"])
        self.assertEqual(result["files_changed"], 1)
        self.assertEqual(result["head_sha"], "abc123")

    async def test_already_up_to_date_pushes_nothing(self):
        empty_plan = ts.SyncPlan(to_push=[], to_delete=[], head_sha="abc123")

        async def fake_compute_plan(sha):
            return empty_plan

        apply_called = unittest.mock.AsyncMock()

        with (
            unittest.mock.patch.object(ts, "compute_plan", fake_compute_plan),
            unittest.mock.patch.object(ts, "apply_plan", apply_called),
        ):
            result = await ts.sync_transport("m1", "/home/u/wc-proxy", "abc123")

        apply_called.assert_not_awaited()
        self.assertTrue(result["ok"])
        self.assertEqual(result["files_changed"], 0)

    async def test_failure_reports_ok_false_with_no_head_sha(self):
        """No head_sha on failure -- the caller (routes/transports.py) must
        not advance last_synced_sha, or the failed diff is lost on the next
        sync. Asserted at the boundary the caller reads, not just internally."""
        plan = ts.SyncPlan(to_push=["db.py"], to_delete=[], head_sha="abc123")

        async def fake_compute_plan(sha):
            return plan

        async def fake_apply_plan(machine_id, remote_path, p):
            raise ts.SyncError("transfer failed after 0 file(s): connection reset")

        with (
            unittest.mock.patch.object(ts, "compute_plan", fake_compute_plan),
            unittest.mock.patch.object(ts, "apply_plan", fake_apply_plan),
        ):
            result = await ts.sync_transport("m1", "/home/u/wc-proxy", "")

        self.assertFalse(result["ok"])
        self.assertEqual(result["head_sha"], "")
        self.assertIn("connection reset", result["reason"])


if __name__ == "__main__":
    unittest.main()
