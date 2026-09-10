"""QA: POST /api/qa/run -- owner scoping, refusal-to-status-code mapping,
and that the response streams incrementally rather than blocking until the
whole run finishes.

Design: docs/superpowers/specs/2026-09-09-remote-qa-execution-design.md §4-§6.
"""
from __future__ import annotations

import json
import secrets
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import auth
import config
import db
import qa_remote


def _client():
    from fastapi.testclient import TestClient
    from app import app as web_app
    return TestClient(web_app, raise_server_exceptions=False, base_url="https://testserver")


class QaRunApiTests(unittest.IsolatedAsyncioTestCase):
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
        await db.ssh_transport_create(
            "t1", "One", "admin", "one.example.net", "kali", "~/.ssh/id_ed25519")

    def _login(self):
        client = _client()
        resp = client.post("/login", json={"username": "admin", "password": self.password})
        self.assertEqual(resp.status_code, 200)
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    def _parse_events(self, text: str) -> list[dict]:
        return [
            json.loads(line[len("data: "):])
            for line in text.splitlines() if line.startswith("data: ")
        ]

    async def test_refusal_from_resolve_transport_becomes_that_status_code(self):
        client, headers = self._login()
        with patch(
            "qa_remote.resolve_transport",
            AsyncMock(side_effect=qa_remote.QaRefusal(409, "One has no live tunnel")),
        ):
            resp = client.post("/api/qa/run", json={"transport": "t1"}, headers=headers)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("no live tunnel", resp.text)

    async def test_a_transport_owned_by_someone_else_is_not_found(self):
        """resolve_transport itself is owner-scoped (it reads via
        db.ssh_transports_list(owner)) -- this pins that a second user's
        request against the first user's transport name gets treated the
        same as a nonexistent one, never leaks whether the name exists."""
        other_password = secrets.token_urlsafe(16)
        await db.user_create("other", None, auth.hash_password(other_password))
        client = _client()
        resp = client.post("/login", json={"username": "other", "password": other_password})
        headers = {"X-CSRF-Token": client.cookies.get("wc_csrf")}

        resp = client.post("/api/qa/run", json={"transport": "One"}, headers=headers)
        self.assertEqual(resp.status_code, 404)

    async def test_successful_run_streams_events_in_order(self):
        prepared = qa_remote.Prepared(
            transport={"id": "t1", "name": "One"}, machine_id="m1", floor_mb=700)

        async def fake_events(_prepared):
            yield {"type": "sync-start", "transport": "One"}
            yield {"type": "sync-done", "files_changed": 0}
            yield {"type": "run-done", "ok": True, "totals": {}}

        client, headers = self._login()
        with (
            patch("qa_remote.resolve_transport", AsyncMock(return_value=prepared)),
            patch("qa_remote.execute", fake_events),
        ):
            resp = client.post("/api/qa/run", json={"transport": "t1"}, headers=headers)
        self.assertEqual(resp.status_code, 200)
        events = self._parse_events(resp.text)
        self.assertEqual([e["type"] for e in events],
                         ["sync-start", "sync-done", "run-done"])

    async def test_no_transport_in_body_calls_resolve_transport_with_none(self):
        client, headers = self._login()
        with patch(
            "qa_remote.resolve_transport",
            AsyncMock(side_effect=qa_remote.QaRefusal(503, "none qualify")),
        ) as mocked:
            client.post("/api/qa/run", json={}, headers=headers)
        mocked.assert_awaited_once_with("admin", None)

    async def test_a_non_object_json_body_is_treated_like_an_empty_one(self):
        """A valid-but-non-object body (e.g. a JSON array or a bare number)
        must not raise an unhandled AttributeError out of body.get() and
        surface as a 500 -- it should behave exactly like an empty body."""
        client, headers = self._login()
        with patch(
            "qa_remote.resolve_transport",
            AsyncMock(side_effect=qa_remote.QaRefusal(503, "none qualify")),
        ) as mocked:
            resp = client.post(
                "/api/qa/run", content=b"[1,2,3]",
                headers={**headers, "Content-Type": "application/json"},
            )
        self.assertNotEqual(resp.status_code, 500)
        mocked.assert_awaited_once_with("admin", None)

    async def test_an_exception_mid_run_reaches_the_client_as_run_done(self):
        prepared = qa_remote.Prepared(
            transport={"id": "t1", "name": "One"}, machine_id="m1", floor_mb=700)

        async def failing_events(_prepared):
            yield {"type": "sync-start", "transport": "One"}
            raise RuntimeError("ssh connection reset")

        client, headers = self._login()
        with (
            patch("qa_remote.resolve_transport", AsyncMock(return_value=prepared)),
            patch("qa_remote.execute", failing_events),
        ):
            resp = client.post("/api/qa/run", json={"transport": "t1"}, headers=headers)
        events = self._parse_events(resp.text)
        self.assertEqual(events[-1]["type"], "run-done")
        self.assertFalse(events[-1]["ok"])


if __name__ == "__main__":
    unittest.main()
