"""A busy terminal marks the chat driving it, not every chat that shares its id.

The symptom, reported on 2026-09-26: three running terminals produced eight
pulsing rows. Measured against the live console at the time -- eight chats with
`terminal_busy` true, three distinct sessions behind them, and ZERO chats
actually running a turn. Sessions carry two or three chats each, because a
conversation keeps its `session_id` after it stops being the one that session
is working in, and nothing clears the link.

`terminal_busy` was `session_id in busy_sessions`, which is a fact about the
session being read as a fact about the chat.

Which chat is driving cannot come from the session file -- those carry
sessionId, status, pid and cwd, and no chat id -- nor from recency: on the live
data `session_proc` was empty for every chat and two chats on one session
shared an identical last-message second. `routed_requests` does carry it: a row
is written when the console routes a prompt into a session, naming the chat it
came from. That is the same table the usage attribution already trusts.

The limitation, stated rather than hidden: work typed directly into a terminal
window writes no routed_request, so for such a session the driver is genuinely
unknown. The rule marks nothing in that case rather than guessing which of
three conversations a person is typing into -- except where the session carries
exactly one chat, which is unambiguous.
"""
from __future__ import annotations

import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auth
import config
import db

HTTPS = "https://testserver"


def _client():
    from fastapi.testclient import TestClient

    from app import app as web_app
    return TestClient(web_app, raise_server_exceptions=False, base_url=HTTPS,
                      follow_redirects=True)


class TerminalBusyDriverTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

        self.password = secrets.token_urlsafe(16)
        await db.user_create("alice", None, auth.hash_password(self.password))
        row = await db.user_get_by_name("alice")
        self.owner = row["id"]

        # Three conversations, one terminal session -- the shape measured on
        # the live console, where one session carried three chats.
        self.session = "sess-shared"
        self.chats = ["chat-old", "chat-driving", "chat-other"]
        for chat_id in self.chats:
            await db.chat_create(chat_id, chat_id, None,
                                 f"{self.tmp.name}/p", self.owner)
            await db.chat_set_session(chat_id, self.session)

    def _login(self):
        client = _client()
        resp = client.post("/login",
                           json={"username": "alice", "password": self.password})
        self.assertEqual(resp.status_code, 200, "the fixture must log in")
        return client

    def _busy(self, *session_ids):
        """Patch the busy-session probe, so no session files are needed."""
        import routes.chats as chats_routes
        return patch.object(chats_routes, "_busy_terminal_sessions",
                            new=_async_set(set(session_ids)))

    def _marked(self, client) -> set[str]:
        resp = client.get("/api/chats")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        rows = body if isinstance(body, list) else (body.get("chats") or [])
        return {c["id"] for c in rows if c.get("terminal_busy")}

    async def test_only_the_routed_chat_is_marked(self):
        """The reported bug. Three chats, one session, one routed prompt."""
        from routes.db_usage import routed_request_add
        await routed_request_add(self.session, "chat-old", self.owner, 0, "first")
        await routed_request_add(self.session, "chat-driving", self.owner, 10, "later")

        client = self._login()
        with self._busy(self.session):
            marked = self._marked(client)
        self.assertEqual(
            marked, {"chat-driving"},
            "a busy terminal must mark the conversation it is working in, not "
            "every conversation that once shared its session")

    async def test_a_session_with_one_chat_is_unambiguous(self):
        """No routed request, but only one candidate -- marking it is not a
        guess."""
        await db.chat_create("solo", "solo", None, f"{self.tmp.name}/p", self.owner)
        await db.chat_set_session("solo", "sess-solo")

        client = self._login()
        with self._busy("sess-solo"):
            marked = self._marked(client)
        self.assertEqual(marked, {"solo"})

    async def test_nothing_is_marked_when_the_driver_is_unknown(self):
        """Typed straight into the terminal: no routed request exists, and
        three conversations are equally plausible. Marking all three is the
        behaviour being removed; marking one would be a guess reported as a
        fact."""
        client = self._login()
        with self._busy(self.session):
            marked = self._marked(client)
        self.assertEqual(marked, set())

    async def test_an_idle_session_marks_nothing(self):
        from routes.db_usage import routed_request_add
        await routed_request_add(self.session, "chat-driving", self.owner, 1, "x")
        client = self._login()
        with self._busy():
            self.assertEqual(self._marked(client), set())


def _async_set(value):
    async def _probe():
        return value
    return _probe


if __name__ == "__main__":
    unittest.main()
