"""A web request for a linked conversation reaches its live terminal.

A conversation created by resuming a CLI session has two possible homes for a
turn: a fresh `claude --resume` process, or the interactive terminal the user
actually has open. Spawning the second process does the work correctly and
invisibly -- the user types a request in the browser, watches their terminal,
and sees nothing happen, while two processes append to one transcript.

Routing to the live window instead means the request and every step of the
answer appear where the user is looking, and the web conversation collects
them through the existing transcript sync.

Covers:
* prompts.send_text — payload sanitising, the screen and tmux command shapes,
  and that Enter is a separate step so a failed keystroke cannot still submit.
* prompts.deliver_request — the no-window case reports itself.
* _route_to_live_terminal — falls back to the headless turn rather than
  dropping a request when there is no window or the terminal refuses.
* The stream and submit handlers — a routed request persists the user message,
  says so in a way the client renders, and never spawns a turn.
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import auth
import config
import db
import prompts
import runner
from routes import chats as chat_routes

SCREEN = {"kind": "screen", "session": "2126909.pts-5.kali-2", "window": "0"}
TMUX = {"kind": "tmux", "window": "%3"}
SESSION_ID = "b198eb69-a405-445a-96d5-1ea3e3f70b92"


class SendTextTests(unittest.TestCase):
    """The payload that reaches the multiplexer."""

    def setUp(self):
        self.session_db_patch = patch.object(config, "SESSION_DB_PATH", "/dev/null")
        self.session_db_patch.start()

    def tearDown(self):
        self.session_db_patch.stop()

    def _calls(self, target, text, returncode=0):
        seen = []

        def _fake(argv):
            seen.append(argv)
            return SimpleNamespace(returncode=returncode, stdout="", stderr="")

        with patch.object(prompts, "_run", _fake):
            ok = prompts.send_text(target, text)
        return ok, seen

    def test_screen_types_then_presses_enter_separately(self):
        """Two calls, so a failure to type cannot still submit a blank line."""
        ok, calls = self._calls(SCREEN, "make it 20% wider")
        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][:6],
                         ["screen", "-S", SCREEN["session"], "-p", "0", "-X"])
        self.assertEqual(calls[0][-1], "make it 20% wider")
        self.assertEqual(calls[1][-1], "\r")

    def test_tmux_sends_the_text_literally(self):
        ok, calls = self._calls(TMUX, "make it 20% wider")
        self.assertTrue(ok)
        self.assertIn("-l", calls[0])
        self.assertEqual(calls[0][-1], "make it 20% wider")

    def test_a_failure_to_type_does_not_press_enter(self):
        ok, calls = self._calls(SCREEN, "hello", returncode=1)
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1, "Enter was sent after typing failed")

    def test_control_characters_are_stripped(self):
        """A payload carrying its own newline would submit more than one
        request -- the caller asked to send exactly one."""
        ok, calls = self._calls(SCREEN, "first line\nrm -rf /\n")
        self.assertTrue(ok)
        self.assertNotIn("\n", calls[0][-1])
        self.assertEqual(calls[0][-1], "first linerm -rf /")

    def test_escape_sequences_are_stripped(self):
        ok, calls = self._calls(SCREEN, "up\x1b[Aup")
        self.assertTrue(ok)
        self.assertNotIn("\x1b", calls[0][-1])

    def test_empty_and_whitespace_are_refused(self):
        for text in ("", "   ", "\n\n", "\x00"):
            with self.subTest(text=text):
                ok, calls = self._calls(SCREEN, text)
                self.assertFalse(ok)
                self.assertEqual(calls, [])

    def test_overlong_text_is_truncated_not_refused(self):
        ok, calls = self._calls(SCREEN, "x" * 9000)
        self.assertTrue(ok)
        self.assertLessEqual(len(calls[0][-1]), prompts._TEXT_MAX)

    def test_unknown_target_kind_refused(self):
        ok, calls = self._calls({"kind": "carrier-pigeon"}, "hello")
        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_non_dict_target_refused(self):
        self.assertFalse(prompts.send_text(None, "hello"))
        self.assertFalse(prompts.send_text("screen", "hello"))

    def test_deliver_is_still_restricted_to_its_key_set(self):
        """send_text must not have widened the prompt-answering path."""
        _ok, calls = [], []
        with patch.object(prompts, "_run", lambda argv: calls.append(argv)):
            self.assertFalse(prompts.deliver(SCREEN, "rm -rf /"))
        self.assertEqual(calls, [])


class DeliveryRefusalTests(unittest.TestCase):
    """The server must never type into its own window.

    locate() derives a window from the pid in ~/.claude/sessions/<id>.json.
    For a web-created session db.write_claude_session_file records the
    WebConsole's OWN pid, and the server runs inside screen too -- so that
    window resolves to the server's. If it happens to be sitting at a shell,
    typing text and pressing Enter runs it. An authenticated web request must
    not become a shell command.
    """

    def test_the_servers_own_pid_is_never_a_delivery_target(self):
        """Isolates the own-pid guard.

        Asserting on the bare pid passed for the wrong reason: the test runner
        is not called "claude", so the comm check rejected it and the own-pid
        guard was never exercised. Mutating that guard away left this green.
        Here the other two guards are neutralised -- no ancestry, and comm
        reports claude -- so only the own-pid check can refuse it.
        """
        import os
        with patch.object(prompts, "_parents", return_value=[]), \
                patch.object(pathlib.Path, "read_text", return_value="claude\n"):
            self.assertFalse(
                prompts._is_claude_process(os.getpid()),
                "the console must never deliver into its own process's window",
            )
            # Control: the same conditions accept a different pid, proving the
            # neutralisation worked and this is not passing on a technicality.
            self.assertTrue(prompts._is_claude_process(os.getpid() + 100000))

    def test_an_ancestor_of_the_server_is_refused(self):
        """A web-created session file names the app; walking up from it finds
        the shell or multiplexer that launched us."""
        import os
        ancestors = prompts._parents(os.getpid())
        if not ancestors:
            self.skipTest("no parent processes visible")
        for pid, _name in ancestors:
            with self.subTest(pid=pid):
                self.assertFalse(prompts._is_claude_process(pid))

    def test_a_non_claude_process_is_refused(self):
        """Isolates the comm guard.

        PID 1 alone proved nothing: it is an ancestor of the test runner, so
        the ancestry walk refused it and the comm check never ran. Removing the
        comm check left this green. Ancestry is neutralised here so only comm
        can decide.
        """
        with patch.object(prompts, "_parents", return_value=[]), \
                patch.object(pathlib.Path, "read_text", return_value="bash\n"):
            self.assertFalse(
                prompts._is_claude_process(999_001),
                "a live process that is not claude must not be typed into",
            )
        with patch.object(prompts, "_parents", return_value=[]), \
                patch.object(pathlib.Path, "read_text", return_value="claude\n"):
            self.assertTrue(prompts._is_claude_process(999_001))

    def test_an_unreadable_process_is_refused(self):
        """A pid that has exited between the file naming it and the delivery."""
        with patch.object(prompts, "_parents", return_value=[]), \
                patch.object(prompts, "_process_name", return_value=""):
            self.assertFalse(prompts._is_claude_process(999_002))

    def test_missing_and_absurd_pids_are_refused(self):
        for pid in (None, 0, -1, 2 ** 31):
            with self.subTest(pid=pid):
                self.assertFalse(prompts._is_claude_process(pid))

    def test_delivery_refuses_when_the_pid_is_not_claude(self):
        with patch.object(prompts, "session_pid", return_value=1), \
                patch.object(prompts, "locate", return_value=SCREEN) as located:
            outcome = prompts.deliver_request(SESSION_ID, "hello")
        self.assertFalse(outcome["delivered"])
        self.assertIn("no identified claude process", outcome["reason"])
        located.assert_not_called()

    def test_sharing_the_servers_window_is_allowed_and_recorded(self):
        """A window match must not veto delivery, though it looks like it should.

        Measured on this deployment: the console runs in STY 2126909.pts-5
        window 0, and cweb2's claude runs in the same window because the
        console was launched with `&` from it. Vetoing on a window match would
        refuse the very session this routing was built for.

        It is redundant as well as harmful. F-01 works by naming the console's
        own pid so the walk upwards lands in the console's window; refusing our
        pid and our ancestry kills that at the source. A pid past those checks
        is a different, live, verified claude. The overlap is recorded so a
        delivery stays attributable, not refused.
        """
        shared = {"kind": "screen", "session": "2126909.pts-5.kali-2",
                  "window": "0", "shares_server_window": True}
        with patch.object(prompts, "session_pid", return_value=4242), \
                patch.object(prompts, "_is_claude_process", return_value=True), \
                patch.object(prompts, "locate", return_value=shared), \
                patch.object(prompts, "send_text", return_value=True):
            outcome = prompts.deliver_request(SESSION_ID, "hello")
        self.assertTrue(outcome["delivered"],
                        "a shared window must not block the session it belongs to")
        self.assertTrue(outcome["shares_server_window"])

    def test_a_window_the_server_does_not_share_is_also_allowed(self):
        other = {"kind": "screen", "session": "1234.pts-9.host", "window": "7",
                 "shares_server_window": False}
        with patch.object(prompts, "session_pid", return_value=4242), \
                patch.object(prompts, "_is_claude_process", return_value=True), \
                patch.object(prompts, "locate", return_value=other), \
                patch.object(prompts, "send_text", return_value=True):
            outcome = prompts.deliver_request(SESSION_ID, "hello")
        self.assertTrue(outcome["delivered"])
        self.assertFalse(outcome["shares_server_window"])

    def test_every_refusal_falls_back_rather_than_dropping_the_request(self):
        """The unsafe direction must also be the inert one."""
        with patch.object(prompts, "session_pid", return_value=1):
            outcome = prompts.deliver_request(SESSION_ID, "hello")
        self.assertFalse(outcome["delivered"])
        self.assertIsNone(outcome["target"])


class DeliverRequestTests(unittest.TestCase):

    def test_no_window_reports_itself(self):
        with patch.object(prompts, "_is_claude_process", return_value=True), \
                patch.object(prompts, "session_pid", return_value=4242), \
                patch.object(prompts, "locate", return_value=None):
            outcome = prompts.deliver_request(SESSION_ID, "hello")
        self.assertFalse(outcome["delivered"])
        self.assertIn("no live terminal", outcome["reason"])

    def test_a_refusing_terminal_is_reported_not_swallowed(self):
        with patch.object(prompts, "_is_claude_process", return_value=True), \
                patch.object(prompts, "session_pid", return_value=4242), \
                patch.object(prompts, "locate", return_value=SCREEN), \
                patch.object(prompts, "send_text", return_value=False):
            outcome = prompts.deliver_request(SESSION_ID, "hello")
        self.assertFalse(outcome["delivered"])
        self.assertTrue(outcome["reason"])
        self.assertEqual(outcome["target"], SCREEN)

    def test_success_carries_the_target(self):
        with patch.object(prompts, "_is_claude_process", return_value=True), \
                patch.object(prompts, "session_pid", return_value=4242), \
                patch.object(prompts, "locate", return_value=SCREEN), \
                patch.object(prompts, "send_text", return_value=True):
            outcome = prompts.deliver_request(SESSION_ID, "hello")
        self.assertTrue(outcome["delivered"])
        self.assertEqual(outcome["target"], SCREEN)


async def _setup(tc):
    td = tempfile.TemporaryDirectory()
    tc.tmpdir = td
    tc._db_patch = patch.object(config, "DB_PATH", f"{td.name}/db")
    tc._root_patch = patch.object(config, "PROJECTS_ROOT", f"{td.name}/projects")
    tc._db_patch.start()
    tc._root_patch.start()
    await db.init()
    await auth.bootstrap_admin()


async def _teardown(tc):
    await db.close()
    tc._db_patch.stop()
    tc._root_patch.stop()
    tc.tmpdir.cleanup()


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    """Which home a turn gets."""

    async def asyncSetUp(self):
        await _setup(self)

    async def asyncTearDown(self):
        await _teardown(self)

    async def test_a_chat_with_no_session_is_never_routed(self):
        chat = {"id": "c1", "session_id": None, "work_dir": "/tmp"}
        self.assertIsNone(await chat_routes._route_to_live_terminal(chat, "hello"))

    async def test_no_live_window_falls_back(self):
        """A missing terminal must run the headless turn, not drop the request."""
        chat = {"id": "c1", "session_id": SESSION_ID, "work_dir": "/tmp"}
        with patch.object(
            prompts, "deliver_request",
            return_value={"delivered": False, "reason": "no live terminal window",
                          "target": None},
        ):
            self.assertIsNone(await chat_routes._route_to_live_terminal(chat, "hello"))

    async def test_a_refusing_terminal_falls_back(self):
        chat = {"id": "c1", "session_id": SESSION_ID, "work_dir": "/tmp"}
        with patch.object(
            prompts, "deliver_request",
            return_value={"delivered": False, "reason": "refused", "target": SCREEN},
        ):
            self.assertIsNone(await chat_routes._route_to_live_terminal(chat, "hello"))

    async def test_a_live_window_is_used(self):
        chat = {"id": "c1", "session_id": SESSION_ID, "work_dir": "/tmp"}
        with patch.object(
            prompts, "deliver_request",
            return_value={"delivered": True, "reason": "", "target": SCREEN},
        ):
            routed = await chat_routes._route_to_live_terminal(chat, "hello")
        self.assertTrue(routed["delivered"])


class StreamRoutingTests(unittest.IsolatedAsyncioTestCase):
    """The SSE path, which is what the browser actually uses."""

    async def asyncSetUp(self):
        await _setup(self)
        # stream_handler authenticates from the wc_session cookie itself
        # rather than from request.state, so a real session is needed.
        self.sid, _csrf = auth.session_new("admin", "admin")
        await db.chat_create("c1", "cweb2", None, "/tmp", "admin")
        await db.db_conn.execute(
            "UPDATE chats SET session_id = ? WHERE id = ?", (SESSION_ID, "c1")
        )
        await db.db_conn.commit()

    async def asyncTearDown(self):
        await _teardown(self)

    def _request(self, body):
        return SimpleNamespace(
            method="POST",
            url=SimpleNamespace(path="/api/chats/c1/stream"),
            cookies={"wc_session": self.sid},
            headers={"accept": "text/event-stream"},
            query_params={},
            client=SimpleNamespace(host="127.0.0.1"),
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value=body),
        )

    async def _events(self):
        response = await chat_routes.stream_handler(
            self._request({"content": "make it wider"}), "c1"
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
        return [
            json.loads(line[len("data: "):])
            for chunk in chunks
            for line in chunk.splitlines()
            if line.startswith("data: ")
        ]

    async def test_a_routed_request_never_spawns_a_turn(self):
        """The whole point: one process, and it is the one on screen."""
        stream = AsyncMock()
        with patch.object(
            prompts, "deliver_request",
            return_value={"delivered": True, "reason": "", "target": SCREEN},
        ), patch.object(runner, "stream_turn", stream):
            await self._events()
        stream.assert_not_called()

    async def test_the_user_message_is_still_recorded(self):
        with patch.object(
            prompts, "deliver_request",
            return_value={"delivered": True, "reason": "", "target": SCREEN},
        ):
            await self._events()
        activity = await db.chat_last_activity("admin")
        self.assertEqual(activity["c1"]["role"], "user")
        self.assertEqual(activity["c1"]["preview"], "make it wider")

    async def test_the_page_is_told_where_the_request_went(self):
        """Using an event type the client renders -- a new one would be
        silently dropped by conversation.js and show the user nothing."""
        with patch.object(
            prompts, "deliver_request",
            return_value={"delivered": True, "reason": "", "target": SCREEN},
        ):
            events = await self._events()
        kinds = [e.get("type") for e in events]
        self.assertIn("text", kinds)
        self.assertIn("done", kinds)
        notice = next(e for e in events if e.get("type") == "text")
        self.assertIn("terminal session", notice["content"])

    async def test_without_a_window_the_normal_turn_still_runs(self):
        async def _stream(*_a, **_kw):
            yield {"type": "text", "content": "answered headlessly"}
            yield {"type": "done"}

        with patch.object(
            prompts, "deliver_request",
            return_value={"delivered": False, "reason": "no live terminal window",
                          "target": None},
        ), patch.object(runner, "stream_turn", _stream), \
                patch.object(chat_routes, "_prepare_transcript_for_backend", AsyncMock()):
            events = await self._events()
        texts = [e.get("content") for e in events if e.get("type") == "text"]
        self.assertIn("answered headlessly", texts)


if __name__ == "__main__":
    unittest.main()
