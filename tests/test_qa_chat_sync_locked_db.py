"""QA: a locked database must not turn a sync poll into a 500.

`POST /api/chats/{id}/sync` is polled every few seconds for as long as a
session-linked conversation is open. Its only write is bookkeeping — advancing
`chats.transcript_offset` to record how far the transcript has been read.

Two writers on one SQLite file is enough to make that write fail: observed
live when a second uvicorn started against the production database, which
produced a 500 with a 200-line ASGI traceback per poll, and a red error in the
user's browser console, for a condition that clears on its own.

The distinction these tests pin down:

* `sqlite3.OperationalError` ("database is locked") is retryable. The route
  reports no rows and says so in the log; the offset was not advanced by the
  write that failed, so the next poll re-reads the same range and loses
  nothing.
* Anything else still surfaces. `handle_chats_sync_all` catches bare
  `Exception` because one unreadable transcript must not cost a whole sweep,
  but this route serves the one conversation the user is looking at, and a
  real fault there should not be silently reported as "nothing new".

The second test is the one that keeps the first honest: widening the catch to
bare `Exception` — the obvious "fix" if this ever fails again — would make a
genuinely broken sync indistinguishable from an idle one.
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.responses import JSONResponse

from routes import chats as chat_routes


class _Request:
    """Only what handle_chat_sync reads off the request."""

    def __init__(self, user: str = "admin"):
        self.state = type("S", (), {"session": {"user": user}})()


_CHAT = {"id": "chat-1", "session_id": "sess-1", "user": "admin"}


class SyncToleratesALockedDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, sync_side_effect):
        with patch.object(chat_routes.db, "chat_get",
                          AsyncMock(return_value=dict(_CHAT))), \
                patch.object(chat_routes, "_sync_linked_chat",
                             AsyncMock(side_effect=sync_side_effect)):
            return await chat_routes.handle_chat_sync(_Request(), "chat-1")

    async def test_a_locked_database_is_reported_as_nothing_to_sync(self):
        response = await self._call(
            sqlite3.OperationalError("database is locked"))
        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 200)

    async def test_the_response_still_says_the_chat_is_linked(self):
        """`linked: False` would tell the page to stop polling entirely, which
        is the opposite of what a retryable failure calls for."""
        import json

        response = await self._call(
            sqlite3.OperationalError("database is locked"))
        body = json.loads(bytes(response.body))
        self.assertEqual(body["messages"], [])
        self.assertTrue(body["linked"])
        self.assertTrue(body["retry"])

    async def test_the_failure_is_logged_rather_than_swallowed_silently(self):
        with self.assertLogs(chat_routes._log, level="WARNING") as caught:
            await self._call(sqlite3.OperationalError("database is locked"))
        self.assertTrue(
            any("sync_failed" in line and "chat-1" in line
                for line in caught.output),
            f"the chat id must be greppable in the log, got {caught.output}",
        )

    async def test_a_non_retryable_failure_still_surfaces(self):
        """The guard against widening the catch to bare Exception."""
        with self.assertRaises(ValueError):
            await self._call(ValueError("something genuinely broken"))

    async def test_a_healthy_sync_is_unaffected(self):
        import json

        with patch.object(chat_routes.db, "chat_get",
                          AsyncMock(return_value=dict(_CHAT))), \
                patch.object(chat_routes, "_sync_linked_chat",
                             AsyncMock(return_value=[("assistant", "hello")])):
            response = await chat_routes.handle_chat_sync(_Request(), "chat-1")
        body = json.loads(bytes(response.body))
        self.assertEqual(
            body["messages"], [{"role": "assistant", "content": "hello"}])
        self.assertTrue(body["linked"])
        self.assertNotIn("retry", body)


if __name__ == "__main__":
    unittest.main()
