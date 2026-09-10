"""QA: _agent_events_sync reads only what was appended, and loses nothing.

Measured 2026-09-09: agent_traffic() scans the 12 most recently modified
transcripts, which are by definition the ones being actively appended to --
86, 50, 49, 48, 44, 40, 31, 13MB on this host, ~450MB re-read in full on
every call, 31.4s wall and 603MB peak RSS. That is what disabled
sync_request_watcher and what made the "Messages between sessions" panel hang
for ~30s.

Transcripts are append-only, so the fix reads only the new bytes and keeps
the earlier parse. The whole risk of that trade lives in the boundary: a
growing transcript's last line is routinely half-written, so resuming from a
raw byte count would start mid-record and silently drop the message that line
carried. These tests are aimed at that boundary rather than at the happy path,
plus the two other ways an incremental reader goes wrong -- a file that
shrinks, and a cache the caller can mutate.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import transcripts


def _incoming(peer, body):
    return {
        "type": "user",
        "timestamp": "2026-09-09T10:00:00Z",
        "message": {"role": "user", "content":
                    f'<cross-session-message from="uds:/run/x.sock" '
                    f'from-name="{peer}">{body}</cross-session-message>'},
    }


def _sent(to, body):
    return {
        "type": "assistant",
        "timestamp": "2026-09-09T10:00:05Z",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "SendMessage",
             "input": {"to": to, "message": body, "summary": ""}}]},
    }


class AgentEventsIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sess.jsonl"
        self.path.write_bytes(b"")
        transcripts._agent_events_cache.clear()
        self.addCleanup(transcripts._agent_events_cache.clear)

    def _append_records(self, *records):
        with self.path.open("ab") as fh:
            for r in records:
                fh.write(json.dumps(r).encode() + b"\n")

    def _append_raw(self, raw: bytes):
        with self.path.open("ab") as fh:
            fh.write(raw)

    def _events(self):
        return transcripts._agent_events_sync(self.path, "sess", "title")

    def _texts(self):
        return [e["text"] for e in self._events()]

    # ── the boundary, which is the whole risk of reading incrementally ────

    def test_a_half_written_record_is_not_lost_when_it_completes(self):
        """The failure an offset-based reader exists to avoid. The first read
        sees a line with no terminating newline yet; it must not be counted as
        consumed, or the message it carries is dropped forever once the rest
        of the line lands."""
        self._append_records(_incoming("cweb2", "first"))
        self.assertEqual(self._texts(), ["first"])

        # A record still being written: no trailing newline.
        partial = json.dumps(_incoming("cweb2", "second")).encode()
        self._append_raw(partial[:40])
        self.assertEqual(self._texts(), ["first"],
                         "a partial record must not be parsed as if complete")

        # The rest of that line arrives.
        self._append_raw(partial[40:] + b"\n")
        self.assertEqual(self._texts(), ["first", "second"],
                         "the completed record was dropped -- the partial line "
                         "was wrongly counted as consumed")

    def test_a_complete_and_a_partial_record_in_one_read(self):
        """The case that actually separates a record-boundary offset from a
        raw-size one, and the one a live append-only file produces constantly:
        a single read that ends mid-record *after* a complete record. Storing
        the raw size here marks the partial's bytes consumed, so when the rest
        of that line lands the reader resumes past its start and the message
        is gone. (An earlier version of this file only ever appended a partial
        on its own, where cut == -1 and both behaviours coincide -- so it
        passed against exactly this bug.)"""
        self._append_records(_incoming("cweb2", "first"))
        self._events()

        complete = json.dumps(_sent("cweb2", "second")).encode() + b"\n"
        partial = json.dumps(_incoming("cweb2", "third")).encode()
        self._append_raw(complete + partial[:30])

        self.assertEqual(self._texts(), ["first", "second"],
                         "the complete record should parse and the partial "
                         "should not")

        self._append_raw(partial[30:] + b"\n")
        self.assertEqual(
            self._texts(), ["first", "second", "third"],
            "the third message was lost -- the partial record's bytes were "
            "counted as consumed alongside the complete one",
        )

    def test_a_partial_record_that_never_completes_blocks_nothing(self):
        self._append_records(_incoming("cweb2", "first"))
        self._append_raw(b'{"type": "user", "message": {"content": "unfinis')
        self.assertEqual(self._texts(), ["first"])
        # And a later complete record after the junk line still parses.
        self._append_raw(b'\n')
        self._append_records(_incoming("cweb2", "third"))
        self.assertIn("third", self._texts())

    # ── growth ────────────────────────────────────────────────────────────

    def test_records_appended_after_a_read_are_found(self):
        self._append_records(_incoming("cweb2", "one"))
        self.assertEqual(self._texts(), ["one"])
        self._append_records(_sent("cweb2", "two"))
        self.assertEqual(self._texts(), ["one", "two"])

    def test_an_unchanged_file_is_not_re_read(self):
        self._append_records(_incoming("cweb2", "one"))
        self._events()
        real_open = Path.open
        opens = []

        def counting_open(self_path, *a, **kw):
            opens.append(str(self_path))
            return real_open(self_path, *a, **kw)

        with patch.object(Path, "open", counting_open):
            self._events()
        self.assertEqual(opens, [], "an unchanged transcript was opened again")

    def test_only_the_appended_bytes_are_read(self):
        """The point of the change: cost proportional to what was added."""
        self._append_records(_incoming("cweb2", "one"))
        self._events()
        before = self.path.stat().st_size
        self._append_records(_sent("cweb2", "two"))
        appended = self.path.stat().st_size - before
        read_sizes = []
        real_open = Path.open

        def watching_open(self_path, *a, **kw):
            handle = real_open(self_path, *a, **kw)
            real_read = handle.read

            def counting_read(*ra, **rkw):
                data = real_read(*ra, **rkw)
                read_sizes.append(len(data))
                return data

            handle.read = counting_read
            return handle

        with patch.object(Path, "open", watching_open):
            self._events()
        self.assertTrue(read_sizes, "the grown file was never read")
        # The appended bytes, plus at most the 64-byte anchor read back to
        # prove the file was not rewritten under us. Still strict enough to
        # fail a full re-read, which is the regression this guards.
        self.assertGreaterEqual(sum(read_sizes), appended)
        self.assertLessEqual(
            sum(read_sizes), appended + transcripts._RESUME_ANCHOR_BYTES,
            f"read {sum(read_sizes)} bytes for a {appended}-byte append; the "
            f"whole {before + appended}-byte file was re-read instead of only "
            f"what changed plus the anchor",
        )

    # ── shrinking / replacement ───────────────────────────────────────────

    def test_a_file_rewritten_larger_than_the_old_offset_is_re_read(self):
        """The hole a shrink-only check leaves, and the one that is actually
        reachable: repair_if_needed() rewrites a transcript in place, and if
        the session then appends past the old offset the file is *larger*
        than before, so it looks exactly like growth. Resuming would carry
        events for records that no longer exist. Caught by the 64-byte anchor
        before the resume point, not by the size.

        An earlier version of this test truncated and appended only a small
        record, leaving the file smaller than the old offset -- so it took the
        shrink path and passed against this bug."""
        self._append_records(*[_incoming("cweb2", f"old {i}") for i in range(6)])
        self.assertEqual(len(self._texts()), 6)
        old_offset = transcripts._agent_events_cache[str(self.path)][0]

        # Rewritten shorter, then grown well past where the old parse stopped.
        self.path.write_bytes(b"")
        self._append_records(*[_incoming("cweb9", f"new {i}") for i in range(8)])
        self.assertGreater(self.path.stat().st_size, old_offset,
                           "precondition: must look like growth, not a shrink")

        texts = self._texts()
        self.assertTrue(all(t.startswith("new ") for t in texts), texts)
        self.assertEqual(len(texts), 8)

    def test_a_truncated_file_is_re_read_from_the_start(self):
        """A shrunk file is not the file that was parsed, so the cached parse
        no longer describes it -- resuming from the old offset would seek past
        the end and report nothing at all."""
        self._append_records(_incoming("cweb2", "old one"), _incoming("cweb2", "old two"))
        self.assertEqual(len(self._texts()), 2)

        self.path.write_bytes(b"")
        self._append_records(_incoming("cweb9", "replaced"))
        self.assertEqual(self._texts(), ["replaced"])

    # ── the cache must not be corruptible by its caller ───────────────────

    def test_the_caller_cannot_mutate_the_cache(self):
        """_agent_traffic_sync writes sender/recipient into the dicts it gets
        back. Handing out the cached dicts would let that mutation persist
        into every later call."""
        self._append_records(_incoming("cweb2", "one"))
        first = self._events()
        first[0]["text"] = "tampered"
        first[0]["sender"] = "injected"
        second = self._events()
        self.assertEqual(second[0]["text"], "one")
        self.assertNotIn("sender", second[0])

    def test_a_stale_title_is_not_served_from_cache(self):
        """The title comes from the caller's listing, not the file, so a
        cached parse must not pin the title it happened to be built with."""
        self._append_records(_incoming("cweb2", "one"))
        transcripts._agent_events_sync(self.path, "sess", "old title")
        again = transcripts._agent_events_sync(self.path, "sess", "new title")
        self.assertEqual(again[0]["session_title"], "new title")

    # ── failure handling ──────────────────────────────────────────────────

    def test_a_missing_file_yields_nothing_and_is_not_cached(self):
        missing = Path(self.tmp.name) / "gone.jsonl"
        self.assertEqual(
            transcripts._agent_events_sync(missing, "s", "t"), [])
        self.assertNotIn(str(missing), transcripts._agent_events_cache)

    def test_a_read_failure_serves_what_was_already_parsed(self):
        self._append_records(_incoming("cweb2", "one"))
        self.assertEqual(self._texts(), ["one"])
        self._append_records(_sent("cweb2", "two"))

        def boom(*a, **kw):
            raise OSError("stubbed read failure")

        with patch.object(Path, "open", boom):
            served = self._events()
        self.assertEqual([e["text"] for e in served], ["one"],
                         "a transient read failure must not erase the "
                         "previously parsed events")
        # And the cache was not poisoned -- the next call still picks up "two".
        self.assertEqual(self._texts(), ["one", "two"])


if __name__ == "__main__":
    unittest.main()
