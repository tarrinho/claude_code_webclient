"""QA: pending_question caches on file size, and the cache never hides a
question that arrived after the last scan.

Why the cache exists (measured 2026-09-09): the scan reads the *whole*
transcript -- deliberately, because a session can sit on a prompt for a long
time with nothing appended after it, so a tail-read would miss exactly the
prompt this is for. On cweb2's 86 MB transcript that scan is ~1.3-1.7s, and
it ran on every poll: every 4s per open conversation (app.js
QUESTION_POLL_MS) plus every 5s server-side per armed chat (auto_answer,
via routes.chats._pending_prompt). Same verdict, 86 MB of disk and a
line-by-line json.loads(), several times a minute.

The correctness argument for keying on size alone is that a new question and
an answer are both *appended* records, so neither can change the verdict
without the file growing. These tests are that argument, executed: the
interesting cases are all "the file changed, so the cached verdict must be
abandoned", not the happy path.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import transcripts


def _ask(qid="q1", question="Pick one"):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": qid, "name": "AskUserQuestion",
            "input": {"questions": [{
                "question": question, "header": "Choice", "multiSelect": False,
                "options": [{"label": "A", "description": "first"}],
            }]},
        }]},
    }


def _answer(qid="q1"):
    return {
        "type": "user",
        "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": qid, "content": "answered",
        }]},
    }


def _filler(n=1):
    """An appended record that is not a question and not an answer -- grows
    the file without changing the verdict."""
    return {"type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": f"chatter {n}"}]}}


class PendingQuestionCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sess.jsonl"
        self.path.write_text("")
        # transcript_path() globs the projects dir; point it at this file
        # directly so the test exercises the cache, not path resolution.
        patcher = patch.object(transcripts, "transcript_path",
                              lambda _sid: self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        transcripts._pending_question_cache.clear()
        self.addCleanup(transcripts._pending_question_cache.clear)

    def _append(self, *records):
        with self.path.open("a") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")

    # ── the property that makes the cache safe ────────────────────────────

    def test_a_question_appended_after_a_cached_none_is_still_found(self):
        """The failure this cache could plausibly cause, and must not: a
        'no question' verdict remembered while a question was arriving."""
        self.assertIsNone(transcripts.pending_question("s"))   # caches None
        self._append(_ask())
        found = transcripts.pending_question("s")
        self.assertIsNotNone(found, "the cache hid a newly-appended question")
        self.assertEqual(found["id"], "q1")

    def test_an_answer_appended_after_a_cached_question_clears_it(self):
        self._append(_ask())
        self.assertIsNotNone(transcripts.pending_question("s"))  # caches it
        self._append(_answer())
        self.assertIsNone(
            transcripts.pending_question("s"),
            "the cache kept serving a question that had since been answered",
        )

    def test_growth_that_changes_nothing_still_returns_the_same_verdict(self):
        """A rescan is triggered by any size change, so the verdict has to
        survive one -- this is the case that proves the rescan is correct,
        not merely that it happened."""
        self._append(_ask())
        first = transcripts.pending_question("s")
        self._append(_filler(1))
        second = transcripts.pending_question("s")
        self.assertEqual(first, second)

    def test_repeated_calls_on_an_unchanged_file_agree(self):
        self._append(_ask())
        results = [transcripts.pending_question("s") for _ in range(4)]
        self.assertEqual(results[0], results[-1])
        self.assertTrue(all(r == results[0] for r in results))

    # ── the cache actually caches ─────────────────────────────────────────

    def test_an_unchanged_file_is_not_re_read(self):
        self._append(_ask())
        transcripts.pending_question("s")            # populates the cache
        real_read_bytes = Path.read_bytes
        calls = []

        def counting_read_bytes(self_path, *a, **kw):
            calls.append(str(self_path))
            return real_read_bytes(self_path, *a, **kw)

        with patch.object(Path, "read_bytes", counting_read_bytes):
            transcripts.pending_question("s")
        self.assertEqual(
            calls, [], "an unchanged transcript was read again; the cache is "
                       "not being consulted")

    def test_the_cache_is_keyed_on_the_size_actually_read(self):
        """Keyed on len(raw), not the earlier stat() -- the file can grow
        between the two, and remembering the stat() size would mean the next
        call compares against a number no scan ever parsed."""
        self._append(_ask())
        transcripts.pending_question("s")
        cached_size, _ = transcripts._pending_question_cache[str(self.path)]
        self.assertEqual(cached_size, len(self.path.read_bytes()))

    # ── failure handling ──────────────────────────────────────────────────

    def test_a_missing_file_is_not_cached_as_a_verdict(self):
        """An unreadable file must not have 'no question' remembered against
        it -- the next call should try again rather than trust a result no
        bytes backed."""
        missing = Path(self.tmp.name) / "gone.jsonl"
        with patch.object(transcripts, "transcript_path", lambda _sid: missing):
            self.assertIsNone(transcripts.pending_question("s"))
        self.assertNotIn(str(missing), transcripts._pending_question_cache)

    def test_a_returned_dict_cannot_corrupt_the_cache(self):
        """Callers get a copy; mutating it must not change what the next
        caller reads back."""
        self._append(_ask())
        first = transcripts.pending_question("s")
        first["id"] = "tampered"
        second = transcripts.pending_question("s")
        self.assertEqual(second["id"], "q1")


if __name__ == "__main__":
    unittest.main()
