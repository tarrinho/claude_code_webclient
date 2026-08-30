"""QA coverage for following conversations nobody is looking at.

``/api/chats/{id}/sync`` only ever ran for the conversation on screen, so a
chat whose terminal was busy kept its old ``updated_at`` and sat in the sidebar
looking idle until you opened it. The list was being refreshed every six
seconds and refreshed faithfully -- it was the rows behind it that were stale,
which is the worse failure of the two because the page looks live.

``POST /api/chats/sync`` sweeps every linked conversation the caller owns.

Covers: that a turn typed into a terminal reaches a conversation that is not
open, that the byte cursor stops a second sweep re-importing it, that
``updated_at`` moves so the sidebar can reorder, that unlinked and other
owners' conversations are left alone, and that one unreadable transcript does
not cost the whole sweep.
"""
from __future__ import annotations

import json
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import app
import config
import db


def assistant(text: str, session_id: str) -> dict:
    return {
        "type": "assistant",
        "sessionId": session_id,
        "message": {
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [{"type": "text", "text": text}],
        },
    }


def request_for(user: str = "admin"):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(session={"user": user, "role": "admin"})
    )


class SyncAllBase(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects = root / "projects"
        self.projects.mkdir()
        self.claude = root / "claude"
        self.transcript_dir = self.claude / "-home-kali-demo"
        self.transcript_dir.mkdir(parents=True)

        self.db_patch = patch.object(config, "DB_PATH", str(root / "wc.db"))
        self.root_patch = patch.object(config, "PROJECTS_ROOT", str(self.projects))
        self.claude_patch = patch.object(db, "_CLAUDE_PROJECTS_DIR", self.claude)
        for p in (self.db_patch, self.root_patch, self.claude_patch):
            p.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        for p in (self.claude_patch, self.root_patch, self.db_patch):
            p.stop()
        self.tmp.cleanup()

    async def make_chat(self, title, session_id=None, owner="admin"):
        chat_id = uuid.uuid4().hex
        await db.chat_create(chat_id, title, "", str(self.projects), owner)
        if session_id:
            await db.chat_set_session(chat_id, session_id)
        return chat_id

    def write_transcript(self, session_id, records):
        path = self.transcript_dir / f"{session_id}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        return path

    async def sweep(self, user="admin"):
        response = await app.handle_chats_sync_all(request_for(user))
        return json.loads(response.body)


class SweepImportsTests(SyncAllBase):

    async def test_a_terminal_turn_reaches_a_conversation_that_is_not_open(self):
        """The whole point: nothing here opens the conversation first."""
        session_id = "aaaaaaaa-1111-2222-3333-444444444444"
        chat_id = await self.make_chat("alpha", session_id)
        self.write_transcript(session_id, [assistant("typed in the terminal", session_id)])

        result = await self.sweep()

        self.assertEqual(result["changed"], {chat_id: 1})
        messages = await db.messages_get(chat_id)
        self.assertEqual([m["content"] for m in messages], ["typed in the terminal"])

    async def test_a_second_sweep_does_not_import_the_same_turn(self):
        """The byte cursor is what keeps a 30s poll from duplicating history."""
        session_id = "bbbbbbbb-1111-2222-3333-444444444444"
        chat_id = await self.make_chat("alpha", session_id)
        self.write_transcript(session_id, [assistant("once", session_id)])

        first = await self.sweep()
        second = await self.sweep()

        self.assertEqual(first["changed"], {chat_id: 1})
        self.assertEqual(second["changed"], {}, "a re-read must import nothing")
        self.assertEqual(len(await db.messages_get(chat_id)), 1)

    async def test_only_what_was_appended_is_imported(self):
        session_id = "cccccccc-1111-2222-3333-444444444444"
        chat_id = await self.make_chat("alpha", session_id)
        path = self.write_transcript(session_id, [assistant("first", session_id)])
        await self.sweep()

        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(assistant("second", session_id)) + "\n")
        result = await self.sweep()

        self.assertEqual(result["changed"], {chat_id: 1})
        self.assertEqual(
            [m["content"] for m in await db.messages_get(chat_id)], ["first", "second"]
        )

    async def test_an_empty_transcript_reports_no_change(self):
        session_id = "dddddddd-1111-2222-3333-444444444444"
        await self.make_chat("alpha", session_id)
        self.write_transcript(session_id, [])

        result = await self.sweep()

        self.assertEqual(result["changed"], {})
        self.assertEqual(result["scanned"], 1, "it was still looked at")


