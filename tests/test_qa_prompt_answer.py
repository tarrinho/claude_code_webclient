"""QA: answering a terminal session's prompt from the browser.

A question asked by an interactive Claude Code session is unreachable through
the obvious channels -- AskUserQuestion is absent from every --print turn's tool
list, a cross-session message is not processed while the session is blocked in
the prompt, dev.tty.legacy_tiocsti is 0, and the session's stdin is a tty rather
than a pipe. The terminal multiplexer hosting it is the channel that works.

The risky part is not delivery but aim: Enter confirms whatever is highlighted,
and the highlight is wherever it was last left. So these tests concentrate on
the guards -- that a target is only chosen once the prompt is confirmed on
screen, that the selection is verified as it moves, and that nothing but a
navigation key or an option digit can ever be delivered.
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest
from shutil import rmtree as shutil_rmtree
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, patch

import prompts
import transcripts
from routes import chats as chat_routes

SCREEN = {"kind": "screen", "session": "123.pts-6.host", "window": "2"}

PROMPT = """
 ☐ Weekend
Which of these is your preferred weekend activity?
❯ 1. Hiking outdoors
     Fresh air
  2. Reading a book
  3. Coding a side project
  4. Watching a movie
  5. Type something.
  6. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel
"""


def _prompt_at(index: int) -> str:
    """The same prompt with the highlight on *index*."""
    out = []
    for line in PROMPT.splitlines():
        stripped = line.replace("❯", " ", 1) if "❯" in line else line
        if stripped.strip().startswith(f"{index}."):
            stripped = "❯" + stripped[1:]
        out.append(stripped)
    return "\n".join(out)


# ── What can be delivered ─────────────────────────────────────────────────────


class DeliveryGuardQA(unittest.TestCase):
    def test_only_navigation_keys_and_digits_are_accepted(self):
        for good in ("enter", "up", "down", "escape", "1", "9"):
            self.assertTrue(good in prompts._KEYS or good in prompts._DIGITS, good)

    def test_arbitrary_text_is_refused(self):
        # This must never become a way to type into somebody's terminal.
        with patch.object(prompts, "_run") as run:
            for bad in ("rm -rf /", "ls\n", "", "0", "10", "yes", "\r\nwhoami",
                        "enter; rm -rf /", None, 3):
                self.assertFalse(prompts.deliver(SCREEN, bad), repr(bad))
            run.assert_not_called()

    def test_an_unknown_target_kind_is_refused(self):
        with patch.object(prompts, "_run") as run:
            self.assertFalse(prompts.deliver({"kind": "carrier-pigeon"}, "enter"))
            self.assertFalse(prompts.deliver({}, "enter"))
            self.assertFalse(prompts.deliver("not a dict", "enter"))
            run.assert_not_called()

    def test_screen_delivery_uses_a_fixed_argv(self):
        calls = []
        def fake_run(argv):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(prompts, "_run", fake_run):
            self.assertTrue(prompts.deliver(SCREEN, "enter"))
        self.assertEqual(calls[0][:2], ["screen", "-S"])
        self.assertIn("stuff", calls[0])
        self.assertNotIn("-c", calls[0])          # never a shell
        self.assertEqual(calls[0][-1], "\r")

    def test_tmux_delivery_sends_literally(self):
        calls = []
        def fake_run(argv):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(prompts, "_run", fake_run):
            self.assertTrue(prompts.deliver({"kind": "tmux", "window": "%3"}, "2"))
        self.assertEqual(calls[0][:2], ["tmux", "send-keys"])
        self.assertIn("-l", calls[0])             # literal, not a key name


# ── Reading the prompt off the screen ─────────────────────────────────────────


class SnapshotReadingQA(unittest.TestCase):
    def test_the_highlighted_option_is_identified(self):
        self.assertEqual(prompts.selected_index(PROMPT), 1)
        self.assertEqual(prompts.selected_index(_prompt_at(3)), 3)

    def test_no_highlight_reads_as_unknown(self):
        self.assertIsNone(prompts.selected_index("nothing here"))
        self.assertIsNone(prompts.selected_index(""))

    def test_every_visible_option_is_reported(self):
        options = prompts.visible_options(PROMPT)
        self.assertEqual([o["index"] for o in options], [1, 2, 3, 4, 5, 6])

    def test_options_the_tool_call_never_declared_are_included(self):
        # The live prompt offers free text and "Chat about this" on top of the
        # declared answers; hiding them would hide real choices.
        labels = [o["label"] for o in prompts.visible_options(PROMPT)]
        self.assertIn("Type something.", labels)
        self.assertIn("Chat about this", labels)

    def test_the_selected_flag_follows_the_cursor(self):
        options = prompts.visible_options(_prompt_at(4))
        self.assertEqual([o["index"] for o in options if o["selected"]], [4])

    def test_duplicate_numbers_are_not_double_counted(self):
        doubled = PROMPT + "\n  1. Hiking outdoors\n"
        self.assertEqual(len(prompts.visible_options(doubled)), 6)


# ── Aiming before firing ──────────────────────────────────────────────────────


class AimingQA(unittest.TestCase):
    """The window a keystroke goes to.

    ``_environ`` is patched per-pid rather than with a single return value,
    because ``locate`` now reads the environment of two different processes --
    the session's and its own -- and a fixture that answers identically for
    both cannot tell the guard from the thing it guards against.
    """

    SERVER: ClassVar[dict[str, str]] = {"STY": "server.host", "WINDOW": "0"}

    def _locate(self, session_id, env_by_pid, session_pid=99, chain=None):
        chain = chain or [(session_pid, "claude")]
        our_pid = os.getpid()
        env_by_pid = {our_pid: self.SERVER, **env_by_pid}
        with patch.object(prompts, "session_pid", return_value=session_pid), \
             patch.object(prompts, "_parents",
                          side_effect=lambda pid, limit=12: (
                              [(our_pid, "python3")] if pid == our_pid else chain)), \
             patch.object(prompts, "_environ",
                          side_effect=lambda pid: env_by_pid.get(pid, {})):
            return prompts.locate(session_id)

    def test_the_window_comes_from_the_process_environment(self):
        # screen exports STY and WINDOW into every window, so the window is
        # stated rather than guessed.
        target = self._locate("sess", {99: {"STY": "123.pts-6.host", "WINDOW": "2"}})
        self.assertEqual(
            {k: target[k] for k in ("kind", "session", "window")},
            {"kind": "screen", "session": "123.pts-6.host", "window": "2"})

    def test_a_tmux_pane_is_identified_by_its_own_variable(self):
        target = self._locate(
            "sess", {99: {"TMUX_PANE": "%7", "TMUX": "/tmp/sock,1,0"}})
        self.assertEqual(target["window"], "%7")

    def test_a_session_outside_a_multiplexer_has_no_window(self):
        self.assertIsNone(self._locate("sess", {99: {"TERM": "xterm"}}))

    def test_two_sessions_in_one_screen_are_not_confused(self):
        """The bug this replaced: selection by screen content.

        cweb3 and cweb5 shared a screen session. Quoting cweb5's question in
        cweb3's own output was enough for a content match to pick cweb3's
        window, which would have typed the answer into the wrong terminal.
        Observed live, not hypothetical -- so the window must come from the
        process, and content is only ever a confirmation.
        """
        for who, window in (("other", "3"), ("target", "2")):
            target = self._locate(who, {99: {"STY": "1.host", "WINDOW": window}})
            self.assertEqual(target["window"], window, who)

    def test_sharing_the_servers_window_is_reported_not_refused(self):
        """The obvious guard here is wrong, so it is a flag rather than a veto.

        The console runs inside screen and an interactive agent runs in the same
        window. Refusing our own window would refuse that agent -- the one the
        routing feature exists to reach -- while the hole it was meant to close
        (a session file naming the server's own pid) is already closed by the
        ancestry check in session_pid.
        """
        target = self._locate("sess", {99: dict(self.SERVER)})
        self.assertIsNotNone(target)
        self.assertTrue(target["shares_server_window"])

    def test_a_window_elsewhere_is_not_flagged(self):
        target = self._locate("sess", {99: {"STY": "other.host", "WINDOW": "0"}})
        self.assertFalse(target["shares_server_window"])

    def test_a_sibling_window_in_the_servers_screen_is_not_flagged(self):
        # Same screen session, different window: not the server's window.
        sibling = {"STY": self.SERVER["STY"], "WINDOW": "4"}
        target = self._locate("sess", {99: sibling})
        self.assertEqual(target["window"], "4")
        self.assertFalse(target["shares_server_window"])


class SessionPidTrustQA(unittest.TestCase):
    """``~/.claude/sessions/*.json`` is a claim, not an authority (F-02)."""

    def _sessions_dir(self, files):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil_rmtree, tmp)
        for name, body in files.items():
            (pathlib.Path(tmp) / name).write_text(json.dumps(body))
        return pathlib.Path(tmp)

    def _resolve(self, files, claude_pids, ancestors=(4242,)):
        directory = self._sessions_dir(files)
        with patch.object(prompts, "_SESSIONS_DIR", directory), \
             patch.object(prompts, "_is_claude",
                          side_effect=lambda pid: pid in claude_pids), \
             patch.object(prompts, "_self_and_ancestors",
                          return_value=set(ancestors)):
            return prompts.session_pid("sess-1")

    def test_a_live_claude_pid_resolves(self):
        self.assertEqual(
            self._resolve({"501.json": {"sessionId": "sess-1", "pid": 501}}, {501}),
            501)

    def test_a_pid_that_is_not_claude_is_refused(self):
        # Otherwise a file naming any live pid aims keystrokes at its terminal.
        self.assertIsNone(
            self._resolve({"501.json": {"sessionId": "sess-1", "pid": 501}}, set()))

    def test_the_servers_own_ancestry_is_refused(self):
        self.assertIsNone(
            self._resolve({"a.json": {"sessionId": "sess-1", "pid": 4242}},
                          {4242}, ancestors=(4242,)))

    def test_two_pids_claiming_one_session_is_refused_not_resolved(self):
        """A contradiction is an attack signal, so neither claim wins.

        Choosing one would let a planted file race the real entry, and the
        planted one only has to be read first.
        """
        self.assertIsNone(self._resolve(
            {"501.json": {"sessionId": "sess-1", "pid": 501},
             "777.json": {"sessionId": "sess-1", "pid": 777}},
            {501, 777}))

    def test_the_same_pid_named_twice_is_not_a_contradiction(self):
        # The filename and the recorded pid agreeing is the normal case.
        self.assertEqual(self._resolve(
            {"501.json": {"sessionId": "sess-1", "pid": 501}}, {501}), 501)

    def test_an_unrelated_session_is_ignored(self):
        self.assertIsNone(self._resolve(
            {"501.json": {"sessionId": "other", "pid": 501}}, {501}))

    def test_is_claude_survives_a_bare_version_number_as_comm(self):
        """Real process, real /proc -- every other case here mocks `_is_claude`
        itself, which is exactly how this bug shipped unnoticed.

        The CLI installs each version as
        ~/.local/share/claude/versions/<version> and is exec'd from that path
        directly, so the kernel's `comm` for a live session is the bare
        version string ("2.1.260"), containing no "claude" at all. Every
        session on the host failed the old comm-only check at once, silently:
        a failed identity check here reads as "no pending prompt", not an
        error. `cmdline`'s first argument is still the launch path, which does
        contain it.
        """
        import shutil
        import subprocess
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil_rmtree, tmp)
        fake_install = pathlib.Path(tmp) / "claude" / "versions"
        fake_install.mkdir(parents=True)
        fake_binary = fake_install / "9.9.9"
        shutil.copy("/bin/sleep", fake_binary)
        proc = subprocess.Popen([str(fake_binary), "30"])
        self.addCleanup(proc.kill)
        try:
            self.assertEqual(
                pathlib.Path(f"/proc/{proc.pid}/comm").read_text().strip(),
                "9.9.9",
                "the fixture's own premise -- comm must not contain 'claude'",
            )
            self.assertTrue(prompts._is_claude(proc.pid))
        finally:
            proc.kill()
            proc.wait()

    def test_is_claude_still_refuses_an_unrelated_process(self):
        """The fallback must not turn into "anything alive counts"."""
        import subprocess
        proc = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(proc.kill)
        try:
            self.assertFalse(prompts._is_claude(proc.pid))
        finally:
            proc.kill()
            proc.wait()

    def test_a_window_that_is_not_prompting_is_not_a_target(self):
        # Identity alone is not enough: navigation keys sent to a window that
        # is not asking anything would type into whatever it is doing.
        with patch.object(prompts, "locate",
                          return_value={"kind": "screen", "session": "s",
                                        "window": "2"}), \
             patch.object(prompts, "refresh", return_value="just some output"):
            self.assertIsNone(prompts.find_target("sess"))

    def test_a_prompt_is_recognised_by_its_shape(self):
        self.assertTrue(prompts.looks_like_a_prompt(PROMPT))
        self.assertFalse(prompts.looks_like_a_prompt("● Ran a command\n❯ "))
        self.assertFalse(prompts.looks_like_a_prompt(""))
        # Options without the confirmation hint are not a live prompt.
        self.assertFalse(prompts.looks_like_a_prompt("  1. one\n  2. two\n"))

    def test_a_mismatched_needle_refuses_the_target(self):
        with patch.object(prompts, "locate",
                          return_value={"kind": "screen", "session": "s",
                                        "window": "2"}), \
             patch.object(prompts, "refresh", return_value=PROMPT):
            self.assertIsNone(prompts.find_target("sess", "a different question"))
            self.assertIsNotNone(prompts.find_target("sess", "Which of these"))

    def test_answer_refuses_when_the_selection_cannot_be_seen(self):
        with patch.object(prompts, "refresh", return_value="no prompt here"):
            result = prompts.answer(SCREEN, 2)
        self.assertFalse(result["ok"])
        self.assertIn("selected", result["reason"].lower())

    def test_answer_refuses_an_option_not_on_offer(self):
        with patch.object(prompts, "refresh", return_value=PROMPT):
            result = prompts.answer(SCREEN, 9)
        self.assertFalse(result["ok"])
        self.assertIn("not on offer", result["reason"])

    def test_answer_navigates_then_confirms(self):
        sent, position = [], [1]
        def fake_deliver(_target, key):
            sent.append(key)
            if key == "down":
                position[0] += 1
            elif key == "up":
                position[0] -= 1
            return True
        with patch.object(prompts, "deliver", fake_deliver), \
             patch.object(prompts, "refresh", lambda _t: _prompt_at(position[0])), \
             patch.object(prompts.time, "sleep", lambda _s: None):
            result = prompts.answer(SCREEN, 3)
        self.assertTrue(result["ok"])
        self.assertEqual(result["label"], "Coding a side project")
        # Two moves down, then exactly one confirmation.
        self.assertEqual(sent, ["down", "down", "enter"])

    def test_answer_moves_upward_when_needed(self):
        position = [4]
        sent = []
        def fake_deliver(_target, key):
            sent.append(key)
            position[0] += 1 if key == "down" else -1 if key == "up" else 0
            return True
        with patch.object(prompts, "deliver", fake_deliver), \
             patch.object(prompts, "refresh", lambda _t: _prompt_at(position[0])), \
             patch.object(prompts.time, "sleep", lambda _s: None):
            result = prompts.answer(SCREEN, 2)
        self.assertTrue(result["ok"])
        self.assertEqual(sent, ["up", "up", "enter"])

    def test_answer_stops_if_the_highlight_does_not_move(self):
        # Otherwise it would keep pressing down and then confirm whatever was
        # under the cursor -- the wrong answer, silently.
        sent = []
        def fake_deliver(_target, key):
            sent.append(key)
            return True
        with patch.object(prompts, "deliver", fake_deliver), \
             patch.object(prompts, "refresh", lambda _t: _prompt_at(1)), \
             patch.object(prompts.time, "sleep", lambda _s: None):
            result = prompts.answer(SCREEN, 3)
        self.assertFalse(result["ok"])
        self.assertNotIn("enter", sent)

    def test_answer_never_confirms_when_delivery_fails(self):
        with patch.object(prompts, "deliver", lambda _t, _k: False), \
             patch.object(prompts, "refresh", return_value=PROMPT):
            result = prompts.answer(SCREEN, 3)
        self.assertFalse(result["ok"])

    def test_answering_the_already_highlighted_option_sends_only_enter(self):
        sent = []
        with patch.object(prompts, "deliver",
                          lambda _t, k: (sent.append(k), True)[1]), \
             patch.object(prompts, "refresh", return_value=PROMPT), \
             patch.object(prompts.time, "sleep", lambda _s: None):
            result = prompts.answer(SCREEN, 1)
        self.assertTrue(result["ok"])
        self.assertEqual(sent, ["enter"])


# ── The API contract ──────────────────────────────────────────────────────────


class QuestionAPIQA(unittest.IsolatedAsyncioTestCase):
    def _req(self, chat_id="c1", body=None):
        return SimpleNamespace(
            method="POST" if body is not None else "GET",
            url=SimpleNamespace(path=f"/api/chats/{chat_id}/question"),
            cookies={}, headers={"accept": "*/*"}, query_params={},
            path_params={"chat_id": chat_id},
            client=SimpleNamespace(host="127.0.0.1"),
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value=body if body is not None else {}),
        )

    async def test_unknown_chat_is_a_404(self):
        from fastapi import HTTPException

        import app
        with patch.object(app.db, "chat_get", AsyncMock(return_value=None)), \
             self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_question_get(self._req())
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_a_chat_with_no_session_is_not_pending(self):
        import app
        with patch.object(app.db, "chat_get",
                          AsyncMock(return_value={"session_id": None})):
            body = json.loads((await chat_routes.handle_chat_question_get(self._req())).body)
        self.assertFalse(body["pending"])

    async def test_no_question_reports_not_pending(self):
        import app
        with patch.object(app.db, "chat_get",
                          AsyncMock(return_value={"session_id": "s1"})), \
             patch.object(transcripts, "pending_question", return_value=None):
            body = json.loads((await chat_routes.handle_chat_question_get(self._req())).body)
        self.assertFalse(body["pending"])

    async def test_an_unreachable_session_is_pending_but_not_answerable(self):
        import app
        pending = {"id": "q1", "needle": "Which of these",
                   "questions": [{"question": "Which of these", "header": "W",
                                  "options": [{"label": "A", "description": ""}]}]}
        with patch.object(app.db, "chat_get",
                          AsyncMock(return_value={"session_id": "s1"})), \
             patch.object(transcripts, "pending_question", return_value=pending), \
             patch.object(prompts, "find_target", return_value=None):
            body = json.loads((await chat_routes.handle_chat_question_get(self._req())).body)
        self.assertTrue(body["pending"])
        self.assertFalse(body["answerable"])
        self.assertIn("screen or tmux", body["reason"])
        self.assertNotIn("needle", body)

    async def test_the_live_options_are_returned(self):
        import app
        pending = {"id": "q1", "needle": "Which of these",
                   "questions": [{"question": "Which of these", "header": "W",
                                  "options": []}]}
        with patch.object(app.db, "chat_get",
                          AsyncMock(return_value={"session_id": "s1"})), \
             patch.object(transcripts, "pending_question", return_value=pending), \
             patch.object(prompts, "find_target",
                          return_value={**SCREEN, "snapshot": PROMPT}):
            body = json.loads((await chat_routes.handle_chat_question_get(self._req())).body)
        self.assertTrue(body["answerable"])
        self.assertEqual(body["selected"], 1)
        self.assertEqual(len(body["options"]), 6)

    async def test_answer_validates_the_index(self):
        from fastapi import HTTPException

        import app
        for bad in ({"index": 0}, {"index": 10}, {"index": "three"},
                    {"index": None}, {}):
            with patch.object(app.db, "chat_get",
                              AsyncMock(return_value={"session_id": "s1"})), \
                 self.assertRaises(HTTPException, msg=str(bad)) as ctx:
                await chat_routes.handle_chat_question_answer(self._req(body=bad))
            self.assertEqual(ctx.exception.status_code, 400, str(bad))

    async def test_answering_with_nothing_pending_is_a_conflict(self):
        from fastapi import HTTPException

        import app
        with patch.object(app.db, "chat_get",
                          AsyncMock(return_value={"session_id": "s1"})), \
             patch.object(transcripts, "pending_question", return_value=None), \
             self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_question_answer(self._req(body={"index": 1}))
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_a_failed_answer_surfaces_as_a_conflict_not_a_success(self):
        from fastapi import HTTPException

        import app
        pending = {"id": "q1", "needle": "Which of these", "questions": [{}]}
        with patch.object(app.db, "chat_get",
                          AsyncMock(return_value={"session_id": "s1"})), \
             patch.object(transcripts, "pending_question", return_value=pending), \
             patch.object(prompts, "find_target",
                          return_value={**SCREEN, "snapshot": PROMPT}), \
             patch.object(prompts, "answer",
                          return_value={"ok": False, "reason": "did not move"}), \
             self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_question_answer(self._req(body={"index": 2}))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("did not move", str(ctx.exception.detail))


if __name__ == "__main__":
    unittest.main()
