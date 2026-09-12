"""QA coverage for reading CLI transcripts and reopening past conversations.

Covers:
* Record parsing -- which JSONL records become turns and which are noise.
* Tool-call summarising, including malformed ``input``.
* Transcript location and the charset guard that keeps a session id from
  widening the search or escaping the projects directory.
* Forward reads, tail truncation, and paging backwards through a long file.
* Reading a session's cwd and title back out of its own transcript.
* The three /api/transcripts endpoints.
* Workspace adoption when resuming, including every fallback path.
* Resume falling back to a transcript when no session is running.

Everything runs against a temporary projects directory; the real ~/.claude
tree is never read or written.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import config
import db
import transcripts
from routes import misc as misc_routes
from tests.testing_model import TESTING_MODEL

# ── Helpers ──────────────────────────────────────────────────────────────────


def assistant(text, session_id="s1", model=TESTING_MODEL, **extra):
    record = {
        "type": "assistant",
        "sessionId": session_id,
        "message": {"role": "assistant", "model": model,
                    "content": [{"type": "text", "text": text}]},
    }
    record.update(extra)
    return record


def user(text, **extra):
    record = {"type": "user", "message": {"role": "user",
              "content": [{"type": "text", "text": text}]}}
    record.update(extra)
    return record


def write_transcript(root: Path, session_id: str, records, project="-home-kali-demo"):
    """Write records the way Claude stores them: <projects>/<dir>/<id>.jsonl."""
    directory = root / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def make_request(query=None, session=None):
    request = types.SimpleNamespace(
        method="GET",
        url=types.SimpleNamespace(path="/api/transcripts"),
        cookies={},
        headers={},
        client=types.SimpleNamespace(host="127.0.0.1"),
        state=types.SimpleNamespace(
            session=session or {"user": "admin", "role": "admin"}),
        query_params=dict(query or {}),
    )
    request.json = AsyncMock(return_value={})
    request.is_disconnected = AsyncMock(return_value=False)
    return request


class TranscriptRootMixin:
    """Point the transcript code at a temporary projects directory."""

    def set_up_root(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Both transcripts.list_recent and db._session_transcript_paths read
        # this one module attribute, so patching it isolates the whole feature.
        self.root_patch = patch.object(db, "_CLAUDE_PROJECTS_DIR", self.root)
        self.root_patch.start()

    def tear_down_root(self):
        self.root_patch.stop()
        self.tmp.cleanup()


# ── Record parsing ───────────────────────────────────────────────────────────


class RecordParsingTests(unittest.TestCase):
    """Only user and assistant records carry conversation; the rest is noise."""

    def test_assistant_text_becomes_a_turn(self):
        turn = transcripts._turn_from_record(assistant("hello"))
        self.assertEqual(turn["role"], "assistant")
        self.assertEqual(turn["blocks"], [{"kind": "text", "text": "hello"}])
        self.assertEqual(turn["model"], TESTING_MODEL)

    def test_user_text_becomes_a_turn(self):
        turn = transcripts._turn_from_record(user("do the thing"))
        self.assertEqual(turn["role"], "user")
        self.assertEqual(turn["blocks"][0]["text"], "do the thing")

    def test_bare_string_content_is_accepted(self):
        """message.content is sometimes a plain string rather than blocks."""
        record = {"type": "user", "message": {"role": "user", "content": "plain"}}
        turn = transcripts._turn_from_record(record)
        self.assertEqual(turn["blocks"], [{"kind": "text", "text": "plain"}])

    def test_machinery_record_types_are_dropped(self):
        """A real transcript is mostly non-conversational bookkeeping."""
        for kind in ("attachment", "file-history-snapshot", "mode",
                     "queue-operation", "system", "last-prompt"):
            record = {"type": kind, "message": {"role": "user",
                      "content": [{"type": "text", "text": "x"}]}}
            self.assertIsNone(transcripts._turn_from_record(record), kind)

    def test_tool_result_becomes_a_flagged_output_turn(self):
        """Tool output is kept, but never presented as something the user said.

        It used to be dropped outright, which made a run of checks unreadable
        in the viewer: the stages produce nothing but output, so the page
        showed a list of command names and no results.
        """
        record = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "huge output"}]}}
        turn = transcripts._turn_from_record(record)
        self.assertTrue(turn["tool_output"],
                        "replayed output must not be credited to the operator")
        self.assertEqual(turn["blocks"], [{
            "kind": "result", "id": "t1", "text": "huge output",
            "truncated": False, "error": False,
        }])

    def test_a_typed_message_is_not_flagged_as_tool_output(self):
        """The flag has to distinguish, or it says nothing."""
        turn = transcripts._turn_from_record(user("I typed this"))
        self.assertFalse(turn.get("tool_output"))

    def test_empty_thinking_is_dropped_but_real_thinking_is_kept(self):
        """Stored thinking blocks are often signature-only with no text."""
        empty = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": "abc"}]}}
        self.assertIsNone(transcripts._turn_from_record(empty))

        real = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "weighing it up", "signature": "abc"}]}}
        turn = transcripts._turn_from_record(real)
        self.assertEqual(turn["blocks"], [{"kind": "thinking", "text": "weighing it up"}])

    def test_message_that_is_not_a_dict_is_dropped(self):
        self.assertIsNone(
            transcripts._turn_from_record({"type": "user", "message": "nope"}))

    def test_non_dict_record_is_dropped(self):
        self.assertIsNone(transcripts._turn_from_record(["not", "a", "record"]))

    def test_sidechain_is_flagged(self):
        turn = transcripts._turn_from_record(assistant("sub", isSidechain=True))
        self.assertTrue(turn["sidechain"])


class ToolDetailTests(unittest.TestCase):
    """The one-line summary is a label; the detail is what actually ran.

    "Bash(Stage 15 docs + version sweep)" is a description written for a human
    and says nothing about the command. The viewer showed only that, so a
    reader could not tell what a call did or what it returned.
    """

    def test_the_command_is_carried_not_just_its_description(self):
        block = {"name": "Bash", "input": {
            "description": "Stage 15 docs + version sweep",
            "command": "grep -n FastAPI README.md"}}
        self.assertEqual(transcripts._tool_summary(block),
                         "Bash(Stage 15 docs + version sweep)")
        detail, truncated = transcripts._tool_detail(block)
        self.assertEqual(detail, "grep -n FastAPI README.md")
        self.assertFalse(truncated)

    def test_newlines_survive(self):
        """A shell script collapsed onto one line is unreadable."""
        script = "cd /tmp\nls -la\necho done"
        detail, _ = transcripts._tool_detail({"name": "Bash", "input": {"command": script}})
        self.assertEqual(detail, script)

    def test_a_single_field_is_not_labelled(self):
        detail, _ = transcripts._tool_detail({"name": "Bash", "input": {"command": "ls"}})
        self.assertEqual(detail, "ls", "one field needs no key prefix")

    def test_several_fields_are_labelled(self):
        """An Edit's two strings would otherwise run together unidentified."""
        detail, _ = transcripts._tool_detail({"name": "Edit", "input": {
            "file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"}})
        self.assertIn("old_string: a", detail)
        self.assertIn("new_string: b", detail)

    def test_long_input_is_capped_and_says_so(self):
        detail, truncated = transcripts._tool_detail(
            {"name": "Bash", "input": {"command": "x" * 9000}})
        self.assertTrue(truncated)
        self.assertLessEqual(len(detail), transcripts._TOOL_DETAIL_MAX)

    def test_no_recognised_field_yields_no_detail(self):
        detail, truncated = transcripts._tool_detail({"name": "Task", "input": {"other": 1}})
        self.assertEqual(detail, "")
        self.assertFalse(truncated)

    def test_non_dict_input_does_not_raise(self):
        for payload in ("a string", ["a"], 7, None):
            transcripts._tool_detail({"name": "Weird", "input": payload})

    def test_the_rendered_block_actually_carries_the_detail(self):
        """The tests above call _tool_detail directly, which proves nothing
        about what reaches the client. Removing the field from the block left
        every one of them green, so assert the wiring itself."""
        block = transcripts._blocks_from_content([{
            "type": "tool_use", "id": "t1", "name": "Bash",
            "input": {"description": "Stage 15", "command": "grep -n x README.md"},
        }])[0]
        self.assertEqual(block["detail"], "grep -n x README.md",
                         "the client renders block['detail']; without it the "
                         "viewer shows only the description again")
        self.assertFalse(block["detail_truncated"])

    def test_a_block_with_no_detail_omits_the_field(self):
        """An absent field is how the client knows there is nothing to fold."""
        block = transcripts._blocks_from_content(
            [{"type": "tool_use", "id": "t1", "name": "Task", "input": {"other": 1}}])[0]
        self.assertNotIn("detail", block)


