"""QA: a prompt that exists only on the terminal is visible and answerable.

A session can be blocked on a question the transcript has no record of. The case
that exposed it was a permission prompt:

    Permission rule Bash(curl*) requires confirmation for this command.
    Do you want to proceed?
    ❯ 1. Yes
      2. No

That is a TUI interaction. The CLI never writes it to the transcript JSONL, so
``transcripts.pending_question`` returns None for it. All three question
handlers -- get, answer and dismiss -- gated on that one call, so all three
agreed no question was waiting while the session sat blocked on one, and the
console offered no way to answer it. Meanwhile the session's own status file said
`waiting` and the terminal had been showing the prompt, parseable, the whole
time: ``looks_like_a_prompt`` returned True and ``visible_options`` returned
both choices. Every piece was present and nothing connected them.

Three readers looked at the classifier and agreed the badge was at fault. It was
not -- the row *was* in the waiting feed. The fault was that nothing ever asked
the terminal, which is the only place the question existed.

The fixtures here are the real layout, copied off a live session's screen rather
than imagined, because two of its details are load-bearing and neither is what
you would invent: the question block contains a blank line, and its lines are
separate rows.
"""
from __future__ import annotations

import inspect
import unittest
from unittest.mock import AsyncMock, patch

import app
import prompts

# Verbatim from `screen -X hardcopy` of a blocked session. Two details matter:
#
#   - a blank line sits *between* the two lines of the question. Treating a
#     blank as the end of the block kept only "Do you want to proceed?" -- the
#     half that does not say what is being approved.
#   - the framed command above it is 40+ lines of commit message in the original.
#     The frame is the stop that keeps it out of the question text.
REAL_SNAPSHOT = """\
   │ small thing to get wrong in the one line that tells you which backend you are
   │ about to talk to." && git log --oneline -1; echo; timeout 300 git push origin main
   Commit and push the banner fix

 Permission rule Bash(git push*) requires confirmation for this command.

 Do you want to proceed?
 ❯ 1. Yes
   2. No
   Esc to cancel · Tab to amend · ctrl+e to explain
"""

NOT_A_PROMPT = """\
 Running the suite now.
   169 passed, 68 subtests passed in 12.80s
 Esc to interrupt
"""


def _read_with(snapshot: str, session_id: str = "s1"):
    """`read_prompt` against *snapshot*, with the multiplexer stubbed out."""
    target = {"kind": "screen", "session": "1.pts-1", "window": "0"}
    with (
        patch.object(prompts, "locate", return_value=target),
        patch.object(prompts, "refresh", return_value=snapshot),
    ):
        return prompts.read_prompt(session_id)


class PromptTextTests(unittest.TestCase):
    """Reading the question off the screen."""

    def test_a_blank_line_inside_the_block_does_not_end_it(self):
        """The bug that made the feature useless while appearing to work.

        `read_prompt` returned a prompt, `pending` was True, options were right
        -- and the question read "Do you want to proceed?" with no trace of
        what was being proposed. Answerable but not decidable.
        """
        lines = prompts.prompt_lines(REAL_SNAPSHOT)
        self.assertIn(
            "Permission rule Bash(git push*) requires confirmation for this command.",
            lines,
            "the line naming what is being approved was dropped, so the user is "
            "asked to confirm something the prompt does not identify",
        )
        self.assertIn("Do you want to proceed?", lines)

    def test_the_framed_command_is_not_read_as_the_question(self):
        """Box-drawing characters stop the walk upwards.

        Without the stop, a prompt about a commit would quote the whole commit
        message back as its question.
        """
        text = prompts.prompt_text(REAL_SNAPSHOT)
        self.assertNotIn("git push origin main", text)
        self.assertNotIn("│", text)

    def test_lines_are_returned_top_to_bottom(self):
        text = prompts.prompt_text(REAL_SNAPSHOT)
        self.assertLess(
            text.index("Permission rule"), text.index("Do you want to proceed"),
            "the block is collected by walking upwards and must be reversed "
            "before display, or the question reads backwards",
        )

    def test_a_run_of_two_blanks_ends_the_block(self):
        snapshot = "far above\n\n\n the question\n ❯ 1. Yes\n Esc to cancel\n"
        self.assertEqual(prompts.prompt_lines(snapshot), ["the question"])

    def test_collection_is_capped(self):
        """An unframed prompt cannot drag unbounded output into the question."""
        snapshot = "".join(f" line {n}\n" for n in range(40))
        snapshot += " ❯ 1. Yes\n Esc to cancel\n"
        self.assertLessEqual(
            len(prompts.prompt_lines(snapshot)), prompts._PROMPT_TEXT_MAX_LINES)

    def test_no_options_means_no_question(self):
        self.assertEqual(prompts.prompt_lines(NOT_A_PROMPT), [])
        self.assertEqual(prompts.prompt_text(NOT_A_PROMPT), "")


