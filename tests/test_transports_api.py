from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import auth
import config
import db
from routes import transports as transports_routes


def _client():
    from fastapi.testclient import TestClient
    from app import app as web_app
    return TestClient(web_app, raise_server_exceptions=False, base_url="https://testserver")


class TransportsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.validate_patch = patch.object(transports_routes, "_validate_host", return_value="127.0.0.1")
        self.db_patch.start()
        self.root_patch.start()
        self.validate_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.validate_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        import secrets
        self.password = secrets.token_urlsafe(16)
        await db.user_create("admin", None, auth.hash_password(self.password))

    def _login(self):
        client = _client()
        resp = client.post("/login", json={"username": "admin", "password": self.password})
        self.assertEqual(resp.status_code, 200)
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    async def test_create_then_list(self):
        client, headers = self._login()
        resp = client.post(
            "/api/transports",
            json={"name": "Kali3", "ssh_host": "kali-3.tail850c40.ts.net",
                  "ssh_user": "kali", "ssh_key_path": "~/.ssh/id_ed25519"},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        transport_id = resp.json()["id"]

        resp = client.get("/api/transports", headers=headers)
        self.assertEqual(resp.status_code, 200)
        ids = [t["id"] for t in resp.json()["transports"]]
        self.assertIn(transport_id, ids)

    async def test_create_requires_ssh_host_and_key_path(self):
        client, headers = self._login()
        resp = client.post(
            "/api/transports", json={"name": "x", "ssh_user": "kali"}, headers=headers,
        )
        self.assertEqual(resp.status_code, 400)

    async def test_patch_updates_name(self):
        client, headers = self._login()
        created = client.post(
            "/api/transports",
            json={"name": "Kali3", "ssh_host": "h", "ssh_user": "kali", "ssh_key_path": "k"},
            headers=headers,
        ).json()
        resp = client.patch(
            f"/api/transports/{created['id']}", json={"name": "Kali3 renamed"}, headers=headers,
        )
        self.assertEqual(resp.status_code, 200)
        got = client.get(f"/api/transports/{created['id']}", headers=headers).json()
        self.assertEqual(got["name"], "Kali3 renamed")

    async def test_delete_removes_it(self):
        client, headers = self._login()
        created = client.post(
            "/api/transports",
            json={"name": "Kali3", "ssh_host": "h", "ssh_user": "kali", "ssh_key_path": "k"},
            headers=headers,
        ).json()
        resp = client.delete(f"/api/transports/{created['id']}", headers=headers)
        self.assertEqual(resp.status_code, 200)
        resp = client.get(f"/api/transports/{created['id']}", headers=headers)
        self.assertEqual(resp.status_code, 404)

    async def test_test_route_calls_ssh_test_connection(self):
        client, headers = self._login()
        with patch(
            "tunnel_manager_ssh.test_ssh_connection",
            AsyncMock(return_value={"ok": True, "error": None}),
        ):
            resp = client.post(
                "/api/transports/test",
                json={"ssh_host": "h", "ssh_user": "kali", "ssh_key_path": "k"},
                headers=headers,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])

    async def test_endpoints_require_auth(self):
        client = _client()
        self.assertEqual(client.get("/api/transports").status_code, 401)
        self.assertEqual(client.post("/api/transports", json={}).status_code, 401)
