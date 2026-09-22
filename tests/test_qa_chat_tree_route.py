"""QA: GET /api/chats carries the composed tree."""
from __future__ import annotations

import secrets
import tempfile
import unittest
from unittest.mock import patch

import auth
import config
import db

HTTPS = "https://testserver"


def _client():
    from fastapi.testclient import TestClient

    from app import app as web_app
    return TestClient(web_app, raise_server_exceptions=False, base_url=HTTPS)


class ChatsListTreeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for attr, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            patcher = patch.object(config, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        self.password = secrets.token_urlsafe(16)
        await db.user_create("alice", None, auth.hash_password(self.password))
        row = await db.user_get_by_name("alice")
        self.owner = row["id"]
        import pathlib
        pathlib.Path(f"{self.tmp.name}/p").mkdir(parents=True, exist_ok=True)
        await db.chat_create("parent", "Parent", None, f"{self.tmp.name}/p",
                             self.owner)

    def _login(self):
        client = _client()
        response = client.post(
            "/login", json={"username": "alice", "password": self.password})
        self.assertEqual(response.status_code, 200, "the fixture must log in")
        return client

    def test_every_chat_carries_a_children_list(self):
        response = self._login().get("/api/chats")
        self.assertEqual(response.status_code, 200, response.text)
        chats = response.json()["chats"]
        self.assertTrue(chats)
        for chat in chats:
            self.assertIn("children", chat)
            self.assertIsInstance(chat["children"], list)

    async def test_a_recorded_subagent_appears_as_a_child(self):
        await db.subagent_record("parent", [
            {"tool_use_id": "tu_1", "agent_type": "code-review",
             "description": "review", "status": "done",
             "started_at": "2026-09-21T10:00:00Z",
             "ended_at": "2026-09-21T10:00:30Z"}])
        response = self._login().get("/api/chats")
        parent = next(c for c in response.json()["chats"] if c["id"] == "parent")
        self.assertEqual(len(parent["children"]), 1)
        self.assertEqual(parent["children"][0]["kind"], "subagent")
        self.assertEqual(parent["children"][0]["agent_type"], "code-review")

    async def test_a_voice_child_is_nested_and_not_also_a_root(self):
        await db.chat_create("kid", "Voice", None, f"{self.tmp.name}/p",
                             self.owner)
        # chat_update's signature is (chat_id, owner_id, **fields) -- the
        # owner is positional and not optional.
        await db.chat_update("kid", self.owner, parent_chat_id="parent")
        response = self._login().get("/api/chats")
        chats = response.json()["chats"]
        self.assertNotIn("kid", [c["id"] for c in chats])
        parent = next(c for c in chats if c["id"] == "parent")
        self.assertEqual([k["id"] for k in parent["children"]], ["kid"])
