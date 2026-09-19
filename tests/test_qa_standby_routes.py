"""QA: POST /api/chats/{id}/standby route error details.

Covers the four failure paths that the frontend must surface to the user:
* 404 - chat not found
* 400 - chat has no linked session (session_id is missing)
* 400 - session name cannot be resolved from ~/.claude/sessions/*.json
* 409 - several live sessions share the conversation's session id
* 409 - the script refused on one of its own rules (exit 3)
* 404 - nothing to suspend: no match, or a stale pid (exit 1)
* 500 - standby script fails in an unexpected way (any other return code)
* 200 - success, includes resume_command

The frontend (web/assets/app.js:standbyChat) reads response.json().error
on non-200, so every error must include an "error" field in the body.
The original bug: the frontend threw a generic "Could not standby conversation"
without reading the API response body, so users never saw which path failed.

All tests use local temp state. No live model, proxy, or network service.
No real sleep processes: the standby script path is exercised via mocks.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import auth
import config
import db
from routes import db_chats

HTTPS = "https://testserver"


def _client(follow_redirects: bool = True):
    from fastapi.testclient import TestClient

    from app import app as web_app
    return TestClient(
        web_app, raise_server_exceptions=False, base_url=HTTPS,
        follow_redirects=follow_redirects,
    )


class StandbyRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
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
        self.chat_id = uuid.uuid4().hex
        # The session["user"] is the UUID, not the username, so chat rows
        # must be owned by the UUID to be found by chat_get.
        self.alice_id = (await db.user_get_by_name("alice"))["id"]

        # Session files that _find_session_name() reads from ~/.claude/sessions/*.json
        #
        # Named <pid>.json for a *live* pid, which is the only shape that can
        # actually be stood by. This fixture used to write
        # `<session-uuid>.json` with `"pid": 1`, and every test here passed --
        # but bin/wc-session-standby.sh skips any file whose basename is not
        # numeric ("only <pid>.json files hold a live process"), so that shape
        # could never be matched in production. The tests mock the script, so
        # they never exercised the one step that rejects it, and the suite
        # agreed with a fixture the real system cannot serve.
        #
        # What that hid, observed 2026-09-15: Standby returned "no running
        # session found matching 'api.anthropic.com : 39 : Status'" for a
        # session that was running the whole time as `multi-agent`.
        # A temp HOME, not the operator's. These records are read by the
        # standby route and by bin/wc-session-standby.sh, so writing them to
        # the real ~/.claude/sessions put test fixtures into the registry that
        # decides which live process gets SIGTERMed. One such leak was still on
        # this host on 2026-09-18: cweb-real-0000-...json, naming pid 1.
        # test_qa_session_name_resolution.py already patches Path.home; this
        # does the same.
        self.home = Path(self.tmp.name) / "home"
        self.home_patch = patch.object(Path, "home", staticmethod(lambda: self.home))
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.claude_sessions = Path.home() / ".claude" / "sessions"
        self.real_session_id = "cweb-real-0000-0000-0000-000000000001"
        (self.claude_sessions).mkdir(parents=True, exist_ok=True)
        (self.claude_sessions / f"{os.getpid()}.json").write_text(
            json.dumps({
                "pid": os.getpid(),
                "name": "cweb-real",
                "kind": "interactive",
                "entrypoint": "cli",
                "status": "idle",
                "sessionId": self.real_session_id,
                "cwd": str(Path.home()),
                "updatedAt": 1789495645000,
            })
        )

    def _login(self, who: str):
        client = _client()
        response = client.post(
            "/login", json={"username": who, "password": self.passwords[who]}
        )
        self.assertEqual(response.status_code, 200, "fixture must log in")
        return client, {"X-CSRF-Token": client.cookies.get("wc_csrf")}

    async def _create_chat(self, session_id=None, owner=None, work_dir=None):
        """Create a chat row with optional session_id (mimics test_qa_chats.py pattern)."""
        owner = owner or self.alice_id
        wd = work_dir or f"{self.tmp.name}/projects/{self.chat_id}"
        Path(wd).mkdir(parents=True, exist_ok=True)
        await db_chats.chat_create(self.chat_id, "Test chat", "desc", wd, owner)
        if session_id is not None:
            await db.db_conn.execute(
                "UPDATE chats SET session_id = ? WHERE id = ? AND owner_id = ?",
                (session_id, self.chat_id, owner),
            )
            await db.db_conn.commit()

    # -- 404: chat not found --

    async def test_404_when_chat_does_not_exist(self):
        """Unknown chat_id returns 404 with error detail in body."""
        client, headers = self._login("alice")
        r = client.post("/api/chats/nonexistent-uuid/standby", headers=headers)
        self.assertEqual(r.status_code, 404, r.text)
        body = r.json()
        self.assertIn("error", body)
        self.assertIn("Chat not found", body["error"])

    async def test_404_when_chat_belongs_to_other_user(self):
        """Bob cannot standby Alice's chat."""
        chat_id_bob = uuid.uuid4().hex
        bob_id = (await db.user_get_by_name("bob"))["id"]
        wd_bob = f"{self.tmp.name}/projects/{chat_id_bob}"
        Path(wd_bob).mkdir(parents=True, exist_ok=True)
        await db_chats.chat_create(chat_id_bob, "Bob's chat", "desc", wd_bob, bob_id)
        await db.db_conn.execute(
            "UPDATE chats SET session_id = ? WHERE id = ? AND owner_id = ?",
            ("abc123", chat_id_bob, bob_id),
        )
        await db.db_conn.commit()
        alice_client, alice_headers = self._login("alice")
        r = alice_client.post(f"/api/chats/{chat_id_bob}/standby", headers=alice_headers)
        self.assertEqual(r.status_code, 404, r.text)
        body = r.json()
        self.assertIn("error", body)

    # -- 400: no linked session --

    async def test_400_when_chat_has_no_session_id(self):
        """A chat created without session_id cannot be stood by."""
        await self._create_chat(session_id=None)
        client, headers = self._login("alice")
        r = client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)
        self.assertEqual(r.status_code, 400, r.text)
        body = r.json()
        self.assertIn("error", body)
        self.assertIn("no linked session", body["error"].lower())

    # -- 400: cannot resolve session name --

    async def test_400_when_session_id_not_in_claude_sessions(self):
        """A valid session_id that doesn't match ~/.claude/sessions/*.json."""
        fake_sid = uuid.uuid4().hex
        await self._create_chat(session_id=fake_sid)
        client, headers = self._login("alice")

        r = client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)
        self.assertEqual(r.status_code, 400, r.text)
        body = r.json()
        self.assertIn("error", body)
        self.assertIn("session name", body["error"].lower())

    # -- bound to a process: no resolution, no guess --

    async def _bind(self, proc: str):
        await db.db_conn.execute(
            "UPDATE chats SET session_proc = ? WHERE id = ?", (proc, self.chat_id))
        await db.db_conn.commit()

    def _second_session_file(self, name="cweb-real-second"):
        """A second live process on the SAME session id -- the ambiguity."""
        f = self.claude_sessions / f"{os.getppid()}.json"
        f.write_text(json.dumps({
            "entrypoint": "cli", "pid": os.getppid(), "procStart": 4242,
            "pidDomain": "testdomain", "name": name, "status": "idle",
            "sessionId": self.real_session_id, "cwd": str(Path.home()),
            "updatedAt": 1789495999000,
        }))
        self.addCleanup(lambda: f.unlink(missing_ok=True))
        return f

    async def test_a_bound_chat_suspends_its_own_process_despite_ambiguity(self):
        """The point of the column. Two live processes share the session id;
        the chat names one of them, so there is nothing to resolve and the 409
        does not apply."""
        self._second_session_file()
        await self._create_chat(session_id=self.real_session_id)
        await self._bind(f"testdomain|{os.getppid()}|4242")
        client, headers = self._login("alice")

        async_mock = unittest.mock.AsyncMock(returncode=0)
        async_mock.communicate = unittest.mock.AsyncMock(return_value=(b"", b""))
        with patch("routes.chats.asyncio.create_subprocess_exec",
                   return_value=async_mock) as spawn:
            r = client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)

        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("cweb-real-second", spawn.call_args[0],
                      "must signal the bound process, not the tie-break winner")

    async def test_a_binding_whose_process_is_gone_is_404_not_a_substitute(self):
        """The failure mode this column exists to prevent: the bound process
        has exited, another live process still shares the session id, and the
        old path would have suspended that one instead."""
        self._second_session_file()
        await self._create_chat(session_id=self.real_session_id)
        await self._bind("testdomain|999999999|1")
        client, headers = self._login("alice")

        with patch("routes.chats.asyncio.create_subprocess_exec") as spawn:
            r = client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)
            spawn.assert_not_called()

        self.assertEqual(r.status_code, 404, r.text)
        self.assertIn("no longer running", r.json()["error"])

    async def test_a_recycled_pid_does_not_answer_for_the_old_process(self):
        """Same pid, different start tick. Without procStart in the key this
        would suspend whatever program inherited the pid."""
        self._second_session_file()
        await self._create_chat(session_id=self.real_session_id)
        await self._bind(f"testdomain|{os.getppid()}|1")  # same pid, older start
        client, headers = self._login("alice")

        with patch("routes.chats.asyncio.create_subprocess_exec") as spawn:
            r = client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)
            spawn.assert_not_called()

        self.assertEqual(r.status_code, 404, r.text)

    async def test_an_unbound_chat_still_uses_the_old_resolution(self):
        """Every row written before this column is NULL, and must keep
        working rather than becoming unsuspendable."""
        await self._create_chat(session_id=self.real_session_id)
        client, headers = self._login("alice")
        async_mock = unittest.mock.AsyncMock(returncode=0)
        async_mock.communicate = unittest.mock.AsyncMock(return_value=(b"", b""))
        with patch("routes.chats.asyncio.create_subprocess_exec",
                   return_value=async_mock):
            r = client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)
        self.assertEqual(r.status_code, 200, r.text)

    # -- 409: the session id is ambiguous --

    async def test_409_when_several_live_sessions_share_the_session_id(self):
        """Observed on this host 2026-09-18: three live sessions shared one
        `sessionId` -- multiagent2 (1798), multiagent3 - testusage (3659449)
        and multiagent - benchmark plan (3763786). `_find_session_name`
        tie-breaks on updatedAt, so a standby click on a chat titled
        `cweb4 - voice issue` resolved to `multiagent3 - testusage` and would
        have SIGTERMed a session nobody named.

        The tie-break is fine for a display label and wrong for choosing a
        victim, so this endpoint refuses instead, and names every candidate so
        the operator can suspend the right one deliberately.

        The script must never be reached: asserting the refusal alone would
        pass even if the process had already been signalled."""
        second = self.claude_sessions / f"{os.getppid()}.json"
        second.write_text(json.dumps({
            "pid": os.getppid(),
            "name": "cweb-real-second",
            "status": "idle",
            "sessionId": self.real_session_id,
            "cwd": str(Path.home()),
            "updatedAt": 1789495999000,
        }))
        self.addCleanup(lambda: second.unlink(missing_ok=True))

        await self._create_chat(session_id=self.real_session_id)
        client, headers = self._login("alice")

        with patch("routes.chats.asyncio.create_subprocess_exec") as spawn:
            r = client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)
            spawn.assert_not_called()

        self.assertEqual(r.status_code, 409, r.text)
        error = r.json()["error"]
        self.assertIn("cweb-real", error)
        self.assertIn("cweb-real-second", error)
        self.assertIn("2 live sessions", error)

    async def test_the_chat_is_not_marked_on_standby_when_it_is_ambiguous(self):
        """A refusal that still flipped the flag would leave a live session
        labelled as suspended, which is how the 2026-09-15 incident read from
        the sidebar."""
        second = self.claude_sessions / f"{os.getppid()}.json"
        second.write_text(json.dumps({
            "pid": os.getppid(), "name": "cweb-real-second", "status": "idle",
            "sessionId": self.real_session_id, "cwd": str(Path.home()),
            "updatedAt": 1789495999000,
        }))
        self.addCleanup(lambda: second.unlink(missing_ok=True))
        await self._create_chat(session_id=self.real_session_id)
        client, headers = self._login("alice")
        client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)
        row = await db.chat_get(self.chat_id, self.alice_id)
        self.assertIsNone(row.get("standby_reason"))

    # -- exit codes: each one is a different answer to the operator --

    async def _run_with_returncode(self, code: int, stderr: bytes):
        """POST standby with the script mocked to exit *code*."""
        await self._create_chat(session_id=self.real_session_id)
        client, headers = self._login("alice")
        async_mock = unittest.mock.AsyncMock(returncode=code)
        async_mock.communicate = unittest.mock.AsyncMock(return_value=(b"", stderr))
        with patch(
            "routes.chats.asyncio.create_subprocess_exec",
            return_value=async_mock,
        ):
            return client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)

    async def test_409_when_the_script_refuses_on_its_own_rules(self):
        """Exit 3: the session is there, alive, and protected -- mid-turn,
        waiting on a question, or not idle long enough.

        This is the path a user actually hits. It used to be a 500, which reads
        as "the server broke" and sends people looking for a fault that is not
        there; the session was in fact being protected exactly as designed. The
        script's own sentence is passed through, because it names the session,
        the pid and the rule."""
        r = await self._run_with_returncode(
            3, b"session 'multiagent3' (pid 3659449) is mid-turn -- refusing to standby it.")
        self.assertEqual(r.status_code, 409, r.text)
        body = r.json()
        self.assertIn("error", body)
        self.assertIn("mid-turn", body["error"])
        self.assertIn("3659449", body["error"],
                      "the operator needs the pid to act on this")

    async def test_404_when_there_is_nothing_to_suspend(self):
        """Exit 1: no session matched, or the record named a dead pid. Not a
        server fault and not a conflict -- the thing is simply not there."""
        r = await self._run_with_returncode(
            1, b"no running session found matching 'ghost' (checked pid and name)")
        self.assertEqual(r.status_code, 404, r.text)
        self.assertIn("no running session", r.json()["error"].lower())

    async def test_500_only_for_an_unexpected_exit_code(self):
        """Anything the script does not document stays a 500. A new exit code
        must not be silently reclassified as a refusal the user can act on."""
        r = await self._run_with_returncode(7, b"bash: line 1: python3: command not found")
        self.assertEqual(r.status_code, 500, r.text)
        self.assertIn("standby script failed", r.json()["error"].lower())

    # -- 200: success --

    async def test_success_returns_ok_and_resume_command(self):
        """Happy path: script succeeds, returns resume_command."""
        await self._create_chat(session_id=self.real_session_id)
        client, headers = self._login("alice")

        async_mock = unittest.mock.AsyncMock(returncode=0)
        async_mock.communicate = unittest.mock.AsyncMock(return_value=(b"", b""))

        with patch(
            "routes.chats.asyncio.create_subprocess_exec",
            return_value=async_mock,
        ):
            r = client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertTrue(body["ok"])
            self.assertTrue(body["standby"])
            self.assertIn("resume_command", body)
            # Not "screen": standby SIGTERMs the process and launches nothing,
            # so there is no screen window to reattach to and `screen -r` sends
            # the operator after something that does not exist. What resumes a
            # standby'd session is wake -- the menu action, or the script this
            # names. Asserting the word "screen" passed happily while the text
            # was wrong, which is how it shipped.
            resume = body["resume_command"]
            self.assertIn("Wake", resume)
            self.assertIn("wc-session-wake.sh", resume)
            self.assertNotIn("screen -r", resume)

    # -- wake route: POST /api/chats/{id}/wake --

    async def test_400_when_chat_is_not_on_standby(self):
        """A normal chat (no standby_reason) cannot be woken."""
        await self._create_chat(session_id=self.real_session_id)
        client, headers = self._login("alice")
        r = client.post(f"/api/chats/{self.chat_id}/wake", headers=headers)
        self.assertEqual(r.status_code, 400, r.text)
        body = r.json()
        self.assertIn("error", body)
        self.assertIn("not on standby", body["error"].lower())

    async def test_wake_400_when_standby_flag_is_stale(self):
        """A chat without session_id but with a stale standby flag returns 400.

        Named apart from the standby test above deliberately. Both were called
        test_400_when_chat_has_no_session_id, so this definition shadowed the
        other and unittest only ever collected one of the two -- the standby
        case at line 139 had not run since it was written. Found by flake8
        F811 during a rules.md section 9 pass on 2026-09-14.
        """
        await self._create_chat(session_id=None)
        # Inject a fake standby_reason to simulate a stale flag.
        await db.db_conn.execute(
            "UPDATE chats SET standby_reason = ? WHERE id = ? AND owner_id = ?",
            ("test", self.chat_id, self.alice_id),
        )
        await db.db_conn.commit()
        client, headers = self._login("alice")
        r = client.post(f"/api/chats/{self.chat_id}/wake", headers=headers)
        self.assertEqual(r.status_code, 400, r.text)
        body = r.json()
        self.assertIn("error", body)
        self.assertIn("no linked session", body["error"].lower())

    async def test_success_clears_standby_and_returns_message(self):
        """Happy wake path: clears standby_reason, returns ok message."""
        await self._create_chat(session_id=self.real_session_id)
        # Set standby_reason so the chat looks like it was stood-by first.
        await db.db_conn.execute(
            "UPDATE chats SET standby_reason = ? WHERE id = ? AND owner_id = ?",
            ("standby requested at 2026-09-01T00:00:00", self.chat_id, self.alice_id),
        )
        await db.db_conn.commit()
        client, headers = self._login("alice")

        async_mock = unittest.mock.AsyncMock(returncode=0)
        async_mock.communicate = unittest.mock.AsyncMock(return_value=(b"", b""))

        with patch(
            "routes.chats.asyncio.create_subprocess_exec",
            return_value=async_mock,
        ):
            r = client.post(f"/api/chats/{self.chat_id}/wake", headers=headers)
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertTrue(body["ok"])
            self.assertFalse(body["standby"])
            self.assertIn("resume_command", body)

        # Verify standby_reason is actually cleared in DB.
        chat = await db.chat_get(self.chat_id, self.alice_id)
        self.assertIsNone(chat.get("standby_reason"))

    # -- frontend assertion: error detail in body --

    async def test_error_detail_is_in_response_body_not_silenced(self):
        """Regression test: every error path includes 'error' so the frontend
        can display it. Previously the frontend threw a generic error without
        reading the API response body."""
        # Test 404 path
        client, headers = self._login("alice")
        r = client.post("/api/chats/nonexistent/standby", headers=headers)
        self.assertEqual(r.status_code, 404)
        body = r.json()
        self.assertIn("error", body)
        self.assertIsInstance(body["error"], str)
        self.assertGreater(len(body["error"]), 0)

        # Test 400 path (no session_id)
        await self._create_chat(session_id=None)
        r = client.post(f"/api/chats/{self.chat_id}/standby", headers=headers)
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertIn("error", body)
        self.assertIsInstance(body["error"], str)


if __name__ == "__main__":
    unittest.main()