class ReadPromptTests(unittest.TestCase):
    """The observation primitive."""

    def test_it_reports_the_prompt(self):
        got = _read_with(REAL_SNAPSHOT)
        self.assertIsNotNone(got)
        self.assertEqual(got["source"], "terminal")
        self.assertTrue(got["approval"])
        self.assertEqual(got["questions"][0]["header"], "Permission")

    def test_the_needle_is_a_contiguous_run_of_the_screen(self):
        """The property that decides whether answering works at all.

        `find_target` confirms the prompt is still up with
        ``needle.strip()[:60] in snapshot``. The question text is built by
        joining rows with spaces, and that joined string appears nowhere on a
        screen where those rows are separate lines -- so using it as the needle
        made the check fail every time, `find_target` return None, and the
        endpoint answer "this session is not running inside screen or tmux".
        A false explanation, for a session that was.
        """
        got = _read_with(REAL_SNAPSHOT)
        self.assertIn(
            got["needle"].strip()[:60], REAL_SNAPSHOT,
            "the needle is not present on the screen it was read from, so "
            "find_target will refuse the answer and blame the multiplexer",
        )

    def test_the_needle_is_specific_rather_than_the_last_line(self):
        """"Do you want to proceed?" is shown by every permission prompt.

        As a needle it would confirm a *different* prompt just as readily as
        this one, which is the check's whole purpose.
        """
        got = _read_with(REAL_SNAPSHOT)
        self.assertNotEqual(got["needle"].strip(), "Do you want to proceed?")
        self.assertIn("Permission rule", got["needle"])

    def test_options_are_left_for_the_terminal_to_supply(self):
        """Empty, for the reason `transcripts._approval_block` leaves it empty.

        The endpoint reads the real labels with `visible_options`. A list
        invented here would put answers in front of the user that the terminal
        never offered, and answering is by index.
        """
        self.assertEqual(_read_with(REAL_SNAPSHOT)["questions"][0]["options"], [])

    def test_it_carries_no_transcript_id(self):
        """Nothing recorded this question, so there is no id to claim."""
        self.assertEqual(_read_with(REAL_SNAPSHOT)["id"], "")

    def test_it_matches_the_shape_callers_already_handle(self):
        """Callers must not branch on where the question came from."""
        got = _read_with(REAL_SNAPSHOT)
        for key in ("id", "questions", "approval", "needle"):
            self.assertIn(key, got)

    def test_ordinary_output_is_not_a_question(self):
        self.assertIsNone(_read_with(NOT_A_PROMPT))

    def test_an_unhosted_session_is_not_a_question(self):
        with patch.object(prompts, "locate", return_value=None):
            self.assertIsNone(prompts.read_prompt("s1"))


