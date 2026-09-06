"""QA: the sidebar's queued-prompt count must distinguish held from pending.

`GET /api/chats` used to report only `queued` -- the total row count in
`turn_queue` for that chat, pending and held summed together. A chat with 3
prompts safely waiting to auto-send looked identical, on the sidebar, to one
with 3 held because its last turn broke and needs a Send/Discard decision.
The open conversation's own queue panel already colours held differently
(`.queue-bar[data-held="yes"]`); the sidebar gave no reason to ever open it.

Fixed with `db.queue_held_counts` (held-only, same shape as `queue_counts`)
and a new `queued_held` field alongside `queued` in the chat-list response.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import db
from routes import chats as chat_routes


class QueueHeldCountsQA(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(
            config, "PROJECTS_ROOT", f"{self.tmp.name}/projects"
        )
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        self.chat_a = "a" * 32
        self.chat_b = "b" * 32
        await db.chat_create(
            self.chat_a, "healthy queue", None, f"{self.tmp.name}/projects", "admin",
        )
        await db.chat_create(
            self.chat_b, "broken queue", None, f"{self.tmp.name}/projects", "admin",
        )

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_held_counts_are_separate_from_the_total(self):
        # chat_a: 2 healthy pending prompts, nothing held.
        await db.queue_add(self.chat_a, "admin", "first")
        await db.queue_add(self.chat_a, "admin", "second")
        # chat_b: 1 pending, 1 held (its predecessor's turn broke).
        await db.queue_add(self.chat_b, "admin", "third")
        await db.queue_add(self.chat_b, "admin", "fourth")
        await db.queue_hold_all(self.chat_b)
        # queue_hold_all holds every *pending* row -- release one back so
        # chat_b ends up with a genuine pending+held mix, not all-held,
        # which would not distinguish this from a simpler "any held" flag.
        rows = await db.queue_list(self.chat_b, "admin")
        await db.queue_release(rows[0]["id"], "admin")

        totals = await db.queue_counts("admin")
        held = await db.queue_held_counts("admin")

        self.assertEqual(totals[self.chat_a], 2)
        self.assertNotIn(
            self.chat_a, held,
            "a chat with zero held prompts must not appear in the held map "
            "at all -- the caller only ever checks it with .get(id, 0)",
        )
        self.assertEqual(totals[self.chat_b], 2)
        self.assertEqual(
            held[self.chat_b], 1,
            "chat_b has exactly one held row after releasing the other back "
            "to pending -- queue_held_counts must reflect state, not just "
            "count everything queue_hold_all touched",
        )


class ChatsListQueuedHeldFieldQA(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(
            config, "PROJECTS_ROOT", f"{self.tmp.name}/projects"
        )
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        self.chat_id = "c" * 32
        await db.chat_create(
            self.chat_id, "held chat", None, f"{self.tmp.name}/projects", "admin",
        )

    async def asyncTearDown(self):
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

    async def test_queued_held_is_zero_when_nothing_is_held(self):
        await db.queue_add(self.chat_id, "admin", "pending only")
        row = await self._row()
        self.assertEqual(row["queued"], 1)
        self.assertEqual(
            row["queued_held"], 0,
            "a purely-pending queue must report queued_held=0, not omit the "
            "field or leave it truthy -- the sidebar badge's colour depends "
            "on this being a real, present zero",
        )

    async def test_queued_held_reflects_a_broken_turn(self):
        await db.queue_add(self.chat_id, "admin", "will be held")
        await db.queue_hold_all(self.chat_id)
        row = await self._row()
        self.assertEqual(row["queued"], 1)
        self.assertEqual(
            row["queued_held"], 1,
            "GET /api/chats did not surface the held prompt separately from "
            "the total -- the sidebar badge cannot distinguish 'safely "
            "waiting' from 'needs a decision' without this",
        )


if __name__ == "__main__":
    unittest.main()
