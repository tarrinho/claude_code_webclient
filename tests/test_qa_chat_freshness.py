"""QA: a conversation's listed time reflects work done in the terminal.

`chats.updated_at` only advanced when the web UI touched a conversation -- a
turn sent here, or the sync that runs while it is the *open* chat. Work done in
the terminal moved the transcript and nothing else, so the sidebar aged a
conversation that was in active use and kept ageing it for as long as the
browser was looking somewhere else. Measured on real data before the fix, one
conversation was five minutes stale and the drift had no upper bound.

The correction is applied on read, not written back: a write per poll would
churn the database, and `position` is the user's own ordering and must not move
because a file changed on disk.
"""
from __future__ import annotations

import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db
import transcripts
from routes import chats as chat_routes


def _at(path: Path, when: datetime.datetime) -> None:
    os.utime(path, (when.timestamp(), when.timestamp()))


class LiveUpdatedAtTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        Path(config.PROJECTS_ROOT).mkdir(parents=True, exist_ok=True)
        await db.chat_create("linked", "Linked", None, config.PROJECTS_ROOT, "admin")
        await db.chat_set_session("linked", "sess-1")
        await db.chat_create("plain", "Plain", None, config.PROJECTS_ROOT, "admin")
        self.tx = self.root / "sess-1.jsonl"
        self.tx.write_text("{}\n")

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    def _resolver(self):
        return patch.object(
            transcripts, "transcript_path",
            lambda s: self.tx if s == "sess-1" else None,
        )

    async def _live(self):
        with self._resolver():
            return await chat_routes._live_updated_at(await db.chat_list("admin"))

    async def test_terminal_activity_freshens_a_linked_chat(self):
        _at(self.tx, datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2))
        live = await self._live()
        self.assertIn("linked", live)

    async def test_the_correction_is_the_later_of_the_two(self):
        later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2)
        _at(self.tx, later)
        live = await self._live()
        stored = {c["id"]: c["updated_at"] for c in await db.chat_list("admin")}
        self.assertGreater(live["linked"], stored["linked"])

    async def test_an_older_transcript_never_moves_the_time_backwards(self):
        """Web activity legitimately outruns the transcript; it must win."""
        _at(self.tx, datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1))
        self.assertNotIn("linked", await self._live())

    async def test_a_chat_with_no_session_is_untouched(self):
        _at(self.tx, datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=2))
        self.assertNotIn("plain", await self._live())

    async def test_a_missing_transcript_is_not_an_error(self):
        with patch.object(transcripts, "transcript_path", lambda _s: None):
            self.assertEqual(await chat_routes._live_updated_at(await db.chat_list("admin")), {})

    async def test_an_unreadable_transcript_is_skipped_not_raised(self):
        """One bad path must not blank the whole sidebar."""
        missing = self.root / "gone.jsonl"
        with patch.object(transcripts, "transcript_path", lambda _s: missing):
            self.assertEqual(await chat_routes._live_updated_at(await db.chat_list("admin")), {})

    async def test_no_linked_chats_does_no_filesystem_work(self):
        """The common case for an unlinked workspace stays free."""
        calls = []
        with patch.object(chat_routes, "_transcript_mtimes_sync", lambda ids: calls.append(ids) or {}):
            await chat_routes._live_updated_at([{"id": "plain", "session_id": None}])
        self.assertEqual(calls, [])

    async def test_lookup_runs_off_the_event_loop(self):
        """Locating a transcript globs the projects directory."""
        # routes/chats.py, not app.py: the helper moved there in the 0.10.0
        # split. Assert the marker before slicing on it -- a scan of the wrong
        # file raises IndexError, which reads as a broken test rather than the
        # missing to_thread this is here to catch.
        source = Path(chat_routes.__file__).read_text()
        marker = "async def _live_updated_at"
        self.assertIn(marker, source, "the helper is no longer in routes/chats.py")
        body = source.split(marker)[1].split("\nasync def ")[0]
        self.assertIn("asyncio.to_thread", body)


class TimestampFormatTests(unittest.TestCase):
    """The comparison is lexicographic, so the format has to be stable."""

    def test_mtimes_are_fixed_width_utc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text("{}")
            with patch.object(transcripts, "transcript_path", lambda _s: path):
                out = chat_routes._transcript_mtimes_sync(["s"])
        stamp = out["s"]
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        # Same shape db._now() produces, or the comparison silently misorders.
        self.assertEqual(len(stamp), len(db._now()))


if __name__ == "__main__":
    unittest.main()