class HasPromptTests(unittest.TestCase):
    """The polled path."""

    def setUp(self):
        prompts._prompt_cache.clear()
        self.addCleanup(prompts._prompt_cache.clear)

    def test_repeated_asks_capture_once(self):
        """Two surfaces poll every few seconds; each capture is a subprocess."""
        calls = []

        def counted(session_id):
            calls.append(session_id)
            return {"needle": "x"}

        with patch.object(prompts, "read_prompt", counted):
            self.assertTrue(prompts.has_prompt("s1"))
            self.assertTrue(prompts.has_prompt("s1"))
            self.assertTrue(prompts.has_prompt("s1"))
        self.assertEqual(len(calls), 1, "the cache is not being consulted")

    def test_the_cache_expires(self):
        calls = []
        with patch.object(prompts, "read_prompt",
                          lambda sid: calls.append(sid) or {"needle": "x"}):
            prompts.has_prompt("s1", ttl_s=0.0)
            prompts.has_prompt("s1", ttl_s=0.0)
        self.assertEqual(len(calls), 2, "a stale answer is being served forever")

    def test_a_capture_failure_is_not_a_question(self):
        """A surface must render when the multiplexer misbehaves."""
        with patch.object(prompts, "read_prompt", side_effect=OSError("boom")):
            self.assertFalse(prompts.has_prompt("s1"))

    def test_the_cache_is_bounded(self):
        with patch.object(prompts, "read_prompt", lambda sid: None):
            for n in range(prompts._PROMPT_CACHE_MAX + 50):
                prompts.has_prompt(f"s{n}")
        self.assertLessEqual(
            len(prompts._prompt_cache), prompts._PROMPT_CACHE_MAX + 1)


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    """`_pending_prompt`: transcript first, terminal second."""

    async def test_the_transcript_wins_when_it_has_one(self):
        """Order is not only about richness.

        A session can hold a recorded question *and* show a prompt. The recorded
        one is the question the user was actually asked, so reading the screen
        first would answer the wrong one.
        """
        recorded = {"id": "tool_1", "questions": [{"question": "recorded?"}],
                    "approval": False, "needle": "recorded?"}
        with (
            patch.object(app.transcripts, "pending_question", return_value=recorded),
            patch.object(app.prompts, "read_prompt", return_value={"id": ""}) as live,
        ):
            got = await app._pending_prompt("s1")
        self.assertEqual(got["id"], "tool_1")
        live.assert_not_called()

    async def test_the_terminal_is_used_when_the_transcript_is_silent(self):
        with (
            patch.object(app.transcripts, "pending_question", return_value=None),
            patch.object(app.prompts, "read_prompt",
                         return_value={"id": "", "source": "terminal"}),
        ):
            got = await app._pending_prompt("s1")
        self.assertEqual(got["source"], "terminal")

    async def test_neither_is_no_question(self):
        with (
            patch.object(app.transcripts, "pending_question", return_value=None),
            patch.object(app.prompts, "read_prompt", return_value=None),
        ):
            self.assertIsNone(await app._pending_prompt("s1"))


class EveryHandlerUsesTheResolverTests(unittest.TestCase):
    """All three, because all three were wrong the same way.

    The bug was not one handler missing a case. `pending_question` was called
    directly in get, answer and dismiss, so a prompt was invisible, unanswerable
    *and* undismissable -- three symptoms of one line repeated three times.
    Fixing the one the user noticed would have left the other two.
    """

    HANDLERS = (
        "handle_chat_question_get",
        "handle_chat_question_answer",
        "handle_chat_question_dismiss",
    )

    def test_none_of_them_calls_the_transcript_directly(self):
        for name in self.HANDLERS:
            with self.subTest(handler=name):
                body = inspect.getsource(getattr(app, name))
                self.assertNotIn(
                    "transcripts.pending_question", body,
                    f"{name} asks the transcript directly, so it is blind to "
                    "any prompt the CLI does not record",
                )
                self.assertIn("_pending_prompt", body)


