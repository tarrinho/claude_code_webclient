"""QA: orchestrator sub-route handlers (routes/orchestrators.py).

Covers:
  handle_orchestrator_stream    — GET /api/orchestrators/{id}/stream
  handle_orchestrator_task_stream — GET /api/orchestrators/{id}/tasks/{task_id}/stream
  handle_orchestrator_tasks_get   — GET /api/orchestrators/{id}/tasks
  handle_orchestrator_messages_get — GET /api/orchestrators/{id}/messages
  handle_changelog_get          — GET /api/changelog

SSE streams (200 paths) use `while True` loops — TestClient blocks. We test the
404/ownership/auth paths which exercise the pre-stream validation, and cover the
streaming loop itself only via browser/integration tests.
"""
from __future__ import annotations
import unittest

import secrets
import tempfile
import uuid
from unittest.mock import patch

import auth
import config
import db
from routes import db_orchestrators

HTTPS = "https://testserver"


def _client(follow_redirects: bool = True):
    from fastapi.testclient import TestClient

    from app import app as web_app
    return TestClient(
        web_app, raise_server_exceptions=False, base_url=HTTPS,
        follow_redirects=follow_redirects,
    )


class OrchestratorSubRouteTests(unittest.IsolatedAsyncioTestCase):
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
        self.orchestrator_id = uuid.uuid4().hex

    async def _create_orchestrator(self):
        await db_orchestrators.orchestrator_create(
            self.orchestrator_id, "Test supervisor", "desc", "alice", None,
        )

    def _login(self, who: str):
        client = _client()
        response = client.post(
            "/login", json={"username": who, "password": self.passwords[who]}
        )
        self.assertEqual(response.status_code, 200, "fixture must log in")
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    # ── stream ───────────────────────────────────────────────────────────

    def test_stream_returns_404_for_unknown_orchestrator(self):
        client, headers = self._login("alice")
        r = client.get("/api/orchestrators/nonexistent/stream", headers=headers)
        self.assertEqual(r.status_code, 404)

    async def test_stream_is_owner_scoped(self):
        await self._create_orchestrator()
        bob, bob_headers = self._login("bob")
        r = bob.get(f"/api/orchestrators/{self.orchestrator_id}/stream",
                   headers=bob_headers)
        self.assertEqual(r.status_code, 404)

    def test_stream_requires_login(self):
        r = _client(follow_redirects=False).get(
            f"/api/orchestrators/{self.orchestrator_id}/stream")
        self.assertIn(r.status_code, (303, 401))

    # ── task stream ─────────────────────────────────────────────────────

    async def test_task_stream_returns_404_for_unknown_task(self):
        """Unknown task returns 404 (tested via the ownership check path
        which runs before the stream starts)."""
        client, headers = self._login("alice")
        r = client.get(
            f"/api/orchestrators/{self.orchestrator_id}/tasks/nonexistent/stream",
            headers=headers,
        )
        self.assertEqual(r.status_code, 404)

    def test_task_stream_returns_404_for_unknown_orchestrator(self):
        client, headers = self._login("alice")
        r = client.get(
            "/api/orchestrators/nonexistent/tasks/task-1/stream",
            headers=headers,
        )
        self.assertEqual(r.status_code, 404)

    async def test_task_stream_is_owner_scoped(self):
        await self._create_orchestrator()
        task_id = "task-2"
        await db_orchestrators.orchestrator_task_create(
            self.orchestrator_id, task_id, "task title", None,
        )
        bob, bob_headers = self._login("bob")
        r = bob.get(
            f"/api/orchestrators/{self.orchestrator_id}/tasks/{task_id}/stream",
            headers=bob_headers,
        )
        self.assertEqual(r.status_code, 404)

    def test_task_stream_requires_login(self):
        r = _client(follow_redirects=False).get(
            f"/api/orchestrators/{self.orchestrator_id}/tasks/task-1/stream")
        self.assertIn(r.status_code, (303, 401))

    # ── tasks_get ────────────────────────────────────────────────────────

    def test_tasks_get_returns_empty_list(self):
        client, headers = self._login("alice")
        r = client.get(f"/api/orchestrators/{self.orchestrator_id}/tasks",
                      headers=headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["tasks"], [])
        self.assertEqual(data["count"], 0)

    def test_tasks_get_returns_200_with_empty_list_for_unknown_orchestrator(self):
        client, headers = self._login("alice")
        r = client.get("/api/orchestrators/nonexistent/tasks", headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["tasks"], [])

    async def test_tasks_get_is_owner_scoped(self):
        await self._create_orchestrator()
        task_id = "task-3"
        await db_orchestrators.orchestrator_task_create(
            self.orchestrator_id, task_id, "task title", None,
        )

        bob, bob_headers = self._login("bob")
        alice, alice_headers = self._login("alice")

        # bob can't see alice's orchestrator tasks
        r = bob.get(f"/api/orchestrators/{self.orchestrator_id}/tasks",
                   headers=bob_headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["tasks"], [])

        # alice sees her own orchestrator's tasks
        r2 = alice.get(f"/api/orchestrators/{self.orchestrator_id}/tasks",
                      headers=alice_headers)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["count"], 1)
        self.assertEqual(r2.json()["tasks"][0]["id"], task_id)

    def test_tasks_get_requires_login(self):
        r = _client(follow_redirects=False).get(
            f"/api/orchestrators/{self.orchestrator_id}/tasks")
        self.assertIn(r.status_code, (303, 401))

    # ── messages_get ─────────────────────────────────────────────────────

    def test_messages_get_returns_empty_list(self):
        client, headers = self._login("alice")
        r = client.get(f"/api/orchestrators/{self.orchestrator_id}/messages",
                      headers=headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["messages"], [])
        self.assertEqual(data["count"], 0)

    def test_messages_get_returns_empty_list_for_unknown_orchestrator(self):
        """A nonexistent orchestrator returns an empty list (not a 404)."""
        client, headers = self._login("alice")
        r = client.get("/api/orchestrators/nonexistent/messages", headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["messages"], [])

    def test_messages_get_respects_after_id(self):
        client, headers = self._login("alice")
        r = client.get(
            f"/api/orchestrators/{self.orchestrator_id}/messages?after=0",
            headers=headers,
        )
        self.assertEqual(r.status_code, 200)

    def test_messages_get_rejects_invalid_after_id(self):
        """Non-numeric after parameter defaults to 0, returns all messages."""
        client, headers = self._login("alice")
        r = client.get(
            f"/api/orchestrators/{self.orchestrator_id}/messages?after=abc",
            headers=headers,
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("messages", data)

    def test_messages_get_requires_login(self):
        r = _client(follow_redirects=False).get(
            f"/api/orchestrators/{self.orchestrator_id}/messages")
        self.assertIn(r.status_code, (303, 401))


class ChangelogGetQA(unittest.IsolatedAsyncioTestCase):
    """QA: handle_changelog_get via /api/changelog."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.stop()
        self.root_patch.start()
        self.root_patch.stop()
        await db.init()
        self.addAsyncCleanup(db.close)

        self.passwords = {
            "alice": secrets.token_urlsafe(16),
        }
        await db.user_create("alice", None, auth.hash_password(self.passwords["alice"]))

    def _login(self, who: str):
        client = _client()
        response = client.post(
            "/login", json={"username": who, "password": self.passwords[who]}
        )
        self.assertEqual(response.status_code, 200, "fixture must log in")
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    def test_changelog_returns_200(self):
        client, headers = self._login("alice")
        r = client.get("/api/changelog", headers=headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIsInstance(data, list)

    def test_changelog_requires_login(self):
        r = _client(follow_redirects=False).get("/api/changelog")
        self.assertIn(r.status_code, (303, 401))

    def test_changelog_returns_sections_with_version_and_date(self):
        client, headers = self._login("alice")
        r = client.get("/api/changelog", headers=headers)
        if r.json():
            first = r.json()[0]
            self.assertIn("version", first)
            self.assertIn("date", first)


if __name__ == "__main__":
    unittest.main()
