"""QA: scratch-space reclaim for the Server tab.

This deletes files, which is irreversible, so the tests here are weighted
towards what must NOT be offered rather than towards the happy path. Each
exclusion in ``sys_reclaim.preview`` gets a test that fails if the exclusion
is removed: an entry wrongly offered here is somebody's in-flight test run or
their editor's buffer, and nothing downstream will catch it.

The one that matters most is ``test_a_path_a_process_holds_open_is_skipped``.
Age alone is not enough -- a suite that has been running for three hours has
scratch directories that are both old and live -- so the open-descriptor scan
is the check that makes "without impacting the service" true rather than
likely.
"""
from __future__ import annotations

import os
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import auth
import config
import db
from routes import sys_reclaim


HTTPS = "https://testserver"
OLD = 60 * 60 * 24 * 3  # three days


def _age(path: str, seconds: int) -> None:
    """Backdate mtime (and atime) so the entry reads as stale."""
    when = time.time() - seconds
    os.utime(path, (when, when))


class PreviewExclusionTests(unittest.TestCase):
    """What ``preview`` refuses to offer, one reason at a time.

    The temporary directory stands in for a tmpfs root, so ``_tmpfs_roots``
    is patched -- the real one checks /proc/mounts and would return the
    host's own /tmp, which these tests must never scan let alone delete.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        patcher = patch.object(sys_reclaim, "_tmpfs_roots", lambda: [self.root])
        patcher.start()
        self.addCleanup(patcher.stop)
        # No process holds anything in this fixture unless a test says so.
        patcher = patch.object(sys_reclaim, "_protected_paths", lambda: set())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make(self, name, *, age=OLD, is_dir=False, size=1024):
        path = os.path.join(self.root, name)
        if is_dir:
            os.mkdir(path)
            inner = os.path.join(path, "payload")
            Path(inner).write_bytes(b"x" * size)
            _age(inner, age)
        else:
            Path(path).write_bytes(b"x" * size)
        _age(path, age)
        return path

    def _offered(self):
        return {row["path"] for row in sys_reclaim.preview()["entries"]}

    def _reason_for(self, path):
        for row in sys_reclaim.preview()["skipped"]:
            if row["path"] == path:
                return row["reason"]
        return None

    def test_a_stale_webconsole_leftover_is_offered(self):
        path = self._make("dast_wc_12345.db")
        self.assertIn(path, self._offered())

    def test_it_is_classified_as_this_project_s_own(self):
        """The class decides the default checkbox state, so it is load bearing."""
        self._make("dast_wc_12345.db")
        row = sys_reclaim.preview()["entries"][0]
        self.assertEqual(row["kind"], "webconsole")

    def test_an_unrecognised_name_is_offered_as_other(self):
        self._make("somebody-elses-scratch")
        row = sys_reclaim.preview()["entries"][0]
        self.assertEqual(row["kind"], "other")

    def test_a_recent_file_is_skipped(self):
        path = self._make("dast_wc_recent.db", age=60)
        self.assertNotIn(path, self._offered())
        self.assertIn("min ago", self._reason_for(path) or "")

    def test_a_directory_is_as_new_as_its_newest_file(self):
        """An old shell around a live tree must not read as stale.

        This is how a long-running suite looks: the directory was created
        hours ago and is being written to right now.
        """
        path = self._make("wcval", is_dir=True, age=OLD)
        fresh = os.path.join(path, "in-progress")
        Path(fresh).write_bytes(b"live")
        self.assertNotIn(path, self._offered())

    def test_a_path_a_process_holds_open_is_skipped(self):
        """The check that keeps a peer's running job out of the list."""
        path = self._make("wcg", is_dir=True)
        held = os.path.join(path, "payload")
        with patch.object(sys_reclaim, "_protected_paths", lambda: {held}):
            self.assertNotIn(path, self._offered())
            self.assertEqual(self._reason_for(path),
                             "a running process is using it")

    def test_a_protected_name_is_never_offered(self):
        """Infrastructure that satisfies every other rule and must still stay."""
        for name in (".X11-unix", "systemd-private-abc", "screen-1", "tmux-1000"):
            path = self._make(name, is_dir=True)
            self.assertNotIn(path, self._offered(), name)

    def test_a_socket_is_never_offered(self):
        import socket

        path = os.path.join(self.root, "live.sock")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.bind(path)
        _age(path, OLD)
        self.assertNotIn(path, self._offered())

    def test_another_uid_s_entry_is_never_offered(self):
        path = self._make("not-ours")
        with patch.object(sys_reclaim, "_own_uid", lambda: os.getuid() + 1):
            self.assertNotIn(path, self._offered())

    def test_this_deployment_s_database_is_never_offered(self):
        path = self._make("webconsole.db")
        with patch.object(config, "DB_PATH", path):
            self.assertNotIn(path, self._offered())
            self.assertEqual(self._reason_for(path),
                             "this deployment's database")

    def test_a_symlink_out_of_the_root_is_refused(self):
        """A link is not followed out of tmpfs, and is not deleted either."""
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        path = os.path.join(self.root, "escape")
        os.symlink(outside.name, path)
        self.assertNotIn(path, self._offered())

    def test_nothing_outside_the_top_level_is_listed(self):
        """Only direct children are candidates, never what is inside them."""
        parent = self._make("wc-tree", is_dir=True)
        offered = self._offered()
        self.assertIn(parent, offered)
        self.assertNotIn(os.path.join(parent, "payload"), offered)

    def test_the_total_is_the_sum_of_what_is_offered(self):
        self._make("wc-a", size=2 * 1024 * 1024)
        self._make("wc-b", size=1024 * 1024)
        stats = sys_reclaim.preview()
        self.assertAlmostEqual(
            stats["total_mb"], sum(r["size_mb"] for r in stats["entries"]), places=1
        )


