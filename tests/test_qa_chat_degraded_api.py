"""QA: GET /api/chats and GET /api/chats/{id} must actually surface
`degraded`/`degraded_reason` in their JSON response.

Both columns exist on the `chats` table (see tests/test_qa_chat_degraded.py
for the DB-layer contract) and both handlers build an explicit response
shape rather than serializing the row as-is, so adding a column to the table
does nothing for the sidebar badge (web/assets/chat-list.js) until the two
handlers are updated to actually project it. This is the test that should
have caught that gap and did not, so it drives the fix rather than trailing it.
"""
from __future__ import annotations

import secrets
import tempfile
import unittest
from unittest.mock import patch

import auth
import config
import db

HTTPS = "https://testserver"


def _client(follow_redirects: bool = True):
    from fastapi.testclient import TestClient

    from app import app as web_app
    return TestClient(
        web_app, raise_server_exceptions=False, base_url=HTTPS,
        follow_redirects=follow_redirects,
    )


class ChatDegradedApiShapeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

        self.password = secrets.token_urlsafe(16)
        await db.user_create("alice", None, auth.hash_password(self.password))
        await db.chat_create("c1", "Alice's", None, f"{self.tmp.name}/p", "alice")
        await db.chat_mark_degraded("c1", "usage", "no frame reached the handler")

    def _login(self):
        client = _client()
        response = client.post(
            "/login", json={"username": "alice", "password": self.password}
        )
        self.assertEqual(response.status_code, 200, "the fixture must log in")
        return client

    def test_list_endpoint_carries_degraded_and_reason(self):
        client = self._login()
        r = client.get("/api/chats")
        self.assertEqual(r.status_code, 200)
        chats = {c["id"]: c for c in r.json()["chats"]}
        self.assertIn("c1", chats)
        self.assertTrue(chats["c1"]["degraded"])
        self.assertIn("usage:", chats["c1"]["degraded_reason"])

    def test_single_chat_endpoint_carries_degraded_and_reason(self):
        client = self._login()
        r = client.get("/api/chats/c1")
        self.assertEqual(r.status_code, 200)
        chat = r.json()["chat"]
        self.assertTrue(chat["degraded"])
        self.assertIn("usage:", chat["degraded_reason"])

    async def test_a_never_marked_chat_reports_false_and_none(self):
        await db.chat_create("c2", "Also Alice's", None, f"{self.tmp.name}/p2", "alice")
        client = self._login()

        r_list = client.get("/api/chats")
        c2_list = next(c for c in r_list.json()["chats"] if c["id"] == "c2")
        self.assertFalse(c2_list["degraded"])
        self.assertIsNone(c2_list["degraded_reason"])

        r_single = client.get("/api/chats/c2")
        c2_single = r_single.json()["chat"]
        self.assertFalse(c2_single["degraded"])
        self.assertIsNone(c2_single["degraded_reason"])


if __name__ == "__main__":
    unittest.main()
