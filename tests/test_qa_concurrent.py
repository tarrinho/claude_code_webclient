"""QA coverage for reading a transcript while a live session is writing it.

The viewer follows a session that is still running, so every read races an
append. transcripts._read_range_sync stops on the last complete line and leaves
a trailing partial for the next read, and the live tail resumes from the byte
offset the previous read returned. None of that interleaving was exercised:
the existing transcript tests all read files that had stopped changing.

The failure this guards against is silent. A read that consumed a half-written
line would either drop a turn at the boundary or parse a truncated record, and
a resume offset that included the partial line would re-emit or skip turns --
in every case the conversation still renders, just wrong.

Scope note: transcript parsing itself is covered by tests/test_qa_transcripts.py
(cweb4). This file only covers the concurrent-append behaviour.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import transcripts
from tests.testing_model import TESTING_MODEL

SESSION = "99999999-8888-7777-6666-555555555555"


def _record(text: str, role: str = "user") -> str:
    if role == "assistant":
        message = {
            "role": "assistant",
            "model": TESTING_MODEL,
            "content": [{"type": "text", "text": text}],
        }
    else:
        message = {"role": "user", "content": text}
    return json.dumps(
        {"type": role, "timestamp": "2026-08-29T12:00:00Z", "message": message}
    )


class _LiveTranscript:
    """A transcript file that can be appended to between reads."""

    def __init__(self, root: Path):
        self.path = root / "-home-kali-projects" / f"{SESSION}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()

    def append(self, *texts: str, role: str = "user") -> None:
        with self.path.open("ab") as handle:
            for text in texts:
                handle.write((_record(text, role) + "\n").encode())

    def append_partial(self, fragment: bytes) -> None:
        """Simulate a record caught mid-write by the reader."""
        with self.path.open("ab") as handle:
            handle.write(fragment)

    def complete_partial(self, rest: bytes) -> None:
        with self.path.open("ab") as handle:
            handle.write(rest)


class ConcurrentAppendTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "-home-kali-projects").mkdir(parents=True)
        self._dir_patch = patch.object(db, "_CLAUDE_PROJECTS_DIR", self.root)
        self._dir_patch.start()
        self.live = _LiveTranscript(self.root)

    def tearDown(self):
        self._dir_patch.stop()
        self.tmp.cleanup()

    def _texts(self, result) -> list[str]:
        return [turn["blocks"][0]["text"] for turn in result["turns"]]

    async def test_appends_between_reads_are_picked_up_from_the_offset(self):
        self.live.append("one", "two")
        first = await transcripts.read_turns(SESSION)
        self.assertEqual(self._texts(first), ["one", "two"])

        self.live.append("three")
        second = await transcripts.read_turns(SESSION, offset=first["offset"])
        self.assertEqual(self._texts(second), ["three"])

    async def test_no_turn_is_delivered_twice_across_a_follow(self):
        """The resume offset must not replay what the caller already has."""
        self.live.append("one", "two")
        page = await transcripts.read_turns(SESSION)
        seen = self._texts(page)
        offset = page["offset"]

        for batch in (("three",), ("four", "five"), ("six",)):
            self.live.append(*batch)
            page = await transcripts.read_turns(SESSION, offset=offset)
            seen += self._texts(page)
            offset = page["offset"]

        self.assertEqual(seen, ["one", "two", "three", "four", "five", "six"])
        self.assertEqual(len(seen), len(set(seen)))

    async def test_half_written_record_is_not_parsed(self):
        """A record caught mid-write must be invisible, not truncated."""
        self.live.append("complete")
        self.live.append_partial(b'{"type": "user", "timestamp": "", "message": {"ro')

        page = await transcripts.read_turns(SESSION)
        self.assertEqual(self._texts(page), ["complete"])

    async def test_offset_excludes_the_partial_so_it_is_re_read(self):
        self.live.append("complete")
        tail = _record("late arrival").encode() + b"\n"
        head, rest = tail[:30], tail[30:]
        self.live.append_partial(head)

        page = await transcripts.read_turns(SESSION)
        self.assertLess(
            page["offset"],
            self.live.path.stat().st_size,
            "offset must stop before the partial line",
        )

        # The writer finishes the record; the next read picks it up whole.
        self.live.complete_partial(rest)
        page = await transcripts.read_turns(SESSION, offset=page["offset"])
        self.assertEqual(self._texts(page), ["late arrival"])

    async def test_partial_line_alone_yields_nothing_and_does_not_advance(self):
        self.live.append_partial(b'{"type": "user", "message": {"role": "us')
        page = await transcripts.read_turns(SESSION)
        self.assertTrue(page["found"])
        self.assertEqual(page["turns"], [])
        self.assertEqual(page["offset"], 0, "a partial-only file has nothing to resume past")

    async def test_empty_live_file_is_found_but_empty(self):
        """A session that has just started has a file with nothing in it yet."""
        page = await transcripts.read_turns(SESSION)
        self.assertTrue(page["found"])
        self.assertEqual(page["turns"], [])
        self.assertTrue(page["at_start"])

    async def test_reading_at_eof_repeatedly_is_stable(self):
        self.live.append("only")
        page = await transcripts.read_turns(SESSION)
        offset = page["offset"]
        for _ in range(3):
            page = await transcripts.read_turns(SESSION, offset=offset)
            self.assertEqual(page["turns"], [])
            self.assertEqual(page["offset"], offset, "a quiet file must not move the offset")

    async def test_growth_past_the_tail_threshold_mid_follow(self):
        """Following forwards must keep working once the file gets large.

        The tail-start branch only applies at offset 0, so a follower that is
        already past it must not be bounced back to the end of the file.
        """
        self.live.append(*[f"turn {i} " + "x" * 400 for i in range(200)])
        page = await transcripts.read_turns(SESSION)
        offset = page["offset"]

        while self.live.path.stat().st_size <= transcripts.HISTORY_TAIL_BYTES:
            self.live.append(*[f"grow {i} " + "x" * 400 for i in range(200)])

        page = await transcripts.read_turns(SESSION, offset=offset)
        self.assertTrue(page["turns"], "a follower past offset 0 stopped seeing new turns")
        self.assertTrue(
            all(text.startswith("grow") for text in self._texts(page)),
            "following forwards re-delivered turns the caller already had",
        )

    async def test_tail_open_on_a_live_large_file_starts_on_a_line_boundary(self):
        """Opening a large transcript seeks into the middle of a record.

        The seek lands mid-line, so the partial head must be dropped and
        `start` advanced past it. If it is not, `start` is not a line boundary
        and every subsequent read_before window inherits the misalignment --
        which is invisible in the rendered conversation, because a fragment of
        JSON simply fails to parse and is skipped.
        """
        while self.live.path.stat().st_size <= transcripts.HISTORY_TAIL_BYTES:
            self.live.append(*[f"turn {i} " + "x" * 400 for i in range(200)])

        page = await transcripts.read_turns(SESSION)
        self.assertTrue(page["truncated"])
        start = page["start"]
        self.assertGreater(start, 0)
        with self.live.path.open("rb") as handle:
            handle.seek(start - 1)
            self.assertEqual(
                handle.read(1), b"\n", "start is not positioned on a line boundary"
            )

    async def test_appends_do_not_disturb_backwards_paging(self):
        """Paging back while the file grows must still reach the beginning."""
        self.live.append(*[f"turn {i} " + "y" * 400 for i in range(1500)])
        page = await transcripts.read_turns(SESSION)
        cursor = page["start"]
        self.assertGreater(cursor, 0)

        collected = self._texts(page)
        guard = 0
        while cursor > 0:
            guard += 1
            self.assertLess(guard, 200, "backwards paging did not terminate")
            self.live.append("appended during paging")
            page = await transcripts.read_before(SESSION, cursor)
            self.assertLess(page["start"], cursor, "paging must move towards the start")
            collected = self._texts(page) + collected
            cursor = page["start"]

        self.assertEqual(collected[0], "turn 0 " + "y" * 400)
        # Appends land after the window being walked, so they must not appear.
        self.assertNotIn("appended during paging", collected)


if __name__ == "__main__":
    unittest.main()
