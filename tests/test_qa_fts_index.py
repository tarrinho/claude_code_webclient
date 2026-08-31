"""QA: FTS5 index maintenance.

tests/test_qa_chats.py covers search behaviour end to end; these pin the
maintenance primitives underneath it, which end-to-end search cannot
distinguish. Three defects motivate them:

* Indexing was O(n^2): every append re-selected and re-inserted the whole
  conversation, so a long chat degraded without any search result changing.
* chat_delete removed the message rows before purging the index, so the purge
  subquery matched nothing and entries were orphaned permanently.
* The indexed text carries the chat title, so a rename left every row stale
  and title search silently kept matching the old name.

Grouped by level: UnitQA (primitives against a real SQLite file),
IntegrationQA (the write paths that call them), AcceptanceUATQA (search as a
user experiences it).
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from unittest.mock import patch

import config
import db


class FtsMixin:
    async def init_temp_db(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(
            config, "PROJECTS_ROOT", f"{self.tmp.name}/projects"
        )
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def close_temp_db(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def fts_rowids(self) -> set[int]:
        cur = await db.db_conn.execute("SELECT rowid FROM messages_fts")
        return {row["rowid"] for row in await cur.fetchall()}

    async def fts_count(self) -> int:
        cur = await db.db_conn.execute("SELECT COUNT(*) AS c FROM messages_fts")
        return (await cur.fetchone())["c"]

    async def fts_match(self, term: str) -> set[int]:
        cur = await db.db_conn.execute(
            "SELECT rowid FROM messages_fts WHERE content MATCH ?", (term,)
        )
        return {row["rowid"] for row in await cur.fetchall()}


# ── Unit: the primitives ───────────────────────────────────────────────────────


class UnitQA(FtsMixin, unittest.IsolatedAsyncioTestCase):
    """Index primitives, exercised against a real SQLite file."""

    async def asyncSetUp(self):
        await self.init_temp_db()
        await db.chat_create("c1", "Ledger", None, f"{self.tmp.name}/projects", "admin")

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def _insert_message(self, content: str, chat_id: str = "c1") -> int:
        """Insert a message row *without* touching the index."""
        cur = await db.db_conn.execute(
            "INSERT INTO messages (chat_id, role, content, created_at) "
            "VALUES (?, 'user', ?, ?)",
            (chat_id, content, db._now()),
        )
        await db.db_conn.commit()
        return cur.lastrowid

    async def test_index_ids_indexes_only_what_it_is_given(self):
        first = await self._insert_message("alpha unicorn")
        second = await self._insert_message("beta narwhal")
        await db._fts_index_ids([first])
        self.assertEqual(await self.fts_rowids(), {first})
        self.assertEqual(await self.fts_match("unicorn"), {first})
        self.assertEqual(await self.fts_match("narwhal"), set())
        await db._fts_index_ids([second])
        self.assertEqual(await self.fts_rowids(), {first, second})

    async def test_index_ids_is_idempotent(self):
        mid = await self._insert_message("alpha unicorn")
        await db._fts_index_ids([mid])
        await db._fts_index_ids([mid])
        # Re-indexing must replace, not duplicate, or MATCH returns the row twice.
        self.assertEqual(await self.fts_count(), 1)

    async def test_index_ids_tolerates_none_and_empty(self):
        for arg in ([], [None], [None, None]):
            await db._fts_index_ids(arg)
        self.assertEqual(await self.fts_count(), 0)

    async def test_index_ids_skips_messages_with_empty_content(self):
        blank = await self._insert_message("")
        await db._fts_index_ids([blank])
        self.assertEqual(await self.fts_count(), 0)

    async def test_index_ids_prefixes_the_chat_title(self):
        # Title is baked in so a title search also matches through the index.
        mid = await self._insert_message("body text only")
        await db._fts_index_ids([mid])
        self.assertEqual(await self.fts_match("Ledger"), {mid})

    async def test_forget_ids_removes_only_what_it_is_given(self):
        first = await self._insert_message("alpha")
        second = await self._insert_message("beta")
        await db._fts_index_ids([first, second])
        await db._fts_forget_ids([first])
        self.assertEqual(await self.fts_rowids(), {second})

    async def test_forget_ids_tolerates_none_and_empty(self):
        mid = await self._insert_message("alpha")
        await db._fts_index_ids([mid])
        for arg in ([], [None]):
            await db._fts_forget_ids(arg)
        self.assertEqual(await self.fts_rowids(), {mid})

    async def test_forget_ids_works_after_the_rows_are_deleted(self):
        # The ordering that broke chat_delete: ids must still purge once the
        # message rows they refer to are gone.
        mid = await self._insert_message("alpha")
        await db._fts_index_ids([mid])
        await db.db_conn.execute("DELETE FROM messages WHERE id = ?", (mid,))
        await db.db_conn.commit()
        await db._fts_forget_ids([mid])
        self.assertEqual(await self.fts_count(), 0)

    async def test_rebuild_reindexes_one_chat(self):
        await db.chat_create("c2", "Other", None, f"{self.tmp.name}/projects", "admin")
        mine = await self._insert_message("alpha", "c1")
        theirs = await self._insert_message("beta", "c2")
        await db._fts_rebuild("c1")
        self.assertEqual(await self.fts_rowids(), {mine})
        await db._fts_rebuild("c2")
        self.assertEqual(await self.fts_rowids(), {mine, theirs})

    async def test_rebuild_all_covers_every_chat(self):
        await db.chat_create("c2", "Other", None, f"{self.tmp.name}/projects", "admin")
        mine = await self._insert_message("alpha", "c1")
        theirs = await self._insert_message("beta", "c2")
        await db._fts_rebuild()
        self.assertEqual(await self.fts_rowids(), {mine, theirs})

    async def test_rebuild_does_not_duplicate_existing_entries(self):
        mid = await self._insert_message("alpha")
        await db._fts_index_ids([mid])
        await db._fts_rebuild("c1")
        self.assertEqual(await self.fts_count(), 1)

    async def test_the_shared_connection_sets_a_busy_timeout(self):
        """Index maintenance runs on the shared connection now, so this is the
        timeout that matters.

        It was the only one of the project's writer connections that never set
        one, inheriting sqlite3's undocumented 5-second default while the other
        two chose 5000ms explicitly. A wait is a slow request; a timeout is a
        lost write.
        """
        cursor = await db.db_conn.execute("PRAGMA busy_timeout")
        self.assertEqual((await cursor.fetchone())[0], db._BUSY_TIMEOUT_MS)

    async def test_maintenance_never_raises_without_the_fts_table(self):
        # FTS5 is unavailable on some SQLite builds; maintenance must degrade
        # quietly rather than break the write path that calls it.
        await db.db_conn.execute("DROP TABLE messages_fts")
        await db.db_conn.commit()
        mid = await self._insert_message("alpha")
        await db._fts_index_ids([mid])
        await db._fts_forget_ids([mid])
        await db._fts_rebuild("c1")
        await db._fts_rebuild()

    async def test_maintenance_does_not_block_the_event_loop(self):
        # The sync sqlite3 work runs in a thread; a ticker on the loop must
        # keep advancing while a rebuild is in flight.
        for i in range(200):
            await self._insert_message(f"row {i}")
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0)
                ticks += 1

        task = asyncio.create_task(ticker())
        await db._fts_rebuild()
        task.cancel()
        self.assertGreater(ticks, 0)


# ── Integration: the write paths ───────────────────────────────────────────────


class IntegrationQA(FtsMixin, unittest.IsolatedAsyncioTestCase):
    """The DB operations that maintain the index as a side effect."""

    async def asyncSetUp(self):
        await self.init_temp_db()
        await db.chat_create("c1", "Ledger", None, f"{self.tmp.name}/projects", "admin")

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def test_append_indexes_the_new_message(self):
        mid = await db.messages_append("c1", "user", "alpha unicorn")
        self.assertEqual(await self.fts_match("unicorn"), {mid})

    async def test_batch_indexes_every_row(self):
        ids = await db.messages_batch("c1", [("user", "alpha"), ("assistant", "beta")])
        self.assertEqual(await self.fts_rowids(), set(ids))

    async def test_appends_accumulate_rather_than_replace(self):
        first = await db.messages_append("c1", "user", "alpha")
        second = await db.messages_append("c1", "user", "beta")
        self.assertEqual(await self.fts_rowids(), {first, second})

    async def test_repeated_appends_do_not_duplicate_index_rows(self):
        # A correct index holds exactly one row per message. Note this alone
        # does NOT detect the O(n^2) rebuild, which produced the same counts --
        # see test_append_writes_exactly_one_index_row.
        ids = [await db.messages_append("c1", "user", f"row {i}") for i in range(12)]
        self.assertEqual(await self.fts_count(), len(ids))

    async def test_append_writes_exactly_one_index_row(self):
        """Appending must cost one index write, not one per existing message.

        The original implementation deleted and re-inserted the whole
        conversation on every append. Row counts came out identical, so no
        assertion about index contents could tell the two apart -- only
        counting the writes does.
        """
        for i in range(10):
            await db.messages_append("c1", "user", f"row {i}")

        inserts = 0
        original = db.db_conn.execute

        async def counting(sql, *args, **kwargs):
            nonlocal inserts
            if isinstance(sql, str) and "INSERT INTO messages_fts" in sql:
                inserts += 1
            return await original(sql, *args, **kwargs)

        with patch.object(db.db_conn, "execute", counting):
            await db.messages_append("c1", "user", "the eleventh row")

        self.assertEqual(
            inserts, 1, f"appending wrote {inserts} index rows; expected 1"
        )

    async def test_batch_writes_one_index_row_per_message(self):
        for i in range(10):
            await db.messages_append("c1", "user", f"row {i}")

        inserts = 0
        original = db.db_conn.execute

        async def counting(sql, *args, **kwargs):
            nonlocal inserts
            if isinstance(sql, str) and "INSERT INTO messages_fts" in sql:
                inserts += 1
            return await original(sql, *args, **kwargs)

        with patch.object(db.db_conn, "execute", counting):
            await db.messages_batch("c1", [("user", "a"), ("assistant", "b")])

        self.assertEqual(
            inserts, 2, f"batching two messages wrote {inserts} index rows; expected 2"
        )

    async def test_index_maintenance_opens_no_second_connection(self):
        """The fix, pinned directly.

        Index maintenance used to open a fresh sqlite3 connection per call in a
        worker thread, which made it a second writer against the same file.
        WAL allows one writer at a time, so every message write was followed by
        an index write that raced it, and the loser waited out its busy timeout
        and reported "database is locked" -- 492 of 660 such errors in one
        day's production log came from that pair.

        Asserting on the count of connections rather than on the index contents
        is deliberate: the index ends up correct either way, so nothing about
        the resulting rows can tell the two arrangements apart. Only counting
        the connections can.
        """
        import sqlite3 as _sqlite3

        opened = []
        original = _sqlite3.connect

        def counting(*args, **kwargs):
            opened.append(args[0] if args else kwargs.get("database"))
            return original(*args, **kwargs)

        with patch.object(_sqlite3, "connect", counting):
            await db.messages_append("c1", "user", "indexed on the shared handle")
            await db.messages_batch("c1", [("user", "a"), ("assistant", "b")])
            await db._fts_rebuild("c1")

        self.assertEqual(
            opened, [],
            f"index maintenance opened {len(opened)} extra connection(s): {opened}",
        )

    async def test_a_failed_index_write_leaves_no_open_transaction(self):
        """The hazard this change introduces, asserted where it actually bites.

        On a dedicated connection a failed index write was isolated. On the
        shared one, a sequence that fails *part way* leaves a write
        transaction open -- and that transaction holds the writer lock, which
        blocks auth.py's session connection and stops the WAL checkpointing.
        The damage is cross-connection, so the honest assertion is about the
        transaction state rather than about whether this connection can still
        write: it can, inside the transaction it never closed.

        A first version of this test dropped messages_fts and checked that
        writes still landed. It passed with the rollback removed, because a
        statement against a missing table fails at prepare time and opens no
        transaction -- so it exercised nothing. Failing the INSERT after the
        DELETE has already succeeded is what reaches the branch.
        """
        import sqlite3 as _sqlite3

        original = db.db_conn.execute

        async def failing(sql, *args, **kwargs):
            if isinstance(sql, str) and "INSERT INTO messages_fts" in sql:
                raise _sqlite3.OperationalError("simulated mid-sequence failure")
            return await original(sql, *args, **kwargs)

        with patch.object(db.db_conn, "execute", failing):
            mid = await db.messages_append("c1", "user", "the write that trips it")

        self.assertIsNotNone(mid, "the message write itself must still succeed")
        self.assertFalse(
            db.db_conn.in_transaction,
            "a failed index write left a transaction open, holding the writer "
            "lock against every other connection",
        )

    async def test_delete_purges_the_index(self):
        await db.messages_batch("c1", [("user", "alpha"), ("assistant", "beta")])
        self.assertEqual(await self.fts_count(), 2)
        await db.chat_delete("c1", "admin")
        self.assertEqual(await self.fts_count(), 0)

    async def test_delete_leaves_other_chats_indexed(self):
        await db.chat_create("c2", "Other", None, f"{self.tmp.name}/projects", "admin")
        await db.messages_append("c1", "user", "alpha")
        keep = await db.messages_append("c2", "user", "beta")
        await db.chat_delete("c1", "admin")
        self.assertEqual(await self.fts_rowids(), {keep})

    async def test_rename_reindexes_so_title_search_follows(self):
        mid = await db.messages_append("c1", "user", "body text")
        self.assertEqual(await self.fts_match("Ledger"), {mid})
        await db.chat_update("c1", "admin", title="Accounts")
        self.assertEqual(await self.fts_match("Accounts"), {mid})
        self.assertEqual(await self.fts_match("Ledger"), set())

    async def test_rename_by_a_non_owner_does_not_reindex(self):
        mid = await db.messages_append("c1", "user", "body text")
        self.assertFalse(await db.chat_update("c1", "mallory", title="Hijacked"))
        self.assertEqual(await self.fts_match("Ledger"), {mid})

    async def test_fork_indexes_the_copied_messages(self):
        await db.messages_batch("c1", [("user", "alpha unicorn")])
        forked = await db.chat_fork("c1", "admin")
        self.assertIsNotNone(forked)
        # Both the original and the copy must be findable.
        self.assertEqual(len(await self.fts_match("unicorn")), 2)


# ── Acceptance: search as the user sees it ─────────────────────────────────────


class AcceptanceUATQA(FtsMixin, unittest.IsolatedAsyncioTestCase):
    """Search outcomes a user would notice."""

    async def asyncSetUp(self):
        await self.init_temp_db()
        await db.chat_create("c1", "Ledger", None, f"{self.tmp.name}/projects", "admin")

    async def asyncTearDown(self):
        await self.close_temp_db()

    async def test_i_can_find_a_message_i_just_sent(self):
        await db.messages_append("c1", "user", "remember the unicorn pancakes")
        hits = await db.chat_search("admin", "unicorn")
        self.assertEqual([h["id"] for h in hits], ["c1"])

    async def test_a_deleted_conversation_stops_appearing_in_search(self):
        await db.messages_append("c1", "user", "unicorn pancakes")
        await db.chat_delete("c1", "admin")
        self.assertEqual(await db.chat_search("admin", "unicorn"), [])

    async def test_i_cannot_search_another_users_messages(self):
        await db.chat_create("c2", "Theirs", None, f"{self.tmp.name}/projects", "bob")
        await db.messages_append("c2", "user", "unicorn secret")
        self.assertEqual(await db.chat_search("admin", "unicorn"), [])
        self.assertEqual([h["id"] for h in await db.chat_search("bob", "unicorn")], ["c2"])

    async def test_renaming_a_conversation_lets_me_find_it_by_the_new_name(self):
        await db.messages_append("c1", "user", "body text")
        await db.chat_update("c1", "admin", title="Quarterly Accounts")
        hits = await db.chat_search("admin", "Quarterly")
        self.assertEqual([h["id"] for h in hits], ["c1"])

    async def test_search_returns_a_snippet(self):
        await db.messages_append("c1", "user", "remember the unicorn pancakes")
        hits = await db.chat_search("admin", "unicorn")
        self.assertIn("unicorn", hits[0]["snippet"])

    async def test_search_survives_fts_being_unavailable(self):
        # A search must return nothing rather than 500 the endpoint.
        await db.messages_append("c1", "user", "unicorn")
        await db.db_conn.execute("DROP TABLE messages_fts")
        await db.db_conn.commit()
        self.assertEqual(await db.chat_search("admin", "unicorn"), [])


if __name__ == "__main__":
    unittest.main()
