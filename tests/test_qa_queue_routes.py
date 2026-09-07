"""QA: queue route handlers (routes/chats.py).

Covers:
  handle_queue_list     — GET /api/chats/{id}/queue
  handle_queue_delete   — DELETE /api/chats/{id}/queue/{queue_id}
  handle_queue_release  — POST /api/chats/{id}/queue/{queue_id}/release
"""
from __future__ import annotations
import unittest

import secrets
import tempfile
from unittest.mock import patch

import auth
import config
import db
import turns

HTTPS = "https://testserver"


def _client(follow_redirects: bool = True):
    from fastapi.testclient import TestClient

    from app import app as web_app
    return TestClient(
        web_app, raise_server_exceptions=False, base_url=HTTPS,
        follow_redirects=follow_redirects,
    )


class QueueRouteTests(unittest.IsolatedAsyncioTestCase):
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

        self.passwords = {
            "alice": secrets.token_urlsafe(16),
            "bob": secrets.token_urlsafe(16),
        }
        await db.user_create("alice", None, auth.hash_password(self.passwords["alice"]))
        await db.user_create(
            "bob", None, auth.hash_password(self.passwords["bob"]), role="user"
        )
        self.chat_id = "q-chat-1"
        await db.chat_create(self.chat_id, "Queue test", None,
                            f"{self.tmp.name}/p", "alice")

    def _login(self, who: str):
        client = _client()
        response = client.post(
            "/login", json={"username": who, "password": self.passwords[who]}
        )
        self.assertEqual(response.status_code, 200, "fixture must log in")
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    async def _insert_queue_item(self, prompt: str, owner: str) -> int:
        """Add a queue item and return its DB id."""
        await db.queue_add(self.chat_id, owner, prompt, None)
        cur = await db.db_conn.execute(
            "SELECT MAX(id) AS mid FROM turn_queue WHERE chat_id = ? AND owner_id = ?",
            (self.chat_id, owner),
        )
        row = await cur.fetchone()
        return row["mid"] if row and row["mid"] else 0

    # ── queue_list ───────────────────────────────────────────────────────

    def test_list_returns_empty_when_no_queue(self):
        client, headers = self._login("alice")
        r = client.get(f"/api/chats/{self.chat_id}/queue", headers=headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["queue"], [])
        self.assertIn("max", data)
        self.assertIn("running", data)

    def test_list_returns_404_for_unknown_chat(self):
        client, headers = self._login("alice")
        r = client.get("/api/chats/nonexistent/queue", headers=headers)
        self.assertEqual(r.status_code, 404)

    async def test_list_is_owner_scoped(self):
        """Each user can only see queue items in chats they own."""
        # Create a chat owned by bob (signature: chat_create(chat_id, title, desc, work_dir, owner_id))
        bob_chat_id = "q-chat-bob"
        await db.chat_create(bob_chat_id, "Bob's chat", None, f"{self.tmp.name}/p", "bob")

        alice_client, alice_headers = self._login("alice")
        bob, bob_headers = self._login("bob")

        # Insert items in each user's own chat
        await db.queue_add(self.chat_id, "alice", "alice-prompt", None)
        await db.queue_add(bob_chat_id, "bob", "bob-prompt", None)

        # alice sees her own chat's queue
        r1 = alice_client.get(f"/api/chats/{self.chat_id}/queue",
                              headers=alice_headers)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(len(r1.json()["queue"]), 1)

        # bob sees his own chat's queue
        r2 = bob.get(f"/api/chats/{bob_chat_id}/queue", headers=bob_headers)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(len(r2.json()["queue"]), 1)

    def test_list_requires_login(self):
        r = _client(follow_redirects=False).get(f"/api/chats/{self.chat_id}/queue")
        self.assertIn(r.status_code, (303, 401))

    # ── queue_delete ─────────────────────────────────────────────────────

    async def test_delete_removes_prompt(self):
        client, headers = self._login("alice")
        row_id = await self._insert_queue_item("delete-me", "alice")
        self.assertNotEqual(row_id, 0)

        r = client.delete(f"/api/chats/{self.chat_id}/queue/{row_id}", headers=headers)
        self.assertEqual(r.status_code, 200)

        r2 = client.get(f"/api/chats/{self.chat_id}/queue", headers=headers)
        self.assertEqual(r2.json()["queue"], [])

    def test_delete_returns_404_for_unknown_queue_id(self):
        client, headers = self._login("alice")
        r = client.delete(f"/api/chats/{self.chat_id}/queue/99999", headers=headers)
        self.assertEqual(r.status_code, 404)

    def test_delete_returns_404_for_unknown_chat(self):
        client, headers = self._login("alice")
        r = client.delete("/api/chats/nonexistent/queue/1", headers=headers)
        self.assertEqual(r.status_code, 404)

    async def test_delete_is_owner_scoped(self):
        """Bob can only delete his own items; alice's item stays."""
        alice_client, alice_headers = self._login("alice")
        row_id = await self._insert_queue_item("protected", "alice")

        bob, bob_headers = self._login("bob")
        r = bob.delete(f"/api/chats/{self.chat_id}/queue/{row_id}", headers=bob_headers)
        self.assertEqual(r.status_code, 404)

        # alice should still see her item
        r2 = alice_client.get(f"/api/chats/{self.chat_id}/queue", headers=alice_headers)
        self.assertEqual(len(r2.json()["queue"]), 1)

    def test_delete_requires_login(self):
        r = _client(follow_redirects=False).delete(f"/api/chats/{self.chat_id}/queue/1")
        self.assertIn(r.status_code, (303, 401))

    # ── queue_release ───────────────────────────────────────────────────

    async def test_release_returns_ok_when_not_running(self):
        """When nothing is running, release starts immediately."""
        client, headers = self._login("alice")
        row_id = await self._insert_queue_item("release-me", "alice")

        r = client.post(f"/api/chats/{self.chat_id}/queue/{row_id}/release",
                       headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_release_returns_404_for_unknown_queue_id(self):
        client, headers = self._login("alice")
        r = client.post(f"/api/chats/{self.chat_id}/queue/99999/release",
                       headers=headers)
        self.assertEqual(r.status_code, 404)

    def test_release_returns_404_for_unknown_chat(self):
        client, headers = self._login("alice")
        r = client.post("/api/chats/nonexistent/queue/1/release", headers=headers)
        self.assertEqual(r.status_code, 404)

    async def test_release_is_owner_scoped(self):
        """Bob can only release items in his own chat."""
        alice_client, alice_headers = self._login("alice")
        row_id = await self._insert_queue_item("protected-release", "alice")

        bob, bob_headers = self._login("bob")
        r = bob.post(f"/api/chats/{self.chat_id}/queue/{row_id}/release",
                    headers=bob_headers)
        self.assertEqual(r.status_code, 404)

        # alice can still release it
        r2 = alice_client.post(f"/api/chats/{self.chat_id}/queue/{row_id}/release",
                              headers=alice_headers)
        self.assertEqual(r2.status_code, 200)

    def test_release_requires_login(self):
        r = _client(follow_redirects=False).post(f"/api/chats/{self.chat_id}/queue/1/release")
        self.assertIn(r.status_code, (303, 401))


if __name__ == "__main__":
    unittest.main()
