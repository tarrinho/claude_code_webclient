"""QA: a transcript turn is recorded once, however many times it is imported.

Measured against this deployment on 2026-09-22: 23,557 of 217,074 usage rows
were exact duplicates -- same session, same millisecond timestamp, same model,
and all four token counts equal. 10.9% of the table. Every turn count and every
token total on the Usage page was inflated by it, and the newest duplicate was
two days old, so it was still happening.

Three things were ruled out by measurement rather than by reading:

* Not the documented "one row per turn per model" split that
  ``usage_agent_totals`` describes. Of the extra rows, zero carried a second
  model.
* Not a batching fault. Duplicate pairs were never adjacent in rowid -- 0 of
  23,448 groups -- and 97.7% sat more than 10,000 rows apart, so the two copies
  were written by separate import passes, not by one pass inserting twice.
* Not spread evenly. 12 sessions out of 2,060 held all of it, two of them 97.5%.

The suspected reader defect is that ``transcripts._usage_since_sync`` resumes on
a byte offset guarded only by ``offset >= size``, where the sibling read path
carries ``_RESUME_ANCHOR_BYTES`` precisely because a size comparison cannot tell
"appended to" from "rewritten to a similar length". These cases deliberately do
not depend on that being the right diagnosis: the table refuses a repeat no
matter which path offers one.

**The key is the transcript line, not the row's contents.** An earlier version
of this migration keyed on (session, timestamp, model, four token counts) and
was wrong: ``usage_import`` can legitimately write two rows for one session at
one timestamp with identical counts, told apart only by which routed request
they answer. ``tests/test_qa_usage_origin.py`` builds exactly that pair, and the
content key silently dropped the second -- losing real spend to prevent a
double count. Production happens to contain no such pair today, which is
precisely why relying on that would have been a trap.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import config
import db

_COLS = (
    "chat_id, session_id, owner_id, model, provider, input_tokens, "
    "output_tokens, cache_read_tokens, cache_creation_tokens, is_error, "
    "created_at, origin, source_offset"
)
# One turn: session sess-a, the line ending at byte 4096 of its transcript.
_TURN = (
    "'', 'sess-a', 'admin', 'claude-opus-5', 'cli', 100, 20, 7, 3, 0, "
    "'2026-09-20T11:04:43.666Z', 'terminal', 4096"
)


class UsageDuplicateImportQA(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.db_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.tmp.cleanup()

    async def _count(self) -> int:
        cur = await db.db_conn.execute("SELECT COUNT(*) c FROM usage_events")
        return (await cur.fetchone())["c"]

    async def _insert_raw(self, values: str) -> None:
        """Insert bypassing OR IGNORE, to build a pre-migration state."""
        await db.db_conn.execute(
            f"INSERT INTO usage_events ({_COLS}) VALUES ({values})")
        await db.db_conn.commit()

    async def test_the_same_transcript_line_cannot_be_recorded_twice(self):
        """The property the whole change exists for."""
        await self._insert_raw(_TURN)
        with self.assertRaises(sqlite3.IntegrityError):
            await self._insert_raw(_TURN)
        self.assertEqual(await self._count(), 1)

    async def test_or_ignore_skips_instead_of_aborting_the_batch(self):
        """A repeated turn must not take the turns after it down with it.

        Without OR IGNORE the index raises mid-batch and the rollback discards
        every row in that transaction -- trading an over-count for silent data
        loss, which is strictly worse.
        """
        await self._insert_raw(_TURN)
        await db.db_conn.execute(
            f"INSERT OR IGNORE INTO usage_events ({_COLS}) VALUES ({_TURN})")
        later = _TURN.replace(", 4096", ", 8192")
        await db.db_conn.execute(
            f"INSERT OR IGNORE INTO usage_events ({_COLS}) VALUES ({later})")
        await db.db_conn.commit()
        self.assertEqual(await self._count(), 2)

    async def test_two_turns_at_one_timestamp_are_both_kept(self):
        """The case that forced the key away from row contents.

        Two routed requests answered in the same session at the same recorded
        timestamp, with identical token counts, differing only in the line they
        came from. Both are real spend. A content-based key drops the second;
        `tests/test_qa_usage_origin.py::RoutedAttributionQA` is where this was
        caught for real, against three of its cases at once.
        """
        await self._insert_raw(_TURN)
        second = _TURN.replace("''", "'c2'", 1).replace(", 4096", ", 5120")
        await self._insert_raw(second)
        self.assertEqual(await self._count(), 2)

    async def test_a_different_session_at_the_same_offset_is_not_a_duplicate(self):
        """Offsets are per-transcript, so the session is half the key.

        Byte 4096 of one session's transcript has nothing to do with byte 4096
        of another's. Keying on the offset alone would collapse unrelated turns
        across every session on the machine.
        """
        await self._insert_raw(_TURN)
        await self._insert_raw(_TURN.replace("'sess-a'", "'sess-b'"))
        self.assertEqual(await self._count(), 2)

    async def test_rows_without_an_offset_are_not_constrained(self):
        """The index is partial, and this is why.

        Web turns are written by `usage_record` at request time and never come
        from a transcript, so they carry no offset. Every row predating the
        column is in the same position: the offsets those came from were not
        recorded and cannot be recovered. NULL must therefore opt out of the
        constraint rather than collide -- SQLite treats NULLs as distinct in a
        unique index, and the partial clause makes that explicit.
        """
        web = ("'c1', '', 'admin', 'claude-opus-5', 'anthropic', 1, 1, 0, 0, 0, "
               "'2026-09-20T11:04:43.666Z', 'web', NULL")
        await self._insert_raw(web)
        await self._insert_raw(web)
        self.assertEqual(await self._count(), 2)

    async def test_history_is_collapsed_on_content_keeping_the_first(self):
        """The backfill half, which cannot use the offset key.

        Rows written before `source_offset` existed carry NULL, so they are
        invisible to the index and have to be matched on what they contain.
        That is safe here and was checked rather than assumed: including
        chat_id catches 20,821 of the 23,557, and every one of the 2,736 groups
        in the difference holds an empty chat_id beside a real one while
        spanning two origins -- a re-import after the routed markers changed.
        Groups holding two *different* real chat_ids, which a content key would
        wrongly collapse, number exactly zero in production.

        MIN(id) wins HERE only because no member of this group carries a
        chat_id, which is the fallback arm of the keep rule. The rule itself
        prefers an attributed row -- see
        test_the_survivor_keeps_the_attribution. This docstring used to assert
        that MIN(id) "keeps ids stable for anything that recorded one", which
        is the exact sentence 14cbaab4 removed from the source as unfounded:
        nothing declares a foreign key to usage_events. Two files disagreeing
        about a rule is how the next reader learns the wrong one.
        """
        old = _TURN.replace(", 4096", ", NULL")
        await self._insert_raw(old)
        await self._insert_raw(old)
        await self._insert_raw(old)
        self.assertEqual(await self._count(), 3)
        cur = await db.db_conn.execute("SELECT MIN(id) m FROM usage_events")
        first = (await cur.fetchone())["m"]

        from routes.db_usage import _ensure_usage_uniqueness
        await _ensure_usage_uniqueness()

        self.assertEqual(await self._count(), 1)
        cur = await db.db_conn.execute("SELECT id FROM usage_events")
        self.assertEqual((await cur.fetchone())["id"], first)

    async def test_the_survivor_keeps_the_attribution(self):
        """Row counts cannot see this, which is the point.

        The duplicates are re-imports written after the routed markers changed,
        so the copy carrying the corrected chat_id is the LATER one. Keeping
        MIN(id) therefore kept the unattributed row and deleted the attributed
        sibling: measured against production, 1,402 groups and 1,403 real
        chat_ids destroyed, unrecoverably.

        A test asserting only "23,557 removed, redundancy zero" passes against
        both the broken and the fixed version -- the count, the totals and the
        redundancy are all correct either way. Only the survivor's contents
        tell the two apart.
        """
        unattributed = _TURN.replace(", 4096", ", NULL")
        attributed = (
            "'c-real', 'sess-a', 'admin', 'claude-opus-5', 'cli', 100, 20, 7, 3, 0, "
            "'2026-09-20T11:04:43.666Z', 'web-routed', NULL")
        await self._insert_raw(unattributed)   # lower id, no chat_id
        await self._insert_raw(attributed)     # higher id, carries the chat_id

        from routes.db_usage import _ensure_usage_uniqueness
        await _ensure_usage_uniqueness()

        self.assertEqual(await self._count(), 1)
        cur = await db.db_conn.execute(
            "SELECT chat_id, origin FROM usage_events")
        row = await cur.fetchone()
        self.assertEqual(row["chat_id"], "c-real",
                         "the surviving row lost its conversation")
        # The whole row survives, not a patched hybrid: origin has to agree
        # with chat_id, or the origin breakdown files routed spend as
        # untargeted terminal usage.
        self.assertEqual(row["origin"], "web-routed",
                         "origin and chat_id disagree on the surviving row")

    async def test_a_group_with_no_attribution_still_keeps_the_oldest(self):
        """The fallback. Most duplicate groups carry no chat_id at all."""
        unattributed = _TURN.replace(", 4096", ", NULL")
        await self._insert_raw(unattributed)
        cur = await db.db_conn.execute("SELECT MIN(id) m FROM usage_events")
        first = (await cur.fetchone())["m"]
        await self._insert_raw(unattributed)

        from routes.db_usage import _ensure_usage_uniqueness
        await _ensure_usage_uniqueness()

        self.assertEqual(await self._count(), 1)
        cur = await db.db_conn.execute("SELECT id FROM usage_events")
        self.assertEqual((await cur.fetchone())["id"], first)

    async def test_two_real_turns_with_offsets_survive_the_migration(self):
        """The content key must not be applied to rows that have an identity.

        `test_two_turns_at_one_timestamp_are_both_kept` asserts the INDEX
        tolerates this pair. It never calls the migration, so it said nothing
        about the DELETE -- and the DELETE was matching on content across every
        row in the table, offsets included, which collapsed exactly the pair
        the offset key exists to keep apart. Found in review of 14cbaab4.
        """
        first = _TURN.replace(", 4096", ", 4096")
        second = (
            "'c2', 'sess-a', 'admin', 'claude-opus-5', 'cli', 100, 20, 7, 3, 0, "
            "'2026-09-20T11:04:43.666Z', 'web-routed', 5120")
        await self._insert_raw(first)
        await self._insert_raw(second)

        from routes.db_usage import _ensure_usage_uniqueness
        await _ensure_usage_uniqueness()

        self.assertEqual(await self._count(), 2,
                         "a distinct routed turn was deleted by content match")

    async def test_colliding_offsets_do_not_stop_the_server_booting(self):
        """The regression the attributed keep-rule introduced.

        Where the attributed copy carries an offset another surviving turn also
        holds -- what a rewritten transcript produces -- preferring it leaves
        two survivors sharing (session_id, source_offset), and the CREATE
        UNIQUE INDEX inside this same migration then raises inside db.init().
        The failure is not a bad number; it is a server that does not start.

        MIN(id) happened to avoid this by keeping the offsetless copy, so the
        dry-run against production was clean and said nothing about it.
        """
        await db.db_conn.execute("DROP INDEX IF EXISTS idx_usage_import_once")
        # A duplicate pair whose attributed copy sits at offset 9 ...
        await self._insert_raw(_TURN.replace(", 4096", ", NULL"))
        await self._insert_raw(
            "'c-real', 'sess-a', 'admin', 'claude-opus-5', 'cli', 100, 20, 7, 3, 0, "
            "'2026-09-20T11:04:43.666Z', 'web-routed', 9")
        # ... and a different turn already holding offset 9.
        await self._insert_raw(
            "'', 'sess-a', 'admin', 'claude-opus-5', 'cli', 200, 20, 7, 3, 0, "
            "'2026-09-20T11:05:00.000Z', 'terminal', 9")

        from routes.db_usage import _ensure_usage_uniqueness
        # The assertion is that this does not raise.
        await _ensure_usage_uniqueness()

        cur = await db.db_conn.execute(
            "SELECT COUNT(*) c FROM usage_events "
            "WHERE source_offset IS NOT NULL "
            "GROUP BY session_id, source_offset HAVING c > 1")
        self.assertIsNone(await cur.fetchone(),
                          "two rows still share (session_id, source_offset)")

    async def test_the_migration_is_idempotent(self):
        from routes.db_usage import _ensure_usage_uniqueness
        await self._insert_raw(_TURN.replace(", 4096", ", NULL"))
        await _ensure_usage_uniqueness()
        await _ensure_usage_uniqueness()
        self.assertEqual(await self._count(), 1)

    async def test_the_importer_writes_the_offset(self):
        """Without this the index guards nothing.

        The constraint is only as good as the column feeding it, and a row
        inserted with a NULL offset opts out silently -- exactly the shape of
        failure this whole file exists to catch, one level down.
        """
        rows = [{
            "model": "claude-opus-5", "input_tokens": 10, "output_tokens": 2,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "context_unsplit": False, "offset": 1234,
            "timestamp": "2026-09-21T00:00:00.000Z", "after_prompt": "",
        }]
        await db.usage_import("admin", "sess-z", rows, 1234)
        cur = await db.db_conn.execute(
            "SELECT source_offset FROM usage_events WHERE session_id = 'sess-z'")
        self.assertEqual((await cur.fetchone())["source_offset"], 1234)

    async def test_a_re_import_of_the_same_lines_writes_nothing(self):
        """End to end, through the real importer rather than raw SQL."""
        rows = [{
            "model": "claude-opus-5", "input_tokens": 10, "output_tokens": 2,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "context_unsplit": False, "offset": off,
            "timestamp": "2026-09-21T00:00:00.000Z", "after_prompt": "",
        } for off in (10, 20, 30)]
        first = await db.usage_import("admin", "sess-y", rows, 30)
        again = await db.usage_import("admin", "sess-y", rows, 30)
        self.assertEqual(first, 3)
        self.assertEqual(again, 0, "a repeated import must report what it wrote")
        cur = await db.db_conn.execute(
            "SELECT COUNT(*) c FROM usage_events WHERE session_id = 'sess-y'")
        self.assertEqual((await cur.fetchone())["c"], 3)


if __name__ == "__main__":
    unittest.main()