class ToolResultTests(unittest.TestCase):
    """Output is shown, capped, and marked when it failed."""

    def _result(self, content, **extra):
        item = {"type": "tool_result", "tool_use_id": "t1", "content": content}
        item.update(extra)
        blocks = transcripts._blocks_from_content([item])
        return blocks[0] if blocks else None

    def test_string_content_is_kept(self):
        self.assertEqual(self._result("done")["text"], "done")

    def test_block_list_content_is_flattened(self):
        block = self._result([{"type": "text", "text": "one"},
                              {"type": "text", "text": "two"}])
        self.assertEqual(block["text"], "one\ntwo")

    def test_an_error_is_flagged(self):
        self.assertTrue(self._result("boom", is_error=True)["error"])

    def test_long_output_is_capped_and_says_so(self):
        block = self._result("y" * 9000)
        self.assertTrue(block["truncated"])
        self.assertLessEqual(len(block["text"]), transcripts._TOOL_RESULT_MAX)

    def test_empty_output_produces_no_block(self):
        self.assertIsNone(self._result("   "))

    def test_the_call_and_its_result_share_an_id(self):
        """The client attaches output to its call, so the ids must line up."""
        call = transcripts._blocks_from_content(
            [{"type": "tool_use", "id": "abc", "name": "Bash",
              "input": {"command": "ls"}}])[0]
        self.assertEqual(call["id"], "abc")
        self.assertEqual(self._result("out")["id"], "t1")


class ToolSummaryTests(unittest.TestCase):
    """Tool calls collapse to one readable line."""

    def test_description_is_preferred(self):
        summary = transcripts._tool_summary(
            {"name": "Bash", "input": {"command": "ls -la", "description": "List files"}})
        self.assertEqual(summary, "Bash(List files)")

    def test_falls_back_to_another_known_key(self):
        summary = transcripts._tool_summary(
            {"name": "Read", "input": {"file_path": "/tmp/x.py"}})
        self.assertEqual(summary, "Read(/tmp/x.py)")

    def test_name_only_when_no_useful_input(self):
        self.assertEqual(
            transcripts._tool_summary({"name": "Task", "input": {"other": 1}}), "Task")

    def test_non_dict_input_does_not_raise(self):
        """input is not always an object, and assuming it was crashed the parse."""
        for payload in ("a string", ["a", "list"], 7, None):
            self.assertEqual(
                transcripts._tool_summary({"name": "Weird", "input": payload}), "Weird")

    def test_long_detail_is_clipped(self):
        summary = transcripts._tool_summary(
            {"name": "Bash", "input": {"command": "x" * 400}})
        self.assertLessEqual(len(summary), transcripts._TOOL_SUMMARY_MAX + len("Bash()") + 1)
        # The ellipsis marks the clipped detail, inside the parentheses.
        self.assertTrue(summary.startswith("Bash("))
        self.assertTrue(summary.endswith(")"))
        self.assertIn("…", summary)


# ── Locating a transcript ────────────────────────────────────────────────────


class TranscriptPathTests(TranscriptRootMixin, unittest.TestCase):

    def setUp(self):
        self.set_up_root()

    def tearDown(self):
        self.tear_down_root()

    def test_finds_a_transcript_one_level_down(self):
        written = write_transcript(self.root, "abc123", [assistant("hi", "abc123")])
        self.assertEqual(transcripts.transcript_path("abc123"), written)

    def test_missing_session_returns_none(self):
        self.assertIsNone(transcripts.transcript_path("nope"))

    def test_path_traversal_ids_are_refused(self):
        """The id is interpolated into a glob, so its charset is restricted."""
        for bad in ("../../etc/passwd", "a/../../", "..", "a/b", "x\\y", ""):
            self.assertIsNone(transcripts.transcript_path(bad), bad)

    def test_a_traversal_that_would_otherwise_resolve_is_refused(self):
        """The charset guard must be what blocks this, not luck.

        Ids like ``../../etc/passwd`` match nothing through the glob whether or
        not the guard exists, so a test using only those passes even with the
        guard deleted -- confirmed by mutating it away. ``*/../<dir>/<name>``
        genuinely resolves, so this is the shape that exercises the guard.
        """
        write_transcript(self.root, "secret", [assistant("private", "secret")],
                         project="-home-kali-demo")
        reachable = sorted(self.root.glob("*/../-home-kali-demo/secret.jsonl"))
        self.assertEqual(len(reachable), 1,
                         "fixture must be reachable by traversal or this proves nothing")
        self.assertIsNone(transcripts.transcript_path("../-home-kali-demo/secret"))


