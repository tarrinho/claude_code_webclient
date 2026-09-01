"""QA: a question asked in prose is marked as one in the conversation.

The console had two ways to notice an agent asking for something and both
missed the case that actually happens.

``transcripts.pending_question()`` reads the CLI transcript for an
``AskUserQuestion`` tool block with no ``tool_result``. That is what fills the
question bar, and it is exact -- but an agent that simply *writes* "Which do you
want?" produces no tool block at all, so the bar cannot see it however plainly
the question is put.

``_attention()`` does read text, and it feeds the sidebar highlight. But
``classify_chat`` judges a conversation by its **newest** message only. An agent
that asks and then carries on working buries its own question, and the highlight
goes out. That is not hypothetical: the cweb4 conversation asked three questions
in prose (13:48, 16:00, 19:00), was never answered, produced another ninety
minutes of routine output, and by then nothing anywhere in the console said a
question was outstanding. The user found them by reading the terminal.

So the mark lives on the **message**, where burying it is not possible. Every
message carries whether it asked something, the conversation view marks the ones
that did, and scrolling back finds them.

Two decisions worth stating because the alternatives look reasonable:

* The judgement is ``app._asks_a_question``, server-side, and the client is told
  the answer rather than deciding it. A copy of the heuristic in JavaScript
  would drift from the Python one, and this repository has already paid for that
  exact shape of duplication more than once.
* Only assistant messages are marked. A user message ending in "?" is the user
  asking Claude, which needs nothing from the user; marking it would put a
  "needs you" badge on the half of the conversation that is already theirs.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import app
import auth
import config
import db

ROOT = Path(__file__).resolve().parents[1]
CONVERSATION_JS = ROOT / "web" / "assets" / "conversation.js"
STYLES = ROOT / "web" / "assets" / "styles.css"

CHAT_ID = "beefcafe" * 4

# The real thing, from the transcript that exposed this. Kept verbatim rather
# than tidied: it is caveman-voiced, has a backticked path in the middle and two
# clauses before the "?", and a heuristic that only handles well-formed English
# would pass a prettier fixture and fail this.
REAL_ASK = ("Me ask before, you not answer yet: me drive browser with "
            "`/run`, or you look self?")


def _request():
    return SimpleNamespace(
        method="GET",
        url=SimpleNamespace(path=f"/api/chats/{CHAT_ID}"),
        cookies={},
        headers={"accept": "*/*"},
        query_params={},
        client=SimpleNamespace(host="127.0.0.1"),
        state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
        json=AsyncMock(return_value={}),
    )


class ServedMessagesTests(unittest.IsolatedAsyncioTestCase):
    """GET /api/chats/{id} is where the client learns which rows asked."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._patches = [
            unittest.mock.patch.object(config, "DB_PATH", f"{self.tmp.name}/db"),
            unittest.mock.patch.object(
                config, "PROJECTS_ROOT", f"{self.tmp.name}/projects"),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        await auth.bootstrap_admin()
        await db.chat_create(CHAT_ID, "cweb4", None, "/tmp", "admin")

    async def _messages(self):
        response = await app.handle_chat_get(_request(), CHAT_ID)
        return json.loads(response.body)["messages"]

    async def test_every_message_carries_the_flag(self):
        """Guards the rest: a missing key reads as false at every call site."""
        await db.messages_batch(CHAT_ID, [("user", "go"), ("assistant", "ok")])
        served = await self._messages()
        self.assertEqual(len(served), 2)
        for message in served:
            with self.subTest(role=message["role"]):
                self.assertIn("question", message)
                self.assertIsInstance(message["question"], bool)

    async def test_a_prose_question_is_marked(self):
        """The whole point, in the words that exposed the gap."""
        await db.messages_batch(CHAT_ID, [("user", "go"), ("assistant", REAL_ASK)])
        served = await self._messages()
        self.assertTrue(
            served[-1]["question"],
            "an agent asking in prose was not marked as asking")

    async def test_routine_output_is_not_marked(self):
        """A mark on everything carries no information."""
        await db.messages_batch(CHAT_ID, [
            ("user", "go"),
            ("assistant", "Reading the file now."),
            ("assistant", "I will check the tests:"),
        ])
        served = await self._messages()
        self.assertFalse(served[1]["question"])
        self.assertFalse(
            served[2]["question"],
            "a trailing colon is a reason to look, not a question to answer")

    async def test_a_user_question_is_not_marked(self):
        """The user asking Claude needs nothing from the user."""
        await db.messages_batch(CHAT_ID, [("user", "which file is it?")])
        served = await self._messages()
        self.assertFalse(
            served[0]["question"],
            "marked the user's own question as needing their attention")

    async def test_a_buried_question_is_still_marked(self):
        """The regression proper.

        The sidebar highlight reads the newest message only, so an agent that
        asks and keeps working erases its own question. This is that exact
        sequence -- ask, then ninety minutes of routine output -- and the mark
        has to survive it, because surviving it is the reason the mark is on the
        message rather than on the conversation.
        """
        await db.messages_batch(CHAT_ID, [
            ("user", "go"),
            ("assistant", REAL_ASK),
            ("assistant", "Reading the file now."),
            ("assistant", "`Bash(capture DOMContentLoaded)`"),
            ("assistant", "init() is wired correctly."),
        ])
        served = await self._messages()
        self.assertTrue(served[1]["question"], "the question lost its mark")
        self.assertFalse(served[-1]["question"], "the newest row is not an ask")
        self.assertEqual(
            [m["question"] for m in served], [False, True, False, False, False],
            "exactly one row asked something")

    async def test_several_questions_are_all_marked(self):
        """cweb4 asked three times before anyone noticed; all three count."""
        await db.messages_batch(CHAT_ID, [
            ("assistant", "Which do you want?"),
            ("assistant", "Reading the file."),
            ("assistant", REAL_ASK),
        ])
        served = await self._messages()
        self.assertEqual([m["question"] for m in served], [True, False, True])


