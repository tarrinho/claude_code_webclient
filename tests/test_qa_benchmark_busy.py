"""QA: what counts as a busy box.

Asserted in BOTH directions. A test that only proves a busy box stops the
sweep would pass for a detector that always reports busy, which would mean the
benchmark never runs and nothing would notice until the table went stale.

Voice is asserted NOT to count (spec 8.3): routes/voice.py is CLAUDE.md
section 0's documented exception and speaks to an OpenAI-compatible endpoint
directly, so a voice conversation does not contend with the CLI transport the
harness measures over.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import benchmark_sweep
import config
import db


class BusyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value); p.start(); self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def _message(self, created_at: str, chat_id="c1"):
        await db.db_conn.execute(
            "INSERT INTO chats (id, title, work_dir, owner_id, created_at) "
            "VALUES (?, 'c', '/tmp', 'admin', ?)", (chat_id, created_at))
        await db.db_conn.execute(
            "INSERT INTO messages (chat_id, role, content, created_at) "
            "VALUES (?, 'user', 'hi', ?)", (chat_id, created_at))
        await db.db_conn.commit()

    async def test_an_idle_box_is_not_busy(self):
        """The direction that catches an always-busy detector."""
        with patch("runner.slots_busy", return_value=False):
            busy, reason = await benchmark_sweep.box_is_busy()
        self.assertFalse(busy, reason)

    async def test_a_turn_in_flight_is_busy(self):
        with patch("runner.slots_busy", return_value=True):
            busy, reason = await benchmark_sweep.box_is_busy()
        self.assertTrue(busy)
        self.assertIn("turn", reason)

    async def test_a_recent_message_is_busy(self):
        await self._message(db._now())
        with patch("runner.slots_busy", return_value=False):
            busy, reason = await benchmark_sweep.box_is_busy()
        self.assertTrue(busy)
        self.assertIn("message", reason)

    async def test_an_old_message_is_not_busy(self):
        await self._message("2020-01-01T00:00:00Z")
        with patch("runner.slots_busy", return_value=False):
            busy, reason = await benchmark_sweep.box_is_busy()
        self.assertFalse(busy, reason)

    async def test_an_active_voice_session_alone_is_not_busy(self):
        """Voice does not contend with the CLI transport (spec 8.3)."""
        await db.db_conn.execute(
            "INSERT INTO chats (id, title, work_dir, owner_id, created_at, "
            " voice_mode) VALUES ('v1', 'v', '/tmp', 'admin', ?, 1)",
            ("2020-01-01T00:00:00Z",))
        await db.db_conn.commit()
        with patch("runner.slots_busy", return_value=False):
            busy, reason = await benchmark_sweep.box_is_busy()
        self.assertFalse(busy, reason)

    async def test_a_message_earlier_today_but_outside_margin_is_not_busy(self):
        """Regression test for the SQL datetime('now') string-comparison bug.

        SQLite's datetime('now') emits 'YYYY-MM-DD HH:MM:SS' (space, no
        trailing Z) while db._now() emits 'YYYY-MM-DDTHH:MM:SSZ' (T, and a
        trailing Z). Compared as strings the two forms first differ at the
        date/time separator, where 'T' (0x54) sorts above ' ' (0x20) -- so
        `created_at >= datetime('now', '-10 minutes')` is satisfied by any
        timestamp written today, no matter how many hours ago, and the box
        would read as busy all day. A message from 6 hours ago -- well
        outside the 10-minute idle margin, but still "today" -- is exactly
        the case that string comparison gets wrong and a Python-computed
        cutoff gets right.
        """
        six_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=6)
                         ).strftime("%Y-%m-%dT%H:%M:%SZ")
        await self._message(six_hours_ago)
        with patch("runner.slots_busy", return_value=False):
            busy, reason = await benchmark_sweep.box_is_busy()
        self.assertFalse(busy, reason)


if __name__ == "__main__":
    unittest.main()
