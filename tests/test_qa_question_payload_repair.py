"""QA: a question whose options are over-escaped is recovered, not discarded.

An OpenAI-compatible gateway (vLLM, LiteLLM -- anything whose tool ids look like
``chatcmpl-tool-*``) sends tool arguments as a *string* of JSON. Qwen 3.6 emitted
one that is half raw and half escaped: correctly quoted up to character 279, then
``\\"label\\"`` and ``\\"description\\"`` from there on. ``json.loads`` fails at
exactly the character where the escaping changes, so the console logged
``question_payload_unparseable`` and showed the question with no options.

The payload it happened to lose was a real one -- "Which of these would give the
biggest improvement to daily usage", with four options -- so the cost of giving
up was the whole point of the question.

Two things are fixed here and both are narrow on purpose.

**The repair is a fallback, never a first attempt.** Well-formed JSON is parsed
normally. Only after ``json.loads`` raises is the over-escaping undone, and the
result is accepted only if it parses *and* has the shape questions have. That
ordering matters: ``\\"`` is legitimate inside a JSON string value, so a payload
containing ``"He said \\"hi\\""`` is valid and must not be touched. Repairing
first would corrupt it.

``unicode_escape`` is deliberately **not** used, though it also recovers this
payload. It decodes bytes as latin-1, so it turns "café" into "cafÃ©" and mangles
every non-ASCII question in the process. Replacing ``\\"`` with ``"`` fixes this
defect and nothing else, which is what a repair should do.

**The warning is logged once per block, not once per poll.** The transcript is
re-read on a timer, so one malformed block produced 749 identical lines in a
single day -- the largest single source of noise in the production log. The
defect is worth reporting; reporting it 749 times buries everything else.
"""
from __future__ import annotations

import json
import unittest

import transcripts

# The real payload, taken verbatim from the transcript that exposed this. The
# escaping switches mid-string, which is the whole defect; a hand-tidied fixture
# would parse cleanly and prove nothing.
REAL_BROKEN = '[{"question": "Which of these would give the biggest improvement to daily use?", "header": "Next feature", "options": [{"label": "Syntax highlighting for code blocks", "description": "Prism.js CDN to render assistant code with colors. Medium effort (JS + CSS only)."}, {"label": \\"Retry individual messages\\", \\"description": \\"Re-submit a specific user message, keep all prior messages intact. High-value, needs UI + API changes.\\"}, {"label\\": \\"\'Stop\' button for live streaming\\", \\"description": "Visible abort button during streaming. AbortController exists but no visible UI affordance.\\"}, {"label\\": \\"Visible thinking/status indicator\\", \\"description\\": "Animate \'connecting… / thinking…\' into a pulsing status line under the current message.\\"}], "multiSelect": false}]'

# Valid JSON that *contains* an escaped quote inside a value. Parses on the first
# attempt, so the repair must never see it -- and would corrupt it if it did.
LEGITIMATE_ESCAPES = json.dumps(
    [{"question": 'He said "hi" — now what?', "header": "Quote",
      "options": [{"label": 'Say "hello"', "description": "Reply in kind."}]}]
)

# Non-ASCII, to pin that the repair does not mangle text the way unicode_escape
# would. Valid JSON, so it takes the fast path.
NON_ASCII = json.dumps(
    [{"question": "Qual é a próxima ação?", "header": "Ação",
      "options": [{"label": "Continuar", "description": "Seguir em frente."}]}]
)


