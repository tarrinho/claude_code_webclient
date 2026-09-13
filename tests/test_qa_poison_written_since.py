"""QA: measure whether a turn wrote blocks a strict API would refuse.

Instrumentation, not a guard. It exists to settle one question that repeated
reading of the source could not: a gateway-backed conversation is poisoned at
rest and repaired at the start of every turn, so who is putting the poison
back, and does it ever happen on a backend that should not produce it?

Scanning the whole file per turn would answer that and cost a full parse of a
transcript that routinely runs to tens of megabytes. The turn's own byte offset
is already known -- `transcript_size()` before it starts -- so only the bytes it
appended are read, which is the part the question is actually about.

`transcripts.poison_written_since` is the counter. It reuses `_is_refused_block`
so it can never disagree with the repair about what counts as poison: two
implementations of that rule is the failure mode CLAUDE.md keeps naming.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import transcripts


def _record(session_id: str, content: list[dict], model: str = "some/model") -> dict:
    return {
        "uuid": "u",
        "parentUuid": None,
        "sessionId": session_id,
        "type": "assistant",
        "message": {"role": "assistant", "model": model, "content": content},
    }


class PoisonWrittenSinceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root_patch = patch.object(db, "_CLAUDE_PROJECTS_DIR", self.root)
        self.root_patch.start()
        self.sid = "abc123"
        self.dir = self.root / "proj"
        self.dir.mkdir()
        self.path = self.dir / f"{self.sid}.jsonl"

    def tearDown(self):
        self.root_patch.stop()
        self.tmp.cleanup()

    def _append(self, records: list[dict]) -> int:
        """Append *records* and return the byte offset before they were written."""
        before = self.path.stat().st_size if self.path.exists() else 0
        with self.path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        return before

    def test_a_clean_turn_reports_nothing(self):
        offset = self._append([_record(self.sid, [
            {"type": "thinking", "thinking": "real", "signature": "sig"},
            {"type": "text", "text": "an answer"},
        ])])
        counts = transcripts.poison_written_since(self.sid, offset)
        self.assertEqual(counts["foreign_thinking"], 0)
        self.assertEqual(counts["empty_text"], 0)

    def test_unsigned_thinking_is_counted(self):
        offset = self._append([_record(self.sid, [
            {"type": "thinking", "thinking": "from a gateway", "signature": ""},
            {"type": "text", "text": "an answer"},
        ])])
        counts = transcripts.poison_written_since(self.sid, offset)
        self.assertEqual(counts["foreign_thinking"], 1)

    def test_empty_text_is_counted(self):
        offset = self._append([_record(self.sid, [{"type": "text", "text": ""}])])
        counts = transcripts.poison_written_since(self.sid, offset)
        self.assertEqual(counts["empty_text"], 1)

    def test_blank_thinking_with_a_signature_is_not_poison(self):
        """The correction that cost this investigation a wrong turn: Claude Code
        stores the signature and drops the reasoning text, so a blank-but-signed
        block is the normal shape of a healthy Anthropic transcript. Counting it
        would report every working conversation as broken."""
        offset = self._append([_record(self.sid, [
            {"type": "thinking", "thinking": "", "signature": "a" * 800},
            {"type": "text", "text": "an answer"},
        ])])
        counts = transcripts.poison_written_since(self.sid, offset)
        self.assertEqual(counts["foreign_thinking"], 0)
        self.assertEqual(counts["empty_text"], 0)

    def test_only_the_bytes_after_the_offset_are_read(self):
        """The whole point of taking an offset. Poison already in the file
        belongs to an earlier turn and must not be attributed to this one --
        otherwise every turn on a long conversation reports the same backlog."""
        self._append([_record(self.sid, [
            {"type": "thinking", "thinking": "old", "signature": ""},
        ])])
        offset = self.path.stat().st_size
        self._append([_record(self.sid, [{"type": "text", "text": "clean"}])])
        counts = transcripts.poison_written_since(self.sid, offset)
        self.assertEqual(counts["foreign_thinking"], 0)

    def test_the_model_that_wrote_the_poison_is_reported(self):
        """Which backend produced it is the question being asked. A gateway
        writing unsigned thinking is expected; Anthropic doing it would be the
        finding."""
        offset = self._append([_record(self.sid, [
            {"type": "thinking", "thinking": "x", "signature": ""},
        ], model="nvidia/Qwen3.6-35B-A3B-NVFP4")])
        counts = transcripts.poison_written_since(self.sid, offset)
        self.assertIn("nvidia/Qwen3.6-35B-A3B-NVFP4", counts["models"])

    def test_a_clean_turn_names_no_model(self):
        offset = self._append([_record(self.sid, [{"type": "text", "text": "ok"}])])
        counts = transcripts.poison_written_since(self.sid, offset)
        self.assertEqual(counts["models"], [])

    def test_an_unreadable_line_is_skipped_not_fatal(self):
        """A transcript is appended to by another process while this reads it,
        so a torn final line is normal rather than exceptional."""
        offset = self.path.stat().st_size if self.path.exists() else 0
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
            handle.write(json.dumps(_record(self.sid, [
                {"type": "thinking", "thinking": "x", "signature": ""},
            ])) + "\n")
        counts = transcripts.poison_written_since(self.sid, offset)
        self.assertEqual(counts["foreign_thinking"], 1)

    def test_a_missing_transcript_reports_nothing(self):
        counts = transcripts.poison_written_since("nosuchsession", 0)
        self.assertEqual(counts["foreign_thinking"], 0)
        self.assertEqual(counts["empty_text"], 0)

    def test_an_offset_past_the_end_reports_nothing(self):
        self._append([_record(self.sid, [{"type": "text", "text": "ok"}])])
        counts = transcripts.poison_written_since(self.sid, 10_000_000)
        self.assertEqual(counts["foreign_thinking"], 0)

    def test_it_agrees_with_the_repair_about_what_poison_is(self):
        """Bound to `_is_refused_block` rather than reimplementing it. Two
        copies of this rule already drifted apart once in this repo."""
        block = {"type": "thinking", "thinking": "x", "signature": ""}
        offset = self._append([_record(self.sid, [block])])
        counted = transcripts.poison_written_since(self.sid, offset)
        self.assertEqual(
            counted["foreign_thinking"] + counted["empty_text"],
            1 if transcripts._is_refused_block(block) else 0,
        )


if __name__ == "__main__":
    unittest.main()
