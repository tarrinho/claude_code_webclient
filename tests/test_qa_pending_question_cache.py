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
        """Patches Path.open, not Path.read_bytes. An earlier version of this
        test patched read_bytes, which the incremental implementation never
        calls -- so it passed without observing anything at all. A test that
        cannot fail is worse than no test, because it reads as coverage."""
        self._append(_ask())
        transcripts.pending_question("s")            # populates the cache
        real_open = Path.open
        opens = []

        def counting_open(self_path, *a, **kw):
            opens.append(str(self_path))
            return real_open(self_path, *a, **kw)

        with patch.object(Path, "open", counting_open):
            transcripts.pending_question("s")
        self.assertEqual(
            opens, [], "an unchanged transcript was opened again; the cache is "
                       "not being consulted")

    def test_the_cache_records_a_record_boundary_not_a_raw_size(self):
        """The offset must land immediately after a newline. A growing
        transcript's last line is routinely half-written, and remembering a
        raw byte count would resume mid-record next time and drop the message
        that line carried."""
        self._append(_ask())
        transcripts.pending_question("s")
        raw = self.path.read_bytes()
        offset, _anchor, asked, answered = transcripts._pending_question_cache[str(self.path)]
        self.assertEqual(offset, len(raw),
                         "this file ends with a newline, so the boundary is "
                         "the whole file")
        self.assertEqual(raw[offset - 1:offset], b"\n")
        self.assertEqual(set(asked), {"q1"})
        self.assertEqual(answered, set())

    # ── incremental reading ───────────────────────────────────────────────

    def test_a_complete_and_a_partial_record_in_one_read(self):
        """The case that separates a record-boundary offset from a raw-size
        one, and the one a live transcript produces constantly: a read ending
        mid-record after a complete record. Counting the partial's bytes as
        consumed loses the question that line was carrying, permanently."""
        self._append(_filler(1))
        transcripts.pending_question("s")

        complete = json.dumps(_filler(2)).encode() + b"\n"
        partial = json.dumps(_ask("q9", "Late question")).encode()
        with self.path.open("ab") as fh:
            fh.write(complete + partial[:30])
        self.assertIsNone(transcripts.pending_question("s"),
                          "a partial record must not be parsed as complete")

        with self.path.open("ab") as fh:
            fh.write(partial[30:] + b"\n")
        found = transcripts.pending_question("s")
        self.assertIsNotNone(
            found, "the question was lost -- the partial record's bytes were "
                   "counted as consumed alongside the complete one")
        self.assertEqual(found["id"], "q9")

    def test_only_the_appended_bytes_are_read(self):
        """The whole point: cost proportional to what was added, which is what
        a size-keyed cache of the verdict could not deliver for a transcript
        being appended to every few seconds."""
        self._append(_ask())
        transcripts.pending_question("s")
        before = self.path.stat().st_size
        self._append(_filler(1))
        appended = self.path.stat().st_size - before

        sizes = []
        real_open = Path.open

        def watching_open(self_path, *a, **kw):
            handle = real_open(self_path, *a, **kw)
            real_read = handle.read

            def counting_read(*ra, **rkw):
                data = real_read(*ra, **rkw)
                sizes.append(len(data))
                return data

            handle.read = counting_read
            return handle

        with patch.object(Path, "open", watching_open):
            transcripts.pending_question("s")
        # The appended bytes, plus at most the 64-byte anchor read back to
        # prove the file was not rewritten under us.
        self.assertGreaterEqual(sum(sizes), appended)
        self.assertLessEqual(
            sum(sizes), appended + transcripts._RESUME_ANCHOR_BYTES,
            f"read {sum(sizes)} bytes for a {appended}-byte append; the whole "
            f"{before + appended}-byte file was re-read",
        )

    def test_a_file_rewritten_larger_than_the_old_offset_is_re_read(self):
        """The reachable version of the rewrite case: repair_if_needed()
        rewrites a transcript in place, the session appends past the old
        offset, and the file is now *larger* -- indistinguishable from growth
        by size alone. Resuming would keep asks and answers for records that
        are no longer in the file. The 64-byte anchor is what notices."""
        self._append(_ask("q1"), *[_filler(i) for i in range(6)])
        self.assertEqual(transcripts.pending_question("s")["id"], "q1")
        old_offset = transcripts._pending_question_cache[str(self.path)][0]

        # q1 is gone from the rewritten file; q2 is the only ask in it now.
        self.path.write_text("")
        self._append(_ask("q2", "Fresh"), *[_filler(i) for i in range(10)])
        self.assertGreater(self.path.stat().st_size, old_offset,
                           "precondition: must look like growth, not a shrink")

        found = transcripts.pending_question("s")
        self.assertEqual(
            found["id"], "q2",
            "served a question from the pre-rewrite file -- the stale asks "
            "were carried across a rewrite",
        )

    def test_a_rewrite_that_removes_the_question_reports_no_question(self):
        """The case that actually catches stale state being carried across a
        rewrite. Where the rewritten file still has a pending question, a
        stale ask is masked -- the verdict returns the newest unanswered one
        and the newer real ask wins by insertion order. Here the rewritten
        file has *no* pending question, so carrying the old asks invents one
        that no longer exists anywhere in the file: a phantom prompt the UI
        would offer to answer."""
        self._append(_ask("q1"), *[_filler(i) for i in range(6)])
        self.assertEqual(transcripts.pending_question("s")["id"], "q1")
        old_offset = transcripts._pending_question_cache[str(self.path)][0]

        # Rewritten with no unanswered question at all, and larger than the
        # old offset so it cannot be caught as a shrink.
        self.path.write_text("")
        self._append(*[_filler(i) for i in range(20)])
        self.assertGreater(self.path.stat().st_size, old_offset,
                           "precondition: must look like growth, not a shrink")

        self.assertIsNone(
            transcripts.pending_question("s"),
            "reported a question that no longer exists in the file -- stale "
            "asks were carried across the rewrite",
        )

    def test_a_truncated_file_is_re_read_from_the_start(self):
        self._append(_ask("q1"))
        self.assertEqual(transcripts.pending_question("s")["id"], "q1")
        self.path.write_text("")
        self._append(_ask("q2", "Fresh question"))
        found = transcripts.pending_question("s")
        self.assertEqual(found["id"], "q2",
                         "a shrunk file must be re-read, not resumed from a "
                         "stale offset")

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
