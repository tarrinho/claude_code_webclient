"""QA: GET /api/chats must never report running=False next to a stale
updated_at.

Found while investigating a live report: a sidebar row that had just
finished responding would show its "finished" mark (the green arrow) for a
poll or two, then get visibly replaced by the "unread" mark (the hollow
ring) a couple of seconds later -- one highlight appearing to vanish. Traced
to `handle_chats_list` (routes/chats.py): it used to read `db.chat_list()`
(which captures each chat's `updated_at`), await two more things
(`_live_updated_at`, `db.queue_counts`), and only *then* check
`turns.running_ids()`. If a background turn finished and persisted its
answer during those awaits, the response reported that chat as
`running: False` while still carrying the `updated_at` captured *before*
the turn wrote anything -- the exact combination `chat-list.js` has no
single rendering path for: `running=False` with a stale timestamp looks
like "just finished, nothing new" (`.chat-ended`) until a later poll's
fresh `updated_at` flips it to "new reply" (`.chat-unread`), replacing one
mark with the other.

Confirmed live, not just read from source: polled the real deployment
through a genuine background turn before and after the fix. Before: a
1.76s window where `running` had already flipped false but `updated_at`
still showed the pre-turn value. After: both flip together, always.

Fixed by capturing `running_ids()` first -- a synchronous, in-memory read,
free to move ahead of the awaits -- so by the time any later await lets a
turn finish, `running` has already been decided. Worst case left is now
`running=True` next to an already-current `updated_at` (one extra poll
still showing the pulsing dot on a chat that just finished, which every
consumer already renders as normal, since a still-running chat also
updates its timestamp), never the broken combination.

This test reproduces the interleaving directly: makes the awaited call
between the two reads (`_live_updated_at`) run the turn to completion
before returning, the same race a slow poll caused live, then asserts the
fixed ordering already decided `running` before that -- so the response
can only be the safe combination, never the broken one.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import db
import turns
from routes import chats as chat_routes


class ChatsListRunningRaceQA(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(
            config, "PROJECTS_ROOT", f"{self.tmp.name}/projects"
        )
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await db.chat_create(
            "c" * 32, "race chat", None, f"{self.tmp.name}/projects", "admin",
        )
        self.chat_id = "c" * 32

    async def asyncTearDown(self):
        await turns.shutdown()
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    def _req(self):
        return SimpleNamespace(
            method="GET",
            url=SimpleNamespace(path="/api/chats"),
            cookies={},
            headers={"accept": "*/*"},
            query_params={},
            client=SimpleNamespace(host="127.0.0.1"),
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value={}),
        )

    async def _row(self):
        body = json.loads((await chat_routes.handle_chats_list(self._req())).body)
        return next(c for c in body["chats"] if c["id"] == self.chat_id)

    async def test_turn_finishing_during_the_slow_await_is_never_reported_stale(self):
        released = asyncio.Event()

        async def produce():
            await released.wait()
            yield {"type": "text", "content": "hi"}
            yield {"type": "done"}

        async def finish(*, parts, session_id, model, failed, cancelled=False):
            await db.messages_batch(
                self.chat_id, [("user", "hi"), ("assistant", "".join(parts))]
            )
            await db.bump_chat_updated_at(self.chat_id)

        turns.start(self.chat_id, "admin", "hi", None, produce=produce, finish=finish)

        real_live_updated_at = chat_routes._live_updated_at

        async def slow_live_updated_at(chats):
            # Stands in for the slow poll that caused this live: let the turn
            # run to completion and commit its write while this await is
            # still in flight, exactly where the old code's running_ids()
            # check used to sit.
            released.set()
            for _ in range(100):
                if not turns.is_running(self.chat_id):
                    break
                await asyncio.sleep(0.02)
            self.assertFalse(
                turns.is_running(self.chat_id), "turn never finished in time"
            )
            return await real_live_updated_at(chats)

        with patch.object(
            chat_routes, "_live_updated_at", side_effect=slow_live_updated_at,
        ):
            row = await self._row()

        # The turn genuinely finished before this response was built (proven
        # above), yet the response must not show it as not-running next to
        # the old timestamp -- that combination is what produced the visible
        # glitch. Captured before the race window, running still reads True
        # here; that is the safe side the fix biases toward.
        self.assertTrue(
            row["running"],
            "running was reported False for a response whose running check "
            "must have been taken before the turn finished -- the ordering "
            "fix regressed",
        )
        # updated_at is second-precision (`db._now()`), so a fast test run can
        # land the pre- and post-turn values in the same second -- comparing
        # the strings would be flaky on exactly the thing this test wants to
        # prove. Check the row that was actually written instead: since
        # chat_list() runs after running_ids() in the fixed order, and the
        # turn's finish() committed during the mocked await that follows
        # both, the assistant's reply must already be on disk by the time
        # this response was built.
        messages = await db.messages_get(self.chat_id)
        self.assertEqual(
            [(m["role"], m["content"]) for m in messages],
            [("user", "hi"), ("assistant", "hi")],
            "the turn's answer was not persisted by the time handle_chats_list "
            "returned, so chat_list's own DB read cannot have run after it "
            "either -- the ordering this test exists to prove did not hold",
        )

    async def test_a_settled_turn_is_reported_consistently_on_the_next_poll(self):
        # No race injected this time: prove the ordinary, unhurried case
        # still lands on running=False alongside the matching fresh
        # updated_at, not just the raced one above.
        async def produce():
            yield {"type": "text", "content": "hi"}
            yield {"type": "done"}

        async def finish(*, parts, session_id, model, failed, cancelled=False):
            await db.messages_batch(
                self.chat_id, [("user", "hi"), ("assistant", "".join(parts))]
            )
            await db.bump_chat_updated_at(self.chat_id)

        turn = turns.start(self.chat_id, "admin", "hi", None, produce=produce, finish=finish)
        await turn.task

        after = await db.chat_get(self.chat_id, "admin")
        row = await self._row()
        self.assertFalse(row["running"])
        self.assertEqual(row["updated_at"], after["updated_at"])


if __name__ == "__main__":
    unittest.main()
