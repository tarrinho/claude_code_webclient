"""QA: the left panel marks which highlighted rows actually asked something.

Every row in the Supervisor section is waiting on the user, so a mark on all of
them would carry no information. What it distinguishes is the rows that asked a
question from the rows that failed, reported a blocker, or simply stopped
talking -- three states that also sit there waiting and are answered by doing
something other than typing a reply.

The signal is deliberately narrower than _attention(). That function returns
"asks" for a trailing colon, for phrases that request input, and -- in the CLI
branch -- from a session's status alone, where no text has been read at all. A
"?" is a claim that there is a question to answer, so it is made only where one
is actually visible.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

import app
import classification
import shared

ROOT = Path(__file__).resolve().parents[1]
CHAT_LIST = ROOT / "web" / "assets" / "chat-list.js"
STYLES = ROOT / "web" / "assets" / "styles.css"

def _classifier_source() -> str:
    """The module that owns classify_chat and its waiting rows.

    app.py until 0.10.0, classification.py after it. Both are read and joined
    so the assertions hold either side of the split rather than needing the two
    changes to land in one commit.
    """
    parts = []
    for name in ("classification.py", "app.py"):
        path = ROOT / name
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8"))
    if not parts:
        raise AssertionError("neither classification.py nor app.py is readable")
    return "\n".join(parts)



class SignalTests(unittest.TestCase):
    """_asks_a_question is the whole judgement; test it directly."""

    def test_a_plain_question_counts(self):
        self.assertTrue(app._asks_a_question("Shall I commit this?"))

    def test_a_question_inside_closing_punctuation_counts(self):
        """A question can end in a quote or bracket and still be a question."""
        for text in ('Ready to go?"', "Ready to go?)", "Ready to go?`"):
            with self.subTest(text=text):
                self.assertTrue(app._asks_a_question(text))

    def test_the_pending_note_counts(self):
        """The strongest evidence: a structured question rendered to text.

        Its presence means a question block exists with no answer, which is the
        same condition that raises the answerable question bar.
        """
        text = f"Which backend should I use? {shared._QUESTION_PENDING_NOTE}"
        self.assertTrue(app._asks_a_question(text))

    def test_a_trailing_colon_does_not_count(self):
        """An invitation to read on, not a question.

        This asserted _attention("...:") == "asks" as well, to document the
        contrast. That was false coupling: _attention is not this feature's
        function, someone else has since dropped its colon rule, and the test
        failed for a change that was both correct and none of its business. A
        test should pin the behaviour it is named for and nothing else.
        """
        self.assertFalse(app._asks_a_question("Here are the options:"))
        self.assertFalse(app._asks_a_question("Here is what I found:"))

    def test_a_request_for_input_does_not_count(self):
        self.assertFalse(app._asks_a_question("Let me know how you want to proceed."))

    def test_a_mid_sentence_question_mark_does_not_count(self):
        """Only the end of the message counts, matching _attention()."""
        self.assertFalse(
            app._asks_a_question("You asked whether it works? It does. All green."))

    def test_empty_and_missing_text_are_safe(self):
        for text in ("", "   ", None):
            with self.subTest(text=text):
                self.assertFalse(app._asks_a_question(text))


class PanelTests(unittest.TestCase):
    """The marker has to be rendered, and rendered only when the flag is set."""

    def setUp(self):
        self.source = CHAT_LIST.read_text(encoding="utf-8")

    def test_the_marker_is_rendered_for_waiting_rows(self):
        self.assertIn("supervisor-asks", self.source)
        self.assertIn("'?'", self.source)

    def test_it_is_guarded_by_the_server_flag(self):
        """Not by reason: "asks" is set from session status with nothing read."""
        self.assertIn("if (entry.question)", self.source)

    def test_it_carries_a_text_alternative(self):
        """The glyph alone is meaningless to a screen reader."""
        block = self.source.split("supervisor-asks", 1)[1][:400]
        self.assertIn("aria-label", block)
        self.assertIn("title", block)

    def test_the_marker_is_styled(self):
        self.assertIn(".supervisor-asks{", STYLES.read_text(encoding="utf-8"))

    def test_the_stylesheet_cache_key_was_bumped(self):
        """A new rule nobody can see is not a fix.

        index.html asks for styles.css with a ?v= key; leaving it alone serves
        every returning browser the cached sheet, and the marker renders as an
        unstyled bare "?" against the title.
        """
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        match = re.search(r"styles\.css\?v=(\d+)", html)
        self.assertIsNotNone(match, "the cache key is gone from index.html")
        self.assertGreaterEqual(int(match.group(1)), 26)


class LongMessageTests(unittest.TestCase):
    """A question must not stop counting because the message got long.

    classify_chat derived `reason` from the preview (first 200 chars) while
    deriving `question` from the tail (last 200). Anything longer than roughly
    400 characters therefore had its question read from one end and its
    classification from the other: the row fell past the "asks" branch into the
    `done` promotion and was announced as finished work, unmarked, while the
    agent waited. Found by cweb4; the asymmetry was mine.
    """

    def _classify(self, content: str) -> dict:
        chat = {"id": "c1", "title": "t", "session_id": "", "archived": False}
        last = {
            "role": "assistant",
            "created_at": "2026-09-01T15:00:00",
            "preview": content[:200],
            "tail": content[-200:],
        }
        return classification.classify_chat(chat, last, set(), {}, {}, {}, {}, {})

    FILLER = "I compared the three options and checked each one carefully. " * 10

    def test_a_short_question_is_an_ask(self):
        """The control: this always worked, and must keep working."""
        row = self._classify("Shall I commit this?")
        self.assertEqual(row["reason"], "asks")
        self.assertTrue(row["question"])

    def test_the_same_question_at_the_end_of_a_long_message_is_too(self):
        row = self._classify(self.FILLER + "Shall I commit this?")
        self.assertEqual(row["reason"], "asks",
                         "a long message's question was announced as finished work")
        self.assertTrue(row["question"], "and carried no marker")

    def test_a_pending_structured_question_survives_a_long_message(self):
        """The worse half: this one is definitely unanswered."""
        row = self._classify(
            self.FILLER + f"Which one do you want? {shared._QUESTION_PENDING_NOTE}")
        self.assertEqual(row["reason"], "asks")
        self.assertTrue(row["question"])

    def test_a_long_message_that_asks_nothing_is_still_not_an_ask(self):
        """The fix must not promote every long message into the highlights."""
        row = self._classify(self.FILLER + "All twelve tests pass.")
        self.assertNotEqual(row.get("reason"), "asks")
        self.assertFalse(row.get("question"))


class WiringTests(unittest.TestCase):
    """Every row the panel can highlight must carry the flag."""

    def test_each_waiting_entry_sets_question(self):
        """A row without the key renders no marker, whatever it asked.

        Parsed rather than counted. Counting occurrences of the key looked
        equivalent and was not: ``if kind == "question":`` contains the literal
        `"question":` too, so a substring tally passed with two of its matches
        being comparisons rather than keys, and would have kept passing after a
        genuinely unflagged row was added. Every dict that says it is a waiting
        row is now required to carry the flag, which is the actual contract --
        eight sessions edit this file and two of these sites were written by
        someone else after this test.
        """
        tree = ast.parse(_classifier_source())
        waiting: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            pairs = {
                k.value: v for k, v in zip(node.keys, node.values)
                if isinstance(k, ast.Constant)
            }
            status = pairs.get("status")
            if (isinstance(status, ast.Constant) and status.value == "waiting"
                    and "question" not in pairs):
                waiting.append(node.lineno)
        self.assertEqual(
            waiting, [],
            f"waiting rows built without a 'question' flag at lines {waiting}; "
            "they render no marker however they were worded",
        )

    def test_the_check_above_can_actually_fail(self):
        """A guard that cannot fail is worse than none: it reads as covered."""
        tree = ast.parse(
            'x = {"status": "waiting", "reason": "asks"}\n'
            'y = {"status": "waiting", "question": False}\n'
        )
        missing = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Dict)
            and any(isinstance(k, ast.Constant) and k.value == "status"
                    for k in node.keys)
            and not any(isinstance(k, ast.Constant) and k.value == "question"
                        for k in node.keys)
        ]
        self.assertEqual(missing, [1])

    def test_a_failure_is_never_marked_as_a_question(self):
        # classification.py, not app.py: the whole attention cluster moved
        # there in 0.10.0. Reading app.py found no `"reason": "failed"` at all,
        # so the split raised IndexError rather than failing an assertion --
        # a test that cannot find its subject should say so, not crash.
        source = _classifier_source()
        self.assertIn('"reason": "failed"', source,
                      "the failure row is not where this test is looking")
        failed = source.split('"reason": "failed"', 1)[1][:300]
        self.assertIn('"question": False', failed)


if __name__ == "__main__":
    unittest.main()
