"""QA: the plumbing between a transcript and a usage row.

The attribution rules have tests in ``test_qa_usage_origin.py``. What had none
was everything that feeds them -- reading a transcript, deciding what prompt a
session is working on, and the app-level wiring that records a routed request in
the first place.

`_prompt_boundary` in particular was validated by hand in a shell session and
never written down, which is the same as not having been validated: the next
person to touch a record shape has nothing to break. The record shapes below are
copied from real transcripts on this machine, including the two that caused
trouble -- ``queue-operation`` (input typed into a busy session, queued rather
than run) and a ``user`` record carrying a tool result rather than a prompt.

Layered as elsewhere: unit, then a real file on disk, then the endpoints, then
parity between the streaming and blocking paths -- which have diverged twice in
this codebase and so are asserted rather than assumed.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import db
import runner
import transcripts
import turns
from routes import chats as chat_routes

# ── Unit: what counts as a prompt boundary ────────────────────────────────────


class PromptBoundaryQA(unittest.TestCase):
    """Which records say "the session is now working on this"."""

    def test_an_attachment_states_the_prompt_being_started(self):
        # The most reliable signal: written when the CLI actually begins work,
        # which is what makes attribution survive queueing.
        self.assertEqual(
            transcripts._prompt_boundary(
                {"type": "attachment", "attachment": {"prompt": "do the thing"}}
            ),
            "do the thing",
        )

    def test_a_queue_operation_states_a_prompt_that_is_waiting(self):
        # This is what a request typed into a busy session looks like, and
        # missing it was why offset-based attribution credited the wrong work.
        self.assertEqual(
            transcripts._prompt_boundary(
                {"type": "queue-operation", "content": "queued ask"}
            ),
            "queued ask",
        )

    def test_a_typed_user_record_is_a_boundary_in_both_content_shapes(self):
        self.assertEqual(
            transcripts._prompt_boundary(
                {"type": "user", "message": {"content": "typed by hand"}}),
            "typed by hand",
        )
        self.assertEqual(
            transcripts._prompt_boundary(
                {"type": "user",
                 "message": {"content": [{"type": "text", "text": "block form"}]}}),
            "block form",
        )

    def test_a_tool_result_is_not_a_prompt(self):
        """The case most likely to break this.

        A tool result comes back as a ``user`` record. Reading it as a new prompt
        would end a routed request's ownership on its own tool call, so every
        turn after the first would fall back to the terminal.
        """
        self.assertIsNone(transcripts._prompt_boundary({
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "x" * 500}
            ]},
        }))

    def test_records_that_say_nothing_are_not_boundaries(self):
        for record in (
            {"type": "assistant", "message": {"content": []}},
            {"type": "attachment", "attachment": {}},
            {"type": "attachment"},
            {"type": "queue-operation", "content": "   "},
            {"type": "queue-operation"},
            {"type": "user", "message": {}},
            {"type": "user"},
            {"type": "system", "content": "boot"},
            {},
        ):
            self.assertIsNone(transcripts._prompt_boundary(record), repr(record))

    def test_normalising_survives_the_two_records_of_one_prompt(self):
        # The console holds what it sent; the transcript holds what the CLI
        # received. They differ in whitespace and case.
        self.assertEqual(
            db.normalise_prompt("  Do   The\nThing  "),
            db.normalise_prompt("do the thing"),
        )
        self.assertEqual(db.normalise_prompt(""), "")
        self.assertEqual(db.normalise_prompt(None), "")
        # Bounded, so a very long prompt still compares in constant work.
        self.assertEqual(len(db.normalise_prompt("y" * 5000)), 200)


# ── Integration: a real transcript on disk ────────────────────────────────────


class TranscriptReadingQA(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "projects"
        (self.root / "proj").mkdir(parents=True)
        self.session = "sess-abc"
        self.path = self.root / "proj" / f"{self.session}.jsonl"
        self.patch = patch.object(db, "_CLAUDE_PROJECTS_DIR", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def _write(self, records):
        with self.path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    @staticmethod
    def _assistant(model="claude-opus-5", cache=True, inp=100, out=10, when="2026-08-30T10:00:00Z"):
        usage = {"input_tokens": inp, "output_tokens": out}
        if cache:
            usage["cache_read_input_tokens"] = 90
            usage["cache_creation_input_tokens"] = 1
        return {"type": "assistant", "timestamp": when,
                "message": {"model": model, "usage": usage}}

    def test_size_is_reported_and_a_missing_transcript_is_zero(self):
        self._write([self._assistant()])
        self.assertEqual(transcripts.transcript_size(self.session),
                         self.path.stat().st_size)
        self.assertEqual(transcripts.transcript_size("no-such-session"), 0)

    def test_each_row_carries_the_prompt_in_force_when_it_ran(self):
        self._write([
            {"type": "user", "message": {"content": "first ask"}},
            self._assistant(),
            self._assistant(),
            {"type": "attachment", "attachment": {"prompt": "second ask"}},
            self._assistant(),
        ])
        rows, _end = transcripts._usage_since_sync(self.path, 0)
        self.assertEqual([r["after_prompt"] for r in rows],
                         ["first ask", "first ask", "second ask"])

    def test_a_tool_result_between_turns_does_not_end_ownership(self):
        # The regression this guards: every turn after the first falling back to
        # the terminal because the agent used a tool.
        self._write([
            {"type": "user", "message": {"content": "the ask"}},
            self._assistant(),
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "output"}]}},
            self._assistant(),
        ])
        rows, _end = transcripts._usage_since_sync(self.path, 0)
        self.assertEqual([r["after_prompt"] for r in rows], ["the ask", "the ask"])

    def test_queued_input_claims_the_turns_that_eventually_run(self):
        self._write([
            {"type": "user", "message": {"content": "what it was already doing"}},
            self._assistant(),                                  # not the request
            {"type": "queue-operation", "content": "the routed ask"},
            self._assistant(),                                  # the request
        ])
        rows, _end = transcripts._usage_since_sync(self.path, 0)
        self.assertEqual([r["after_prompt"] for r in rows],
                         ["what it was already doing", "the routed ask"])

    def test_rows_carry_their_own_byte_offset_and_the_cursor_advances(self):
        self._write([self._assistant(), self._assistant()])
        rows, end = transcripts._usage_since_sync(self.path, 0)
        self.assertEqual(len(rows), 2)
        self.assertLess(rows[0]["offset"], rows[1]["offset"])
        self.assertEqual(end, self.path.stat().st_size)

    def test_reading_from_a_cursor_returns_only_what_was_appended(self):
        self._write([self._assistant()])
        _first, cursor = transcripts._usage_since_sync(self.path, 0)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self._assistant(out=99)) + "\n")
        rows, _end = transcripts._usage_since_sync(self.path, cursor)
        self.assertEqual([r["output_tokens"] for r in rows], [99])

    def test_a_partial_trailing_line_is_left_for_the_next_read(self):
        # A transcript is appended to while it is read; half a line must not be
        # parsed, and its bytes must not be consumed.
        self._write([self._assistant()])
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write('{"type": "assistant", "mess')
        rows, end = transcripts._usage_since_sync(self.path, 0)
        self.assertEqual(len(rows), 1)
        self.assertLess(end, self.path.stat().st_size)

    def test_unsplit_context_is_flagged_per_model(self):
        self._write([
            self._assistant(model="gw/model", cache=False, inp=106769),
            self._assistant(model="claude-opus-5", cache=True, inp=200_000),
        ])
        rows, _end = transcripts._usage_since_sync(self.path, 0)
        self.assertEqual([r["context_unsplit"] for r in rows], [True, False])


# ── Component: the app-level wiring ──────────────────────────────────────────


class MarkRoutedQA(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.db_patch.start()
        await db.init()
        await db.chat_create("c1", "cweb5", None, "/tmp/w", "admin")
        await db.chat_set_session("c1", "sess-live")

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.tmp.cleanup()

    async def test_a_marker_records_the_prompt_and_the_offset(self):
        chat = await db.chat_get("c1", "admin")
        with patch.object(transcripts, "transcript_size", return_value=4242):
            await chat_routes._mark_routed(chat, "admin", "please do the thing")
        marks = await db.routed_markers("sess-live")
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["chat_id"], "c1")
        self.assertEqual(marks[0]["from_offset"], 4242)
        # The prompt is the thing attribution matches on; without it the marker
        # is back to guessing by position.
        self.assertEqual(marks[0]["prompt"], "please do the thing")

    async def test_a_conversation_with_no_session_records_nothing(self):
        await db.chat_create("c2", "web only", None, "/tmp/x", "admin")
        chat = await db.chat_get("c2", "admin")
        await chat_routes._mark_routed(chat, "admin", "anything")
        self.assertEqual(await db.routed_markers(""), [])

    async def test_a_very_long_prompt_is_stored_bounded(self):
        chat = await db.chat_get("c1", "admin")
        with patch.object(transcripts, "transcript_size", return_value=0):
            await chat_routes._mark_routed(chat, "admin", "z" * 9000)
        marks = await db.routed_markers("sess-live")
        self.assertLessEqual(len(marks[0]["prompt"]), 4000)


class MigrationQA(unittest.IsolatedAsyncioTestCase):
    """An existing database must gain the prompt column without losing rows."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = f"{self.tmp.name}/db"
        # A routed_requests table as it existed before prompts were stored.
        con = sqlite3.connect(self.path)
        con.execute(
            "CREATE TABLE routed_requests ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,"
            " chat_id TEXT NOT NULL, owner_id TEXT NOT NULL,"
            " from_offset INTEGER NOT NULL, created_at TEXT NOT NULL)"
        )
        con.execute(
            "INSERT INTO routed_requests "
            "(session_id, chat_id, owner_id, from_offset, created_at) "
            "VALUES ('s1', 'c1', 'admin', 10, '2026-08-30T10:00:00Z')"
        )
        con.commit(); con.close()
        self.db_patch = patch.object(config, "DB_PATH", self.path)
        self.db_patch.start()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.tmp.cleanup()

    async def test_the_prompt_column_is_added_and_the_old_row_survives(self):
        await db.init()
        marks = await db.routed_markers("s1")
        self.assertEqual(len(marks), 1)
        # No prompt to recover, so it cannot be matched -- but it must not
        # crash the import, and it must not claim anything either.
        self.assertEqual(marks[0]["prompt"], "")
        self.assertIsNone(
            db.routed_owner_of(marks, 20, "2026-08-30T10:00:01Z", "some ask")
        )


