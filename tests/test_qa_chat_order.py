"""QA coverage for favouriting and manual conversation ordering.

Covers:
* The ordering contract -- favourites first, then conversations the user has
  placed, then everything else by recency.
* The rule that makes manual ordering worth having: a placed conversation
  keeps its slot when it gets new activity.
* chats_reorder as one transaction, scoped to the ids it was given.
* Clearing placements and returning to pure recency.
* PUT /api/chats/order: validation, ownership, and the empty-list reset.

Ordering assertions are easy to write vacuously -- a fixture whose natural
order already matches the expected one passes no matter what the query does.
Every case here places conversations in an order that differs from both their
creation order and their recency order.
"""
from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import config
import db
from routes import chats as chat_routes


def make_request(body=None, user="admin"):
    request = types.SimpleNamespace(
        method="PUT",
        url=types.SimpleNamespace(path="/api/chats/order"),
        cookies={},
        headers={},
        client=types.SimpleNamespace(host="127.0.0.1"),
        state=types.SimpleNamespace(session={"user": user, "role": "admin"}),
        query_params={},
    )
    request.json = AsyncMock(return_value=body if body is not None else {})
    return request


class ChatOrderBase(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name) / "projects"
        self.projects.mkdir(parents=True)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", str(self.projects))
        self.db_patch.start()
        self.root_patch.start()

        # db._now() has one-second resolution, so several conversations touched
        # inside the same second share an updated_at and "most recent" becomes
        # whatever SQLite happens to return. Drive a monotonic clock instead, so
        # these assertions test the ordering rather than the wall clock.
        self._tick = 0

        def fake_now():
            self._tick += 1
            return f"2026-01-01T00:00:{self._tick:02d}Z"

        self.now_patch = patch.object(db, "_now", fake_now)
        self.now_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.now_patch.stop()
        self.root_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    async def make_chats(self, *names, owner="admin"):
        for name in names:
            await db.chat_create(name, name.title(), None, str(self.projects), owner)

    async def titles(self, owner="admin"):
        return [row["title"] for row in await db.chat_list(owner)]


class OrderingContractTests(ChatOrderBase):

    async def test_unplaced_conversations_sort_by_recency(self):
        await self.make_chats("a", "b", "c")
        await db.chat_set_title("a", "A")  # touches updated_at, so A is newest
        self.assertEqual(await self.titles(), ["A", "C", "B"])

    async def test_placed_conversations_come_first_in_the_chosen_order(self):
        await self.make_chats("a", "b", "c", "d")
        # Deliberately neither creation nor recency order.
        await db.chats_reorder("admin", ["c", "a"])
        self.assertEqual((await self.titles())[:2], ["C", "A"])

    async def test_activity_does_not_dislodge_a_placed_conversation(self):
        """The whole point of placing one: it stays where it was put."""
        await self.make_chats("a", "b", "c")
        await db.chats_reorder("admin", ["c", "a"])
        before = await self.titles()
        await db.chat_set_title("b", "B")  # the *unplaced* one gets activity
        self.assertEqual((await self.titles())[:2], ["C", "A"])
        self.assertEqual(before[:2], (await self.titles())[:2])

    async def test_favourites_outrank_placement(self):
        """A favourite belongs at the top even if something else was placed."""
        await self.make_chats("a", "b", "c")
        await db.chats_reorder("admin", ["c", "a"])
        await db.chat_update("b", "admin", pinned=1)
        self.assertEqual((await self.titles())[0], "B")

    async def test_unplaced_still_sort_by_recency_below_placed(self):
        await self.make_chats("a", "b", "c", "d")
        await db.chats_reorder("admin", ["d"])
        await db.chat_set_title("b", "B")  # newest of the unplaced
        titles = await self.titles()
        self.assertEqual(titles[0], "D")
        self.assertEqual(titles[1], "B")

    async def test_archived_stay_last_regardless_of_placement(self):
        await self.make_chats("a", "b")
        await db.chats_reorder("admin", ["b", "a"])
        await db.chat_update("b", "admin", archived=1)
        self.assertEqual((await self.titles())[-1], "B")


