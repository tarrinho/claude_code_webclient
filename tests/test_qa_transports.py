"""QA: SSH transport CRUD routes (routes/transports.py).

Covers:
  handle_transports_list   — GET /api/transports
  handle_transport_get     — GET /api/transports/{id}
  handle_transport_create  — POST /api/transports
  handle_transport_patch   — PATCH /api/transports/{id}
  handle_transport_delete  — DELETE /api/transports/{id}
  handle_transport_test_raw — POST /api/transports/test
  handle_transport_test_saved — POST /api/transports/{id}/test
"""
from __future__ import annotations

import secrets
import tempfile
import unittest
import uuid
from unittest.mock import patch

import auth
import config
import db
from routes import db_transports

HTTPS = "https://testserver"


def _client(follow_redirects: bool = True):
    from fastapi.testclient import TestClient

    from app import app as web_app
    return TestClient(
        web_app, raise_server_exceptions=False, base_url=HTTPS,
        follow_redirects=follow_redirects,
    )


class TransportRouteTests(unittest.IsolatedAsyncioTestCase):
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
        self.transport_id = uuid.uuid4().hex

    def _login(self, who: str):
        client = _client()
        response = client.post(
            "/login", json={"username": who, "password": self.passwords[who]}
        )
        self.assertEqual(response.status_code, 200, "fixture must log in")
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    # ── list ─────────────────────────────────────────────────────────────

    def test_list_returns_empty_when_none_created(self):
        client, headers = self._login("alice")
        r = client.get("/api/transports", headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["transports"], [])

    async def test_list_is_shared_across_owners(self):
        """Transports became a shared pool on 2026-09-11 -- every account
        sees every transport. This used to assert the opposite
        (test_list_is_owner_scoped); see ssh_transports_list's docstring in
        routes/db_transports.py."""
        client, headers = self._login("alice")
        bob, bob_headers = self._login("bob")

        # Create transport for alice
        await db_transports.ssh_transport_create(
            self.transport_id, "alice-t", "alice", "host.example.com",
            "kali", "/home/kali/.ssh/id_rsa",
        )

        # Both alice and bob see it.
        r = client.get("/api/transports", headers=headers)
        self.assertEqual(len(r.json()["transports"]), 1)

        r2 = bob.get("/api/transports", headers=bob_headers)
        self.assertEqual(len(r2.json()["transports"]), 1)

    def test_requires_login(self):
        r = _client(follow_redirects=False).get("/api/transports")
        self.assertIn(r.status_code, (303, 401))

    # ── create ───────────────────────────────────────────────────────────

    def test_create_succeeds_with_valid_payload(self):
        client, headers = self._login("alice")
        r = client.post("/api/transports", json={
            "name": "my-server",
            "ssh_host": "192.168.1.100",
            "ssh_user": "deploy",
            "ssh_key_path": "/root/.ssh/id_ed25519",
        }, headers=headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["name"], "my-server")
        self.assertIn("id", data)

    def test_create_defaults_ssh_user_to_kali(self):
        client, headers = self._login("alice")
        r = client.post("/api/transports", json={
            "name": "default-user",
            "ssh_host": "10.0.0.1",
            "ssh_key_path": "/root/.ssh/key",
        }, headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_create_rejects_missing_name(self):
        client, headers = self._login("alice")
        r = client.post("/api/transports", json={
            "ssh_host": "10.0.0.1",
            "ssh_key_path": "/root/.ssh/key",
        }, headers=headers)
        self.assertEqual(r.status_code, 400)

    def test_create_rejects_missing_ssh_host(self):
        client, headers = self._login("alice")
        r = client.post("/api/transports", json={
            "name": "test",
            "ssh_key_path": "/root/.ssh/key",
        }, headers=headers)
        self.assertEqual(r.status_code, 400)

    def test_create_rejects_missing_ssh_key_path(self):
        client, headers = self._login("alice")
        r = client.post("/api/transports", json={
            "name": "test",
            "ssh_host": "10.0.0.1",
        }, headers=headers)
        self.assertEqual(r.status_code, 400)

    def test_create_truncates_name_to_100_chars(self):
        client, headers = self._login("alice")
        long_name = "a" * 200
        r = client.post("/api/transports", json={
            "name": long_name,
            "ssh_host": "10.0.0.1",
            "ssh_key_path": "/root/.ssh/key",
        }, headers=headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "a" * 100)

    def test_create_rejects_invalid_host(self):
        client, headers = self._login("alice")
        r = client.post("/api/transports", json={
            "name": "test",
            "ssh_host": "http://evil.com/../../etc/passwd",
            "ssh_key_path": "/root/.ssh/key",
        }, headers=headers)
        self.assertEqual(r.status_code, 400)

    def test_requires_login_for_create(self):
        r = _client(follow_redirects=False).post("/api/transports", json={
            "name": "test",
            "ssh_host": "10.0.0.1",
            "ssh_key_path": "/root/.ssh/key",
        })
        self.assertIn(r.status_code, (303, 401))

    async def test_create_is_visible_to_every_owner(self):
        """Transports became a shared pool on 2026-09-11 -- a transport
        alice creates is visible to bob too. This used to assert the
        opposite (test_create_stores_in_db_owner_scoped); see
        ssh_transports_list's docstring in routes/db_transports.py."""
        client, headers = self._login("alice")
        r = client.post("/api/transports", json={
            "name": "visible-to-alice",
            "ssh_host": "10.0.0.1",
            "ssh_key_path": "/root/.ssh/key",
        }, headers=headers)
        self.assertEqual(r.status_code, 200)
        tid = r.json()["id"]

        bob, bob_headers = self._login("bob")
        r2 = bob.get("/api/transports", headers=bob_headers)
        listed = r2.json()["transports"]
        names = {t["name"] for t in listed}
        self.assertIn("visible-to-alice", names)
        self.assertIn(tid, {t["id"] for t in listed})

        # ssh_transports_list takes an owner argument but no longer filters
        # on it -- any value returns the whole shared pool.
        rows = await db_transports.ssh_transports_list("alice")
        names2 = {t["name"] for t in rows}
        self.assertIn("visible-to-alice", names2)
        self.assertEqual(len(rows), 1)

    # ── get ──────────────────────────────────────────────────────────────

    async def test_get_returns_transport(self):
        client, headers = self._login("alice")
        tid = uuid.uuid4().hex
        await db_transports.ssh_transport_create(
            tid, "g-transport", "alice", "10.0.0.2", "kali", "/root/.ssh/key",
        )
        r2 = client.get(f"/api/transports/{tid}", headers=headers)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["name"], "g-transport")

    def test_get_returns_404_for_unknown_id(self):
        client, headers = self._login("alice")
        r = client.get("/api/transports/00000000000000000000000000000000",
                       headers=headers)
        self.assertEqual(r.status_code, 404)

    async def test_get_200_for_another_users_transport(self):
        """Transports became a shared pool on 2026-09-11 -- any account can
        read any transport. This used to assert a 404
        (test_get_404_for_another_users_transport); see ssh_transport_get's
        docstring in routes/db_transports.py."""
        client, headers = self._login("alice")
        tid = uuid.uuid4().hex
        await db_transports.ssh_transport_create(
            tid, "bob-transport", "bob", "10.0.0.3", "kali", "/root/.ssh/key",
        )

        bob, bob_headers = self._login("bob")
        r2 = bob.get(f"/api/transports/{tid}", headers=bob_headers)
        self.assertEqual(r2.status_code, 200)

        r3 = client.get(f"/api/transports/{tid}", headers=headers)
        self.assertEqual(r3.status_code, 200)

    def test_get_requires_login(self):
        r = _client(follow_redirects=False).get("/api/transports/someid")
        self.assertIn(r.status_code, (303, 401))

    # ── patch ────────────────────────────────────────────────────────────

    async def test_patch_updates_name(self):
        client, headers = self._login("alice")
        tid = uuid.uuid4().hex
        await db_transports.ssh_transport_create(
            tid, "old-name", "alice", "10.0.0.1", "kali", "/root/.ssh/key",
        )
        r2 = client.patch(f"/api/transports/{tid}", json={"name": "new-name"},
                          headers=headers)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json()["ok"])

    async def test_patch_rejects_unknown_field(self):
        client, headers = self._login("alice")
        tid = uuid.uuid4().hex
        await db_transports.ssh_transport_create(
            tid, "test", "alice", "10.0.0.1", "kali", "/root/.ssh/key",
        )
        r2 = client.patch(f"/api/transports/{tid}", json={"fake_field": 42},
                          headers=headers)
        self.assertEqual(r2.status_code, 400)

    async def test_patch_rejects_non_string_values(self):
        client, headers = self._login("alice")
        tid = uuid.uuid4().hex
        await db_transports.ssh_transport_create(
            tid, "test", "alice", "10.0.0.1", "kali", "/root/.ssh/key",
        )
        r2 = client.patch(f"/api/transports/{tid}", json={"ssh_user": 123},
                          headers=headers)
        self.assertEqual(r2.status_code, 400)

    async def test_patch_accepts_null_for_text_fields(self):
        """null values alongside valid fields are accepted — handler returns 200.

        The route filters out null fields before calling the DB update, so the
        request must carry at least one real value to exercise the null path
        without triggering the "no valid fields" 404.
        """
        client, headers = self._login("alice")
        tid = uuid.uuid4().hex
        await db_transports.ssh_transport_create(
            tid, "test", "alice", "10.0.0.1", "kali", "/root/.ssh/key",
        )
        r2 = client.patch(f"/api/transports/{tid}",
                          json={"ssh_user": None, "name": "newname"},
                          headers=headers)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json()["ok"])

    async def test_patch_rejects_empty_ssh_host(self):
        client, headers = self._login("alice")
        tid = uuid.uuid4().hex
        await db_transports.ssh_transport_create(
            tid, "test", "alice", "10.0.0.1", "kali", "/root/.ssh/key",
        )
        r2 = client.patch(f"/api/transports/{tid}", json={"ssh_host": "  "},
                          headers=headers)
        self.assertEqual(r2.status_code, 400)

    def test_patch_requires_login(self):
        r = _client(follow_redirects=False).patch(
            "/api/transports/someid", json={"name": "x"})
        self.assertIn(r.status_code, (303, 401))

    # ── delete ───────────────────────────────────────────────────────────

    async def test_delete_succeeds_when_no_machines_reference(self):
        client, headers = self._login("alice")
        tid = uuid.uuid4().hex
        await db_transports.ssh_transport_create(
            tid, "del-test", "alice", "10.0.0.1", "kali", "/root/.ssh/key",
        )
        r2 = client.delete(f"/api/transports/{tid}", headers=headers)
        self.assertEqual(r2.status_code, 200)

    def test_delete_requires_login(self):
        r = _client(follow_redirects=False).delete("/api/transports/someid")
        self.assertIn(r.status_code, (303, 401))

    async def test_delete_works_for_any_owner(self):
        """Transports became a shared pool on 2026-09-11 -- any account can
        delete any transport. This used to assert alice's delete was
        refused (test_delete_is_owner_scoped); see ssh_transport_delete's
        docstring in routes/db_transports.py."""
        client, headers = self._login("alice")
        tid = uuid.uuid4().hex
        await db_transports.ssh_transport_create(
            tid, "bob-only", "bob", "10.0.0.1", "kali", "/root/.ssh/key",
        )

        r1 = client.delete(f"/api/transports/{tid}", headers=headers)
        self.assertEqual(r1.status_code, 200)

    def test_delete_returns_404_for_unknown_transport(self):
        client, headers = self._login("alice")
        r = client.delete("/api/transports/nonexistent", headers=headers)
        self.assertEqual(r.status_code, 404)

    # ── test raw ─────────────────────────────────────────────────────────

    def test_test_raw_rejects_invalid_host(self):
        client, headers = self._login("alice")
        r = client.post("/api/transports/test", json={
            "ssh_host": "http://evil.com/../../etc/passwd",
            "ssh_user": "kali",
            "ssh_key_path": "/root/.ssh/key",
        }, headers=headers)
        self.assertEqual(r.status_code, 400)

    def test_test_raw_returns_result_structure(self):
        client, headers = self._login("alice")
        r = client.post("/api/transports/test", json={
            "ssh_host": "127.0.0.1",
            "ssh_user": "kali",
            "ssh_key_path": "/root/.ssh/key",
        }, headers=headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("ok", data)
        self.assertIn("error", data)

    def test_test_raw_requires_login(self):
        r = _client(follow_redirects=False).post("/api/transports/test", json={
            "ssh_host": "127.0.0.1",
        })
        self.assertIn(r.status_code, (303, 401))

    # ── test saved ───────────────────────────────────────────────────────

    async def test_test_saved_returns_result_for_existing_transport(self):
        client, headers = self._login("alice")
        tid = uuid.uuid4().hex
        await db_transports.ssh_transport_create(
            tid, "test-saved", "alice", "127.0.0.1", "kali", "/root/.ssh/key",
        )
        r2 = client.post(f"/api/transports/{tid}/test", headers=headers)
        self.assertEqual(r2.status_code, 200)
        data = r2.json()
        self.assertIn("ok", data)

    def test_test_saved_returns_404_for_unknown_transport(self):
        client, headers = self._login("alice")
        r = client.post("/api/transports/00000000000000000000000000000000/test",
                       headers=headers)
        self.assertEqual(r.status_code, 404)

    def test_test_saved_requires_login(self):
        r = _client(follow_redirects=False).post(
            "/api/transports/someid/test")
        self.assertIn(r.status_code, (303, 401))


if __name__ == "__main__":
    unittest.main()