class SlotWaitQA(unittest.IsolatedAsyncioTestCase):
    """Waiting for a concurrency slot must be visible, not look like a stall."""

    def setUp(self):
        self.reset = patch.object(runner, "_sem", None)
        self.reset.start()
        self.addCleanup(self.reset.stop)

    def test_no_semaphore_yet_is_not_busy(self):
        self.assertFalse(runner.slots_busy())

    async def test_busy_only_when_every_slot_is_taken(self):
        import asyncio
        with patch.object(config, "MAX_CONCURRENT", 2):
            sem = runner._get_sem()
            self.assertFalse(runner.slots_busy())
            await sem.acquire()
            self.assertFalse(runner.slots_busy(), "one of two taken")
            await sem.acquire()
            self.assertTrue(runner.slots_busy(), "both taken")
            sem.release(); sem.release()
            self.assertFalse(runner.slots_busy())
        self.assertIsInstance(sem, asyncio.Semaphore)


# ── Parity: the two turn paths must agree ────────────────────────────────────


class RoutedParityQA(unittest.IsolatedAsyncioTestCase):
    """Both entry points must mark a routed request.

    Streaming and blocking have diverged twice in this file -- the omitted
    backend on the streaming payload, and has_api_key -- so this is asserted
    rather than trusted. If only one path marks, usage from the other silently
    reverts to being counted as the terminal's own.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(
            config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self.db_patch.start(); self.root_patch.start()
        await db.init()
        work = Path(config.PROJECTS_ROOT) / "w"
        work.mkdir(parents=True)
        self.chat_id = "r" * 32
        await db.chat_create(self.chat_id, "cweb9", None, str(work), "admin")
        await db.chat_set_session(self.chat_id, "sess-parity")
        self.request = SimpleNamespace(
            cookies={"wc_session": "valid"},
            state=SimpleNamespace(session={"user": "admin"}),
            json=AsyncMock(return_value={"content": "the routed ask"}),
        )

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop(); self.root_patch.stop(); self.tmp.cleanup()

    async def test_the_blocking_path_marks_the_request(self):
        with patch.object(chat_routes, "_route_to_live_terminal",
                          AsyncMock(return_value={"delivered": True})), \
             patch.object(transcripts, "transcript_size", return_value=7):
            await chat_routes.handle_submit_message(self.request, self.chat_id)
        marks = await db.routed_markers("sess-parity")
        self.assertEqual([m["prompt"] for m in marks], ["the routed ask"])

    async def test_the_streaming_path_marks_the_request(self):
        import auth
        with patch.object(auth, "session_get", return_value={"user": "admin"}), \
             patch.object(chat_routes, "_route_to_live_terminal",
                          AsyncMock(return_value={"delivered": True})), \
             patch.object(transcripts, "transcript_size", return_value=9):
            response = await chat_routes.stream_handler(self.request, self.chat_id)
            async for _chunk in response.body_iterator:
                pass
        marks = await db.routed_markers("sess-parity")
        self.assertEqual([m["prompt"] for m in marks], ["the routed ask"])
        self.assertEqual(marks[0]["from_offset"], 9)

    async def test_an_unrouted_turn_marks_nothing(self):
        # Otherwise every ordinary web turn would leave a marker able to claim
        # the session's terminal work.
        with patch.object(chat_routes, "_route_to_live_terminal",
                          AsyncMock(return_value=None)), \
             patch.object(chat_routes, "_start_turn", AsyncMock()), \
             patch.object(turns, "follow", lambda *a, **k: _empty()):
            import auth
            with patch.object(auth, "session_get", return_value={"user": "admin"}):
                response = await chat_routes.stream_handler(self.request, self.chat_id)
                async for _chunk in response.body_iterator:
                    pass
        self.assertEqual(await db.routed_markers("sess-parity"), [])


async def _empty():
    """An async generator that yields nothing, for a stubbed follow()."""
    for item in ():
        yield item


if __name__ == "__main__":
    unittest.main()