# ── Reading and paging ───────────────────────────────────────────────────────


class ReadTurnsTests(TranscriptRootMixin, unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.set_up_root()

    async def asyncTearDown(self):
        self.tear_down_root()

    async def test_unknown_session_is_reported_not_found(self):
        page = await transcripts.read_turns("missing")
        self.assertFalse(page["found"])
        self.assertEqual(page["turns"], [])

    async def test_short_transcript_is_returned_whole(self):
        write_transcript(self.root, "s1", [user("q"), assistant("a", "s1")])
        page = await transcripts.read_turns("s1")
        self.assertTrue(page["found"])
        self.assertEqual([t["role"] for t in page["turns"]], ["user", "assistant"])
        self.assertFalse(page["truncated"])
        self.assertTrue(page["at_start"])

    async def test_offset_resumes_without_repeating(self):
        write_transcript(self.root, "s2", [user("one"), assistant("two", "s2")])
        first = await transcripts.read_turns("s2")
        again = await transcripts.read_turns("s2", first["offset"])
        self.assertEqual(again["turns"], [], "resuming at the end must yield nothing")
        self.assertEqual(again["offset"], first["offset"])

    async def test_long_transcript_is_truncated_to_the_tail(self):
        records = [assistant(f"line {i}", "s3") for i in range(400)]
        write_transcript(self.root, "s3", records)
        with patch.object(transcripts, "HISTORY_TAIL_BYTES", 2000):
            page = await transcripts.read_turns("s3")
        self.assertTrue(page["truncated"])
        self.assertFalse(page["at_start"])
        self.assertGreater(page["start"], 0)
        self.assertLess(len(page["turns"]), 400)
        # The tail must be the *end* of the conversation. A fix that returned
        # the right number of turns from the wrong window would pass a count
        # check and still show the user the wrong part of their history.
        self.assertEqual(page["turns"][-1]["blocks"][0]["text"], "line 399")


class PageBackwardsTests(TranscriptRootMixin, unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.set_up_root()

    async def asyncTearDown(self):
        self.tear_down_root()

    async def test_before_zero_is_the_start(self):
        write_transcript(self.root, "s1", [assistant("only", "s1")])
        page = await transcripts.read_before("s1", 0)
        self.assertTrue(page["at_start"])
        self.assertEqual(page["turns"], [])

    async def test_paging_backwards_recovers_every_turn(self):
        """Without this a long conversation was only ever readable from its tail."""
        total = 300
        write_transcript(self.root, "s4",
                         [assistant(f"line {i}", "s4") for i in range(total)])
        with patch.object(transcripts, "HISTORY_TAIL_BYTES", 1500):
            page = await transcripts.read_turns("s4")
            seen = list(page["turns"])
            earliest, at_start, guard = page["start"], page["at_start"], 0
            while not at_start and guard < 500:
                guard += 1
                older = await transcripts.read_before("s4", earliest)
                self.assertLessEqual(older["start"], earliest, "paging must move back")
                seen = older["turns"] + seen
                earliest, at_start = older["start"], older["at_start"]

        self.assertTrue(at_start, "paging must terminate at the beginning")
        self.assertEqual(len(seen), total, "every turn must be recovered exactly once")
        self.assertEqual(seen[0]["blocks"][0]["text"], "line 0")
        self.assertEqual(seen[-1]["blocks"][0]["text"], f"line {total - 1}")

    async def test_unknown_session_is_reported_not_found(self):
        page = await transcripts.read_before("missing", 100)
        self.assertFalse(page["found"])

    async def test_paging_recovers_everything_when_a_window_exceeds_the_cap(self):
        """A single window can hold far more turns than MAX_TURNS.

        Capping the page while leaving ``start`` at the beginning of the whole
        window silently strips history: the caller then pages back from a byte
        position *below* the turns that were dropped, so no call ever returns
        them. With ~400-byte records one 512 KiB window holds ~1300 turns, and
        walking a 2000-turn transcript recovered exactly half of it.

        The first version of this test used records large enough that a window
        never exceeded the cap, so the capping path never ran and it passed
        against the bug. Small records are what make it fire.
        """
        total = 300
        write_transcript(self.root, "cap",
                         [user(f"turn {i}") for i in range(total)])
        # A window that holds well over the cap, without needing a large file.
        with patch.object(transcripts, "MAX_TURNS", 50), \
             patch.object(transcripts, "HISTORY_TAIL_BYTES", 8000):
            page = await transcripts.read_turns("cap")
            self.assertLessEqual(len(page["turns"]), 50, "the cap must apply")
            seen = list(page["turns"])
            earliest, at_start, guard = page["start"], page["at_start"], 0
            while not at_start and guard < 200:
                guard += 1
                older = await transcripts.read_before("cap", earliest)
                self.assertLess(older["start"], earliest, "paging must move back")
                seen = older["turns"] + seen
                earliest, at_start = older["start"], older["at_start"]

        self.assertTrue(at_start)
        recovered = [t["blocks"][0]["text"] for t in seen]
        self.assertEqual(len(recovered), total, "capping must not drop turns")
        self.assertEqual(recovered, [f"turn {i}" for i in range(total)],
                         "turns must come back in order, with no gaps or repeats")


class SessionMetadataTests(TranscriptRootMixin, unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.set_up_root()

    async def asyncTearDown(self):
        self.tear_down_root()

    async def test_cwd_is_read_from_the_transcript(self):
        """The project directory name is lossy, so the record is the source."""
        write_transcript(self.root, "s5", [
            {"type": "system", "subtype": "init", "cwd": "/home/kali/projects"},
            assistant("hi", "s5"),
        ])
        self.assertEqual(await transcripts.session_cwd("s5"), "/home/kali/projects")

    async def test_missing_cwd_is_empty_not_an_error(self):
        write_transcript(self.root, "s6", [assistant("hi", "s6")])
        self.assertEqual(await transcripts.session_cwd("s6"), "")

    async def test_unknown_session_has_no_cwd(self):
        self.assertEqual(await transcripts.session_cwd("missing"), "")

    async def test_title_comes_from_the_opening_prompt(self):
        write_transcript(self.root, "s7", [user("Fix the login bug\nmore detail"),
                                           assistant("ok", "s7")])
        self.assertEqual(await transcripts.session_title("s7"), "Fix the login bug")

    async def test_slash_command_is_not_used_as_a_title(self):
        write_transcript(self.root, "s8", [user("/init"), user("Real question"),
                                           assistant("ok", "s8")])
        self.assertEqual(await transcripts.session_title("s8"), "Real question")


class ListRecentTests(TranscriptRootMixin, unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.set_up_root()

    async def asyncTearDown(self):
        self.tear_down_root()

    async def test_empty_root_lists_nothing(self):
        self.assertEqual(await transcripts.list_recent(), [])

    async def test_entries_carry_identity_and_title(self):
        write_transcript(self.root, "s9", [user("Ship the thing"), assistant("ok", "s9")])
        listed = await transcripts.list_recent()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["session_id"], "s9")
        self.assertEqual(listed[0]["title"], "Ship the thing")
        self.assertGreater(listed[0]["size"], 0)

    async def test_newest_first(self):
        first = write_transcript(self.root, "old", [assistant("a", "old")])
        second = write_transcript(self.root, "new", [assistant("b", "new")])
        os.utime(first, (time.time() - 500, time.time() - 500))
        listed = await transcripts.list_recent()
        self.assertEqual([e["session_id"] for e in listed], ["new", "old"])
        self.assertTrue(second.exists())

    async def test_limit_is_clamped(self):
        for i in range(5):
            write_transcript(self.root, f"s{i}", [assistant("x", f"s{i}")])
        self.assertEqual(len(await transcripts.list_recent(limit=2)), 2)
        # Absurd limits must not be passed through to the filesystem walk.
        self.assertLessEqual(len(await transcripts.list_recent(limit=10_000)), 5)


# ── HTTP endpoints ───────────────────────────────────────────────────────────


class TranscriptEndpointTests(TranscriptRootMixin, unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.set_up_root()

    async def asyncTearDown(self):
        self.tear_down_root()

    async def test_list_returns_transcripts(self):
        write_transcript(self.root, "e1", [user("Hello there"), assistant("hi", "e1")])
        response = await misc_routes.handle_transcripts_list(make_request({"limit": "10"}))
        payload = json.loads(response.body)
        self.assertEqual(len(payload["transcripts"]), 1)
        self.assertEqual(payload["transcripts"][0]["title"], "Hello there")

    async def test_list_survives_a_junk_limit(self):
        write_transcript(self.root, "e2", [assistant("x", "e2")])
        response = await misc_routes.handle_transcripts_list(make_request({"limit": "abc"}))
        self.assertEqual(response.status_code, 200)

    async def test_get_returns_history(self):
        write_transcript(self.root, "e3", [user("q"), assistant("a", "e3")])
        response = await misc_routes.handle_transcript_get(make_request(), "e3")
        payload = json.loads(response.body)
        self.assertEqual(len(payload["turns"]), 2)
        self.assertIn("offset", payload)
        self.assertIn("start", payload)

    async def test_get_unknown_session_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await misc_routes.handle_transcript_get(make_request(), "missing")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_get_rejects_a_traversal_id(self):
        with self.assertRaises(HTTPException) as ctx:
            await misc_routes.handle_transcript_get(make_request(), "../../etc/passwd")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_get_refuses_a_traversal_that_would_resolve(self):
        """An id that really would reach another file must still be refused."""
        write_transcript(self.root, "hidden", [assistant("private", "hidden")],
                         project="-home-kali-demo")
        self.assertEqual(
            len(sorted(self.root.glob("*/../-home-kali-demo/hidden.jsonl"))), 1)
        with self.assertRaises(HTTPException) as ctx:
            await misc_routes.handle_transcript_get(
                make_request(), "../-home-kali-demo/hidden")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_before_parameter_pages_backwards(self):
        write_transcript(self.root, "e4",
                         [assistant(f"line {i}", "e4") for i in range(200)])
        with patch.object(transcripts, "HISTORY_TAIL_BYTES", 1200):
            first = json.loads(
                (await misc_routes.handle_transcript_get(make_request(), "e4")).body)
            older = json.loads((await misc_routes.handle_transcript_get(
                make_request({"before": str(first["start"])}), "e4")).body)
        self.assertTrue(older["turns"], "paging back must return earlier turns")
        self.assertLess(older["start"], first["start"])

    async def test_before_must_be_a_number(self):
        write_transcript(self.root, "e5", [assistant("x", "e5")])
        with self.assertRaises(HTTPException) as ctx:
            await misc_routes.handle_transcript_get(make_request({"before": "soon"}), "e5")
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_stream_unknown_session_is_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await misc_routes.handle_transcript_stream(make_request(), "missing")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_stream_opens_with_a_start_frame(self):
        write_transcript(self.root, "e6", [assistant("x", "e6")])
        response = await misc_routes.handle_transcript_stream(
            make_request({"offset": "0"}), "e6")
        iterator = response.body_iterator
        try:
            frame = await iterator.__anext__()
        finally:
            await iterator.aclose()
        self.assertTrue(frame.startswith("data: "))
        self.assertEqual(json.loads(frame[len("data: "):])["type"], "start")


# ── Workspace adoption ───────────────────────────────────────────────────────


class AdoptSessionCwdTests(unittest.TestCase):
    """work_dir is where Claude runs, and it must never leave PROJECTS_ROOT."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.patch = patch.object(config, "PROJECTS_ROOT", str(self.root))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_a_cwd_inside_the_root_is_adopted(self):
        inside = self.root / "workspace"
        inside.mkdir()
        self.assertEqual(misc_routes._adopt_session_cwd(str(inside), "abcd1234"), str(inside))

    def test_the_root_itself_is_adopted(self):
        self.assertEqual(misc_routes._adopt_session_cwd(str(self.root), "abcd1234"), str(self.root))

    def test_a_cwd_outside_the_root_falls_back(self):
        result = misc_routes._adopt_session_cwd("/etc", "abcd1234")
        self.assertNotEqual(result, "/etc")
        self.assertTrue(Path(result).resolve().is_relative_to(self.root))

    def test_a_missing_directory_falls_back(self):
        result = misc_routes._adopt_session_cwd(str(self.root / "gone"), "abcd1234")
        self.assertTrue(Path(result).is_dir())
        self.assertTrue(Path(result).resolve().is_relative_to(self.root))

    def test_blank_and_null_fall_back(self):
        for value in ("", "   ", None):
            result = misc_routes._adopt_session_cwd(value, "abcd1234")
            self.assertTrue(Path(result).resolve().is_relative_to(self.root))

    def test_every_outcome_stays_inside_the_root(self):
        """The fallback is the boundary's last line; it must never leak."""
        for value in ("/etc", "/", "../../..", str(self.root / "nope"), "", None):
            result = misc_routes._adopt_session_cwd(value, "abcd1234")
            self.assertTrue(
                Path(result).resolve().is_relative_to(self.root), f"escaped for {value!r}")


# ── Resuming a past conversation ─────────────────────────────────────────────


class ResumeFromTranscriptTests(TranscriptRootMixin, unittest.IsolatedAsyncioTestCase):
    """~/.claude/sessions lists only running sessions, so resume must not rely
    on it alone -- otherwise every finished conversation is unreachable."""

    async def asyncSetUp(self):
        self.set_up_root()
        self.dbtmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.dbtmp.name) / "projects"
        self.workspace.mkdir(parents=True)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.dbtmp.name}/db")
        self.proj_patch = patch.object(config, "PROJECTS_ROOT", str(self.workspace))
        self.db_patch.start()
        self.proj_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.proj_patch.stop()
        self.db_patch.stop()
        self.dbtmp.cleanup()
        self.tear_down_root()

    async def test_resumes_a_finished_conversation(self):
        write_transcript(self.root, "dead1", [
            {"type": "system", "subtype": "init", "cwd": str(self.workspace)},
            user("Investigate the outage"), assistant("looking", "dead1"),
        ])
        with patch.object(db, "read_claude_sessions", AsyncMock(return_value=[])):
            response = await misc_routes.handle_sessions_resume(make_request(), "dead1")
        payload = json.loads(response.body)
        chat = await db.chat_get(payload["id"], "admin")
        self.assertEqual(chat["session_id"], "dead1")
        self.assertEqual(chat["work_dir"], str(self.workspace))
        # A generated name, not the raw prompt: this session is gone and never
        # registered a name of its own, so {transport} : {n} : {task} is all
        # there is to go on. A session that *does* have a name keeps it --
        # test_a_live_session_name_wins_over_the_prompt is the other half of
        # the rule, and the two together are the whole of it.
        #
        # Matched by shape, not equality: the {n} is a running count of
        # local-named chats, so pinning "local : 1" makes this assertion
        # depend on how many other tests in the class created one first.
        # Asserting the exact string passed here and would have failed the
        # moment a test above it started resuming a nameless session.
        self.assertRegex(chat["title"], r"^local : \d+ : Investigate Outage$")

    async def test_a_live_session_name_wins_over_the_prompt(self):
        write_transcript(self.root, "live1", [user("Some prompt"),
                                              assistant("ok", "live1")])
        live = [{"sessionId": "live1", "name": "cweb9", "cwd": str(self.workspace)}]
        with patch.object(db, "read_claude_sessions", AsyncMock(return_value=live)):
            response = await misc_routes.handle_sessions_resume(make_request(), "live1")
        chat = await db.chat_get(json.loads(response.body)["id"], "admin")
        self.assertEqual(chat["title"], "cweb9")

    async def test_unknown_session_with_no_transcript_is_404(self):
        with (
            patch.object(db, "read_claude_sessions", AsyncMock(return_value=[])),
            self.assertRaises(HTTPException) as ctx,
        ):
            await misc_routes.handle_sessions_resume(make_request(), "nothing")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("no running session and no transcript", ctx.exception.detail)

    async def test_resuming_twice_reuses_the_same_chat(self):
        write_transcript(self.root, "dead2", [
            {"type": "system", "subtype": "init", "cwd": str(self.workspace)},
            user("Second look"), assistant("ok", "dead2"),
        ])
        with patch.object(db, "read_claude_sessions", AsyncMock(return_value=[])):
            first = json.loads(
                (await misc_routes.handle_sessions_resume(make_request(), "dead2")).body)
            second = json.loads(
                (await misc_routes.handle_sessions_resume(make_request(), "dead2")).body)
        self.assertEqual(first["id"], second["id"],
                         "resuming again must not create a duplicate chat")


# ── Messages between concurrent sessions ─────────────────────────────────────


def incoming(peer, body, **extra):
    """A message from another session, as the CLI records it."""
    record = {
        "type": "user",
        "timestamp": "2026-08-29T10:00:00Z",
        "message": {"role": "user", "content":
                    f'<cross-session-message from="uds:/run/x.sock" '
                    f'from-name="{peer}">{body}</cross-session-message>'},
    }
    record.update(extra)
    return record


def sent(to, body, summary="", **extra):
    """This session calling SendMessage."""
    record = {
        "type": "assistant",
        "timestamp": "2026-08-29T10:00:05Z",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "SendMessage",
             "input": {"to": to, "message": body, "summary": summary}}]},
    }
    record.update(extra)
    return record


