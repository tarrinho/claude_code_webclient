"""QA: what a restart does to background turns, and to what was queued behind them.

`tests/test_qa_background_turns.py` covers a turn's life inside one process.
These cover the seam that process boundary creates, which a review on
2026-09-10 found unguarded in three places.

The central one, reproduced before it was fixed: `turn_queue` is on disk and
`turns._live` is not, and `_drain()` runs when a turn *finishes*. Restart
mid-turn and the `pending` rows behind it have no trigger left -- the turn that
would have finished no longer exists. Measured in a throwaway database: two
pending rows, `running_ids()` empty, `queue_counts()` reporting them to the
sidebar as "2 queued", `queue_next()` returning a drainable row, and no caller
anywhere for it. They execute only if the user happens to send another message
in that same conversation.

This is the shape of failure the lifespan comment in app.py already records
against a different queue: a `START_TUNNEL` command that "simply sat in the
queue forever". A persisted queue whose consumer only runs on an event a
restart has already destroyed.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db

# Patched config, not importlib.reload. The first version of this file reloaded
# `config` and `db` to point at a throwaway path, which works in isolation and
# hangs the suite: reload rebinds `db.db_conn` under any other module holding a
# live connection, so these two files passed alone (11 and 26) and never
# finished together. patch.object is what tests/test_qa_background_turns.py
# already uses, and it confines the change to this test.
#
# Never db.init() against the real database -- it migrates (CLAUDE.md §9).


class OrphanedQueueTests(unittest.IsolatedAsyncioTestCase):
    """Prompts queued behind a turn the restart destroyed."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.db_patch.start()
        self.db = db
        await db.init()
        self.chat = "c-restart"
        await db.chat_create(self.chat, "T", None, self.tmp.name, "alice")

    async def asyncTearDown(self):
        # close() is required rather than tidy: an open aiosqlite connection
        # keeps a thread alive and IsolatedAsyncioTestCase waits on it, so
        # without this the file hangs instead of failing.
        await db.close()
        self.db_patch.stop()
        self.tmp.cleanup()

    async def test_pending_rows_survive_the_process_that_would_drain_them(self):
        """The precondition. If this stops holding, the rest is moot."""
        await self.db.queue_add(self.chat, "alice", "first", None)
        await self.db.queue_add(self.chat, "alice", "second", None)
        rows = await self.db.queue_list(self.chat, "alice")
        self.assertEqual([r["state"] for r in rows], ["pending", "pending"])

    async def test_nothing_is_running_to_trigger_a_drain(self):
        """A fresh process has an empty _live, so no turn will ever finish and
        call _drain for these rows."""
        import turns
        await self.db.queue_add(self.chat, "alice", "first", None)
        self.assertEqual(turns.running_ids("alice"), set())
        self.assertIsNotNone(
            await self.db.queue_next(self.chat),
            "the row is drainable; the bug is that nothing drains it")

    async def test_startup_holds_them_rather_than_leaving_them_pending(self):
        """The fix. Held is a state the UI already has controls for; pending
        with no runner is a silent stall."""
        await self.db.queue_add(self.chat, "alice", "first", None)
        await self.db.queue_add(self.chat, "alice", "second", None)
        held = await self.db.queue_hold_orphans()
        self.assertEqual(held, 2)
        rows = await self.db.queue_list(self.chat, "alice")
        self.assertEqual([r["state"] for r in rows], ["held", "held"])

    async def test_it_does_not_launch_them(self):
        """Held, not sent. Boot is the wrong moment to spend a turn each with
        nobody watching, and it would contradict the rule _drain already
        follows -- a queued prompt goes only on a clean finish."""
        await self.db.queue_add(self.chat, "alice", "first", None)
        await self.db.queue_hold_orphans()
        self.assertIsNone(
            await self.db.queue_next(self.chat),
            "queue_next only sees 'pending', so a held row cannot be picked up "
            "by a drain that happens later either -- it waits for a person",
        )

    async def test_it_leaves_already_held_rows_alone(self):
        """Idempotent, and it must not disturb rows a real failure held: those
        already carry the user's decision to make."""
        await self.db.queue_add(self.chat, "alice", "first", None)
        await self.db.queue_hold_all(self.chat)
        again = await self.db.queue_hold_orphans()
        self.assertEqual(again, 0, "nothing was pending, so nothing to hold")

    async def test_it_spans_every_conversation(self):
        """A restart orphans the whole process's queues, not one chat's. The
        per-chat queue_hold_all cannot express that, which is why this exists."""
        await db.chat_create("c-two", "T2", None, self.tmp.name, "alice")
        await self.db.queue_add(self.chat, "alice", "a", None)
        await self.db.queue_add("c-two", "alice", "b", None)
        self.assertEqual(await self.db.queue_hold_orphans(), 2)
        for chat in (self.chat, "c-two"):
            rows = await self.db.queue_list(chat, "alice")
            self.assertEqual([r["state"] for r in rows], ["held"])

    async def test_the_sidebar_stops_reporting_them_as_queued_work(self):
        """queue_counts is what paints "N queued". While the rows were pending
        with no runner, that count was a promise nothing would keep."""
        await self.db.queue_add(self.chat, "alice", "first", None)
        before = await self.db.queue_counts("alice")
        self.assertEqual(before.get(self.chat), 1)
        await self.db.queue_hold_orphans()
        held = await self.db.queue_held_counts("alice")
        self.assertEqual(
            held.get(self.chat), 1,
            "they should now read as held, which the UI renders with Send and "
            "Discard rather than as work in progress",
        )


