"""QA: dismissing a highlight over HTTP actually removes it from the feed.

The unit tests around this call ``classify_chat`` directly, and the browser
tests click the control. Neither covered the join between them: the request the
control sends, the mark it writes, and the feed that is supposed to stop
listing the row. The bug lived exactly there -- the mark was written under one
key and read under another, so every layer was individually correct and the
feature did not work.

So these drive the two real handlers in sequence:

    GET  /api/supervisor        -> the row is listed as waiting
    POST /api/supervisor/read   -> exactly what the dismiss control sends
    GET  /api/supervisor        -> the row is no longer waiting

and they do it for the shape production has: a conversation linked to a CLI
session, which is what every highlighted row on this machine turned out to be.
The earlier browser tests seeded a bare chat with no linked session, which is
handled by a different branch -- so they passed against code that could not
work for any real row.
"""
from __future__ import annotations

import datetime
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app
import auth
import config
import db


async def _dismissed_at(owner: str = "admin") -> str:
    """The timestamp the dismissal actually recorded, read back from the DB."""
    marks = await db.read_marks_get(owner)
    mark = marks.get(("chat", CHAT_ID)) or {}
    stamp = mark.get("dismissed_at")
    assert stamp, "no dismissal was recorded, so there is nothing to be later than"
    return stamp


def _after(stamp: str, seconds: int = 1) -> str:
    """*stamp* plus *seconds*, in the same shape ``db._now()`` produces.

    The feed suppresses `stamp <= dismissed_at`, so "later" has to be strictly
    later; matching to the second and relying on ordering within it would make
    the case a coin toss.
    """
    moment = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc
    )
    return (moment + datetime.timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


CHAT_ID = "c0ffee00c0ffee00c0ffee00c0ffee00"
SESSION_ID = "8db15c35-74bc-4670-a617-ad2ff0426ec4"
# Deliberately NOT a question. A message ending in "?" is classified by
# _attention() and handled by an earlier branch of classify_chat -- one that
# reads the chat's own mark and always worked. The branch that was broken is
# reached only when the newest message is *routine output* and the linked CLI
# session has stopped being busy, which is what every real highlighted row
# looked like. A question here makes all of this pass against the bug.
ASK = "Let me study the existing test files before writing."


def _request(body=None, method="GET", path="/api/supervisor"):
    return SimpleNamespace(
        method=method,
        url=SimpleNamespace(path=path),
        cookies={},
        headers={"accept": "*/*"},
        query_params={},
        client=SimpleNamespace(host="127.0.0.1"),
        state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
        json=AsyncMock(return_value=body if body is not None else {}),
    )


def _cli_session(**over):
    """A CLI session as db.read_claude_sessions reports one.

    status_updated_at is empty on purpose: every non-busy session on the real
    machine has no such field, and requiring one is what made the dismissal
    guard fail open. A fixture that supplied it would have hidden the bug.
    """
    base = {
        "id": SESSION_ID, "sessionId": SESSION_ID, "name": "cweb2",
        "kind": "interactive", "entrypoint": "", "live": True,
        "status": "waiting", "status_updated_at": "",
    }
    base.update(over)
    return base


class DismissEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self._root = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self._db.start()
        self._root.start()
        await db.init()
        await auth.bootstrap_admin()
        await db.chat_create(CHAT_ID, "cweb2", None, "/tmp", "admin")
        await db.chat_set_session(CHAT_ID, SESSION_ID)
        await db.db_conn.execute(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?,?,?,?)",
            (CHAT_ID, "assistant", ASK, "2026-08-31T19:00:00Z"),
        )
        await db.db_conn.commit()
        self._cli = patch.object(
            db, "read_claude_sessions", AsyncMock(return_value=[_cli_session()])
        )
        self._cli.start()
        # The transcript store is not what this is about; keep it quiet.
        self._recent = patch.object(
            app.transcripts, "list_recent", AsyncMock(return_value=[])
        )
        self._recent.start()

    async def asyncTearDown(self):
        self._recent.stop()
        self._cli.stop()
        await db.close()
        self._db.stop()
        self._root.stop()
        self.tmp.cleanup()

    async def _feed(self):
        return json.loads((await app.handle_supervisor(_request())).body)

    async def _dismiss(self, kind="chat", ref_id=CHAT_ID):
        """Byte for byte what the row's dismiss control sends."""
        return json.loads((await app.handle_supervisor_read(
            _request({"kind": kind, "id": ref_id, "dismiss": True},
                     method="POST", path="/api/supervisor/read")
        )).body)

    def _waiting_ids(self, feed):
        return [e["id"] for e in feed.get("waiting", [])]

    async def test_the_conversation_is_highlighted_to_begin_with(self):
        """Guards everything below: no highlight, nothing to dismiss."""
        self.assertIn(CHAT_ID, self._waiting_ids(await self._feed()))

    async def test_dismissing_it_removes_it_from_the_highlights(self):
        """The whole point, over the wire the control actually uses."""
        self.assertIn(CHAT_ID, self._waiting_ids(await self._feed()))
        result = await self._dismiss()
        self.assertTrue(result.get("ok"))
        self.assertNotIn(
            CHAT_ID, self._waiting_ids(await self._feed()),
            "the dismissal was accepted and the row stayed highlighted",
        )

    async def test_dismissing_also_marks_it_read(self):
        """It leaves the feed entirely, not just the highlights.

        db.read_mark_set writes the same timestamp to read_at and
        dismissed_at, so a dismissal is also a read -- and a read retires
        routine output. The row therefore does not reappear in the quiet
        "with new output" count either. Asserted because it is the documented
        contract, not because it seemed tidy: the first version of this test
        claimed the opposite and was inventing a requirement.
        """
        await self._dismiss()
        feed = await self._feed()
        everywhere = [e["id"] for section in ("waiting", "working", "updated")
                      for e in feed.get(section, [])]
        self.assertNotIn(CHAT_ID, everywhere)
        mark = await db.read_marks_get("admin")
        entry = mark.get(("chat", CHAT_ID), {})
        self.assertTrue(entry.get("dismissed_at"), "no dismissal recorded")
        self.assertEqual(entry.get("read_at"), entry.get("dismissed_at"),
                         "dismissing must mark it read with the same stamp")

    async def test_dismissing_one_leaves_another_highlighted(self):
        """A per-row control that silences the rest is the clear-all button."""
        other = "d00dfeed" * 4
        await db.chat_create(other, "cweb9", None, "/tmp", "admin")
        # A plain question, so this one is highlighted by the _attention()
        # branch rather than the linked-session one. Two different routes into
        # the highlights is the point: dismissing a row on one must not silence
        # a row on the other.
        await db.db_conn.execute(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?,?,?,?)",
            (other, "assistant", "Which way do you want it?", "2026-08-31T19:00:00Z"),
        )
        await db.db_conn.commit()
        self.assertIn(other, self._waiting_ids(await self._feed()))
        await self._dismiss()
        remaining = self._waiting_ids(await self._feed())
        self.assertNotIn(CHAT_ID, remaining)
        self.assertIn(other, remaining, "dismissing one silenced the others")

    async def test_a_later_ask_brings_it_back(self):
        """Dismissing silences what was there, not everything after it."""
        await self._dismiss()
        self.assertNotIn(CHAT_ID, self._waiting_ids(await self._feed()))
        # Derived from the dismissal, never a literal. This read
        # "2026-09-01T09:00:00Z", which is a *later* ask only while the wall
        # clock is behind 09:00Z on 2026-09-01: `read_mark_set` stamps the
        # dismissal with the real `_now()`, and the feed suppresses anything
        # with `stamp <= dismissed_at`. So the case passed all morning and then
        # failed permanently at 09:00Z, for a reason nothing in the assertion
        # message points at. A fixture that expires is worse than a flaky one --
        # it is green until it is broken for ever.
        later = _after(await _dismissed_at())
        await db.db_conn.execute(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?,?,?,?)",
            (CHAT_ID, "assistant", ASK, later),
        )
        await db.db_conn.commit()
        self.assertIn(
            CHAT_ID, self._waiting_ids(await self._feed()),
            "a new ask after the dismissal must summon again, or the control "
            "is a permanent mute",
        )

    async def test_it_survives_a_second_read_of_the_feed(self):
        """The row returned on the *next* poll, so one read proves nothing."""
        await self._dismiss()
        for attempt in range(3):
            with self.subTest(poll=attempt):
                self.assertNotIn(CHAT_ID, self._waiting_ids(await self._feed()))


