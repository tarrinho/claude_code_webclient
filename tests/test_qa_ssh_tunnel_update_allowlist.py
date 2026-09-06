"""QA: db.ssh_tunnel_update must reject any field not on its allowlist.

Flagged by a rules.md security audit (bandit B608): every other SET-clause
builder in the codebase built from **fields (db_chats.chat_update,
routes/machines.py's field-update helper) checks
`set(fields).issubset(ALLOWED)` before touching SQL; ssh_tunnel_update was
the one still trusting its caller's kwargs directly. Not exploitable today
-- its single call site (tunnel_manager.py) only ever passes hardcoded
keys -- but the column names came from convention, not anything the
interpreter enforces, unlike its siblings.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import db


class SshTunnelUpdateAllowlistQA(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(
            config, "PROJECTS_ROOT", f"{self.tmp.name}/projects"
        )
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        self.machine_id = "m" * 32
        await db.ssh_tunnel_create(machine_id=self.machine_id, local_port=19020)

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_an_allowed_field_is_written(self):
        ok = await db.ssh_tunnel_update(self.machine_id, state="connected")
        self.assertTrue(ok)
        row = await db.ssh_tunnel_get(self.machine_id)
        self.assertEqual(row["state"], "connected")

    async def test_a_field_off_the_allowlist_is_rejected_not_written(self):
        # owner_id is a real column on this table -- exactly the kind of
        # column an update helper must never let an unexpected key reach,
        # since a crafted key becomes a raw SQL identifier, not a bound
        # value. Chosen over a fabricated made-up name so the test proves
        # the guard blocks a real, dangerous target, not just a typo.
        before = await db.ssh_tunnel_get(self.machine_id)
        ok = await db.ssh_tunnel_update(self.machine_id, owner_id="someone-else")
        self.assertFalse(
            ok, "ssh_tunnel_update must return False for a disallowed field, "
            "not silently execute the write",
        )
        after = await db.ssh_tunnel_get(self.machine_id)
        self.assertEqual(
            before["owner_id"], after["owner_id"],
            "the disallowed field reached the UPDATE anyway",
        )

    async def test_a_mixed_call_is_rejected_wholesale(self):
        # One bad key must not let the good ones through partially -- an
        # all-or-nothing allowlist check, like its siblings.
        before = await db.ssh_tunnel_get(self.machine_id)
        ok = await db.ssh_tunnel_update(
            self.machine_id, state="connected", owner_id="someone-else"
        )
        self.assertFalse(ok)
        after = await db.ssh_tunnel_get(self.machine_id)
        self.assertEqual(before["state"], after["state"])


if __name__ == "__main__":
    unittest.main()