class StartupWiringTests(unittest.TestCase):
    """The call has to be *in* the startup path. A correct helper nobody runs
    is the START_TUNNEL bug again."""

    def test_lifespan_holds_orphaned_queue_rows(self):
        source = Path(__file__).resolve().parents[1].joinpath("app.py").read_text(
            encoding="utf-8")
        body = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertIn(
            "db.queue_hold_orphans()", body,
            "nothing in app.py calls it, so queued prompts stay orphaned no "
            "matter how correct the function is",
        )

    def test_it_is_registered_in_the_db_dispatch_table(self):
        """`db.<name>` for an undeclared name raises AttributeError at the call
        site -- registry #80, #81 and #92 are all this same omission."""
        import db
        self.assertTrue(callable(db.queue_hold_orphans))


if __name__ == "__main__":
    unittest.main()


class ReapingHappensOnFinishTests(unittest.IsolatedAsyncioTestCase):
    """Finished turns' buffers are released without waiting for a new one.

    `_reap()` ran only from `start()`, so a finished turn's event buffer was
    freed when the *next* turn began anywhere in the process. A burst of
    conversations followed by an idle console therefore held every buffer,
    waiting for a trigger that idleness guarantees will not arrive. Roughly
    1-4 MB per long turn -- unremarkable alone, and pure waste on a host that
    has been OOM-killed before.

    Isolating that from the reap in `start()` takes some care, which is why the
    first version of this fix had no test that could fail: starting a second
    turn reaps the first anyway. So the sequence here finishes a turn, backdates
    it past the retention window *after* the next turn is already running, and
    then lets that one finish -- at which point only a reap on the finish path
    can collect it.
    """

    async def asyncSetUp(self):
        import turns
        self.turns = turns
        turns._live.clear()

    async def asyncTearDown(self):
        self.turns._live.clear()

    @staticmethod
    def _events(*evts):
        def factory(**_kw):
            async def gen():
                for e in evts:
                    yield e
            return gen()
        return factory

    async def _noop_finish(self, **_kw):
        return None

    async def test_a_finished_turn_is_reaped_without_a_new_turn_starting(self):
        turns = self.turns
        a = turns.start("chat-a", "alice", "hi", None,
                        produce=self._events({"type": "done"}),
                        finish=self._noop_finish)
        await a.task
        self.assertIn("chat-a", turns._live)

        # B starts while A is still inside its retention window, so start()'s
        # own reap leaves A alone -- confirmed, or the rest proves nothing.
        b = turns.start("chat-b", "alice", "hi", None,
                        produce=self._events({"type": "done"}),
                        finish=self._noop_finish)
        self.assertIn("chat-a", turns._live,
                      "A was reaped at B's start; this test is not isolating "
                      "the finish path")

        # Now age A out, with B already running.
        turns._live["chat-a"].finished_at -= (turns._RETAIN_S + 1)
        await b.task

        self.assertNotIn(
            "chat-a", turns._live,
            "an aged-out buffer survived a turn finishing, so it is released "
            "only when some later turn starts -- which an idle console never "
            "does",
        )

    async def test_the_turn_that_just_finished_is_kept(self):
        """Retention is the point of _RETAIN_S: a client that reattaches late
        must still find the tail rather than an empty conversation."""
        turns = self.turns
        a = turns.start("chat-a", "alice", "hi", None,
                        produce=self._events({"type": "text", "content": "x"},
                                             {"type": "done"}),
                        finish=self._noop_finish)
        await a.task
        self.assertIn("chat-a", turns._live)
        self.assertEqual(turns._live["chat-a"].text(), "x")