class AgentTrafficTests(TranscriptRootMixin, unittest.IsolatedAsyncioTestCase):
    """Messages are recorded at both ends and in several record shapes."""

    async def asyncSetUp(self):
        self.set_up_root()

    async def asyncTearDown(self):
        self.tear_down_root()

    async def test_incoming_and_outgoing_are_both_found(self):
        write_transcript(self.root, "a1", [
            incoming("cweb2", "please hold app.js"),
            sent("cweb2", "holding, thanks"),
        ])
        messages = await transcripts.agent_traffic()
        bodies = {m["text"] for m in messages}
        self.assertIn("please hold app.js", bodies)
        self.assertIn("holding, thanks", bodies)

    async def test_direction_becomes_sender_and_recipient(self):
        write_transcript(self.root, "a2", [incoming("cweb3", "your file is red")])
        message = (await transcripts.agent_traffic())[0]
        self.assertEqual(message["sender"], "cweb3")
        # No registry entry for this fixture, so the receiver falls back to a
        # readable identifier rather than showing nothing.
        self.assertTrue(message["recipient"])

    async def test_the_same_message_at_both_ends_collapses_to_one(self):
        """Sender and recipient each record it; the view must show it once.

        Collapsing depends on resolving each session's *own* name from the
        registry: the sender records "to cweb2" while the receiver records
        "from cweb1", and only naming both ends makes those the same message.
        """
        body = "the suite is green, go ahead"
        names = ({"s-a": "cweb1", "s-b": "cweb2"}, {})
        with patch.object(transcripts, "_session_names_sync", lambda: names):
            write_transcript(self.root, "s-a", [sent("cweb2", body)],
                             project="-proj-one")
            write_transcript(self.root, "s-b", [incoming("cweb1", body)],
                             project="-proj-two")
            messages = await transcripts.agent_traffic()

        matching = [m for m in messages if m["text"] == body]
        self.assertEqual(len(matching), 1, "one message must not appear twice")
        self.assertEqual(matching[0]["sender"], "cweb1")
        self.assertEqual(matching[0]["recipient"], "cweb2")

    async def test_an_unnamed_session_still_reports_its_traffic(self):
        """Without a registry entry the ends cannot be matched up.

        Both copies then survive, which is the honest outcome -- better than
        collapsing two messages that could not be shown to be the same one.
        """
        body = "no registry for either end"
        with patch.object(transcripts, "_session_names_sync", lambda: ({}, {})):
            write_transcript(self.root, "s-c", [sent("cweb2", body)])
            messages = await transcripts.agent_traffic()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["recipient"], "cweb2")
        self.assertTrue(messages[0]["sender"], "sender must fall back to something")

    async def test_repeated_record_shapes_collapse_to_one(self):
        """The CLI writes the same message as several record types."""
        body = "claiming db.py"
        wrapped = ('<cross-session-message from="uds:/run/x.sock" '
                   f'from-name="cweb2">{body}</cross-session-message>')
        write_transcript(self.root, "a3", [
            {"type": "queue-operation", "timestamp": "2026-08-29T10:00:00Z",
             "content": wrapped},
            {"type": "attachment", "timestamp": "2026-08-29T10:00:00Z",
             "attachment": {"content": wrapped}},
            incoming("cweb2", body),
        ])
        matching = [m for m in await transcripts.agent_traffic()
                    if m["text"] == body]
        self.assertEqual(len(matching), 1)

    async def test_a_transcript_with_no_traffic_contributes_nothing(self):
        write_transcript(self.root, "quiet", [user("hello"), assistant("hi", "quiet")])
        self.assertEqual(await transcripts.agent_traffic(), [])

    async def test_newest_first(self):
        write_transcript(self.root, "a4", [
            incoming("cweb1", "first", timestamp="2026-08-29T09:00:00Z"),
            incoming("cweb1", "second", timestamp="2026-08-29T11:00:00Z"),
        ])
        messages = await transcripts.agent_traffic()
        self.assertEqual([m["text"] for m in messages], ["second", "first"])

    async def test_limit_is_clamped(self):
        write_transcript(self.root, "a5",
                         [incoming("cweb1", f"msg {i}") for i in range(30)])
        self.assertEqual(len(await transcripts.agent_traffic(limit=5)), 5)

    async def test_malformed_send_input_does_not_raise(self):
        """SendMessage input is not always the shape we expect."""
        write_transcript(self.root, "a6", [
            {"type": "assistant", "timestamp": "2026-08-29T10:00:00Z",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "SendMessage", "input": "not a dict"}]}},
            incoming("cweb1", "still parsed"),
        ])
        messages = await transcripts.agent_traffic()
        self.assertEqual([m["text"] for m in messages], ["still parsed"])

    async def test_a_socket_address_resolves_to_a_session_name(self):
        names = ({}, {"/run/user/1000/cc-socks/999.sock": "cweb7"})
        with patch.object(transcripts, "_session_names_sync", lambda: names):
            write_transcript(self.root, "a7", [
                {"type": "assistant", "timestamp": "2026-08-29T10:00:00Z",
                 "message": {"role": "assistant", "content": [
                     {"type": "tool_use", "name": "SendMessage", "input": {
                         "to": "uds:/run/user/1000/cc-socks/999.sock",
                         "message": "replying down the socket"}}]}},
            ])
            message = (await transcripts.agent_traffic())[0]
        self.assertEqual(message["recipient"], "cweb7",
                         "a uds address must resolve to the session's name")


