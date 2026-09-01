"""QA: detecting that a session's newest turn has finished.

Three signals say whether a conversation task has concluded, and they answer
different questions:

* ``~/.claude/sessions/<pid>.json`` -> ``status`` -- ``busy`` / ``idle`` /
  ``waiting``, written by Claude Code about itself. ``idle`` is "concluded".
* the newest ``assistant`` record's ``message.stop_reason`` -- ``end_turn`` is
  "had nothing further to do", ``tool_use`` is "stopped to run something".
* the process being alive at all -- which separates *concluded* from *ended*.

This file covers the second, because it is the one derived from what the session
actually wrote rather than from what it says about itself.

The polarity matters more than the coverage here. Every live session on this
machine was ``busy`` or ``waiting`` when this was written, so a detector that
returned ``False`` unconditionally agreed with all of them -- the positive case
has to be constructed, or the suite proves only that nothing is ever finished.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import transcripts

END_TURN = {"type": "assistant", "message": {"stop_reason": "end_turn",
                                             "content": [{"type": "text",
                                                          "text": "done"}]}}
TOOL_USE = {"type": "assistant", "message": {"stop_reason": "tool_use",
                                             "content": [{"type": "text",
                                                          "text": "running"}]}}
PROMPT = {"type": "user", "message": {"content": "please do the next thing"}}
# A tool result comes back as a `user` record. It is emphatically NOT a prompt,
# and counting it as one would make every tool call look like new work.
TOOL_RESULT = {"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}}
# The shape that defeats a tail peek: one live session's last six records were
# all metadata like this, with no assistant record among them.
METADATA = [{"type": t} for t in
            ("agent-name", "mode", "permission-mode", "atis-latch")]


def _write(records) -> Path:
    """A transcript on disk. The caller unlinks it; the reader takes a Path."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".jsonl", delete=False, encoding="utf-8"
    ) as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
        return Path(handle.name)


class ConclusionTests(unittest.TestCase):
    def _state(self, records):
        path = _write(records)
        self.addCleanup(path.unlink)
        return transcripts._conclusion_sync(path)

    # ── the positive case, which is the one that can rot into always-False ─────

    def test_end_turn_with_nothing_after_it_is_concluded(self):
        state = self._state([PROMPT, TOOL_USE, END_TURN])
        self.assertEqual(state["stop_reason"], "end_turn")
        self.assertFalse(state["prompt_after"])
        self.assertTrue(state["concluded"])

    def test_metadata_after_the_end_does_not_unconclude_it(self):
        """Claude Code writes housekeeping records after a turn ends.

        This is the shape that defeats reading only the last few records, and it
        must not read as new work either.
        """
        state = self._state([PROMPT, END_TURN, *METADATA])
        self.assertEqual(state["stop_reason"], "end_turn")
        self.assertTrue(state["concluded"])

    def test_a_tool_result_after_the_end_is_not_a_new_prompt(self):
        state = self._state([PROMPT, END_TURN, TOOL_RESULT])
        self.assertTrue(
            state["concluded"],
            "a tool result was counted as a prompt, so no turn can ever conclude",
        )

    # ── the negative cases ────────────────────────────────────────────────────

    def test_stopping_to_run_a_tool_is_not_concluded(self):
        state = self._state([PROMPT, TOOL_USE])
        self.assertEqual(state["stop_reason"], "tool_use")
        self.assertFalse(state["concluded"])

    def test_a_prompt_after_the_end_means_a_new_turn_started(self):
        """Observed live: a session flipped idle -> busy between two reads.

        Its newest stop_reason was still ``end_turn`` from the previous turn, so
        the stop_reason alone reported a working agent as finished. This is the
        case that makes ``prompt_after`` necessary rather than decorative.
        """
        state = self._state([PROMPT, END_TURN, PROMPT])
        self.assertEqual(state["stop_reason"], "end_turn")
        self.assertTrue(state["prompt_after"])
        self.assertFalse(state["concluded"])

    def test_a_queued_prompt_also_starts_a_new_turn(self):
        state = self._state([
            PROMPT, END_TURN,
            {"type": "queue-operation", "content": "and then this"},
        ])
        self.assertFalse(state["concluded"])

    def test_an_attachment_prompt_also_starts_a_new_turn(self):
        state = self._state([
            PROMPT, END_TURN,
            {"type": "attachment", "attachment": {"prompt": "typed while busy"}},
        ])
        self.assertFalse(state["concluded"])

    def test_the_newest_assistant_record_decides(self):
        """A turn that concluded earlier does not make the current one finished."""
        state = self._state([PROMPT, END_TURN, PROMPT, TOOL_USE])
        self.assertEqual(state["stop_reason"], "tool_use")
        self.assertFalse(state["concluded"])

    # ── robustness ────────────────────────────────────────────────────────────

    def test_a_transcript_with_no_assistant_record_is_not_concluded(self):
        state = self._state([PROMPT, *METADATA])
        self.assertIsNone(state["stop_reason"])
        self.assertFalse(state["concluded"])

    def test_an_empty_transcript_is_not_concluded(self):
        state = self._state([])
        self.assertFalse(state["concluded"])

    def test_unparsable_lines_are_skipped_not_fatal(self):
        path = _write([PROMPT, END_TURN])
        self.addCleanup(path.unlink)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{ this is not json\n")
        state = transcripts._conclusion_sync(path)
        self.assertTrue(state["concluded"])

    def test_a_missing_file_is_reported_as_not_concluded(self):
        """Never guess "finished" from an absent transcript.

        Reporting a session as done because its file could not be read would
        retire a live agent from the supervisor's attention, which is the one
        error mode here with a real cost.
        """
        state = transcripts._conclusion_sync(Path("/nonexistent/nope.jsonl"))
        self.assertFalse(state["concluded"])
        self.assertIsNone(state["stop_reason"])

    def test_only_the_tail_is_read(self):
        """A bounded read, so polling stays cheap on a 18 MB transcript.

        The bound is generous -- the newest stop_reason sat within 16 KB on
        every real transcript measured -- but it is a bound, and a stop_reason
        buried behind more than 64 KB of later records is deliberately not
        found rather than paid for.
        """
        filler = {"type": "system", "pad": "x" * 900}
        records = [PROMPT, END_TURN, *([filler] * 200)]
        state = self._state(records)
        self.assertIsNone(
            state["stop_reason"],
            "the whole file was read; this is supposed to be a bounded tail",
        )
        self.assertFalse(state["concluded"])

    def test_the_bound_matches_the_measurement_it_was_chosen_from(self):
        self.assertEqual(transcripts._CONCLUSION_TAIL_BYTES, 64 * 1024)


class AsyncWrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_unknown_session_is_not_concluded(self):
        state = await transcripts.turn_concluded("no-such-session-id")
        self.assertFalse(state["concluded"])
        self.assertIsNone(state["stop_reason"])
        self.assertFalse(state["prompt_after"])


if __name__ == "__main__":
    unittest.main()