class SidebarCanReorderTests(SyncAllBase):
    """A sweep that imports turns but leaves updated_at alone is invisible."""

    async def test_updated_at_moves_when_a_turn_arrives(self):
        session_id = "eeeeeeee-1111-2222-3333-444444444444"
        chat_id = await self.make_chat("alpha", session_id)
        self.write_transcript(session_id, [assistant("new work", session_id)])
        before = (await db.chat_get(chat_id, "admin"))["updated_at"]

        # db._now() has one-second resolution, so a create and a sync inside
        # the same second share a timestamp and this assertion would be
        # measuring the clock rather than the code.
        with patch.object(db, "_now", return_value="2099-01-01T00:00:00Z"):
            await self.sweep()

        after = (await db.chat_get(chat_id, "admin"))["updated_at"]
        self.assertNotEqual(before, after, "the sidebar cannot reorder without this")

    async def test_updated_at_is_left_alone_when_nothing_arrived(self):
        """Otherwise every sweep would shuffle the list for no reason."""
        session_id = "ffffffff-1111-2222-3333-444444444444"
        chat_id = await self.make_chat("alpha", session_id)
        self.write_transcript(session_id, [])
        before = (await db.chat_get(chat_id, "admin"))["updated_at"]

        with patch.object(db, "_now", return_value="2099-01-01T00:00:00Z"):
            await self.sweep()

        self.assertEqual((await db.chat_get(chat_id, "admin"))["updated_at"], before)


class ScopeTests(SyncAllBase):

    async def test_an_unlinked_conversation_is_not_scanned(self):
        """No session id means no transcript; scanning it is a wasted stat."""
        await self.make_chat("no session", None)

        result = await self.sweep()

        self.assertEqual(result["scanned"], 0)
        self.assertEqual(result["changed"], {})

    async def test_another_owners_conversation_is_not_touched(self):
        session_id = "99999999-1111-2222-3333-444444444444"
        theirs = await self.make_chat("theirs", session_id, owner="someone-else")
        self.write_transcript(session_id, [assistant("private", session_id)])

        result = await self.sweep(user="admin")

        self.assertEqual(result["scanned"], 0, "the sweep is owner-scoped")
        self.assertEqual(await db.messages_get(theirs), [])

    async def test_every_linked_conversation_is_swept_not_just_the_first(self):
        sessions = ["1111aaaa-1111-2222-3333-444444444444",
                    "2222bbbb-1111-2222-3333-444444444444"]
        ids = []
        for i, s in enumerate(sessions):
            ids.append(await self.make_chat(f"chat{i}", s))
            self.write_transcript(s, [assistant(f"turn {i}", s)])

        result = await self.sweep()

        self.assertEqual(result["scanned"], 2)
        self.assertEqual(set(result["changed"]), set(ids))


class ResilienceTests(SyncAllBase):

    async def test_one_unreadable_transcript_does_not_cost_the_sweep(self):
        """A sweep is all-or-nothing only if you write it that way."""
        bad = "0000aaaa-1111-2222-3333-444444444444"
        good = "0000bbbb-1111-2222-3333-444444444444"
        await self.make_chat("bad", bad)
        good_id = await self.make_chat("good", good)
        self.write_transcript(bad, [assistant("unreachable", bad)])
        self.write_transcript(good, [assistant("reachable", good)])

        real = app._sync_linked_chat

        async def explode(chat):
            if chat.get("session_id") == bad:
                raise OSError("permission denied")
            return await real(chat)

        with patch.object(app, "_sync_linked_chat", explode):
            result = await self.sweep()

        self.assertEqual(result["changed"], {good_id: 1},
                         "the healthy conversation must still be imported")
        self.assertEqual(len(await db.messages_get(good_id)), 1)

    async def test_a_missing_transcript_is_not_an_error(self):
        """A session whose file was deleted is normal, not exceptional."""
        await self.make_chat("gone", "dead0000-1111-2222-3333-444444444444")

        result = await self.sweep()

        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["changed"], {})


if __name__ == "__main__":
    unittest.main()
