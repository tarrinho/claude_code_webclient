"""QA: declining a question instead of answering it.

Every control in the question bar answered the question, so a question that
did not deserve an answer had two ways out and both were bad: pick something
the user does not mean, which the session then acts on, or leave the prompt
blocking that session for as long as it takes somebody to walk to the
terminal.

Escape is what the prompt itself offers ("Esc to cancel"), and escape has been
in ``prompts._KEYS`` since the module was written -- so nothing new is
delivered here. What is new is what happens afterwards, and that is where the
tests concentrate:

* a snapshot that cannot be read is not a closed prompt. ``looks_like_a_prompt``
  is False for an empty string, so the obvious implementation reports success
  when it has gone blind -- the one claim that leaves a session blocked while
  the UI says it is free;
* *delivered* is reported separately from *ok*, because only one of the two
  failures is safe to retry. A key the terminal refused can be sent again; a
  key that went in and left the prompt open must not be, since the second
  escape reaches whatever the session moved on to and interrupts that.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import prompts
import transcripts
from routes import chats as chat_routes

SCREEN = {"kind": "screen", "session": "123.pts-6.host", "window": "2"}

PROMPT = """
 ☐ Weekend
Which of these is your preferred weekend activity?
❯ 1. Hiking outdoors
  2. Reading a book
  3. Type something.
Enter to select · ↑/↓ to navigate · Esc to cancel
"""

# What the window shows once the prompt has been cancelled: the session's
# ordinary output, with no numbered options and no key hints.
CLOSED = """
> Understood, I will leave that open.

  ⏵⏵ accept edits on (shift+tab to cycle)
