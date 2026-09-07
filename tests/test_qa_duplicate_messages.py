"""QA: a prompt sent from the web must be stored exactly once.

Two independent writers append to the same conversation. `finish()` in
routes/chats.py stores the prompt and the reply when a turn ends, and the
transcript sync imports whatever the CLI appended to the session's JSONL. The
CLI writes the prompt as soon as the turn starts, so between those two moments
the sync can read bytes the turn is about to store itself -- and the user sees
their own message twice.

Three guards are pinned here, each one a way the duplicate actually arrived:

* the sync stands aside entirely while a turn is running for that chat;
* two syncs racing each other cannot both import the same byte range;
* an overlap that does not start at the newest stored row is still trimmed,
  which the old "compare the tail against rows[0]" check missed.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import db
import transcripts
import turns
from routes import chats as chat_routes


def _turn(role: str, text: str) -> dict:
    return {
        "role": role,
        "timestamp": "2026-09-07T10:00:00Z",
        "model": "",
        "blocks": [{"kind": "text", "text": text}],
        "sidechain": False,
    }


def _payload(rows: list[dict], offset: int) -> dict:
    return {
        "turns": rows, "found": True, "truncated": False,
        "start": 0, "offset": offset, "at_start": True,
    }


class DropAlreadyStoredTests(unittest.TestCase):
    """Unit: which imported rows survive the dedup."""

    @staticmethod
    def _stored(*pairs):
        return [{"role": role, "content": content} for role, content in pairs]

    def test_an_exact_repeat_of_the_stored_tail_is_dropped_whole(self):
        stored = self._stored(("user", "hello"), ("assistant", "hi"))
        rows = [("user", "hello"), ("assistant", "hi")]
        self.assertEqual(chat_routes._drop_already_stored(stored, rows), [])

    def test_only_the_overlapping_prefix_is_dropped(self):
        """The turn stored the pair; the transcript block carries it plus more."""
        stored = self._stored(("user", "hello"), ("assistant", "hi"))
        rows = [("user", "hello"), ("assistant", "hi"), ("user", "next")]
        self.assertEqual(
            chat_routes._drop_already_stored(stored, rows), [("user", "next")]
        )

    def test_an_overlap_of_one_row_is_found_below_the_newest(self):
        """The case the old rows[0]-only check could not see."""
        stored = self._stored(
            ("user", "hello"), ("assistant", "hi"), ("user", "second"))
        rows = [("user", "second"), ("assistant", "answer")]
        self.assertEqual(
            chat_routes._drop_already_stored(stored, rows),
            [("assistant", "answer")],
        )

    def test_genuinely_new_rows_are_untouched(self):
        stored = self._stored(("user", "hello"))
        rows = [("assistant", "a brand new reply")]
        self.assertEqual(chat_routes._drop_already_stored(stored, rows), rows)

    def test_no_stored_history_keeps_everything(self):
        rows = [("user", "first ever")]
        self.assertEqual(chat_routes._drop_already_stored([], rows), rows)


class SyncStandsAsideForALiveTurnTests(unittest.IsolatedAsyncioTestCase):
    """Component: the two writers must never both store the same prompt."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        Path(f"{self.tmp.name}/p").mkdir(parents=True, exist_ok=True)
        await db.init()
        await db.chat_create("c1", "Chat", None, f"{self.tmp.name}/p", "admin")
        await db.chat_set_session("c1", "sess-1")

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def _chat(self):
        return await db.chat_get("c1", "admin")

    async def test_nothing_is_imported_while_a_turn_is_running(self):
        """The turn's own finish() stores this prompt; importing it too is the
        duplicate."""
        chat = await self._chat()
        with patch.object(turns, "is_running", return_value=True), \
                patch.object(transcripts, "read_turns", AsyncMock()) as reader:
            rows = await chat_routes._sync_linked_chat(chat)
        self.assertEqual(rows, [])
        reader.assert_not_awaited()
        self.assertEqual(await db.messages_get("c1"), [])

    async def test_the_import_resumes_once_the_turn_has_finished(self):
        chat = await self._chat()
        payload = _payload([_turn("user", "from the terminal")], 400)
        with patch.object(transcripts, "read_turns", AsyncMock(return_value=payload)):
            rows = await chat_routes._sync_linked_chat(chat)
        self.assertEqual(rows, [("user", "from the terminal")])

    async def test_a_web_turns_prompt_is_not_re_imported_afterwards(self):
        """finish() stored the pair. The same bytes then arrive from the CLI
        transcript, and must not become a second copy of the prompt."""
        await db.messages_batch(
            "c1", [("user", "make a chart"), ("assistant", "done")])
        chat = await self._chat()
        payload = _payload(
            [_turn("user", "make a chart"), _turn("assistant", "done")], 900)
        with patch.object(transcripts, "read_turns", AsyncMock(return_value=payload)):
            rows = await chat_routes._sync_linked_chat(chat)
        self.assertEqual(rows, [])
        stored = [(m["role"], m["content"]) for m in await db.messages_get("c1")]
        self.assertEqual(stored, [("user", "make a chart"), ("assistant", "done")])

    async def test_two_concurrent_syncs_import_a_block_once(self):
        """The open conversation polls every 5s while the sweep walks every
        chat: both used to read the same offset and both insert."""
        chat = await self._chat()
        payload = _payload([_turn("user", "typed in the terminal")], 500)

        async def slow_read(_session_id, _offset):
            # Long enough that the second caller is inside the function before
            # the first has advanced the offset, which is the race itself.
            await asyncio.sleep(0.05)
            return payload

        with patch.object(transcripts, "read_turns", slow_read):
            first, second = await asyncio.gather(
                chat_routes._sync_linked_chat(chat),
                chat_routes._sync_linked_chat(chat),
            )
        stored = [(m["role"], m["content"]) for m in await db.messages_get("c1")]
        self.assertEqual(stored, [("user", "typed in the terminal")])
        self.assertEqual(
            len(first) + len(second), 1,
            "both syncs reported importing the row, so both wrote it",
        )


if __name__ == "__main__":
    unittest.main()