class ReorderQueryTests(ChatOrderBase):

    async def test_returns_how_many_were_placed(self):
        await self.make_chats("a", "b")
        self.assertEqual(await db.chats_reorder("admin", ["b", "a"]), 2)

    async def test_unknown_ids_are_counted_out_not_fatal(self):
        await self.make_chats("a")
        self.assertEqual(await db.chats_reorder("admin", ["a", "ghost"]), 1)

    async def test_another_owners_conversation_is_not_moved(self):
        await self.make_chats("mine")
        await self.make_chats("theirs", owner="bob")
        self.assertEqual(await db.chats_reorder("admin", ["theirs"]), 0)
        row = await db.chat_get("theirs", "bob")
        self.assertIsNone(row["position"])

    async def test_omitted_conversations_keep_their_placement(self):
        """Reordering one section must not disturb another."""
        await self.make_chats("a", "b", "c")
        await db.chats_reorder("admin", ["a", "b", "c"])
        await db.chats_reorder("admin", ["c"])          # re-place only c
        self.assertEqual((await db.chat_get("a", "admin"))["position"], 0)
        self.assertEqual((await db.chat_get("b", "admin"))["position"], 1)
        self.assertEqual((await db.chat_get("c", "admin"))["position"], 0)

    async def test_an_empty_list_places_nothing(self):
        await self.make_chats("a")
        self.assertEqual(await db.chats_reorder("admin", []), 0)

    async def test_clearing_returns_to_recency(self):
        """The placed order must differ from the recency order.

        Newest first is C, B, A. Placing them as C, B, A too would make this
        pass whether or not clearing did anything -- the trap this whole file
        is written to avoid.
        """
        await self.make_chats("a", "b", "c")
        await db.chats_reorder("admin", ["a", "b", "c"])
        self.assertEqual(await self.titles(), ["A", "B", "C"])
        cleared = await db.chats_clear_order("admin")
        self.assertEqual(cleared, 3)
        self.assertEqual(await self.titles(), ["C", "B", "A"])

    async def test_clearing_when_nothing_is_placed_is_not_an_error(self):
        await self.make_chats("a")
        self.assertEqual(await db.chats_clear_order("admin"), 0)


class ReorderEndpointTests(ChatOrderBase):

    async def test_places_the_given_order(self):
        await self.make_chats("a", "b", "c")
        response = await chat_routes.handle_chats_reorder(
            make_request({"order": ["c", "a", "b"]}))
        self.assertEqual(json.loads(response.body)["placed"], 3)
        self.assertEqual(await self.titles(), ["C", "A", "B"])

    async def test_empty_order_clears_placements(self):
        await self.make_chats("a", "b")
        await db.chats_reorder("admin", ["b", "a"])
        response = await chat_routes.handle_chats_reorder(make_request({"order": []}))
        payload = json.loads(response.body)
        self.assertEqual(payload["cleared"], 2)
        self.assertIsNone((await db.chat_get("a", "admin"))["position"])

    async def test_a_missing_order_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chats_reorder(make_request({}))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_a_non_list_order_is_rejected(self):
        for value in ("abc", 7, {"a": 1}):
            with self.assertRaises(HTTPException) as ctx:
                await chat_routes.handle_chats_reorder(make_request({"order": value}))
            self.assertEqual(ctx.exception.status_code, 400)

    async def test_non_string_ids_are_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chats_reorder(make_request({"order": ["a", 7]}))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_an_absurd_list_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chats_reorder(
                make_request({"order": [f"c{i}" for i in range(501)]}))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_malformed_json_is_rejected(self):
        request = make_request()
        request.json = AsyncMock(side_effect=ValueError("bad body"))
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chats_reorder(request)
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_one_owner_cannot_reorder_anothers_conversations(self):
        await self.make_chats("theirs", owner="bob")
        response = await chat_routes.handle_chats_reorder(
            make_request({"order": ["theirs"]}, user="admin"))
        self.assertEqual(json.loads(response.body)["placed"], 0)


if __name__ == "__main__":
    unittest.main()