class ExecuteTests(unittest.TestCase):
    """``execute`` deletes exactly what a fresh preview still offers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        for target, replacement in (
            ("_tmpfs_roots", lambda: [self.root]),
            ("_protected_paths", lambda: set()),
        ):
            patcher = patch.object(sys_reclaim, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Two megabytes, not two kilobytes: sizes are reported rounded to a
        # tenth of a megabyte, so a kilobyte-sized fixture makes every freed
        # total read as 0.0 and the size assertions test nothing.
        self.file = os.path.join(self.root, "dast_wc_1.db")
        Path(self.file).write_bytes(b"x" * 2 * 1024 * 1024)
        _age(self.file, OLD)
        self.dir = os.path.join(self.root, "wcval")
        os.mkdir(self.dir)
        Path(os.path.join(self.dir, "payload")).write_bytes(b"y" * 2048)
        _age(os.path.join(self.dir, "payload"), OLD)
        _age(self.dir, OLD)

    def test_a_selected_file_is_deleted(self):
        sys_reclaim.execute([self.file])
        self.assertFalse(os.path.exists(self.file))

    def test_an_unselected_entry_survives(self):
        sys_reclaim.execute([self.file])
        self.assertTrue(os.path.isdir(self.dir))

    def test_a_selected_directory_is_removed_whole(self):
        sys_reclaim.execute([self.dir])
        self.assertFalse(os.path.exists(self.dir))

    def test_a_path_the_preview_does_not_offer_is_refused(self):
        """The stale-page case: the page listed it, the scan no longer does."""
        outside = os.path.join(self.root, "fresh")
        Path(outside).write_bytes(b"live")  # current mtime, so not reclaimable
        result = sys_reclaim.execute([outside])
        self.assertTrue(os.path.exists(outside))
        self.assertEqual(result["refused"], [outside])
        self.assertEqual(result["deleted"], [])

    def test_a_path_outside_the_roots_is_refused(self):
        """Absolute paths from a client are re-derived, never trusted."""
        other = tempfile.NamedTemporaryFile(delete=False)
        self.addCleanup(os.unlink, other.name)
        other.close()
        _age(other.name, OLD)
        result = sys_reclaim.execute([other.name])
        self.assertTrue(os.path.exists(other.name))
        self.assertEqual(result["refused"], [other.name])

    def test_an_empty_selection_deletes_nothing(self):
        sys_reclaim.execute([])
        self.assertTrue(os.path.exists(self.file))
        self.assertTrue(os.path.isdir(self.dir))

    def test_freed_mb_counts_only_what_went(self):
        result = sys_reclaim.execute([self.file])
        self.assertGreater(result["freed_mb"], 0)
        self.assertEqual(len(result["deleted"]), 1)


def _client():
    from fastapi.testclient import TestClient

    from app import app as web_app
    return TestClient(web_app, raise_server_exceptions=False, base_url=HTTPS)


class ReclaimRouteTests(unittest.IsolatedAsyncioTestCase):
    """The two endpoints, including the request that must be refused."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for attr, value in (
            ("DB_PATH", f"{self.tmp.name}/db"),
            ("PROJECTS_ROOT", f"{self.tmp.name}/p"),
        ):
            patcher = patch.object(config, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

        self.password = secrets.token_urlsafe(16)
        await db.user_create("alice", None, auth.hash_password(self.password))

        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        for target, replacement in (
            ("_tmpfs_roots", lambda: [self.root.name]),
            ("_protected_paths", lambda: set()),
        ):
            patcher = patch.object(sys_reclaim, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stale = os.path.join(self.root.name, "dast_wc_9.db")
        Path(self.stale).write_bytes(b"x" * 4096)
        _age(self.stale, OLD)

    def _login(self):
        client = _client()
        response = client.post(
            "/login", json={"username": "alice", "password": self.password}
        )
        self.assertEqual(response.status_code, 200, "the fixture must log in")
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    def _execute(self, body):
        client, headers = self._login()
        return client.post(
            "/api/system/reclaim/execute", json=body, headers=headers
        )

    def test_preview_lists_the_stale_entry(self):
        client, _ = self._login()
        response = client.get("/api/system/reclaim/preview")
        self.assertEqual(response.status_code, 200, response.text)
        paths = [row["path"] for row in response.json()["entries"]]
        self.assertIn(self.stale, paths)

    def test_preview_requires_a_session(self):
        """It reports host paths, so it stays behind auth like every reading."""
        response = _client().get("/api/system/reclaim/preview")
        self.assertIn(response.status_code, (401, 403), response.text)

    def test_a_selected_path_is_deleted(self):
        response = self._execute({"paths": [self.stale]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(os.path.exists(self.stale))

    def test_an_all_stale_selection_is_refused(self):
        response = self._execute({"paths": ["/tmp/gone-since-the-scan"]})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertTrue(os.path.exists(self.stale),
                        "a refused request must delete nothing at all")

    def test_an_empty_selection_is_refused(self):
        response = self._execute({"paths": []})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertTrue(os.path.exists(self.stale))

    def test_a_body_without_paths_is_refused(self):
        """An old client posting nothing must never mean "delete everything"."""
        response = self._execute({})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertTrue(os.path.exists(self.stale))


if __name__ == "__main__":
    unittest.main()