class DismissRequestValidationTests(unittest.IsolatedAsyncioTestCase):
    """The endpoint the control posts to, on its unhappy paths."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self._root = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/projects")
        self._db.start()
        self._root.start()
        await db.init()
        await auth.bootstrap_admin()

    async def asyncTearDown(self):
        await db.close()
        self._db.stop()
        self._root.stop()
        self.tmp.cleanup()

    async def _post(self, body):
        from fastapi import HTTPException
        try:
            response = await app.handle_supervisor_read(
                _request(body, method="POST", path="/api/supervisor/read"))
        except HTTPException as exc:
            return exc.status_code
        return response.status_code

    async def test_an_unknown_kind_is_rejected(self):
        self.assertEqual(await self._post({"kind": "wombat", "id": CHAT_ID}), 400)

    async def test_a_missing_id_is_rejected(self):
        self.assertEqual(await self._post({"kind": "chat", "id": ""}), 400)

    async def test_a_traversal_shaped_id_is_rejected(self):
        """The id reaches a read-mark key; it must not carry a path."""
        self.assertEqual(
            await self._post({"kind": "chat", "id": "../../etc/passwd"}), 400)

    async def test_both_kinds_the_feed_emits_are_accepted(self):
        """A row is either a conversation or a terminal session; both dismiss."""
        for kind, ref in (("chat", CHAT_ID), ("session", SESSION_ID)):
            with self.subTest(kind=kind):
                self.assertEqual(await self._post({"kind": kind, "id": ref}), 200)


if __name__ == "__main__":
    unittest.main()
