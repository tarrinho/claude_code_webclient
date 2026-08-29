"""QA: a linked chat follows its transcript without echoing its own turns.

A turn sent from WebConsole is written to `messages` by the handler AND
appended to the CLI transcript, because the runner resumes the same session.
The sync therefore has to step past those bytes, or every web turn reappears
on the next poll. These tests pin that, and the offset bookkeeping it needs.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import app
import config
import db


def _turn(role, text):
    return {
        "role": role,
        "timestamp": "2026-08-29T10:00:00Z",
        "model": "",
        "blocks": [{"kind": "text", "text": text}],
        "sidechain": False,
    }


def _payload(turns, offset, found=True):
    return {"turns": turns, "found": found, "truncated": False,
            "start": 0, "offset": offset, "at_start": True}


class _Req:
    def __init__(self):
        self.state = SimpleNamespace(session={"user": "admin", "role": "admin"})


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        Path(f"{self.tmp.name}/p").mkdir(parents=True, exist_ok=True)
        await db.chat_create("c1", "Chat", None, f"{self.tmp.name}/p", "admin")
        await db.chat_set_session("c1", "sess-1")

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()


class TranscriptOffsetTests(_Base):
    async def test_offset_defaults_to_zero(self):
        self.assertEqual((await db.chat_get("c1", "admin"))["transcript_offset"], 0)

    async def test_offset_round_trips(self):
        await db.chat_set_transcript_offset("c1", 4096)
        self.assertEqual((await db.chat_get("c1", "admin"))["transcript_offset"], 4096)

    async def test_offset_change_does_not_reorder_the_sidebar(self):
        """Polling must not bump updated_at, or the list churns every 5s."""
        before = (await db.chat_get("c1", "admin"))["updated_at"]
        await db.chat_set_transcript_offset("c1", 512)
        self.assertEqual((await db.chat_get("c1", "admin"))["updated_at"], before)

    async def test_import_records_the_offset(self):
        with patch.object(app.transcripts, "read_turns",
                          AsyncMock(return_value=_payload([_turn("user", "hi")], 900))):
            await app._import_transcript("c1", "sess-1")
        self.assertEqual((await db.chat_get("c1", "admin"))["transcript_offset"], 900)

    async def test_skip_advances_without_importing(self):
        """After a web turn the bytes are consumed, not replayed as messages."""
        with patch.object(app.transcripts, "read_turns",
                          AsyncMock(return_value=_payload([_turn("user", "echo")], 700))):
            await app._skip_transcript_to_end("c1", "sess-1")
        self.assertEqual((await db.chat_get("c1", "admin"))["transcript_offset"], 700)
        self.assertEqual(await db.messages_get("c1"), [])


class ChatSyncHandlerTests(_Base):
    async def test_sync_appends_new_turns(self):
        payload = _payload([_turn("user", "from terminal")], 500)
        with patch.object(app.transcripts, "read_turns", AsyncMock(return_value=payload)):
            resp = await app.handle_chat_sync(_Req(), "c1")
        body = json.loads(resp.body)
        self.assertTrue(body["linked"])
        self.assertEqual([m["content"] for m in body["messages"]], ["from terminal"])
        rows = await db.messages_get("c1")
        self.assertEqual([r["content"] for r in rows], ["from terminal"])

    async def test_sync_reads_from_the_stored_offset(self):
        """The whole point: a 20 MB transcript is never re-parsed per poll."""
        await db.chat_set_transcript_offset("c1", 12345)
        reader = AsyncMock(return_value=_payload([], 12345))
        with patch.object(app.transcripts, "read_turns", reader):
            await app.handle_chat_sync(_Req(), "c1")
        self.assertEqual(reader.await_args.args[1], 12345)

    async def test_repeated_sync_does_not_duplicate(self):
        payload = _payload([_turn("user", "once")], 500)
        with patch.object(app.transcripts, "read_turns", AsyncMock(return_value=payload)):
            await app.handle_chat_sync(_Req(), "c1")
        with patch.object(app.transcripts, "read_turns",
                          AsyncMock(return_value=_payload([], 500))):
            await app.handle_chat_sync(_Req(), "c1")
        self.assertEqual(len(await db.messages_get("c1")), 1)

    async def test_pre_offset_chat_is_not_reimported(self):
        """The migration case, found live: it duplicated 63 messages.

        A chat imported before transcript_offset existed has the column
        default of 0 while already holding its history. Reading from 0 would
        replay every turn it already has.
        """
        await db.messages_batch("c1", [("user", "already here")])
        await db.chat_set_transcript_offset("c1", 0)
        with patch.object(app.transcripts, "read_turns",
                          AsyncMock(return_value=_payload([_turn("user", "already here")], 900))):
            body = json.loads((await app.handle_chat_sync(_Req(), "c1")).body)
        self.assertEqual(body["messages"], [])
        self.assertEqual(len(await db.messages_get("c1")), 1)
        self.assertEqual((await db.chat_get("c1", "admin"))["transcript_offset"], 900)

    async def test_empty_chat_at_offset_zero_still_imports(self):
        """A genuinely fresh linked chat must not be skipped by that guard."""
        with patch.object(app.transcripts, "read_turns",
                          AsyncMock(return_value=_payload([_turn("user", "new")], 300))):
            body = json.loads((await app.handle_chat_sync(_Req(), "c1")).body)
        self.assertEqual([m["content"] for m in body["messages"]], ["new"])

    async def test_unlinked_chat_reports_not_linked(self):
        await db.chat_create("c2", "Plain", None, f"{self.tmp.name}/p", "admin")
        body = json.loads((await app.handle_chat_sync(_Req(), "c2")).body)
        self.assertFalse(body["linked"])
        self.assertEqual(body["messages"], [])

    async def test_missing_transcript_is_not_an_error(self):
        with patch.object(app.transcripts, "read_turns",
                          AsyncMock(return_value=_payload([], 0, found=False))):
            resp = await app.handle_chat_sync(_Req(), "c1")
        self.assertEqual(json.loads(resp.body)["messages"], [])

    async def test_read_error_is_survived(self):
        with patch.object(app.transcripts, "read_turns",
                          AsyncMock(side_effect=OSError("boom"))):
            resp = await app.handle_chat_sync(_Req(), "c1")
        self.assertEqual(json.loads(resp.body)["messages"], [])

    async def test_unknown_chat_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_sync(_Req(), "nope")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_sidechain_only_window_still_advances_the_offset(self):
        """Otherwise subagent bytes are re-read on every single poll."""
        turn = _turn("assistant", "sub")
        turn["sidechain"] = True
        with patch.object(app.transcripts, "read_turns",
                          AsyncMock(return_value=_payload([turn], 800))):
            await app.handle_chat_sync(_Req(), "c1")
        self.assertEqual((await db.chat_get("c1", "admin"))["transcript_offset"], 800)


if __name__ == "__main__":
    unittest.main()
