from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import db


class SshTransportsSchemaTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_ssh_transports_table_exists_with_expected_columns(self):
        cur = await db.db_conn.execute("PRAGMA table_info(ssh_transports)")
        columns = {row["name"] for row in await cur.fetchall()}
        self.assertEqual(
            columns,
            {
                "id", "name", "owner_id", "ssh_host", "ssh_user",
                "ssh_key_path", "ssh_host_key_fingerprint", "remote_path",
                "created_at", "updated_at",
            },
        )

    async def test_create_then_get_round_trips(self):
        created_at = await db.ssh_transport_create(
            "t1", "Kali3", "admin", "kali-3.tail850c40.ts.net", "kali",
            "~/.ssh/id_ed25519",
        )
        self.assertTrue(created_at)
        row = await db.ssh_transport_get("t1", "admin")
        self.assertEqual(row["name"], "Kali3")
        self.assertEqual(row["ssh_host"], "kali-3.tail850c40.ts.net")
        self.assertEqual(row["ssh_user"], "kali")
        self.assertEqual(row["ssh_key_path"], "~/.ssh/id_ed25519")
        self.assertEqual(row["ssh_host_key_fingerprint"], "")
        self.assertEqual(row["remote_path"], "~/wc-proxy")

    async def test_get_is_owner_scoped(self):
        await db.ssh_transport_create("t2", "Kali3", "admin", "h", "kali", "k")
        self.assertIsNone(await db.ssh_transport_get("t2", "someone-else"))

    async def test_list_returns_only_this_owners_transports(self):
        await db.ssh_transport_create("t3", "Kali3", "admin", "h1", "kali", "k")
        await db.ssh_transport_create("t4", "Other", "someone-else", "h2", "kali", "k")
        rows = await db.ssh_transports_list("admin")
        self.assertEqual([r["id"] for r in rows], ["t3"])

    async def test_update_changes_only_given_fields(self):
        await db.ssh_transport_create("t5", "Kali3", "admin", "h", "kali", "k")
        updated = await db.ssh_transport_update("t5", "admin", name="Kali3 renamed")
        self.assertTrue(updated)
        row = await db.ssh_transport_get("t5", "admin")
        self.assertEqual(row["name"], "Kali3 renamed")
        self.assertEqual(row["ssh_host"], "h")  # untouched

    async def test_update_is_owner_scoped(self):
        await db.ssh_transport_create("t6", "Kali3", "admin", "h", "kali", "k")
        updated = await db.ssh_transport_update("t6", "someone-else", name="x")
        self.assertFalse(updated)

    async def test_delete_removes_the_row(self):
        await db.ssh_transport_create("t7", "Kali3", "admin", "h", "kali", "k")
        deleted = await db.ssh_transport_delete("t7", "admin")
        self.assertTrue(deleted)
        self.assertIsNone(await db.ssh_transport_get("t7", "admin"))

    async def test_set_host_key_fingerprint_is_not_owner_scoped(self):
        """Called from the tunnel connect loop, which has no request session
        to scope against -- same reasoning as
        ai_machine_set_ssh_host_key_fingerprint."""
        await db.ssh_transport_create("t8", "Kali3", "admin", "h", "kali", "k")
        await db.ssh_transport_set_host_key_fingerprint("t8", "SHA256:abc")
        row = await db.ssh_transport_get("t8", "admin")
        self.assertEqual(row["ssh_host_key_fingerprint"], "SHA256:abc")
