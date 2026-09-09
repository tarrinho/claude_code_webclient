from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import db


class SyncRequestLifecycleTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_create_defaults_to_pending(self):
        req_id = await db.sync_request_create("t1", "admin", "ui")
        row = await db.sync_request_get(req_id, "admin")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["transport_id"], "t1")
        self.assertEqual(row["requested_by"], "ui")
        self.assertIsNone(row["resolved_at"])

    async def test_create_with_explicit_status(self):
        """The UI path inserts at 'approved' directly -- the click is the
        approval, per the design doc's decision to unify rather than split
        the two entry points into different code paths."""
        req_id = await db.sync_request_create("t1", "admin", "ui", status="approved")
        row = await db.sync_request_get(req_id, "admin")
        self.assertEqual(row["status"], "approved")

    async def test_get_is_owner_scoped(self):
        req_id = await db.sync_request_create("t1", "admin", "ui")
        self.assertIsNone(await db.sync_request_get(req_id, "someone-else"))

    async def test_list_pending_excludes_resolved(self):
        pending_id = await db.sync_request_create("t1", "admin", "ui")
        done_id = await db.sync_request_create("t2", "admin", "ui")
        await db.sync_request_resolve(done_id, "done", files_changed=3)

        pending = await db.sync_request_list_pending("admin")
        ids = {r["id"] for r in pending}
        self.assertIn(pending_id, ids)
        self.assertNotIn(done_id, ids)

    async def test_list_pending_is_owner_scoped(self):
        await db.sync_request_create("t1", "admin", "ui")
        pending = await db.sync_request_list_pending("someone-else")
        self.assertEqual(pending, [])

    async def test_resolve_records_files_changed_and_reason(self):
        req_id = await db.sync_request_create("t1", "admin", "ui")
        await db.sync_request_resolve(req_id, "failed", files_changed=0, reason="boom")
        row = await db.sync_request_get(req_id, "admin")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["files_changed"], 0)
        self.assertEqual(row["reason"], "boom")
        self.assertIsNotNone(row["resolved_at"])


if __name__ == "__main__":
    unittest.main()
