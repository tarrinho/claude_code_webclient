"""QA: session delete route (routes/misc.py).

Covers:
  handle_session_delete — DELETE /api/sessions/{session_id}
"""
from __future__ import annotations
import unittest

import json
import secrets
import tempfile
from pathlib import Path
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


class SessionDeleteRouteTests(unittest.IsolatedAsyncioTestCase):
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
        }
        await db.user_create("alice", None, auth.hash_password(self.passwords["alice"]))

        # Create a fake claude session file that WebConsole wrote.
        self.session_dir = Path(self.tmp.name) / "sessions"
        self.session_dir.mkdir()
        self._dir_patch = patch.object(db, "_CLAUDE_SESSIONS_DIR", self.session_dir)
        self._dir_patch.start()
        _write_fake_session(self.session_dir, "sess-write.json",
                           sessionId="sess-write", name="Write session", pid=99999,
                           entrypoint="webconsole")

    def _login(self, who: str):
        client = _client()
        response = client.post(
            "/login", json={"username": who, "password": self.passwords[who]}
        )
        self.assertEqual(response.status_code, 200, "fixture must log in")
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    def test_delete_webconsole_record_succeeds(self):
        """A session file written by WebConsole can be deleted."""
        client, headers = self._login("alice")
        r = client.delete("/api/sessions/sess-write", headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertFalse((self.session_dir / "test-session.json").exists())

    def test_delete_returns_404_for_unknown_session(self):
        client, headers = self._login("alice")
        r = client.delete("/api/sessions/nonexistent", headers=headers)
        self.assertEqual(r.status_code, 404)

    def test_delete_requires_login(self):
        r = _client(follow_redirects=False).delete("/api/sessions/sess-x")
        self.assertIn(r.status_code, (303, 401))

    def test_refuses_to_delete_non_webconsole_file(self):
        """A file not written by WebConsole (no webconsole signature) is
        refused with a 409 conflict."""
        _write_fake_session(self.session_dir, "sess-cli.json",
                           sessionId="sess-cli", name="CLI session", pid=99998,
                           entrypoint="")
        client, headers = self._login("alice")
        r = client.delete("/api/sessions/sess-cli", headers=headers)
        self.assertEqual(r.status_code, 409)

    def test_path_traversal_is_sanitized(self):
        """The session_id must be sanitized to prevent path traversal."""
        client, headers = self._login("alice")
        r = client.delete("/api/sessions/../../etc/passwd.json", headers=headers)
        self.assertNotEqual(r.status_code, 200)


class SessionDeleteDBTests(unittest.TestCase):
    """db.delete_claude_session_file tests for the non-route path."""

    def test_returns_true_when_webconsole_record_exists(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sessions_dir = Path(tmp.name) / "sessions"
        sessions_dir.mkdir()
        _write_fake_session(sessions_dir, "sess-db.json",
                           sessionId="sess-db", name="DB test", pid=99999,
                           entrypoint="webconsole")
        with patch.object(db, "_CLAUDE_SESSIONS_DIR", sessions_dir):
            result = db.delete_claude_session_file("sess-db")
        self.assertTrue(result)
        self.assertFalse((sessions_dir / "sess-db.json").exists())

    def test_returns_false_for_nonexistent_session(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sessions_dir = Path(tmp.name) / "sessions"
        sessions_dir.mkdir()
        with patch.object(db, "_CLAUDE_SESSIONS_DIR", sessions_dir):
            result = db.delete_claude_session_file("sess-nope")
        self.assertFalse(result)


def _write_fake_session(root: Path, filename: str, **fields) -> Path:
    payload = {
        "sessionId": fields.pop("sessionId", "sess-default"),
        "name": fields.pop("name", "A session"),
        "kind": fields.pop("kind", "interactive"),
        "cwd": fields.pop("cwd", "/home/kali/projects"),
        "pid": fields.pop("pid", 99999),
    }
    payload.update(fields)
    path = root / filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