class RepairTests(unittest.TestCase):
    """The recovery itself."""

    def setUp(self):
        transcripts.reset_reported_payloads()

    def test_the_real_over_escaped_payload_is_recovered(self):
        """The regression proper, in the bytes that caused it."""
        entries, damaged = transcripts._questions_payload(REAL_BROKEN, "chatcmpl-tool-x")
        self.assertFalse(damaged, "recovered payloads must not be flagged damaged")
        self.assertEqual(len(entries), 1)
        self.assertIn("biggest improvement", entries[0]["question"])

    def test_all_four_options_survive(self):
        """Recovering the question but losing the options would miss the point:
        the options are what the user is being asked to choose between."""
        entries, _ = transcripts._questions_payload(REAL_BROKEN, "chatcmpl-tool-x")
        labels = [o["label"] for o in entries[0]["options"]]
        self.assertEqual(len(labels), 4, f"expected 4 options, got {labels}")
        self.assertIn("Retry individual messages", labels)
        self.assertIn("Syntax highlighting for code blocks", labels)

    def test_a_legitimate_escaped_quote_is_not_corrupted(self):
        """The guard against an over-eager repair.

        This is valid JSON with `\\"` inside a value, so it parses on the first
        attempt and the repair must not run. If the repair were applied first it
        would strip the escapes and change the text the user sees.
        """
        entries, damaged = transcripts._questions_payload(LEGITIMATE_ESCAPES, "id")
        self.assertFalse(damaged)
        self.assertEqual(entries[0]["question"], 'He said "hi" — now what?')
        self.assertEqual(entries[0]["options"][0]["label"], 'Say "hello"')

    def test_non_ascii_text_is_unharmed(self):
        """`unicode_escape` also recovers the broken payload and would turn
        "é" into "Ã©". This pins the narrower repair."""
        entries, _ = transcripts._questions_payload(NON_ASCII, "id")
        self.assertEqual(entries[0]["question"], "Qual é a próxima ação?")
        self.assertEqual(entries[0]["header"], "Ação")

    def test_genuine_rubbish_still_degrades_gracefully(self):
        """Not everything is recoverable, and inventing a question from noise
        would be worse than showing none."""
        entries, damaged = transcripts._questions_payload("nope", "id")
        self.assertEqual(entries, [])
        self.assertFalse(damaged, "a short scrap is not evidence a question was asked")

    def test_structured_but_unrecoverable_is_still_reported_damaged(self):
        """A payload that plainly tried to be questions and cannot be read must
        still surface as an unreadable question rather than vanish."""
        broken = '[{"question": "why?", "options": [{"label": ' + "\x00\x01" + '}]}]'
        entries, damaged = transcripts._questions_payload(broken, "id")
        self.assertEqual(entries, [])
        self.assertTrue(damaged)

    def test_a_well_formed_list_is_passed_straight_through(self):
        """The fast path, unchanged: Anthropic sends a real list."""
        payload = [{"question": "ok?", "options": []}]
        entries, damaged = transcripts._questions_payload(payload, "id")
        self.assertIs(entries, payload)
        self.assertFalse(damaged)


class LogVolumeTests(unittest.TestCase):
    """One defect, one line -- not one line per poll."""

    def setUp(self):
        transcripts.reset_reported_payloads()

    def _warnings(self, calls):
        with self.assertLogs("wc.transcripts", level="WARNING") as caught:
            for _ in range(calls):
                transcripts._questions_payload("[{\"question\": \x00}]", "same-id")
            # assertLogs fails the test if nothing is logged, so the first call
            # must produce a record for this to be measuring anything.
            return [r for r in caught.records]

    def test_the_same_block_is_reported_once(self):
        """The transcript is re-read on a timer. One malformed block produced
        749 identical lines in a day, which is what buried the log."""
        records = self._warnings(20)
        self.assertEqual(len(records), 1,
                         f"expected 1 warning for 20 reads, got {len(records)}")

    def test_a_different_block_is_still_reported(self):
        """Deduplicating by id must not silence a second, distinct defect."""
        with self.assertLogs("wc.transcripts", level="WARNING") as caught:
            transcripts._questions_payload("[{\"question\": \x00}]", "id-one")
            transcripts._questions_payload("[{\"question\": \x00}]", "id-two")
        self.assertEqual(len(caught.records), 2)

    def test_the_reset_is_available_for_tests_and_restarts(self):
        """Without a way to clear it, the cache would make this suite
        order-dependent and hide a regression."""
        transcripts._questions_payload("[{\"question\": \x00}]", "id-three")
        transcripts.reset_reported_payloads()
        with self.assertLogs("wc.transcripts", level="WARNING") as caught:
            transcripts._questions_payload("[{\"question\": \x00}]", "id-three")
        self.assertEqual(len(caught.records), 1)


if __name__ == "__main__":
    unittest.main()
