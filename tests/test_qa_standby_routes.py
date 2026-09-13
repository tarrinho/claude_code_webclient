"""QA: POST /api/chats/{id}/standby route error details.

Covers the four failure paths that the frontend must surface to the user:
* 404 - chat not found
* 400 - chat has no linked session (session_id is missing)
* 400 - session name cannot be resolved from ~/.claude/sessions/*.json
* 500 - standby script fails (return code != 0)
* 200 - success, includes resume_command

The frontend (web/assets/app.js:standbyChat) reads response.json().error
on non-200, so every error must include an "error" field in the body.
The original bug: the frontend threw a generic "Could not standby conversation"
without reading the API response body, so users never saw which path failed.

All tests use local temp state. No live model, proxy, or network service.
No real sleep processes: the standby script path is exercised via mocks.
"""
from __future__ import annotations

import json
import secrets
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import auth
import config
import db
from routes import db_chats

HTTPS = "https://testserver"


def _client(follow_redirects: bool = True):
    from fastapi.testclient import TestClient

    from app import app as web_app
    return TestClient(
        web_app, raise_server_exceptions=False, base_url=HTTPS,
        follow_redirects=follow_redirects,
    )


class StandbyRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
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
        self.chat_id = uuid.uuid4().hex
        # The session["user"] is the UUID, not the username, so chat rows
        # must be owned by the UUID to be found by chat_get.
        self.alice_id = (await db.user_get_by_name("alice"))["id"]

        # Session files that _find_session_name() reads from ~/.claude/sessions/*.json
        self.claude_sessions = Path.home() / ".claude" / "sessions"
        self.real_session_id = "cweb-real-0000-0000-0000-000000000001"
        (self.claude_sessions).mkdir(parents=True, exist_ok=True)
        (self.claude_sessions / f"{self.real_session_id}.json").write_text(
            json.dumps({
                "pid": 1,
                "name": "cweb-real",
                "kind": "interactive",
                "entrypoint": "cli",
                "status": "idle",
                "sessionId": self.real_session_id,
                "cwd": str(Path.home()),
            })
        )

    def _login(self, who: str):
        client = _client()
        response = client.post(
            "/login", json={"username": who, "password": self.passwords[who]}
        )
        self.assertEqual(response.status_code, 200, "fixture must log in")
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    async def _create_chat(self, session_id=None, owner=None, work_dir=None):
        """Create a chat row with optional session_id (mimics test_qa_chats.py pattern)."""
        owner = owner or self.alice_id
        wd = work_dir or f"{self.tmp.name}/projects/{self.chat_id}"
        Path(wd).mkdir(parents=True, exist_ok=True)
        await db_chats.chat_create(self.chat_id, "Test chat", "desc", wd, owner)
        if session_id is not None:
            await db.db_conn.execute(
                "UPDATE chats SET session_id = ? WHERE id = ? AND owner_id = ?",
                (session_id, self.chat_id, owner),
            )
            await db.db_conn.commit()

    # -- 404: chat not found --

    async def test_404_when_chat_does_not_exist(self):
        """Unknown chat_id returns 404 with error detail in body."""
        client, headers = self._login("alice")
        r = client.post("/api/chats/nonexistent-uuid/standby", headers=headers)
        self.assertEqual(r.status_code, 404, r.text)
        body = r.json()
        self.assertIn("error", body)
        self.assertIn("Chat not found", body["error"])

    async def test_404_when_chat_belongs_to_other_user(self):
        """Bob cannot standby Alice's chat."""
        chat_id_bob = uuid.uuid4().hex
        bob_id = (await db.user_get_by_name("bob"))["id"]
        wd_bob = f"{self.tmp.name}/projects/{chat_id_bob}"
        Path(wd_bob).mkdir(parents=True, exist_ok=True)
        await db_chats.chat_create(chat_id_bob, "Bob's chat", "desc", wd_bob, bob_id)
        await db.db_conn.execute(
            "UPDATE chats SET session_id = ? WHERE id = ? AND owner_id = ?",
            ("abc123", chat_id_bob, bob_id),
        )
        await db.db_conn.commit()
        alice_client, alice_headers = self._login("alice")
        r = alice_client.post(f"/api/chats/{chat_id_bob}/standby", headers=alice_headers)
        self.assertEqual(r.status_code, 404, r.text)
        body = r.json()
        self.assertIn("error", body)

    # -- 400: no linked session --

    async def test_400_when_chat_has_no_session_id(self):
        """A chat created without session_id cannot be stood by."""
        await self._create_chat(session_id=None)
        client, headers = self._login("alice")
        r = client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)
        self.assertEqual(r.status_code, 400, r.text)
        body = r.json()
        self.assertIn("error", body)
        self.assertIn("no linked session", body["error"].lower())

    # -- 400: cannot resolve session name --

    async def test_400_when_session_id_not_in_claude_sessions(self):
        """A valid session_id that doesn't match ~/.claude/sessions/*.json."""
        fake_sid = uuid.uuid4().hex
        await self._create_chat(session_id=fake_sid)
        client, headers = self._login("alice")

        r = client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)
        self.assertEqual(r.status_code, 400, r.text)
        body = r.json()
        self.assertIn("error", body)
        self.assertIn("session name", body["error"].lower())

    # -- 500: standby script fails --

    async def test_500_when_standby_script_fails(self):
        """The route returns 500 with the script's stderr in detail."""
        await self._create_chat(session_id=self.real_session_id)
        client, headers = self._login("alice")

        mock_communicate = unittest.mock.AsyncMock(return_value=(b"", b"standby failed: process not found"))
        async_mock = unittest.mock.AsyncMock(returncode=1)
        async_mock.communicate = mock_communicate

        with patch(
            "routes.chats.asyncio.create_subprocess_exec",
            return_value=async_mock,
        ):
            r = client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)
            self.assertEqual(r.status_code, 500, r.text)
            body = r.json()
            self.assertIn("error", body)
            self.assertIn("standby script failed", body["error"].lower())

    # -- 200: success --

    async def test_success_returns_ok_and_resume_command(self):
        """Happy path: script succeeds, returns resume_command."""
        await self._create_chat(session_id=self.real_session_id)
        client, headers = self._login("alice")

        async_mock = unittest.mock.AsyncMock(returncode=0)
        async_mock.communicate = unittest.mock.AsyncMock(return_value=(b"", b""))

        with patch(
            "routes.chats.asyncio.create_subprocess_exec",
            return_value=async_mock,
        ):
            r = client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertTrue(body["ok"])
            self.assertTrue(body["standby"])
            self.assertIn("resume_command", body)
            self.assertIn("wc-session-wake.sh", body["resume_command"])

    # -- frontend assertion: error detail in body --

    async def test_error_detail_is_in_response_body_not_silenced(self):
        """Regression test: every error path includes 'error' so the frontend
        can display it. Previously the frontend threw a generic error without
        reading the API response body."""
        # Test 404 path
        client, headers = self._login("alice")
        r = client.post("/api/chats/nonexistent/standby", headers=headers)
        self.assertEqual(r.status_code, 404)
        body = r.json()
        self.assertIn("error", body)
        self.assertIsInstance(body["error"], str)
        self.assertGreater(len(body["error"]), 0)

        # Test 400 path (no session_id)
        await self._create_chat(session_id=None)
        r = client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertIn("error", body)
        self.assertIsInstance(body["error"], str)


if __name__ == "__main__":
    unittest.main()