"""



def _mounted_routes(application):
    """Every route the app serves, including those reached through a router."""
    out = []
    for route in application.routes:
        out.append(route)
        # An included router appears as one wrapper object, not as its routes;
        # effective_candidates() is how the wrapper hands them over.
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            out.extend(candidates())
    return out

class DismissKeystrokeQA(unittest.TestCase):
    """What is sent, and what is claimed about the result."""

    def _dismiss(self, snapshots, delivered=True):
        """Run dismiss() with a scripted view of the terminal.

        *snapshots* is what successive refresh() calls return, so a test can
        say "it still looked like a prompt afterwards" without depending on
        real timing.
        """
        sent: list[str] = []
        seen = list(snapshots)

        def fake_deliver(_target, key):
            sent.append(key)
            return delivered

        def fake_refresh(_target):
            return seen.pop(0) if seen else ""

        with patch.object(prompts, "deliver", fake_deliver), \
             patch.object(prompts, "refresh", fake_refresh), \
             patch.object(prompts.time, "sleep", lambda _s: None):
            return sent, prompts.dismiss(SCREEN)

    def test_a_closed_prompt_is_a_success(self):
        sent, result = self._dismiss([CLOSED])
        self.assertTrue(result["ok"])
        self.assertTrue(result["delivered"])
        self.assertEqual(sent, ["escape"])

    def test_escape_is_the_only_key_sent(self):
        """Not navigation, and above all not enter -- enter would confirm
        whatever the highlight happened to be sitting on, which is answering
        the question by accident and is the exact outcome being avoided."""
        sent, _ = self._dismiss([CLOSED])
        self.assertEqual(sent, ["escape"])
        self.assertNotIn("enter", sent)

    def test_a_refused_key_is_a_failure_that_may_be_retried(self):
        sent, result = self._dismiss([CLOSED], delivered=False)
        self.assertFalse(result["ok"])
        self.assertFalse(result["delivered"])
        self.assertIn("reach", result["reason"])
        self.assertEqual(sent, ["escape"], "one attempt, not a loop")

    def test_a_prompt_still_on_screen_is_a_failure_that_may_not(self):
        """delivered=True is the whole point of this case: the caller must not
        send a second escape, because the first one was accepted."""
        _sent, result = self._dismiss([PROMPT])
        self.assertFalse(result["ok"])
        self.assertTrue(result["delivered"])
        self.assertIn("still open", result["reason"])

    def test_an_unreadable_terminal_is_not_reported_as_closed(self):
        """The failing case of the obvious implementation.

        looks_like_a_prompt("") is False, so a version that only asked "does
        this still look like a prompt?" would turn an unreadable window into
        "the question is gone" -- and the session would sit blocked behind a UI
        that had stopped mentioning it.
        """
        _sent, result = self._dismiss([""])
        self.assertFalse(result["ok"])
        self.assertTrue(result["delivered"])
        self.assertIn("unknown", result["reason"])


class DismissAPIQA(unittest.IsolatedAsyncioTestCase):
    """The route, including which failures are retryable."""

    def _req(self, chat_id="c1"):
        return SimpleNamespace(
            method="DELETE",
            url=SimpleNamespace(path=f"/api/chats/{chat_id}/question"),
            cookies={}, headers={"accept": "*/*"}, query_params={},
            path_params={"chat_id": chat_id},
            client=SimpleNamespace(host="127.0.0.1"),
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value={}),
        )

    PENDING = {
        "id": "toolu_01",
        "needle": "Which of these",
        "questions": [{"question": "Which of these", "header": "Weekend",
                       "options": []}],
    }

    async def test_unknown_chat_is_a_404(self):
        from fastapi import HTTPException

        import app
        with patch.object(app.db, "chat_get", AsyncMock(return_value=None)), \
             self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_question_dismiss(self._req())
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_a_chat_with_no_session_is_a_400(self):
        from fastapi import HTTPException

        import app
        with patch.object(app.db, "chat_get",
                          AsyncMock(return_value={"session_id": None})), \
             self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_question_dismiss(self._req())
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_nothing_pending_is_a_success_not_a_conflict(self):
        """Answering a question that is gone is a 409, because the answer would
        land on whatever prompt appeared next. Declining to answer one that is
        gone has no such hazard -- the caller asked for it to be closed and it
        is closed, so reporting an error here would only teach the UI to show a
        failure for the state the user wanted."""
        import app
        with patch.object(app.db, "chat_get",
                          AsyncMock(return_value={"session_id": "s1"})), \
             patch.object(transcripts, "pending_question", return_value=None):
            response = await chat_routes.handle_chat_question_dismiss(self._req())
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertTrue(body["ok"])
        self.assertTrue(body["already_closed"])

    async def test_an_unreachable_session_is_a_409_that_did_not_deliver(self):
        import app
        with patch.object(app.db, "chat_get",
                          AsyncMock(return_value={"session_id": "s1"})), \
             patch.object(transcripts, "pending_question",
                          return_value=dict(self.PENDING)), \
             patch.object(prompts, "find_target", return_value=None):
            response = await chat_routes.handle_chat_question_dismiss(self._req())
        self.assertEqual(response.status_code, 409)
        body = json.loads(response.body)
        self.assertFalse(body["delivered"])
        self.assertIn("screen or tmux", body["error"])

    async def test_a_delivered_but_open_prompt_says_so_in_the_body(self):
        """The distinction the client needs. A 409 alone cannot say whether
        pressing the button again is safe, and prose in an error string is not
        something a client should be parsing to find out."""
        import app
        with patch.object(app.db, "chat_get",
                          AsyncMock(return_value={"session_id": "s1"})), \
             patch.object(transcripts, "pending_question",
                          return_value=dict(self.PENDING)), \
             patch.object(prompts, "find_target",
                          return_value={**SCREEN, "snapshot": PROMPT}), \
             patch.object(prompts, "dismiss",
                          return_value={"ok": False, "delivered": True,
                                        "reason": "The prompt is still open."}):
            response = await chat_routes.handle_chat_question_dismiss(self._req())
        self.assertEqual(response.status_code, 409)
        body = json.loads(response.body)
        self.assertTrue(body["delivered"])

    async def test_a_refused_key_says_it_did_not_deliver(self):
        import app
        with patch.object(app.db, "chat_get",
                          AsyncMock(return_value={"session_id": "s1"})), \
             patch.object(transcripts, "pending_question",
                          return_value=dict(self.PENDING)), \
             patch.object(prompts, "find_target",
                          return_value={**SCREEN, "snapshot": PROMPT}), \
             patch.object(prompts, "dismiss",
                          return_value={"ok": False, "delivered": False,
                                        "reason": "Could not reach the terminal."}):
            response = await chat_routes.handle_chat_question_dismiss(self._req())
        self.assertEqual(response.status_code, 409)
        self.assertFalse(json.loads(response.body)["delivered"])

    async def test_a_successful_dismissal_is_recorded(self):
        """Answering logs who answered what; declining has to be as
        attributable, or a session that stopped waiting has no explanation
        anywhere for why."""
        import app
        with patch.object(app.db, "chat_get",
                          AsyncMock(return_value={"session_id": "s1"})), \
             patch.object(transcripts, "pending_question",
                          return_value=dict(self.PENDING)), \
             patch.object(prompts, "find_target",
                          return_value={**SCREEN, "snapshot": PROMPT}), \
             patch.object(prompts, "dismiss",
                          return_value={"ok": True, "delivered": True}), \
             self.assertLogs("wc.app", level="INFO") as logs:
            response = await chat_routes.handle_chat_question_dismiss(self._req())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.body)["dismissed"])
        line = "\n".join(logs.output)
        self.assertIn("question_dismissed", line)
        self.assertIn("toolu_01", line)
        self.assertIn("admin", line)

    async def test_the_route_is_registered_for_delete(self):
        """The handler is reachable only if the method is wired. A DELETE that
        404s at the router would fail no other test in this file, because every
        one of them calls the handler directly."""
        import app
        methods = {
            method
            # Not app.app.routes: that list holds one opaque entry per
            # include_router call, so the real routes are a level down and
            # this set came back empty rather than wrong.
            for route in _mounted_routes(app.app)
            if getattr(route, "path", "") == "/api/chats/{chat_id}/question"
            for method in (getattr(route, "methods", None) or set())
        }
        self.assertIn("DELETE", methods)
        # The other two must survive the addition.
        self.assertIn("GET", methods)
        self.assertIn("POST", methods)

    async def test_delete_is_csrf_guarded(self):
        """A prompt closed by a cross-site request is a session unblocked by
        somebody who is not the user. DELETE was already in the middleware's
        set -- this pins it, because a future narrowing of that set would
        silently open this route."""
        import app
        self.assertIn("DELETE", app.CsrfMiddleware._MUTATING)


if __name__ == "__main__":
    unittest.main()
