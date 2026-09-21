"""Bounds and guarantees of voice_context.

Design: docs/superpowers/specs/2026-09-21-voice-session-context-design.md

These are the parts that decide whether a voice session opens with a usable
summary, and whether its fetch tool can be talked into reading a chat it was
not bound to. All pure, so they are tested directly rather than through a
browser or a model call.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import voice_context as vc  # noqa: E402


def _msg(i, content, role="user"):
    return {"id": i, "role": role, "content": content}


class SelectWindowTests(unittest.TestCase):
    def test_a_short_chat_is_taken_whole_and_not_marked_truncated(self):
        """The median chat here is 22 messages. It must arrive complete, and
        must not carry a note telling the model it is seeing only part."""
        messages = [_msg(i, f"line {i}") for i in range(22)]
        window, truncated = vc.select_window(messages)
        self.assertEqual(len(window), 22)
        self.assertFalse(truncated)

    def test_the_message_cap_keeps_the_newest_and_reports_truncation(self):
        messages = [_msg(i, f"line {i}") for i in range(500)]
        window, truncated = vc.select_window(messages)
        self.assertEqual(len(window), vc.WINDOW_MAX_MESSAGES)
        self.assertTrue(truncated)
        # The tail, not the head: a voice session is about what just happened.
        self.assertEqual(window[-1]["id"], 499)

    def test_the_character_cap_is_not_redundant_with_the_message_cap(self):
        """One message in this database is 256,560 characters. Without the
        character bound a 100-message window is unbounded, which is the whole
        reason both exist."""
        messages = [_msg(0, "x" * 250_000), _msg(1, "recent")]
        window, truncated = vc.select_window(messages)
        total = sum(len(m["content"]) for m in window)
        self.assertLessEqual(total, vc.WINDOW_MAX_CHARS + 250_000)
        # The giant one must not have crowded out the newest message.
        self.assertEqual(window[-1]["id"], 1)
        self.assertTrue(truncated)

    def test_a_single_oversized_message_is_still_returned(self):
        """Returning nothing would be worse than returning one huge message:
        the session would open with no context for a chat that plainly has
        some."""
        window, truncated = vc.select_window([_msg(0, "y" * 90_000)])
        self.assertEqual(len(window), 1)
        self.assertFalse(truncated)

    def test_an_empty_chat_yields_an_empty_window(self):
        self.assertEqual(vc.select_window([]), ([], False))


class UsableSummaryTests(unittest.TestCase):
    def test_short_or_empty_answers_are_failures(self):
        """Spec §3: a rung answering "N/A" must escalate, not be stored as the
        session's entire understanding of the conversation."""
        for answer in (None, "", "   ", "N/A", "none", "\n\n"):
            with self.subTest(answer=answer):
                self.assertFalse(vc.is_usable_summary(answer))

    def test_a_real_summary_is_accepted(self):
        self.assertTrue(vc.is_usable_summary(
            "They are fixing the delegation page's cost column and have "
            "decided to record provenance."))

    def test_whitespace_does_not_count_toward_the_minimum(self):
        self.assertFalse(vc.is_usable_summary(" " * 100))


class EligibleRungsTests(unittest.TestCase):
    def test_a_rung_below_the_threshold_is_dropped(self):
        """luna is measured at 0.583 comprehension. Walking it first costs
        9.2s of a 15s budget and fails more often than it succeeds."""
        ladder = ["azure_ai/gpt-5.6-luna", "claude-sonnet-5", "claude-opus-5"]
        acc = {"azure_ai/gpt-5.6-luna": 0.583,
               "claude-sonnet-5": 1.0, "claude-opus-5": 1.0}
        self.assertEqual(vc.eligible_rungs(ladder, acc),
                         ["claude-sonnet-5", "claude-opus-5"])

    def test_an_unmeasured_rung_is_kept(self):
        """Unmeasured is not the same as bad; dropping it would narrow the
        ladder on missing data."""
        ladder = ["mystery", "claude-opus-5"]
        acc = {"mystery": None, "claude-opus-5": 1.0}
        self.assertEqual(vc.eligible_rungs(ladder, acc), ["mystery", "claude-opus-5"])

    def test_a_ladder_that_is_entirely_weak_is_returned_intact(self):
        """Walking a weak ladder beats refusing to try."""
        ladder = ["a", "b"]
        acc = {"a": 0.1, "b": 0.2}
        self.assertEqual(vc.eligible_rungs(ladder, acc), ["a", "b"])

    def test_an_empty_ladder_stays_empty(self):
        self.assertEqual(vc.eligible_rungs([], {}), [])


class FetchToolTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def read(chat_id, low, high):
            self.calls.append((chat_id, low, high))
            return [_msg(i, f"body {i}") for i in range(low, high + 1)]

        self.read = read

    def test_it_is_bound_to_one_chat_and_takes_no_chat_argument(self):
        """The guarantee is structural: there is no parameter through which
        another conversation could be named."""
        fetch = vc.make_fetch_tool("chat-A", self.read)
        fetch(1, 3)
        self.assertEqual(self.calls[0][0], "chat-A")
        props = vc.FETCH_TOOL_SCHEMA["function"]["parameters"]["properties"]
        self.assertEqual(set(props), {"from_id", "to_id"})

    def test_a_reversed_range_is_read_in_order_rather_than_refused(self):
        fetch = vc.make_fetch_tool("chat-A", self.read)
        result = fetch(9, 4)
        self.assertEqual(self.calls[0][1:], (4, 9))
        self.assertEqual([m["id"] for m in result["messages"]], list(range(4, 10)))

    def test_an_empty_range_is_a_true_answer_not_an_error(self):
        fetch = vc.make_fetch_tool("chat-A", lambda *_: [])
        result = fetch(1, 5)
        self.assertEqual(result["messages"], [])
        self.assertFalse(result["truncated"])
        self.assertIn("No messages", result["note"])

    def test_an_over_long_range_truncates_and_names_where_it_stopped(self):
        """A range can legally name 15,175 messages. The model must be able to
        ask for the next span instead of believing it got the whole range."""
        fetch = vc.make_fetch_tool("chat-A", self.read)
        result = fetch(0, 5000)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["messages"]), vc.FETCH_MAX_MESSAGES)
        self.assertIn(str(result["messages"][-1]["id"]), result["note"])

    def test_non_numeric_ids_are_refused_without_raising(self):
        fetch = vc.make_fetch_tool("chat-A", self.read)
        result = fetch("abc", None)
        self.assertEqual(result["messages"], [])
        self.assertEqual(self.calls, [])


class BudgetClockTests(unittest.TestCase):
    def _clock(self, budget=15.0):
        self.t = [1000.0]
        return vc.BudgetClock(budget_s=budget, now=lambda: self.t[0])

    def test_it_refuses_a_rung_that_cannot_finish(self):
        """The measured case from spec §1.1: luna fails at 9.2s, leaving 5.8s,
        and sonnet needs 6.0s. Starting it burns the remainder on a call that
        was always going to be cancelled."""
        clock = self._clock()
        self.t[0] += 9.2
        self.assertAlmostEqual(clock.remaining(), 5.8, places=6)
        self.assertFalse(clock.allows(6.0))

    def test_it_allows_a_rung_that_fits(self):
        clock = self._clock()
        self.t[0] += 1.0
        self.assertTrue(clock.allows(6.0))

    def test_expiry(self):
        clock = self._clock()
        self.assertFalse(clock.expired())
        self.t[0] += 15.0
        self.assertTrue(clock.expired())
        self.assertEqual(clock.remaining(), 0.0)


class SummaryPromptTests(unittest.TestCase):
    def test_truncation_is_declared_to_the_model(self):
        """A summary built from a tail the model believes is the whole chat is
        worse than one it knows is partial."""
        prompt = vc.summary_prompt([_msg(1, "hi")], truncated=True)
        self.assertIn("most recent part", prompt)

    def test_a_whole_chat_carries_no_truncation_note(self):
        prompt = vc.summary_prompt([_msg(1, "hi")], truncated=False)
        self.assertNotIn("most recent part", prompt)

    def test_the_conversation_is_included_with_roles(self):
        prompt = vc.summary_prompt(
            [_msg(1, "what broke?"), _msg(2, "the cache", role="assistant")],
            truncated=False)
        self.assertIn("user: what broke?", prompt)
        self.assertIn("assistant: the cache", prompt)


if __name__ == "__main__":
    unittest.main()
