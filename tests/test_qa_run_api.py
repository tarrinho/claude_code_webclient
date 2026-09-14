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
        # user_create returns None and mints the id internally, so read it back.
        # The route passes the session's *user id* to resolve_transport, not the
        # login name -- app.py:231 has called auth.session_new(user["id"], ...)
        # since login switched from name to id. Asserting on the literal "admin"
        # is what made the two mock assertions below fail.
        cur = await db.db_conn.execute(
            "SELECT id FROM users WHERE name = ?", ("admin",))
        self.admin_id = (await cur.fetchone())["id"]
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

    async def test_a_transport_created_by_someone_else_is_reachable(self):
        """Transports are a shared pool, not per-owner property.

        This test used to assert the opposite -- that another account's
        transport name 404s -- and it was right when it was written. On
        2026-09-11 the scoping was deliberately removed:
        `routes/db_transports.py:44` records that `owner_id` "is accepted but
        no longer filters -- transports are a shared pool across every account",
        and `ssh_transports_list` and `ssh_transport_update` say the same.
        `resolve_transport` reads through `db.ssh_transports_list(owner)`, so
        it inherited the change.

        The old assertion kept passing by accident for three days: it looked
        for 404 and any refusal ahead of the backend check would have given
        one. What it actually gets now is 400 "One has no backend assigned" --
        the *next* check along, which only runs once the name has already
        resolved. So the failure was the test noticing a contract change, not
        an access-control hole. Asserting the reachable case keeps that visible:
        if scoping is ever restored, this fails rather than silently passing.
        """
        other_password = secrets.token_urlsafe(16)
        await db.user_create("other", None, auth.hash_password(other_password))
        client = _client()
        resp = client.post("/login", json={"username": "other", "password": other_password})
        self.assertEqual(resp.status_code, 200, resp.text)
        headers = {"X-CSRF-Token": client.cookies.get("wc_csrf")}

        resp = client.post("/api/qa/run", json={"transport": "One"}, headers=headers)
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("no backend assigned", resp.text)

        # And a name nobody created still 404s -- the refusal above is about
        # the backend, not about the lookup having quietly stopped working.
        resp = client.post("/api/qa/run", json={"transport": "Nope"}, headers=headers)
        self.assertEqual(resp.status_code, 404, resp.text)

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
        mocked.assert_awaited_once_with(self.admin_id, None)

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
        mocked.assert_awaited_once_with(self.admin_id, None)

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