class AgentTrafficEndpointTests(TranscriptRootMixin, unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.set_up_root()

    async def asyncTearDown(self):
        self.tear_down_root()

    async def test_endpoint_returns_messages_and_count(self):
        write_transcript(self.root, "e1", [incoming("cweb2", "hello there")])
        response = await misc_routes.handle_agent_traffic(make_request())
        payload = json.loads(response.body)
        self.assertEqual(payload["count"], len(payload["messages"]))
        self.assertEqual(payload["messages"][0]["text"], "hello there")

    async def test_junk_parameters_do_not_break_it(self):
        write_transcript(self.root, "e2", [incoming("cweb2", "hi")])
        response = await misc_routes.handle_agent_traffic(
            make_request({"limit": "lots", "files": "many"}))
        self.assertEqual(response.status_code, 200)


# ── Repairing a transcript for a strict backend ──────────────────────────────


def linked(uuid, parent, text, kind="assistant"):
    return {
        "type": kind, "uuid": uuid, "parentUuid": parent, "sessionId": "s",
        "timestamp": "2026-08-29T10:00:00Z",
        "message": {"role": kind, "content": [{"type": "text", "text": text}]},
    }


class TranscriptRepairTests(TranscriptRootMixin, unittest.IsolatedAsyncioTestCase):
    """A gateway can record assistant messages whose only content is empty.

    It replays them happily; the Anthropic API rejects the whole request with
    "text content blocks must be non-empty", so such a conversation cannot be
    moved to Anthropic until they are removed.
    """

    async def asyncSetUp(self):
        self.set_up_root()

    async def asyncTearDown(self):
        self.tear_down_root()

    def written(self, session_id="s"):
        path = transcripts.transcript_path(session_id)
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]

    async def test_a_healthy_transcript_is_left_alone(self):
        write_transcript(self.root, "s", [user("hi"), assistant("there", "s")])
        result = await transcripts.repair_if_needed("s")
        self.assertFalse(result["repaired"])
        self.assertEqual(result["removed"], 0)

    async def test_empty_assistant_records_are_removed(self):
        write_transcript(self.root, "s", [
            linked("u1", None, "hello", "user"),
            linked("u2", "u1", ""),
            linked("u3", "u2", "the real reply"),
        ])
        result = await transcripts.repair_if_needed("s")
        self.assertTrue(result["repaired"])
        self.assertEqual(result["removed"], 1)
        self.assertEqual([r["uuid"] for r in self.written()], ["u1", "u3"])

    async def test_children_are_relinked_across_consecutive_removals(self):
        """Two empties in a row must not leave the survivor pointing at nothing."""
        write_transcript(self.root, "s", [
            linked("u1", None, "hello", "user"),
            linked("u2", "u1", ""),
            linked("u3", "u2", ""),
            linked("u4", "u3", "the real reply"),
        ])
        await transcripts.repair_if_needed("s")
        kept = self.written()
        self.assertEqual([r["uuid"] for r in kept], ["u1", "u4"])
        self.assertEqual(kept[-1]["parentUuid"], "u1")

    async def test_no_record_is_left_with_a_missing_parent(self):
        write_transcript(self.root, "s", [
            linked("u1", None, "start", "user"),
            linked("u2", "u1", ""),
            linked("u3", "u2", "reply"),
            linked("u4", "u3", ""),
            linked("u5", "u4", "later"),
        ])
        await transcripts.repair_if_needed("s")
        kept = self.written()
        uuids = {r["uuid"] for r in kept}
        dangling = [r["uuid"] for r in kept
                    if r.get("parentUuid") and r["parentUuid"] not in uuids]
        self.assertEqual(dangling, [])

    async def test_every_non_empty_message_survives(self):
        write_transcript(self.root, "s", [
            linked("u1", None, "keep one", "user"),
            linked("u2", "u1", ""),
            linked("u3", "u2", "keep two"),
            linked("u4", "u3", ""),
            linked("u5", "u4", "keep three"),
        ])
        await transcripts.repair_if_needed("s")
        texts = [block["text"] for record in self.written()
                 for block in record["message"]["content"]]
        self.assertEqual(texts, ["keep one", "keep two", "keep three"])

    async def test_a_backup_is_written_before_the_swap(self):
        write_transcript(self.root, "s", [
            linked("u1", None, "hello", "user"), linked("u2", "u1", "")])
        result = await transcripts.repair_if_needed("s")
        backups = list(transcripts.transcript_path("s").parent.glob("*.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("bak-", result["backup"])
        # The backup must hold the original, empties and all.
        original = [json.loads(line) for line in
                    backups[0].read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(original), 2)

    async def test_repairing_twice_changes_nothing(self):
        write_transcript(self.root, "s", [
            linked("u1", None, "hello", "user"), linked("u2", "u1", "")])
        await transcripts.repair_if_needed("s")
        after_first = transcripts.transcript_path("s").read_text(encoding="utf-8")
        second = await transcripts.repair_if_needed("s")
        self.assertFalse(second["repaired"])
        self.assertEqual(
            transcripts.transcript_path("s").read_text(encoding="utf-8"), after_first)

    async def test_records_that_are_not_touched_keep_their_exact_bytes(self):
        """Rewriting untouched lines risks reordering keys or losing fields."""
        write_transcript(self.root, "s", [
            linked("u1", None, "hello", "user"), linked("u2", "u1", "")])
        path = transcripts.transcript_path("s")
        before = path.read_text(encoding="utf-8").splitlines()[0]
        await transcripts.repair_if_needed("s")
        self.assertEqual(path.read_text(encoding="utf-8").splitlines()[0], before)

    async def test_an_unknown_session_is_not_an_error(self):
        result = await transcripts.repair_if_needed("missing")
        self.assertFalse(result["repaired"])

    async def test_an_assistant_record_with_other_content_is_kept(self):
        """A record carrying a tool call survives; the empty block inside it does not.

        This asserted ``repaired is False`` -- that the record was left entirely
        alone. That encoded the old limitation rather than the contract: the API
        refuses *any* empty text block ("text content blocks must be non-empty"),
        not only a record made of one, so leaving the block in place left the
        conversation just as unreplayable as before.

        The property the test is named for is the one that matters and is
        unchanged: a record carrying real content is never dropped. It is now
        asserted directly, on the record, instead of through ``repaired``.
        """
        write_transcript(self.root, "s", [{
            "type": "assistant", "uuid": "u1", "parentUuid": None,
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": ""},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/a"}}]},
        }])
        await transcripts.repair_if_needed("s")

        path = transcripts.transcript_path("s")
        records = [json.loads(line) for line
                   in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(records), 1, "a record carrying a tool call must stay")
        content = records[0]["message"]["content"]
        self.assertEqual([b["type"] for b in content], ["tool_use"],
                         "the empty text block is what the API refuses")