class FeedMarksTheQuestionTests(unittest.TestCase):
    """The badge's "?" -- being answerable is no use if it cannot be found."""

    SESSION = "sess-1"

    def _classify(self, prompt_map):
        return app.classify_chat(
            {"id": "c1", "title": "cweb2", "session_id": self.SESSION},
            {"role": "assistant", "created_at": "2026-09-02T10:00:00Z",
             "preview": "running the push", "tail": "running the push"},
            frozenset(), {}, {},
            {self.SESSION: "waiting"},
            {},
            {self.SESSION: "2026-09-02T10:00:00Z"},
            prompt_map,
        )

    def test_a_prompt_on_screen_marks_the_row(self):
        entry = self._classify({self.SESSION: True})
        self.assertEqual(entry["status"], "waiting")
        self.assertTrue(
            entry["question"],
            "the row is blocked on a prompt the console can answer and is not "
            "marked as asking anything, so there is nothing to click towards",
        )

    def test_a_blocked_session_with_no_prompt_is_not_marked(self):
        """`waiting` says a person is needed, not that a question was put.

        Marking every blocked row would make the "?" mean "stopped", and the
        badge is only worth having while each mark is real.
        """
        self.assertFalse(self._classify({self.SESSION: False})["question"])

    def test_the_map_is_optional(self):
        """Existing callers pass eight positional arguments."""
        self.assertFalse(self._classify(None)["question"])


class CliMapsTests(unittest.IsolatedAsyncioTestCase):
    """Capturing terminals from a polled endpoint, without paying for it."""

    async def test_only_blocked_sessions_are_captured(self):
        """A busy or idle session is not sitting on a prompt.

        Both callers are polled and each capture costs a subprocess with a
        five-second timeout, so asking about a session whose status already
        answers the question is pure latency on a hot path.
        """
        sessions = [
            {"sessionId": "waiting-1", "status": "waiting", "status_updated_at": "t"},
            {"sessionId": "busy-1", "status": "busy", "status_updated_at": "t"},
            {"sessionId": "idle-1", "status": "idle", "status_updated_at": "t"},
        ]
        asked = []
        with (
            patch.object(app.db, "read_claude_sessions",
                         AsyncMock(return_value=sessions)),
            patch.object(app.prompts, "has_prompt",
                         lambda sid: asked.append(sid) or True),
        ):
            _status, _dismiss, _updated, prompting = await app._cli_maps({})
        self.assertEqual(asked, ["waiting-1"])
        self.assertTrue(prompting["waiting-1"])

    async def test_an_unreadable_registry_yields_four_maps(self):
        """Both call sites unpack four. Three would raise ValueError and take
        the whole endpoint down rather than degrade."""
        with patch.object(app.db, "read_claude_sessions",
                          AsyncMock(side_effect=OSError("boom"))):
            maps = await app._cli_maps({})
        self.assertEqual(len(maps), 4)
        self.assertEqual(maps, ({}, {}, {}, {}))


class RegressionShapeTests(unittest.TestCase):
    """Guards on the two constants this depends on."""

    def test_the_option_pattern_matches_the_real_prompt(self):
        """`prompt_lines` finds the question by locating the first option, so
        its pattern and `visible_options`' pattern must agree about what an
        option looks like."""
        self.assertTrue(prompts.looks_like_a_prompt(REAL_SNAPSHOT))
        options = prompts.visible_options(REAL_SNAPSHOT)
        self.assertEqual([o["label"] for o in options], ["Yes", "No"])
        self.assertEqual(prompts.selected_index(REAL_SNAPSHOT), 1)

    def test_permission_wording_is_recognised(self):
        for text in (
            "Permission rule Bash(curl*) requires confirmation for this command.",
            "Claude wants to run a command",
            "This action requires confirmation",
        ):
            with self.subTest(text=text):
                self.assertTrue(prompts._PERMISSION_RE.search(text))

    def test_ordinary_questions_are_not_labelled_permission(self):
        got = _read_with(
            " Which database should this use?\n ❯ 1. SQLite\n   2. Postgres\n"
            " Esc to cancel\n")
        self.assertEqual(got["questions"][0]["header"], "Waiting")


if __name__ == "__main__":
    unittest.main()
