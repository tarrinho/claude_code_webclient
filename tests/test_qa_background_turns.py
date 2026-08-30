"""QA: a turn that belongs to the server rather than to the browser.

Sending a request used to lock the user into that conversation. The guard in
conversation.js said "Stop the current response before switching", and it was
protecting the turn rather than the interface: the browser's fetch reader *was*
the turn's owner, so closing it disconnected the SSE stream, claude_proxy
terminated the CLI, and the answer was discarded because nothing was persisted
until the `done` event arrived.

The usage row, meanwhile, is written as each usage event arrives -- deliberately,
since the tokens are spent either way. So leaving mid-turn billed the user and
returned nothing. That is the regression these tests exist to hold down, and
`test_leaving_mid_turn_still_persists_the_answer` is the one that fails against
the old design.

Layered the way tests/test_qa_layers.py is: buffer mechanics, then persistence
through the real handler, then the queue, then the endpoint contract.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app
import auth
import config
import db
import turns


def _events(*items):
    """An async producer factory yielding *items*."""
    async def produce():
        for item in items:
            yield item
    return produce


async def _noop_finish(**_kwargs):
    return None


# ── The buffer and its followers ──────────────────────────────────────────────


class BufferQA(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await turns.shutdown()

    async def test_every_event_is_numbered_in_order(self):
        turn = turns.start(
            "c1", "admin", "hi", None,
            produce=_events(
                {"type": "text", "content": "a"},
                {"type": "text", "content": "b"},
                {"type": "done"},
            ),
            finish=_noop_finish,
        )
        await turn.task
        self.assertEqual([e["seq"] for e in turn.events], [1, 2, 3])
        self.assertEqual(turn.text(), "ab")

    async def test_a_follower_attaching_late_gets_the_whole_buffer(self):
        # Switching away and back is exactly this: the events happened while
        # nobody was attached, and they still have to arrive.
        turn = turns.start(
            "c1", "admin", "hi", None,
            produce=_events(
                {"type": "text", "content": "early"},
                {"type": "done"},
            ),
            finish=_noop_finish,
        )
        await turn.task
        seen = [e async for e in turns.follow("c1", 0)]
        self.assertEqual(
            [e.get("content") for e in seen if e["type"] == "text"], ["early"]
        )

    async def test_since_replays_only_the_gap(self):
        turn = turns.start(
            "c1", "admin", "hi", None,
            produce=_events(
                {"type": "text", "content": "one"},
                {"type": "text", "content": "two"},
                {"type": "done"},
            ),
            finish=_noop_finish,
        )
        await turn.task
        seen = [e async for e in turns.follow("c1", 1)]
        self.assertEqual([e["seq"] for e in seen], [2, 3])

    async def test_two_followers_see_the_same_sequence(self):
        # One Event reused across followers would let whichever ran first clear
        # it and leave the other asleep.
        release = asyncio.Event()

        async def produce():
            yield {"type": "text", "content": "first"}
            await release.wait()
            yield {"type": "text", "content": "second"}
            yield {"type": "done"}

        turns.start("c1", "admin", "hi", None, produce=produce,
                    finish=_noop_finish)

        async def watch():
            return [e["seq"] async for e in turns.follow("c1", 0)
                    if e.get("type") != "keepalive"]

        a = asyncio.create_task(watch())
        b = asyncio.create_task(watch())
        await asyncio.sleep(0)
        release.set()
        first, second = await asyncio.wait_for(asyncio.gather(a, b), timeout=2)
        self.assertEqual(first, second)
        self.assertEqual(first, [1, 2, 3])

    async def test_a_reaped_turn_reports_gone_rather_than_silence(self):
        # "Nothing is running" and "you missed it" need different responses from
        # the client: settle the UI, or reload the conversation.
        self.assertEqual(
            [e async for e in turns.follow("never-existed", 5)],
            [{"type": "gone"}],
        )
        # With since=0 there is nothing to have missed, so no sentinel.
        self.assertEqual([e async for e in turns.follow("never-existed", 0)], [])

    async def test_an_error_settles_as_error_and_never_reports_done(self):
        turn = turns.start(
            "c1", "admin", "hi", None,
            produce=_events(
                {"type": "text", "content": "partial"},
                {"type": "error", "error": "boom"},
                {"type": "done"},
            ),
            finish=_noop_finish,
        )
        await turn.task
        self.assertEqual(turn.state, "error")
        self.assertNotIn("done", [e["type"] for e in turn.events])

    async def test_a_producer_that_stops_early_is_an_error(self):
        turn = turns.start(
            "c1", "admin", "hi", None,
            produce=_events({"type": "text", "content": "half"}),
            finish=_noop_finish,
        )
        await turn.task
        self.assertEqual(turn.state, "error")
        self.assertIn("Stream ended before completion", turn.error)

    async def test_finish_runs_once_with_nobody_watching(self):
        calls = []

        async def finish(**kwargs):
            calls.append(kwargs)

        turn = turns.start(
            "c1", "admin", "hi", None,
            produce=_events({"type": "text", "content": "x"}, {"type": "done"}),
            finish=finish,
        )
        await turn.task
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["parts"], ["x"])
        self.assertFalse(calls[0]["failed"])

    async def test_finish_completes_before_followers_are_released(self):
        """Ordering that a client depends on.

        A follower exits the moment the turn stops being "running", and the
        first thing the browser does on `done` is reload the conversation. If
        the terminal state were published before the write landed, the reload
        could miss the answer it had just been shown.
        """
        order = []

        async def finish(**_kwargs):
            await asyncio.sleep(0)
            order.append("persisted")

        turns.start(
            "c1", "admin", "hi", None,
            produce=_events({"type": "done"}),
            finish=finish,
        )

        async def watch():
            async for _ in turns.follow("c1", 0):
                pass
            order.append("follower-exited")

        await asyncio.wait_for(watch(), timeout=2)
        self.assertEqual(order, ["persisted", "follower-exited"])

    async def test_a_second_turn_for_one_conversation_is_refused(self):
        release = asyncio.Event()

        async def produce():
            await release.wait()
            yield {"type": "done"}

        turns.start("c1", "admin", "hi", None, produce=produce,
                    finish=_noop_finish)
        with self.assertRaises(turns.AlreadyRunning):
            turns.start("c1", "admin", "again", None,
                        produce=_events({"type": "done"}),
                        finish=_noop_finish)
        release.set()
        await turns.get("c1").task

    async def test_running_ids_are_scoped_to_their_owner(self):
        release = asyncio.Event()

        async def produce():
            await release.wait()
            yield {"type": "done"}

        turns.start("mine", "admin", "hi", None, produce=produce,
                    finish=_noop_finish)
        turns.start("theirs", "someone-else", "hi", None, produce=produce,
                    finish=_noop_finish)
        self.assertEqual(turns.running_ids("admin"), {"mine"})
        self.assertEqual(turns.running_ids("someone-else"), {"theirs"})
        release.set()


# ── Persistence through the real handler ──────────────────────────────────────


class PersistenceQA(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(
            config, "PROJECTS_ROOT", f"{self.tmp.name}/projects"
        )
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        work_dir = Path(config.PROJECTS_ROOT) / "w"
        work_dir.mkdir(parents=True)
        self.chat_id = "b" * 32
        await db.chat_create(self.chat_id, "Bg", None, str(work_dir), "admin")
        self.request = SimpleNamespace(
            cookies={"wc_session": "valid"},
            json=AsyncMock(return_value={"content": "hello"}),
        )

    async def asyncTearDown(self):
        await turns.shutdown()
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def _messages(self):
        return [
            (m["role"], m["content"]) for m in await db.messages_get(self.chat_id)
        ]

    async def test_leaving_mid_turn_still_persists_the_answer(self):
        """The billing bug. This fails against the pre-background design.

        The client goes away after the first token -- switching conversation,
        reloading, or its phone locking. The turn must finish, store its answer,
        and record its usage exactly once. Before this change the CLI was
        terminated, the answer was dropped, and the usage row stayed.
        """
        release = asyncio.Event()

        async def fake_stream(*_args, **_kwargs):
            yield {"type": "text", "content": "half "}
            await release.wait()
            yield {"type": "text", "content": "and half"}
            yield {
                "type": "usage",
                "models": {"m": {"input_tokens": 10, "output_tokens": 4}},
                "cost_usd": None,
            }
            yield {"type": "done"}

        with patch.object(auth, "session_get", return_value={"user": "admin"}), \
             patch.object(app.runner, "stream_turn", fake_stream):
            response = await app.stream_handler(self.request, self.chat_id)
            iterator = response.body_iterator
            await anext(iterator)
            await anext(iterator)
            await iterator.aclose()          # the user leaves

            release.set()
            await turns.get(self.chat_id).task

        self.assertEqual(
            await self._messages(),
            [("user", "hello"), ("assistant", "half and half")],
        )
        rows = await db.usage_recent("admin", 50)
        self.assertEqual(len(rows), 1, "usage must be recorded exactly once")

    async def test_a_stopped_turn_keeps_what_was_already_paid_for(self):
        # Usage is recorded as it arrives, so discarding the partial answer on a
        # stop leaves the same "billed, nothing returned" state this change
        # exists to remove.
        started = asyncio.Event()

        async def fake_stream(*_args, **_kwargs):
            yield {"type": "text", "content": "some words"}
            started.set()
            await asyncio.Event().wait()

        with patch.object(auth, "session_get", return_value={"user": "admin"}), \
             patch.object(app.runner, "stream_turn", fake_stream):
            response = await app.stream_handler(self.request, self.chat_id)
            iterator = response.body_iterator
            await anext(iterator)
            await anext(iterator)
            await asyncio.wait_for(started.wait(), timeout=1)
            await turns.cancel(self.chat_id)
            await iterator.aclose()

        self.assertEqual(
            await self._messages(),
            [("user", "hello"), ("assistant", "some words")],
        )

    async def test_a_stop_with_nothing_produced_stores_nothing(self):
        # The client puts the prompt back in the composer, so storing it would
        # duplicate it the moment they send again.
        #
        # Started through _start_turn rather than the SSE handler: the handler's
        # generator yields its `start` frame before it creates the turn, so a
        # single anext() leaves nothing to cancel.
        started = asyncio.Event()

        async def fake_stream(*_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()
            yield {"type": "done"}

        chat = await db.chat_get(self.chat_id, "admin")
        with patch.object(app.runner, "stream_turn", fake_stream):
            await app._start_turn(chat, "admin", "hello", None)
            await asyncio.wait_for(started.wait(), timeout=2)
            await turns.cancel(self.chat_id)

        self.assertEqual(await self._messages(), [])


# ── The queue ─────────────────────────────────────────────────────────────────


class QueueQA(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(
            config, "PROJECTS_ROOT", f"{self.tmp.name}/projects"
        )
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        work_dir = Path(config.PROJECTS_ROOT) / "w"
        work_dir.mkdir(parents=True)
        self.chat_id = "q" * 32
        await db.chat_create(self.chat_id, "Queue", None, str(work_dir), "admin")

    async def asyncTearDown(self):
        await turns.shutdown()
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_positions_are_reported_and_order_is_kept(self):
        self.assertEqual(await db.queue_add(self.chat_id, "admin", "one"), 1)
        self.assertEqual(await db.queue_add(self.chat_id, "admin", "two"), 2)
        self.assertEqual(
            [r["prompt"] for r in await db.queue_list(self.chat_id, "admin")],
            ["one", "two"],
        )

    async def test_the_cap_is_enforced(self):
        for index in range(db.QUEUE_MAX):
            self.assertTrue(await db.queue_add(self.chat_id, "admin", str(index)))
        # A queue only drains on a clean finish, so without a ceiling a
        # conversation whose turns keep failing would grow without bound.
        self.assertEqual(await db.queue_add(self.chat_id, "admin", "too many"), 0)

    async def test_a_clean_finish_drains_exactly_one_prompt(self):
        await db.queue_add(self.chat_id, "admin", "next one")
        await db.queue_add(self.chat_id, "admin", "the one after")
        launched = []

        async def launcher(chat_id, owner, prompt, model):
            launched.append(prompt)

        with patch.object(turns, "launcher", launcher):
            turn = turns.start(
                self.chat_id, "admin", "first", None,
                produce=_events({"type": "done"}), finish=_noop_finish,
            )
            await turn.task
        self.assertEqual(launched, ["next one"])
        self.assertEqual(
            [r["prompt"] for r in await db.queue_list(self.chat_id, "admin")],
            ["the one after"],
        )

    async def test_a_failed_turn_holds_the_queue_instead_of_firing_it(self):
        await db.queue_add(self.chat_id, "admin", "do not send me")
        launched = []

        async def launcher(*args):
            launched.append(args)

        with patch.object(turns, "launcher", launcher):
            turn = turns.start(
                self.chat_id, "admin", "first", None,
                produce=_events({"type": "error", "error": "boom"}),
                finish=_noop_finish,
            )
            await turn.task
        self.assertEqual(launched, [], "a broken conversation must not be fed")
        rows = await db.queue_list(self.chat_id, "admin")
        self.assertEqual([r["state"] for r in rows], ["held"])

    async def test_a_held_prompt_can_be_released_or_discarded(self):
        await db.queue_add(self.chat_id, "admin", "held one")
        await db.queue_hold_all(self.chat_id)
        queue_id = (await db.queue_list(self.chat_id, "admin"))[0]["id"]

        self.assertIsNone(await db.queue_next(self.chat_id))
        self.assertTrue(await db.queue_release(queue_id, "admin"))
        self.assertIsNotNone(await db.queue_next(self.chat_id))
        self.assertTrue(await db.queue_delete(queue_id, "admin"))
        self.assertEqual(await db.queue_list(self.chat_id, "admin"), [])

    async def test_a_queue_belongs_to_its_owner(self):
        await db.queue_add(self.chat_id, "admin", "mine")
        self.assertEqual(await db.queue_list(self.chat_id, "someone-else"), [])
        self.assertFalse(await db.queue_delete(1, "someone-else"))
        self.assertEqual(len(await db.queue_list(self.chat_id, "admin")), 1)

    async def test_counts_are_reported_per_conversation(self):
        await db.queue_add(self.chat_id, "admin", "a")
        await db.queue_add(self.chat_id, "admin", "b")
        self.assertEqual(
            await db.queue_counts("admin"), {self.chat_id: 2}
        )


# ── Endpoint contract ─────────────────────────────────────────────────────────


class LiveEndpointQA(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(
            config, "PROJECTS_ROOT", f"{self.tmp.name}/projects"
        )
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        work_dir = Path(config.PROJECTS_ROOT) / "w"
        work_dir.mkdir(parents=True)
        self.chat_id = "l" * 32
        await db.chat_create(self.chat_id, "Live", None, str(work_dir), "admin")

    async def asyncTearDown(self):
        await turns.shutdown()
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    def _request(self, user="admin", since=None):
        params = {} if since is None else {"since": str(since)}
        return SimpleNamespace(
            state=SimpleNamespace(session={"user": user}),
            query_params=params,
            is_disconnected=AsyncMock(return_value=False),
        )

    async def test_idle_reports_not_running_rather_than_failing(self):
        response = await app.handle_chat_live(self._request(), self.chat_id)
        self.assertEqual(json.loads(response.body), {"running": False, "state": "idle"})

    async def test_another_owner_cannot_attach(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_live(self._request(user="intruder"), self.chat_id)
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_attaching_replays_from_since(self):
        turn = turns.start(
            self.chat_id, "admin", "hi", None,
            produce=_events(
                {"type": "text", "content": "one"},
                {"type": "text", "content": "two"},
                {"type": "done"},
            ),
            finish=_noop_finish,
        )
        await turn.task
        response = await app.handle_chat_live(self._request(since=1), self.chat_id)
        body = "".join([
            chunk.decode() if isinstance(chunk, bytes) else chunk
            async for chunk in response.body_iterator
        ])
        self.assertIn('"content": "two"', body)
        self.assertNotIn('"content": "one"', body)

    async def test_the_chat_list_reports_running_and_queued(self):
        await db.queue_add(self.chat_id, "admin", "waiting")
        release = asyncio.Event()

        async def produce():
            await release.wait()
            yield {"type": "done"}

        turns.start(self.chat_id, "admin", "hi", None, produce=produce,
                    finish=_noop_finish)
        request = SimpleNamespace(state=SimpleNamespace(session={"user": "admin"}))
        payload = json.loads((await app.handle_chats_list(request)).body)
        entry = next(c for c in payload["chats"] if c["id"] == self.chat_id)
        self.assertTrue(entry["running"])
        self.assertEqual(entry["queued"], 1)
        release.set()

    async def test_stop_cancels_and_holds_the_queue(self):
        await db.queue_add(self.chat_id, "admin", "behind it")
        started = asyncio.Event()

        async def produce():
            started.set()
            await asyncio.Event().wait()
            yield {"type": "done"}

        turns.start(self.chat_id, "admin", "hi", None, produce=produce,
                    finish=_noop_finish)
        await asyncio.wait_for(started.wait(), timeout=1)
        response = await app.handle_turn_stop(self._request(), self.chat_id)
        payload = json.loads(response.body)
        self.assertTrue(payload["stopped"])
        # A stop is not "move on to the next one".
        self.assertEqual(payload["held"], 1)
        self.assertFalse(turns.is_running(self.chat_id))


if __name__ == "__main__":
    unittest.main()
