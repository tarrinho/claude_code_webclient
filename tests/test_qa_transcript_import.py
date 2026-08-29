"""QA: a resumed CLI session opens with the conversation already in it.

Resuming used to create an empty chat -- the history existed only in the
separate transcript viewer, so the conversation gave no sense of what had been
discussed. The transcript is now imported once, at resume, as ordinary message
rows, which makes it scrollable, searchable, exportable and forkable like any
other conversation.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app
import config
import db


def _turn(role, text, sidechain=False, kind="text"):
    return {
        "role": role,
        "timestamp": "2026-08-29T10:00:00Z",
        "model": "claude-sonnet-5" if role == "assistant" else "",
        "blocks": [{"kind": kind, "text": text}],
        "sidechain": sidechain,
    }


class TurnFlatteningTests(unittest.TestCase):
    """_turn_to_message decides what a replayed turn shows."""

    def test_text_block_becomes_the_body(self):
        self.assertEqual(
            app._turn_to_message(_turn("user", "hello")), ("user", "hello")
        )

    def test_assistant_role_is_preserved(self):
        role, _ = app._turn_to_message(_turn("assistant", "hi"))
        self.assertEqual(role, "assistant")

    def test_unknown_role_falls_back_to_user(self):
        role, _ = app._turn_to_message(_turn("system", "note"))
        self.assertEqual(role, "user")

    def test_tool_block_is_marked_not_dropped(self):
        _role, body = app._turn_to_message(_turn("assistant", "Read foo.py", kind="tool"))
        self.assertEqual(body, "`Read foo.py`")

    def test_thinking_block_is_dropped(self):
        """The terminal collapses thinking; replaying it shows more than was seen."""
        self.assertIsNone(app._turn_to_message(_turn("assistant", "hmm", kind="thinking")))

    def test_sidechain_turn_is_dropped(self):
        """Subagent traffic has nowhere to be labelled in a role/content row."""
        self.assertIsNone(app._turn_to_message(_turn("assistant", "sub", sidechain=True)))

    def test_blank_turn_is_dropped(self):
        self.assertIsNone(app._turn_to_message(_turn("user", "   ")))

    def test_multiple_blocks_join_with_a_blank_line(self):
        turn = {
            "role": "assistant",
            "blocks": [
                {"kind": "text", "text": "one"},
                {"kind": "tool", "text": "Bash ls"},
                {"kind": "text", "text": "two"},
            ],
            "sidechain": False,
        }
        _role, body = app._turn_to_message(turn)
        self.assertEqual(body, "one\n\n`Bash ls`\n\ntwo")


class TranscriptImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        Path(f"{self.tmp.name}/p").mkdir(parents=True, exist_ok=True)
        await db.chat_create("c1", "Chat", None, f"{self.tmp.name}/p", "admin")

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    def _payload(self, turns, found=True, truncated=False):
        return {"turns": turns, "found": found, "truncated": truncated,
                "start": 0, "offset": 0, "at_start": True}

    async def test_imports_turns_in_order(self):
        payload = self._payload([
            _turn("user", "first"),
            _turn("assistant", "second"),
            _turn("user", "third"),
        ])
        with patch.object(app.transcripts, "read_turns", AsyncMock(return_value=payload)):
            count = await app._import_transcript("c1", "sess-1")
        self.assertEqual(count, 3)
        rows = await db.messages_get("c1")
        self.assertEqual([r["content"] for r in rows], ["first", "second", "third"])
        self.assertEqual([r["role"] for r in rows], ["user", "assistant", "user"])

    async def test_missing_transcript_imports_nothing(self):
        with patch.object(app.transcripts, "read_turns",
                          AsyncMock(return_value=self._payload([], found=False))):
            self.assertEqual(await app._import_transcript("c1", "sess-1"), 0)
        self.assertEqual(await db.messages_get("c1"), [])

    async def test_transcript_with_only_sidechains_imports_nothing(self):
        payload = self._payload([_turn("assistant", "sub", sidechain=True)])
        with patch.object(app.transcripts, "read_turns", AsyncMock(return_value=payload)):
            self.assertEqual(await app._import_transcript("c1", "sess-1"), 0)
        self.assertEqual(await db.messages_get("c1"), [])

    async def test_read_error_is_survived(self):
        with patch.object(app.transcripts, "read_turns",
                          AsyncMock(side_effect=OSError("boom"))):
            self.assertEqual(await app._import_transcript("c1", "sess-1"), 0)

    async def test_imported_messages_are_searchable(self):
        """Import goes through messages_batch, so FTS picks the turns up.

        The needle is deliberately unhyphenated: FTS5 reads "-" as its NOT
        operator, so a hyphenated query matches nothing regardless of what is
        indexed. That is a real limitation of the search box, not of the
        import, and testing it here would hide the thing being tested.
        """
        payload = self._payload([_turn("user", "unmistakabletoken")])
        with patch.object(app.transcripts, "read_turns", AsyncMock(return_value=payload)):
            await app._import_transcript("c1", "sess-1")
        results = await db.chat_search("admin", "unmistakabletoken")
        self.assertEqual([r["id"] for r in results], ["c1"])


class ResumeImportTests(unittest.IsolatedAsyncioTestCase):
    """The resume handler must import once and never duplicate."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        Path(f"{self.tmp.name}/p").mkdir(parents=True, exist_ok=True)
        self.sid = "aaaabbbbccccdddd"
        self.payload = {
            "turns": [_turn("user", "earlier"), _turn("assistant", "reply")],
            "found": True, "truncated": False, "start": 0, "offset": 0,
            "at_start": True,
        }

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    def _req(self):
        return SimpleNamespace(state=SimpleNamespace(session={"user": "admin", "role": "admin"}))

    async def _resume(self):
        sessions = [{"sessionId": self.sid, "cwd": f"{self.tmp.name}/p", "name": "cweb2"}]
        with patch.object(db, "read_claude_sessions", AsyncMock(return_value=sessions)), \
             patch.object(db, "write_claude_session_file", lambda *a, **k: None), \
             patch.object(app.transcripts, "read_turns",
                          AsyncMock(return_value=self.payload)):
            resp = await app.handle_sessions_resume(self._req(), self.sid)
        return json.loads(resp.body)

    async def test_first_resume_imports_the_history(self):
        data = await self._resume()
        self.assertEqual(data["imported_messages"], 2)
        rows = await db.messages_get(data["id"])
        self.assertEqual([r["content"] for r in rows], ["earlier", "reply"])

    async def test_second_resume_does_not_duplicate(self):
        """The guard that matters: re-opening must not replay the history."""
        first = await self._resume()
        before = await db.messages_get(first["id"])
        second = await self._resume()
        self.assertEqual(second["id"], first["id"])
        after = await db.messages_get(first["id"])
        self.assertEqual(len(after), len(before))

    async def test_resume_backfills_a_chat_left_empty(self):
        """A chat resumed before the import existed is filled in on reopen."""
        await db.chat_create("old", "Old", None, f"{self.tmp.name}/p", "admin")
        await db.chat_set_session("old", self.sid)
        data = await self._resume()
        self.assertEqual(data["id"], "old")
        self.assertEqual(len(await db.messages_get("old")), 2)

    async def test_backfill_skips_a_chat_that_has_messages(self):
        """A conversation continued in WebConsole is never re-seeded."""
        await db.chat_create("old", "Old", None, f"{self.tmp.name}/p", "admin")
        await db.chat_set_session("old", self.sid)
        await db.messages_append("old", "user", "typed here")
        await self._resume()
        rows = await db.messages_get("old")
        self.assertEqual([r["content"] for r in rows], ["typed here"])


if __name__ == "__main__":
    unittest.main()
