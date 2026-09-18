"""QA: a standby that worked must not be reported as a failure.

Observed 2026-09-15. Standby on a live session returned HTTP 500:

    Standby script failed: pid 2585 did not exit within 5s after SIGTERM.
    The standby record was still written -- resume with wc-session-wake.sh once

The process was gone when checked afterwards. It had been shutting down the
whole time -- the CLI flushes its transcript and session record on the way out,
so the tail of that work is normal rather than a hang, and five seconds was
simply too short a window to see it in.

The cost was not the wrong message. `handle_chat_standby` raised on any non-zero
exit, which happens *before* it marks the chat, so the outcome was:

* the session had been signalled and was dying,
* `wc-session-standby.sh` had already written its resume record,
* the chat's `standby_reason` was never set, and
* the user was told the standby failed.

A dead session attached to a chat that does not know it is on standby is worse
than either a clean success or a clean failure, and it was reachable purely by
the process taking six seconds instead of four.

So the script now exits **2** for this case, distinct from 1. Exit 1 still means
nothing happened at all -- no such session, or refused because it was busy -- and
must stay a 500. Exit 2 means the signal was delivered and the record written,
with only the process's own shutdown outstanding, and the chat is marked.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import unittest
import unittest.mock
import uuid
from pathlib import Path
from unittest.mock import patch

import auth
import config
import db
from routes import db_chats

HTTPS = "https://testserver"


def _client():
    from fastapi.testclient import TestClient

    from app import app as web_app
    return TestClient(web_app, raise_server_exceptions=False, base_url=HTTPS,
                      follow_redirects=True)


def _script(returncode: int, stderr: bytes = b""):
    proc = unittest.mock.AsyncMock(returncode=returncode)
    proc.communicate = unittest.mock.AsyncMock(return_value=(b"", stderr))
    return proc


class LingeringStandbyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for attr, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, attr, value)
            p.start()
            self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

        self.password = secrets.token_urlsafe(16)
        await db.user_create("alice", None, auth.hash_password(self.password))
        self.alice_id = (await db.user_get_by_name("alice"))["id"]

        # A live <pid>.json, the only shape _find_session_name will accept.
        self.session_id = "cweb-lingering-0000-0000-000000000001"
        sessions = Path.home() / ".claude" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        self.session_file = sessions / f"{os.getpid()}.json"
        self.session_file.write_text(json.dumps({
            "pid": os.getpid(), "name": "cweb-lingering", "status": "idle",
            "sessionId": self.session_id, "cwd": str(Path.home()),
            "updatedAt": 1789495645000,
        }))

        self.chat_id = uuid.uuid4().hex
        wd = f"{self.tmp.name}/projects/{self.chat_id}"
        Path(wd).mkdir(parents=True, exist_ok=True)
        await db_chats.chat_create(self.chat_id, "Test chat", "desc", wd, self.alice_id)
        await db.db_conn.execute(
            "UPDATE chats SET session_id = ? WHERE id = ? AND owner_id = ?",
            (self.session_id, self.chat_id, self.alice_id),
        )
        await db.db_conn.commit()

    def _login(self):
        client = _client()
        r = client.post("/login", json={"username": "alice", "password": self.password})
        self.assertEqual(r.status_code, 200, "fixture must log in")
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    async def _standby_reason(self):
        cur = await db.db_conn.execute(
            "SELECT standby_reason FROM chats WHERE id = ?", (self.chat_id,))
        row = await cur.fetchone()
        return row["standby_reason"] if row else None

    async def test_lingering_process_still_marks_the_chat(self):
        """Exit 2: signalled and recorded, shutdown outstanding.

        The chat must end up on standby. Leaving it unmarked is what stranded a
        dead session against a chat that thought it was still running.
        """
        client, headers = self._login()
        with patch("routes.chats.asyncio.create_subprocess_exec",
                   return_value=_script(2, b"pid 2585 has not exited 30s after SIGTERM.")):
            r = client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["standby"])
        self.assertIn("warning", body)
        self.assertTrue(await self._standby_reason(), "chat was not marked on standby")

    async def test_a_real_failure_is_still_an_error_and_marks_nothing(self):
        """Exit 1 means nothing happened, and must not be softened into success.

        The status became 404 on 2026-09-18, when the script gained exit 3 for
        a deliberate refusal: exit 1 is "there is no such session", which is
        not a server fault. The two invariants this test exists for are
        unchanged -- the response is an error carrying the script's own
        sentence, and the chat is not marked on standby."""
        client, headers = self._login()
        with patch("routes.chats.asyncio.create_subprocess_exec",
                   return_value=_script(1, b"no running session found matching 'x'")):
            r = client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)

        self.assertEqual(r.status_code, 404, r.text)
        self.assertGreaterEqual(r.status_code, 400, "must not be softened into success")
        self.assertIn("error", r.json())
        self.assertFalse(await self._standby_reason(),
                         "a failed standby must not mark the chat")

    async def test_clean_success_carries_no_warning(self):
        client, headers = self._login()
        with patch("routes.chats.asyncio.create_subprocess_exec",
                   return_value=_script(0)):
            r = client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["standby"])
        self.assertNotIn("warning", body)
        self.assertTrue(await self._standby_reason())


if __name__ == "__main__":
    unittest.main()
