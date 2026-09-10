from __future__ import annotations

import secrets
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import auth
import config
import db


def _client():
    from fastapi.testclient import TestClient
    from app import app as web_app
    return TestClient(web_app, raise_server_exceptions=False, base_url="https://testserver")


class TransportSyncApiTests(unittest.IsolatedAsyncioTestCase):
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

        self.password = secrets.token_urlsafe(16)
        await db.user_create("admin", None, auth.hash_password(self.password))
        self.transport_id = "t1"
        await db.ssh_transport_create(
            self.transport_id, "Kali3", "admin", "kali-3.example.net", "kali",
            "~/.ssh/id_ed25519",
        )
        await db.ai_machine_create(
            "m1", "Kali3 backend", "", 0, None, "", None, None, "admin",
            transport_id=self.transport_id,
        )

    def _login(self):
        client = _client()
        resp = client.post("/login", json={"username": "admin", "password": self.password})
        self.assertEqual(resp.status_code, 200)
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    def _tunnel_up(self):
        return patch(
            "tunnel_manager.tunnel_status",
            AsyncMock(return_value={"tunnel_up": True, "local_port": 9001}),
        )

    def _tunnel_down(self):
        return patch("tunnel_manager.tunnel_status", AsyncMock(return_value=None))

    async def test_sync_refuses_when_tunnel_is_not_up(self):
        client, headers = self._login()
        with self._tunnel_down():
            resp = client.post(f"/api/transports/{self.transport_id}/sync", headers=headers)
        self.assertEqual(resp.status_code, 409)

    async def test_sync_refuses_with_no_assigned_backend(self):
        await db.ai_machine_delete("m1", "admin")
        client, headers = self._login()
        with self._tunnel_up():
            resp = client.post(f"/api/transports/{self.transport_id}/sync", headers=headers)
        self.assertEqual(resp.status_code, 400)

    async def test_successful_sync_advances_last_synced_sha_and_logs(self):
        client, headers = self._login()
        fake_result = {"ok": True, "files_changed": 3, "reason": "", "head_sha": "newsha123"}
        with (
            self._tunnel_up(),
            patch("transport_sync.sync_transport", AsyncMock(return_value=fake_result)),
        ):
            resp = client.post(f"/api/transports/{self.transport_id}/sync", headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["files_changed"], 3)

        row = await db.ssh_transport_get(self.transport_id, "admin")
        self.assertEqual(row["last_synced_sha"], "newsha123")

    async def test_failed_sync_does_not_advance_last_synced_sha(self):
        """The no-silent-pointer-advancement rule, at the HTTP boundary."""
        client, headers = self._login()
        fake_result = {"ok": False, "files_changed": 0, "reason": "boom", "head_sha": ""}
        with (
            self._tunnel_up(),
            patch("transport_sync.sync_transport", AsyncMock(return_value=fake_result)),
        ):
            resp = client.post(f"/api/transports/{self.transport_id}/sync", headers=headers)
        self.assertEqual(resp.status_code, 502)

        row = await db.ssh_transport_get(self.transport_id, "admin")
        self.assertEqual(row["last_synced_sha"], "")

    async def test_pending_requests_route_is_not_captured_by_transport_id(self):
        """Registered before /api/transports/{transport_id} so "sync-requests"
        is never treated as a transport id -- the regression this pins."""
        client, headers = self._login()
        resp = client.get("/api/transports/sync-requests/pending", headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {"requests": []})

    async def test_approve_runs_the_sync_and_resolves_done(self):
        req_id = await db.sync_request_create(self.transport_id, "admin", "cweb-remote")
        client, headers = self._login()
        fake_result = {"ok": True, "files_changed": 5, "reason": "", "head_sha": "sha456"}
        with (
            self._tunnel_up(),
            patch("transport_sync.sync_transport", AsyncMock(return_value=fake_result)),
        ):
            resp = client.post(
                f"/api/transports/{self.transport_id}/sync-requests/{req_id}/approve",
                headers=headers,
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        row = await db.sync_request_get(req_id, "admin")
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["files_changed"], 5)

    async def test_reject_pushes_nothing(self):
        req_id = await db.sync_request_create(self.transport_id, "admin", "cweb-remote")
        client, headers = self._login()
        sync_called = AsyncMock()
        with patch("transport_sync.sync_transport", sync_called):
            resp = client.post(
                f"/api/transports/{self.transport_id}/sync-requests/{req_id}/reject",
                headers=headers,
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        sync_called.assert_not_awaited()
        row = await db.sync_request_get(req_id, "admin")
        self.assertEqual(row["status"], "rejected")

    async def test_cannot_approve_an_already_resolved_request(self):
        req_id = await db.sync_request_create(self.transport_id, "admin", "cweb-remote")
        await db.sync_request_resolve(req_id, "rejected")
        client, headers = self._login()
        resp = client.post(
            f"/api/transports/{self.transport_id}/sync-requests/{req_id}/approve",
            headers=headers,
        )
        self.assertEqual(resp.status_code, 409)


if __name__ == "__main__":
    unittest.main()
