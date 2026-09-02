"""PUT/GET /api/chats/{id}/auto-answer -- step 3 of the auto-answer knob.

Design: docs/superpowers/specs/2026-09-02-auto-answer-knob-design.md

The property under test that matters most: these routes arm an automatic
approver of permission prompts, so a cross-owner write must not land. Five
db.py functions once accepted owner_id and never used it (fixed in ec548d1);
this is the shape of thing that regression would have made dangerous rather
than merely wrong.
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


class AutoAnswerRouteTests(unittest.IsolatedAsyncioTestCase):
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
        await db.chat_create("c1", "Alice's", None, f"{self.tmp.name}/p", "alice")

    def _login(self, who: str):
        client = _client()
        response = client.post(
            "/login", json={"username": who, "password": self.passwords[who]}
        )
        self.assertEqual(response.status_code, 200, "the fixture must log in")
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    # ── the owner can arm and read it ───────────────────────────────────

    def test_the_owner_can_turn_it_on(self):
        client, headers = self._login("alice")
        r = client.put("/api/chats/c1/auto-answer", json={"enabled": True},
                       headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["enabled"])

        r = client.get("/api/chats/c1/auto-answer", headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["enabled"])
        self.assertEqual(r.json()["log"], [])

    def test_it_is_off_by_default(self):
        client, headers = self._login("alice")
        r = client.get("/api/chats/c1/auto-answer", headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["enabled"])

    def test_turning_it_off_again(self):
        client, headers = self._login("alice")
        client.put("/api/chats/c1/auto-answer", json={"enabled": True}, headers=headers)
        r = client.put("/api/chats/c1/auto-answer", json={"enabled": False},
                       headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["enabled"])

    # ── the security property ────────────────────────────────────────────

    def test_another_user_cannot_arm_alices_chat(self):
        client, headers = self._login("bob")
        r = client.put("/api/chats/c1/auto-answer", json={"enabled": True},
                       headers=headers)
        self.assertEqual(r.status_code, 404, r.text)

        # And the write must not have landed underneath the 404.
        alice, alice_headers = self._login("alice")
        r2 = alice.get("/api/chats/c1/auto-answer", headers=alice_headers)
        self.assertFalse(r2.json()["enabled"], "bob's request armed alice's chat")

    def test_another_user_cannot_read_it_either(self):
        alice, alice_headers = self._login("alice")
        alice.put("/api/chats/c1/auto-answer", json={"enabled": True},
                  headers=alice_headers)

        bob, bob_headers = self._login("bob")
        r = bob.get("/api/chats/c1/auto-answer", headers=bob_headers)
        self.assertEqual(r.status_code, 404)

    def test_an_unknown_chat_is_404(self):
        client, headers = self._login("alice")
        r = client.put("/api/chats/does-not-exist/auto-answer",
                       json={"enabled": True}, headers=headers)
        self.assertEqual(r.status_code, 404)

    # ── request shape ────────────────────────────────────────────────────

    def test_a_non_boolean_enabled_is_rejected(self):
        client, headers = self._login("alice")
        r = client.put("/api/chats/c1/auto-answer", json={"enabled": "yes"},
                       headers=headers)
        self.assertEqual(r.status_code, 400)

    def test_a_missing_enabled_is_rejected(self):
        client, headers = self._login("alice")
        r = client.put("/api/chats/c1/auto-answer", json={}, headers=headers)
        self.assertEqual(r.status_code, 400)

    def test_it_requires_login(self):
        r = _client(follow_redirects=False).get("/api/chats/c1/auto-answer")
        self.assertIn(r.status_code, (303, 401))

    # ── the log surfaces ─────────────────────────────────────────────────

    async def test_the_log_is_returned_newest_first(self):
        await db.chat_auto_answer_set("c1", "alice", True)
        await db.chat_auto_answer_log_append("c1", {"label": "first"})
        await db.chat_auto_answer_log_append("c1", {"label": "second"})
        client, headers = self._login("alice")
        r = client.get("/api/chats/c1/auto-answer", headers=headers)
        labels = [e["label"] for e in r.json()["log"]]
        self.assertEqual(labels, ["second", "first"])


if __name__ == "__main__":
    unittest.main()
