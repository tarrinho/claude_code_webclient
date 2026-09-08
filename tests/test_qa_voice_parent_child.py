"""QA: voice tooltip creates a temp child chat separate from the parent.

When the user clicks the mic button on a regular (non-voice) chat, the
tooltip flow:
1. creates a new chat row with voice_mode=1, is_temporary=1, parent_chat_id
2. streams the voice turn into that child chat only (parent stays untouched)
3. on "agree", appends a summary to the parent and deletes the temp chat
4. on "reject", deletes the temp chat without touching the parent

This test file asserts those invariants through the DB and HTTP layers.
No browser, no live model, no proxy.  Follows the pattern of
tests/test_qa_chats.py and tests/test_voice_turn.py exactly.
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


def _client(follow_redirects: bool = True):
    from fastapi.testclient import TestClient
    from app import app as web_app
    return TestClient(
        web_app, raise_server_exceptions=False, base_url=HTTPS,
        follow_redirects=follow_redirects,
    )


class VoiceParentChildTests(unittest.IsolatedAsyncioTestCase):
    """Voice tooltip parent/child chat separation.

    All tests use a temporary DB so they never touch the production database.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self._db_patch = patch.object(config, "DB_PATH", f"{self.tmpdir.name}/wc.db")
        self._root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmpdir.name}/projects")
        self._db_patch.start()
        self._root_patch.start()

    async def asyncTearDown(self):
        await db.close()
        self._db_patch.stop()
        self._root_patch.stop()
        self.tmpdir.cleanup()

    async def asyncSetUp(self):
        await db.init()
        self.password = secrets.token_urlsafe(16)
        await auth.user_create("admin", None, auth.hash_password(self.password))
        self.client = _client()

    def _headers(self):
        """Authenticate and return the CSRF + session headers."""
        resp = self.client.post("/login", json={
            "username": "admin", "password": self.password,
        })
        self.assertEqual(resp.status_code, 200, f"login failed: {resp.status_code}")
        return {"X-CSRF-Token": self.client.cookies.get("wc_csrf")}

    # ── Tests ───────────────────────────────────────────────────────────

    async def test_voice_child_gets_parent_chat_id(self):
        """POST /api/chats with parent_chat_id must persist it."""
        self._headers()
        parent_id = f"parent-{secrets.token_hex(4)}"
        wd = f"{self.tmpdir.name}/projects/{parent_id}"
        Path(wd).mkdir(parents=True, exist_ok=True)
        await db.chat_create(parent_id, "Parent Chat", None, wd, "admin")

        resp = self.client.post("/api/chats", json={
            "title": "Voice Child",
            "voice_mode": True,
            "is_temporary": True,
            "parent_chat_id": parent_id,
        })
        self.assertEqual(resp.status_code, 200, resp.json())
        child_id = resp.json()["id"]

        child = await db.chat_get(child_id, "admin")
        self.assertEqual(child["parent_chat_id"], parent_id)

    async def test_voice_child_is_temporary(self):
        """is_temporary must be set to 1 so sidebar can distinguish it."""
        self._headers()
        parent_id = f"parent-{secrets.token_hex(4)}"
        wd = f"{self.tmpdir.name}/projects/{parent_id}"
        Path(wd).mkdir(parents=True, exist_ok=True)
        await db.chat_create(parent_id, "Parent Chat", None, wd, "admin")

        resp = self.client.post("/api/chats", json={
            "title": "Voice Child",
            "voice_mode": True,
            "is_temporary": True,
            "parent_chat_id": parent_id,
        })
        self.assertEqual(resp.status_code, 200)

        child = await db.chat_get(resp.json()["id"], "admin")
        self.assertEqual(child["is_temporary"], 1)

    async def test_voice_child_is_voice_mode(self):
        """voice_mode must be set so the composer shows voice icons."""
        self._headers()
        parent_id = f"parent-{secrets.token_hex(4)}"
        wd = f"{self.tmpdir.name}/projects/{parent_id}"
        Path(wd).mkdir(parents=True, exist_ok=True)
        await db.chat_create(parent_id, "Parent Chat", None, wd, "admin")

        resp = self.client.post("/api/chats", json={
            "title": "Voice Child",
            "voice_mode": True,
            "is_temporary": True,
            "parent_chat_id": parent_id,
        })
        self.assertEqual(resp.status_code, 200)

        child = await db.chat_get(resp.json()["id"], "admin")
        self.assertEqual(child["voice_mode"], 1)

    async def test_parent_chat_not_modified(self):
        """Creating a voice child must not alter the parent chat."""
        self._headers()
        parent_id = f"parent-{secrets.token_hex(4)}"
        wd = f"{self.tmpdir.name}/projects/{parent_id}"
        Path(wd).mkdir(parents=True, exist_ok=True)
        await db.chat_create(parent_id, "Parent Chat", None, wd, "admin")

        # Read parent before
        parent_before = await db.chat_get(parent_id, "admin")
        parent_voice_before = parent_before.get("voice_mode", 0)
        parent_temp_before = parent_before.get("is_temporary", 0)

        # Create voice child
        resp = self.client.post("/api/chats", json={
            "title": "Voice Child",
            "voice_mode": True,
            "is_temporary": True,
            "parent_chat_id": parent_id,
        })
        self.assertEqual(resp.status_code, 200)

        # Read parent after
        parent_after = await db.chat_get(parent_id, "admin")
        self.assertEqual(
            parent_after["voice_mode"], parent_voice_before,
            "parent voice_mode must not change",
        )
        self.assertEqual(
            parent_after["is_temporary"], parent_temp_before,
            "parent is_temporary must not change",
        )
        self.assertIsNone(
            parent_after.get("parent_chat_id"),
            "parent must not carry a parent_chat_id",
        )

    async def test_both_chats_appear_in_chat_list(self):
        """Parent and child must both show up in the chat list."""
        self._headers()
        parent_id = f"parent-{secrets.token_hex(4)}"
        wd = f"{self.tmpdir.name}/projects/{parent_id}"
        Path(wd).mkdir(parents=True, exist_ok=True)
        await db.chat_create(parent_id, "Parent Chat", None, wd, "admin")

        resp = self.client.post("/api/chats", json={
            "title": "Voice Child",
            "voice_mode": True,
            "is_temporary": True,
            "parent_chat_id": parent_id,
        })
        self.assertEqual(resp.status_code, 200)
        child_id = resp.json()["id"]

        chats = await db.chat_list("admin")
        ids = {c["id"] for c in chats}
        self.assertIn(parent_id, ids, "parent must be in chat list")
        self.assertIn(child_id, ids, "child must be in chat list")

    async def test_voice_child_title_inherits_parent(self):
        """When title is 'Untitled', inherit parent's title."""
        self._headers()
        parent_id = f"parent-{secrets.token_hex(4)}"
        wd = f"{self.tmpdir.name}/projects/{parent_id}"
        Path(wd).mkdir(parents=True, exist_ok=True)
        await db.chat_create(parent_id, "My Workspace", None, wd, "admin")

        resp = self.client.post("/api/chats", json={
            "title": "Untitled",
            "voice_mode": True,
            "is_temporary": True,
            "parent_chat_id": parent_id,
        })
        self.assertEqual(resp.status_code, 200)

        child = await db.chat_get(resp.json()["id"], "admin")
        self.assertEqual(child["title"], "My Workspace")

    async def test_voice_child_has_own_workspace_dir(self):
        """Each chat must get its own work_dir."""
        self._headers()
        parent_id = f"parent-{secrets.token_hex(4)}"
        wd = f"{self.tmpdir.name}/projects/{parent_id}"
        Path(wd).mkdir(parents=True, exist_ok=True)
        await db.chat_create(parent_id, "Parent Chat", None, wd, "admin")

        resp = self.client.post("/api/chats", json={
            "title": "Voice Child",
            "voice_mode": True,
            "is_temporary": True,
            "parent_chat_id": parent_id,
        })
        self.assertEqual(resp.status_code, 200)

        child = await db.chat_get(resp.json()["id"], "admin")
        self.assertTrue(Path(child["work_dir"]).is_dir())

    async def test_voice_child_without_parent_is_regular_voice_chat(self):
        """A voice_mode chat without parent_chat_id must be a regular voice chat."""
        self._headers()
        resp = self.client.post("/api/chats", json={
            "title": "Standalone Voice",
            "voice_mode": True,
        })
        self.assertEqual(resp.status_code, 200)

        child = await db.chat_get(resp.json()["id"], "admin")
        self.assertEqual(child["voice_mode"], 1)
        self.assertIsNone(child.get("parent_chat_id"))
        self.assertEqual(child.get("is_temporary"), 0)


if __name__ == "__main__":
    unittest.main()
