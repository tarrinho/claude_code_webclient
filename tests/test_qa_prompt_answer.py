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
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import prompts

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
    def test_a_target_is_refused_without_a_needle(self):
        # Delivering to a window we have not confirmed shows the prompt could
        # type into an unrelated terminal.
        self.assertIsNone(prompts.find_target("sess", ""))
        self.assertIsNone(prompts.find_target("sess", "   "))

    def test_a_session_with_no_multiplexer_has_no_target(self):
        with patch.object(prompts, "session_pid", return_value=4242), \
             patch.object(prompts, "_parents", return_value=[(4242, "claude"),
                                                             (1, "systemd")]):
            self.assertIsNone(prompts.find_target("sess", "Which of these"))

    def test_only_the_hosting_screen_session_is_considered(self):
        # A coincidental text match in someone else's screen must not be picked.
        with patch.object(prompts, "session_pid", return_value=99), \
             patch.object(prompts, "_parents",
                          return_value=[(99, "claude"), (123, "screen")]), \
             patch.object(prompts, "_screen_sessions",
                          return_value=["999.pts-1.host", "123.pts-6.host"]), \
             patch.object(prompts, "_screen_windows", return_value=["2"]), \
             patch.object(prompts, "screen_snapshot", return_value=PROMPT):
            target = prompts.find_target("sess", "Which of these")
        self.assertEqual(target["session"], "123.pts-6.host")

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
            await app.handle_chat_question_get(self._req())
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_a_chat_with_no_session_is_not_pending(self):
        import app
        with patch.object(app.db, "chat_get",
                          AsyncMock(return_value={"session_id": None})):
            body = json.loads((await app.handle_chat_question_get(self._req())).body)
        self.assertFalse(body["pending"])

    async def test_no_question_reports_not_pending(self):
        import app
        with patch.object(app.db, "chat_get",
                          AsyncMock(return_value={"session_id": "s1"})), \
             patch.object(app.transcripts, "pending_question", return_value=None):
            body = json.loads((await app.handle_chat_question_get(self._req())).body)
        self.assertFalse(body["pending"])

    async def test_an_unreachable_session_is_pending_but_not_answerable(self):
        import app
        pending = {"id": "q1", "needle": "Which of these",
                   "questions": [{"question": "Which of these", "header": "W",
                                  "options": [{"label": "A", "description": ""}]}]}
        with patch.object(app.db, "chat_get",
                          AsyncMock(return_value={"session_id": "s1"})), \
             patch.object(app.transcripts, "pending_question", return_value=pending), \
             patch.object(app.prompts, "find_target", return_value=None):
            body = json.loads((await app.handle_chat_question_get(self._req())).body)
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
             patch.object(app.transcripts, "pending_question", return_value=pending), \
             patch.object(app.prompts, "find_target",
                          return_value={**SCREEN, "snapshot": PROMPT}):
            body = json.loads((await app.handle_chat_question_get(self._req())).body)
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
                await app.handle_chat_question_answer(self._req(body=bad))
            self.assertEqual(ctx.exception.status_code, 400, str(bad))

    async def test_answering_with_nothing_pending_is_a_conflict(self):
        from fastapi import HTTPException

        import app
        with patch.object(app.db, "chat_get",
                          AsyncMock(return_value={"session_id": "s1"})), \
             patch.object(app.transcripts, "pending_question", return_value=None), \
             self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_question_answer(self._req(body={"index": 1}))
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_a_failed_answer_surfaces_as_a_conflict_not_a_success(self):
        from fastapi import HTTPException

        import app
        pending = {"id": "q1", "needle": "Which of these", "questions": [{}]}
        with patch.object(app.db, "chat_get",
                          AsyncMock(return_value={"session_id": "s1"})), \
             patch.object(app.transcripts, "pending_question", return_value=pending), \
             patch.object(app.prompts, "find_target",
                          return_value={**SCREEN, "snapshot": PROMPT}), \
             patch.object(app.prompts, "answer",
                          return_value={"ok": False, "reason": "did not move"}), \
             self.assertRaises(HTTPException) as ctx:
            await app.handle_chat_question_answer(self._req(body={"index": 2}))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("did not move", str(ctx.exception.detail))


if __name__ == "__main__":
    unittest.main()