class SingleSourceOfTruthTests(unittest.TestCase):
    """The judgement is made once, in Python."""

    def test_the_server_reuses_the_panel_heuristic(self):
        """Not a second definition of "is this a question".

        `_asks_a_question` already decides this for the supervisor panel's "?".
        A conversation that disagreed with the sidebar about the same message
        would be worse than either mark alone.
        """
        body = app.handle_chat_get.__code__.co_consts
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        served = source.split("async def handle_chat_get")[1].split(
            "\nasync def ")[0]
        self.assertIn("_asks_a_question", served,
                      "handle_chat_get must ask the shared helper")
        del body

    def test_the_client_does_not_re_derive_it(self):
        """A regex in JavaScript is a second heuristic that will drift."""
        js = CONVERSATION_JS.read_text(encoding="utf-8")
        for smell in ("endsWith('?')", 'endsWith("?")', "/\\?\\s*$/"):
            self.assertNotIn(
                smell, js,
                f"conversation.js decides questions itself via {smell!r}; the "
                "server already sent the answer")


class ConversationViewTests(unittest.TestCase):
    """The mark reaches the DOM, and is announced rather than colour-only."""

    def setUp(self):
        self.js = CONVERSATION_JS.read_text(encoding="utf-8")
        self.css = STYLES.read_text(encoding="utf-8")

    def test_the_flag_is_passed_into_the_message(self):
        self.assertRegex(
            self.js, r"createMessage\([^)]*message\.question",
            "renderMessages drops the flag before it reaches createMessage")

    def test_the_marker_is_built(self):
        self.assertIn("msg-asks", self.js,
                      "no question marker is created for a message")

    def test_the_marker_is_styled(self):
        self.assertIn(".msg-asks", self.css)

    def test_the_marker_is_not_colour_only(self):
        """A badge distinguished only by colour is invisible to a screen reader
        and to about one man in twelve."""
        block = self.js.split("msg-asks")[1][:600]
        self.assertTrue(
            "aria-label" in block or "title" in block,
            "the marker needs a text label, not just a colour")

    def test_the_marker_text_is_not_interpolated_markup(self):
        """Message content is agent output; the marker must not become a sink."""
        block = self.js.split("msg-asks")[1][:600]
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
            self.assertNotIn(sink, block)


if __name__ == "__main__":
    unittest.main()
