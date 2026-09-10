"""QA: last_qa_synced_sha exists on ssh_transports, and an old database
gains it without losing last_synced_sha.

Two independent sync pointers on the same table -- production
(last_synced_sha) and QA (last_qa_synced_sha) -- must never collide or
overwrite each other's column during migration. See
docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §2.
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


def _fresh_db() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="wc-qa-column-")) / "wc.db"
    os.environ["WC_DB_PATH"] = str(tmp)
    os.environ["WC_SESSION_DB_PATH"] = str(tmp.parent / "sessions.db")
    os.environ.setdefault("WC_PROXY_ENABLED", "0")
    import config

    importlib.reload(config)
    import db

    db.config = config
    return tmp


class QaSyncColumnTests(unittest.IsolatedAsyncioTestCase):
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
            return {r[1] for r in con.execute("PRAGMA table_info(ssh_transports)")}
        finally:
            con.close()

    async def test_the_column_exists_after_init(self):
        self.assertIn("last_qa_synced_sha", self._columns())

    async def test_init_is_idempotent(self):
        await self.db.close()
        await self.db.init()
        self.assertIn("last_qa_synced_sha", self._columns())

    async def test_a_database_without_the_column_gains_it_empty(self):
        """The deploy-safety case: an existing transport must stay usable,
        and its production sync pointer must survive the migration untouched."""
        await self.db.ssh_transport_create(
            "t-old", "Old", "admin", "old.example.net", "kali", "~/.ssh/id_ed25519")
        await self.db.ssh_transport_set_last_synced_sha("t-old", "prodsha123")
        await self.db.close()

        con = sqlite3.connect(str(self.path))
        try:
            con.execute("ALTER TABLE ssh_transports DROP COLUMN last_qa_synced_sha")
            con.commit()
        finally:
            con.close()

        await self.db.init()
        self.assertIn("last_qa_synced_sha", self._columns())
        row = await self.db.ssh_transport_get("t-old", "admin")
        self.assertEqual(row["last_qa_synced_sha"], "")
        self.assertEqual(
            row["last_synced_sha"], "prodsha123",
            "the migration must not disturb the unrelated production pointer")

    async def test_setter_advances_only_the_qa_pointer(self):
        await self.db.ssh_transport_create(
            "t1", "One", "admin", "one.example.net", "kali", "~/.ssh/id_ed25519")
        await self.db.ssh_transport_set_last_synced_sha("t1", "prodsha")
        await self.db.ssh_transport_set_last_qa_synced_sha("t1", "qasha")

        row = await self.db.ssh_transport_get("t1", "admin")
        self.assertEqual(row["last_synced_sha"], "prodsha")
        self.assertEqual(row["last_qa_synced_sha"], "qasha")


if __name__ == "__main__":
    unittest.main()
