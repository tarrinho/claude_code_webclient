"""QA: enabling and disabling a backend at the database layer.

The activate guard is the load-bearing one. Without it, "make default" is a way
to smuggle a shelved backend back into service, and it would do so silently:
the flag would still read disabled while every unpinned turn routed there.

Design: docs/superpowers/specs/2026-09-08-default-and-enabled-backends-design.md
"""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path

from tests.testing_model import TESTING_MODEL


class SetEnabledTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wc-setenabled-")) / "wc.db"
        os.environ["WC_DB_PATH"] = str(tmp)
        os.environ["WC_SESSION_DB_PATH"] = str(tmp.parent / "sessions.db")
        os.environ.setdefault("WC_PROXY_ENABLED", "0")
        import config

        importlib.reload(config)
        import db

        db.config = config
        self.db = db
        await db.init()
        for mid, name in (("m-1", "One"), ("m-2", "Two")):
            await db.ai_machine_create(
                mid, name, "api.anthropic.com", 443, None, TESTING_MODEL,
                "https://api.anthropic.com", None, "admin",
                provider="claude_code",
            )

    async def asyncTearDown(self):
        await self.db.close()

    async def test_disabling_sets_the_flag(self):
        self.assertTrue(await self.db.ai_machine_set_enabled("m-1", "admin", False))
        self.assertEqual((await self.db.ai_machine_get("m-1", "admin"))["enabled"], 0)

    async def test_enabling_sets_it_back(self):
        await self.db.ai_machine_set_enabled("m-1", "admin", False)
        self.assertTrue(await self.db.ai_machine_set_enabled("m-1", "admin", True))
        self.assertEqual((await self.db.ai_machine_get("m-1", "admin"))["enabled"], 1)

    async def test_another_owner_can_disable_it_too(self):
        """Machines became a shared pool on 2026-09-11 -- any account can
        disable any machine. This used to assert the opposite
        (test_it_is_scoped_by_owner); see ai_machine_set_enabled's
        docstring in routes/db_machines.py."""
        self.assertTrue(
            await self.db.ai_machine_set_enabled("m-1", "someone-else", False))
        self.assertEqual((await self.db.ai_machine_get("m-1", "admin"))["enabled"], 0)

    async def test_activate_refuses_a_disabled_machine(self):
        """The guard: 'make default' must not re-enable by the back door."""
        await self.db.ai_machine_set_enabled("m-1", "admin", False)
        self.assertFalse(await self.db.ai_machine_activate("m-1", "admin"))
        self.assertEqual((await self.db.ai_machine_get("m-1", "admin"))["active"], 0)

    async def test_a_refused_activate_leaves_the_previous_default_alone(self):
        """activate() deactivates everything before activating one. A refusal
        that had already run the first UPDATE would leave the owner with no
        default at all -- worse than the state it declined to leave."""
        await self.db.ai_machine_activate("m-2", "admin")
        await self.db.ai_machine_set_enabled("m-1", "admin", False)
        self.assertFalse(await self.db.ai_machine_activate("m-1", "admin"))
        self.assertEqual((await self.db.ai_machine_get("m-2", "admin"))["active"], 1)

    async def test_activate_still_works_for_an_enabled_machine(self):
        self.assertTrue(await self.db.ai_machine_activate("m-1", "admin"))
        self.assertEqual((await self.db.ai_machine_get("m-1", "admin"))["active"], 1)

    async def test_activate_refuses_an_unknown_machine(self):
        """The guard reads the row first, so a missing one must not crash."""
        self.assertFalse(await self.db.ai_machine_activate("nope", "admin"))


if __name__ == "__main__":
    unittest.main()
