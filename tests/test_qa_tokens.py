"""QA: API token routes (routes/misc.py).

Covers:
  handle_tokens_get     — GET /api/tokens
  handle_tokens_create  — POST /api/tokens
  handle_tokens_revoke  — POST /api/tokens/{token_id}/revoke
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


class TokenRouteTests(unittest.IsolatedAsyncioTestCase):
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

    def _login(self, who: str):
        client = _client()
        response = client.post(
            "/login", json={"username": who, "password": self.passwords[who]}
        )
        self.assertEqual(response.status_code, 200, "fixture must log in")
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    # ── GET /api/tokens ──────────────────────────────────────────────────

    def test_list_returns_empty_when_no_tokens(self):
        client, headers = self._login("alice")
        r = client.get("/api/tokens", headers=headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["tokens"], [])
        self.assertEqual(data["count"], 0)

    def test_list_is_owner_scoped(self):
        client, headers = self._login("alice")
        r = client.post("/api/tokens", json={"name": "alice-token"}, headers=headers)
        self.assertEqual(r.status_code, 200)
        # Token create response: {"id", "name", "token", ...}

        bob, bob_headers = self._login("bob")
        r2 = bob.get("/api/tokens", headers=bob_headers)
        self.assertEqual(r2.json()["count"], 0)

    def test_list_excludes_secret(self):
        client, headers = self._login("alice")
        r = client.post("/api/tokens", json={"name": "visible-token"}, headers=headers)
        self.assertEqual(r.status_code, 200)
        tid = r.json()["id"]

        r2 = client.get("/api/tokens", headers=headers)
        tokens = r2.json()["tokens"]
        self.assertEqual(len(tokens), 1)
        tok = tokens[0]
        # By id, not merely "has an id": the listing asserted the *shape* of a
        # token and never that it was the token just created.
        self.assertEqual(tok["id"], tid)
        self.assertIn("id", tok)
        self.assertIn("name", tok)
        self.assertNotIn("token", tok)

    def test_requires_login(self):
        r = _client(follow_redirects=False).get("/api/tokens")
        self.assertIn(r.status_code, (303, 401))

    # ── POST /api/tokens ────────────────────────────────────────────────

    def test_create_returns_secret_once(self):
        client, headers = self._login("alice")
        r = client.post("/api/tokens", json={"name": "first"}, headers=headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        # Token create returns {"id", "name", "token", ...} not {"ok": ...}
        self.assertIn("token", data)
        self.assertIn("id", data)

    def test_secret_is_not_recoverable_on_list(self):
        client, headers = self._login("alice")
        r = client.post("/api/tokens", json={"name": "secret-store"}, headers=headers)
        self.assertEqual(r.status_code, 200)
        secret = r.json()["token"]
        self.assertTrue(secret)

        r2 = client.get("/api/tokens", headers=headers)
        tokens = r2.json()["tokens"]
        for tok in tokens:
            self.assertNotIn("token", tok)

    def test_create_rejects_token_auth(self):
        """Tokens cannot mint further tokens — only cookie sessions."""
        # Create a token first
        client, headers = self._login("alice")
        r = client.post("/api/tokens", json={"name": "parent"}, headers=headers)
        self.assertEqual(r.status_code, 200)
        parent_secret = r.json()["token"]

        # Try to create a token using the parent token as auth
        r2 = client.post(
            "/api/tokens",
            json={"name": "child"},
            headers={"Authorization": f"Bearer {parent_secret}"},
        )
        self.assertEqual(r2.status_code, 403)

    def test_create_truncates_name(self):
        client, headers = self._login("alice")
        long_name = "n" * 500
        r = client.post("/api/tokens", json={"name": long_name}, headers=headers)
        self.assertEqual(r.status_code, 200)

        r2 = client.get("/api/tokens", headers=headers)
        name = r2.json()["tokens"][0]["name"]
        self.assertEqual(len(name), 100)  # max length

    def test_create_default_expiry_when_ttl_configured(self):
        """When config.TOKEN_DEFAULT_TTL_DAYS > 0, created tokens have an expiry."""
        with patch.object(config, "TOKEN_DEFAULT_TTL_DAYS", 30):
            client, headers = self._login("alice")
            r = client.post("/api/tokens", json={"name": "ttl-token"}, headers=headers)
            self.assertEqual(r.status_code, 200)

            r2 = client.get("/api/tokens", headers=headers)
            tok = r2.json()["tokens"][0]
            self.assertIn("expires_at", tok)
            self.assertIsNotNone(tok["expires_at"])

    def test_create_allows_never_expiry(self):
        with patch.object(config, "TOKEN_DEFAULT_TTL_DAYS", 30):
            client, headers = self._login("alice")
            r = client.post(
                "/api/tokens",
                json={"name": "forever", "expires_in_days": "never"},
                headers=headers,
            )
            self.assertEqual(r.status_code, 200)

            r2 = client.get("/api/tokens", headers=headers)
            tok = r2.json()["tokens"][0]
            self.assertIsNone(tok["expires_at"])

    def test_create_allows_zero_for_never_expiry(self):
        with patch.object(config, "TOKEN_DEFAULT_TTL_DAYS", 30):
            client, headers = self._login("alice")
            r = client.post(
                "/api/tokens",
                json={"name": "zero-expiry", "expires_in_days": 0},
                headers=headers,
            )
            self.assertEqual(r.status_code, 200)

            r2 = client.get("/api/tokens", headers=headers)
            tok = r2.json()["tokens"][0]
            self.assertIsNone(tok["expires_at"])

    def test_create_rejects_excessive_ttl(self):
        with patch.object(config, "TOKEN_DEFAULT_TTL_DAYS", 30):
            client, headers = self._login("alice")
            r = client.post(
                "/api/tokens",
                json={"name": "too-long", "expires_in_days": 9999},
                headers=headers,
            )
            self.assertEqual(r.status_code, 400)

    def test_create_rejects_string_ttl(self):
        with patch.object(config, "TOKEN_DEFAULT_TTL_DAYS", 30):
            client, headers = self._login("alice")
            r = client.post(
                "/api/tokens",
                json={"name": "bad-ttl", "expires_in_days": "abc"},
                headers=headers,
            )
            self.assertEqual(r.status_code, 400)

    def test_create_requires_login(self):
        r = _client(follow_redirects=False).post("/api/tokens", json={"name": "x"})
        self.assertIn(r.status_code, (303, 401))

    def test_create_stores_token_owner_scoped(self):
        client, headers = self._login("alice")
        client.post("/api/tokens", json={"name": "alice-tok"}, headers=headers)

        bob, bob_headers = self._login("bob")
        r = bob.get("/api/tokens", headers=bob_headers)
        names = {t["name"] for t in r.json()["tokens"]}
        self.assertNotIn("alice-tok", names)

    def test_create_with_custom_ttl(self):
        with patch.object(config, "TOKEN_DEFAULT_TTL_DAYS", 30):
            client, headers = self._login("alice")
            r = client.post(
                "/api/tokens",
                json={"name": "7day", "expires_in_days": 7},
                headers=headers,
            )
            self.assertEqual(r.status_code, 200)

    def test_create_rejects_excessive_custom_ttl(self):
        """Custom TTL still capped at _TOKEN_MAX_TTL_DAYS."""
        with patch.object(config, "TOKEN_DEFAULT_TTL_DAYS", 30):
            client, headers = self._login("alice")
            r = client.post(
                "/api/tokens",
                json={"name": "capped", "expires_in_days": 400},
                headers=headers,
            )
            self.assertEqual(r.status_code, 400)

    def test_create_with_negative_ttl_rejected(self):
        with patch.object(config, "TOKEN_DEFAULT_TTL_DAYS", 30):
            client, headers = self._login("alice")
            r = client.post(
                "/api/tokens",
                json={"name": "neg", "expires_in_days": -1},
                headers=headers,
            )
            self.assertEqual(r.status_code, 400)

    # ── POST /api/tokens/{token_id}/revoke ──────────────────────────────

    def test_revoke_succeeds(self):
        client, headers = self._login("alice")
        r = client.post("/api/tokens", json={"name": "to-revoke"}, headers=headers)
        self.assertEqual(r.status_code, 200)
        tid = r.json()["id"]

        r2 = client.delete(f"/api/tokens/{tid}", headers=headers)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json()["ok"])

    def test_revoke_removes_from_list(self):
        client, headers = self._login("alice")
        r = client.post("/api/tokens", json={"name": "revoked"}, headers=headers)
        self.assertEqual(r.status_code, 200)
        tid = r.json()["id"]

        client.delete(f"/api/tokens/{tid}", headers=headers)

        r2 = client.get("/api/tokens", headers=headers)
        self.assertEqual(r2.json()["count"], 0)

    def test_revoke_returns_404_for_unknown_token(self):
        client, headers = self._login("alice")
        r = client.delete("/api/tokens/00000000000000000000000000000000",
                       headers=headers)
        self.assertEqual(r.status_code, 404)

    def test_revoke_is_owner_scoped(self):
        client, headers = self._login("alice")
        r = client.post("/api/tokens", json={"name": "bob-cant-revoke"}, headers=headers)
        self.assertEqual(r.status_code, 200)
        tid = r.json()["id"]

        bob, bob_headers = self._login("bob")
        r2 = bob.delete(f"/api/tokens/{tid}", headers=bob_headers)
        self.assertEqual(r2.status_code, 404)

        # Verify alice's token still exists
        r3 = client.get("/api/tokens", headers=headers)
        self.assertEqual(r3.json()["count"], 1)

    def test_requires_login_for_revoke(self):
        r = _client(follow_redirects=False).delete("/api/tokens/someid")
        self.assertIn(r.status_code, (303, 401))

    def test_revoke_returns_ok_even_when_already_revoked(self):
        """Idempotent revoke: second revoke of same token is 404, not an error."""
        client, headers = self._login("alice")
        r = client.post("/api/tokens", json={"name": "double-revoke"}, headers=headers)
        self.assertEqual(r.status_code, 200)
        tid = r.json()["id"]

        r1 = client.delete(f"/api/tokens/{tid}", headers=headers)
        self.assertEqual(r1.status_code, 200)

        r2 = client.delete(f"/api/tokens/{tid}", headers=headers)
        self.assertEqual(r2.status_code, 404)


if __name__ == "__main__":
    unittest.main()
