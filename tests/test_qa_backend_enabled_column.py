"""QA: the enabled column, and that an old database survives gaining it.

`ai_machines.active` means "is the default" on this table. `enabled` is the
new, separate question: may this backend be used at all. The migration is
additive with DEFAULT 1 so every backend that already exists stays usable.

The column-less case is the one that decides whether a mid-work host survives
the deploy, which is why it is asserted rather than assumed.

Design: docs/superpowers/specs/2026-09-08-default-and-enabled-backends-design.md
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.testing_model import TESTING_MODEL


def _fresh_db() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="wc-enabled-")) / "wc.db"
    os.environ["WC_DB_PATH"] = str(tmp)
    os.environ["WC_SESSION_DB_PATH"] = str(tmp.parent / "sessions.db")
    os.environ.setdefault("WC_PROXY_ENABLED", "0")
    import config

    importlib.reload(config)
    import db

    db.config = config
    return tmp


class EnabledColumnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = _fresh_db()
        import db

        self.db = db
        await db.init()

    async def asyncTearDown(self):
        await self.db.close()

    def _columns(self) -> set[str]:
        con = sqlite3.connect(str(self.path))
        try:
            return {r[1] for r in con.execute("PRAGMA table_info(ai_machines)")}
        finally:
            con.close()

    async def test_the_column_exists_after_init(self):
        self.assertIn("enabled", self._columns())

    async def test_init_is_idempotent(self):
        await self.db.close()
        await self.db.init()
        self.assertIn("enabled", self._columns())

    async def test_a_database_without_the_column_gains_it_enabled(self):
        """The deploy-safety case: an existing backend must stay usable."""
        await self.db.close()
        con = sqlite3.connect(str(self.path))
        try:
            con.execute("ALTER TABLE ai_machines DROP COLUMN enabled")
            con.execute(
                "INSERT INTO ai_machines "
                "(id, name, provider, host, port, model, owner_id, active, "
                " created_at, updated_at) "
                "VALUES ('m-old','Old','claude_code','api.anthropic.com',443,"
                f"'{TESTING_MODEL}','admin',1,'2026-01-01T00:00:00Z',"
                "'2026-01-01T00:00:00Z')"
            )
            con.commit()
        finally:
            con.close()
        await self.db.init()
        self.assertIn("enabled", self._columns())
        machine = await self.db.ai_machine_get("m-old", "admin")
        self.assertEqual(machine["enabled"], 1,
                         "an existing backend must not be shelved by the migration")

    async def test_the_readers_all_return_enabled(self):
        await self.db.ai_machine_create(
            "m-1", "One", "api.anthropic.com", 443, None, TESTING_MODEL,
            "https://api.anthropic.com", None, "admin", provider="claude_code",
        )
        await self.db.ai_machine_activate("m-1", "admin")
        self.assertIn("enabled", await self.db.ai_machine_get("m-1", "admin"))
        self.assertIn("enabled", (await self.db.ai_machines_list("admin"))[0])
        self.assertIn("enabled", await self.db.ai_machine_active("admin"))


if __name__ == "__main__":
    unittest.main()