# ── Usage from terminal sessions ─────────────────────────────────────────────


def spent(model=TESTING_MODEL, inp=100, out=20, read=5, create=7, **extra):
    record = {
        "type": "assistant", "timestamp": "2026-08-28T09:00:00Z",
        "message": {"role": "assistant", "model": model,
                    "content": [{"type": "text", "text": "hi"}],
                    "usage": {"input_tokens": inp, "output_tokens": out,
                              "cache_read_input_tokens": read,
                              "cache_creation_input_tokens": create}},
    }
    record.update(extra)
    return record


class CliUsageExtractionTests(TranscriptRootMixin, unittest.IsolatedAsyncioTestCase):
    """Turns run in a terminal never reach this app, so their spend is only
    recoverable from the transcript afterwards."""

    async def asyncSetUp(self):
        self.set_up_root()

    async def asyncTearDown(self):
        self.tear_down_root()

    async def test_token_counts_are_read_from_an_assistant_record(self):
        write_transcript(self.root, "u1", [spent()])
        rows, offset = await transcripts.usage_since("u1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], TESTING_MODEL)
        self.assertEqual(rows[0]["input_tokens"], 100)
        self.assertEqual(rows[0]["output_tokens"], 20)
        self.assertEqual(rows[0]["cache_read_tokens"], 5)
        self.assertEqual(rows[0]["cache_creation_tokens"], 7)
        self.assertGreater(offset, 0)

    async def test_the_cursor_stops_a_second_pass_counting_again(self):
        """Without this every poll would inflate the totals."""
        write_transcript(self.root, "u2", [spent(), spent()])
        first, offset = await transcripts.usage_since("u2")
        again, offset2 = await transcripts.usage_since("u2", offset)
        self.assertEqual(len(first), 2)
        self.assertEqual(again, [])
        self.assertEqual(offset, offset2)

    async def test_only_new_records_are_returned_after_the_cursor(self):
        path = write_transcript(self.root, "u3", [spent(inp=10)])
        _first, offset = await transcripts.usage_since("u3")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(spent(inp=999)) + "\n")
        rows, _ = await transcripts.usage_since("u3", offset)
        self.assertEqual([r["input_tokens"] for r in rows], [999])

    async def test_a_synthetic_model_is_not_counted_as_spend(self):
        """A synthetic reply is the CLI reporting an error, and bought nothing."""
        write_transcript(self.root, "u4", [spent(model="<synthetic>")])
        rows, _ = await transcripts.usage_since("u4")
        self.assertEqual(rows, [])

    async def test_a_record_with_no_tokens_is_skipped(self):
        write_transcript(self.root, "u5", [spent(inp=0, out=0, read=0, create=0)])
        rows, _ = await transcripts.usage_since("u5")
        self.assertEqual(rows, [])

    async def test_user_records_carry_no_usage(self):
        write_transcript(self.root, "u6", [user("hello"), spent()])
        rows, _ = await transcripts.usage_since("u6")
        self.assertEqual(len(rows), 1)

    async def test_cost_is_absent_rather_than_invented(self):
        """A third-party gateway reports no trustworthy cost."""
        write_transcript(self.root, "u7", [spent()])
        rows, _ = await transcripts.usage_since("u7")
        self.assertIsNone(rows[0]["cost_usd"])

    async def test_a_partial_trailing_line_is_not_parsed(self):
        path = write_transcript(self.root, "u8", [spent()])
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"type": "assistant", "message": {"usa')  # mid-write
        rows, offset = await transcripts.usage_since("u8")
        self.assertEqual(len(rows), 1)
        self.assertLess(offset, path.stat().st_size,
                        "the cursor must stop before the incomplete line")

    async def test_an_unknown_session_yields_nothing(self):
        rows, offset = await transcripts.usage_since("missing", 0)
        self.assertEqual((rows, offset), ([], 0))


class CliUsageImportTests(TranscriptRootMixin, unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.set_up_root()
        self.dbtmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.dbtmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", self.dbtmp.name)
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.root_patch.stop()
        self.db_patch.stop()
        self.dbtmp.cleanup()
        self.tear_down_root()

    async def test_imported_rows_reach_the_usage_totals(self):
        write_transcript(self.root, "i1", [spent(inp=500, out=50)])
        imported = await misc_routes._import_cli_usage("admin")
        self.assertEqual(imported, 1)
        totals = await db.usage_totals("admin", days=None)
        row = next(t for t in totals if t["model"] == TESTING_MODEL)
        self.assertEqual(row["input_tokens"], 500)

    async def test_importing_twice_does_not_double_the_totals(self):
        write_transcript(self.root, "i2", [spent(inp=500)])
        await misc_routes._import_cli_usage("admin")
        again = await misc_routes._import_cli_usage("admin")
        self.assertEqual(again, 0, "a second import must add nothing")
        totals = await db.usage_totals("admin", days=None)
        row = next(t for t in totals if t["model"] == TESTING_MODEL)
        self.assertEqual(row["input_tokens"], 500, "totals must not climb on a re-run")

    async def test_the_turns_keep_the_time_they_happened(self):
        """Stamping imported history 'now' would break every windowed query."""
        write_transcript(self.root, "i3", [spent()])
        await misc_routes._import_cli_usage("admin")
        cur = await db.db_conn.execute(
            "SELECT created_at FROM usage_events WHERE provider='cli'")
        self.assertEqual((await cur.fetchone())["created_at"], "2026-08-28T09:00:00Z")

    async def test_rows_are_marked_as_coming_from_the_cli(self):
        write_transcript(self.root, "i4", [spent()])
        await misc_routes._import_cli_usage("admin")
        cur = await db.db_conn.execute(
            "SELECT provider, chat_id, session_id FROM usage_events")
        row = await cur.fetchone()
        self.assertEqual(row["provider"], "cli")
        # The session goes in its own column. usage_recent LEFT JOINs chat_id
        # against chats, so a session id there would join nothing and render a
        # blank title beside real numbers.
        self.assertEqual(row["session_id"], "i4")
        self.assertEqual(row["chat_id"], "")


class FailedTurnDetectionTests(TranscriptRootMixin, unittest.TestCase):
    """A session retrying a dead endpoint reports busy and raised no alert.

    Pedro hit this: cweb5 sat on "API Error: 500 ... Retrying in 11s - attempt
    9/10" and the orchestrator showed nothing. Claude Code's status field said
    busy the whole time -- correctly, it was retrying -- and the orchestrator
    short-circuits on busy without reading the transcript at all, which is what
    keeps polling cheap. So a run going nowhere looked exactly like one doing
    work.

    The failure is recorded structurally: an assistant record whose model is
    "<synthetic>", the CLI speaking in the model's voice. That beats matching
    prose, which is what the existing _attention() does and why it returned
    None for this text -- no trailing "?" and none of its nine blocker phrases.
    """

    def setUp(self):
        self.set_up_root()

    def tearDown(self):
        self.tear_down_root()

    @staticmethod
    def _synthetic(text):
        return {"type": "assistant", "sessionId": "s1",
                "message": {"role": "assistant", "model": "<synthetic>",
                            "content": [{"type": "text", "text": text}]}}

    def _run(self, session_id="s1"):
        import asyncio
        return asyncio.run(transcripts.last_error(session_id))

    def test_the_real_error_pedro_saw_is_reported(self):
        text = ("API Error: 500 litellm.InternalServerError: InternalServerError: "
                "Hosted_vllmException - Cannot connect to host vllm:8000 "
                "[Name or service not known]. Received Model "
                "Group=vllm/Qwen3.6-35B-A3B-NVFP4 - Retrying in 11s - attempt 9/10")
        write_transcript(self.root, "s1", [user("go"), self._synthetic(text)])
        self.assertEqual(self._run(), text)

    def test_a_recovered_session_is_not_reported(self):
        """The whole reason it reads the newest turn rather than scanning.

        A session that failed and then succeeded is healthy. Reporting the old
        failure would leave it flagged until dismissed by hand, which trains
        people to dismiss without looking.
        """
        write_transcript(self.root, "s1", [
            user("go"),
            self._synthetic("API Error: 500 gateway is down"),
            assistant("Recovered, here is the answer"),
        ])
        self.assertIsNone(self._run())

    def test_a_healthy_session_reports_nothing(self):
        write_transcript(self.root, "s1", [user("go"), assistant("all done")])
        self.assertIsNone(self._run())

    def test_a_real_model_saying_the_words_api_error_is_not_a_failure(self):
        """An agent discussing an error has not suffered one.

        This is the false positive a text search would produce, and the reason
        the model field is the gate rather than the wording.
        """
        write_transcript(self.root, "s1", [
            user("what went wrong?"),
            assistant("API Error: 500 was what the log showed, so I retried it"),
        ])
        self.assertIsNone(self._run())

    def test_synthetic_output_that_is_not_an_error_is_ignored(self):
        write_transcript(self.root, "s1", [self._synthetic("Session resumed")])
        self.assertIsNone(self._run())

    def test_an_unknown_session_is_not_an_error(self):
        self.assertIsNone(self._run("no-such-session"))

    def test_only_the_tail_of_a_large_transcript_is_read(self):
        """It runs on every poll for every busy session; the archive is 22 MB."""
        filler = [assistant("x" * 900) for _ in range(400)]
        write_transcript(self.root, "s1", filler + [self._synthetic("API Error: 503 nope")])
        path = self.root / "-home-kali-demo" / "s1.jsonl"
        self.assertGreater(path.stat().st_size, transcripts._ERROR_TAIL_BYTES,
                           "fixture must exceed the tail window or this proves nothing")
        self.assertEqual(self._run(), "API Error: 503 nope")

    def test_a_truncated_first_line_does_not_break_the_read(self):
        """Seeking into the middle of a line is the normal case for a tail read."""
        filler = [assistant("y" * 977) for _ in range(400)]
        write_transcript(self.root, "s1", filler + [assistant("fine")])
        self.assertIsNone(self._run())


if __name__ == "__main__":
    unittest.main()
