"""QA: a transcript the API refuses is repaired once, and the user is told.

Two API errors mean the stored conversation itself cannot be replayed, not
that one turn was unlucky:

    400 messages.N.content.M.thinking: each thinking block must contain
        non-whitespace
    400 messages: text content blocks must be non-empty

The API rejects the *whole request*, and `--resume` re-sends the whole history
on every turn, so once either block is in the file every later turn fails
identically -- permanently, and invisibly.

`bin/claude-transcript-doctor.py` already knows how to remove them. This wires
it to the moment the refusal actually arrives, rather than running it on every
turn. The repair is reactive on purpose: the turn that tripped it is already
lost, so the chat is flagged degraded and the user re-sends. Nothing is
retried automatically -- a retry re-sends the prompt, which this repo has
already been bitten by once (commit 80c00e5).

The load-bearing test here is the negative one: an unrelated failure (429,
500, a context-window error) must never rewrite a transcript.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import config
import db
from routes import chats as chat_routes


class RefusalMatchingTests(unittest.TestCase):
    """Which error strings arm the repair, and -- more importantly -- which do not."""

    def test_a_thinking_refusal_is_recognised(self):
        self.assertTrue(chat_routes._is_transcript_refusal(
            "API Error: 400 messages.52.content.1.thinking: each thinking "
            "block must contain non-whitespace"
        ))

    def test_an_empty_text_refusal_is_recognised(self):
        self.assertTrue(chat_routes._is_transcript_refusal(
            "API Error: 400 messages: text content blocks must be non-empty"
        ))

    def test_matching_ignores_case(self):
        self.assertTrue(chat_routes._is_transcript_refusal(
            "EACH THINKING BLOCK MUST CONTAIN NON-WHITESPACE"
        ))

    def test_a_rate_limit_is_not_a_transcript_problem(self):
        self.assertFalse(chat_routes._is_transcript_refusal(
            "API Error: 429 rate_limit_error: too many requests"
        ))

    def test_a_server_error_is_not_a_transcript_problem(self):
        self.assertFalse(chat_routes._is_transcript_refusal(
            "API Error: 500 internal server error"
        ))

    def test_a_context_window_error_is_not_a_transcript_problem(self):
        """Named explicitly: it is a 400 about message content too, and
        rewriting a transcript because the conversation grew too long would
        destroy history to fix nothing."""
        self.assertFalse(chat_routes._is_transcript_refusal(
            "API Error: 400 ContextWindowExceededError: prompt is too long"
        ))

    def test_an_empty_error_is_not_a_transcript_problem(self):
        self.assertFalse(chat_routes._is_transcript_refusal(""))


class RepairAfterRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await db.chat_create("c1", "Chat", None, f"{self.tmp.name}/p", "admin")

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    def _doctor(self, returncode=0, output=b"repaired 1 session"):
        """A stand-in for the doctor subprocess, at the exec boundary."""
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(output, b""))
        proc.returncode = returncode
        return patch.object(
            chat_routes.asyncio, "create_subprocess_exec",
            AsyncMock(return_value=proc),
        )

    async def test_the_doctor_is_run_for_the_offending_session(self):
        with self._doctor() as spawn:
            await chat_routes._repair_after_refusal("c1", "sess-1")
        spawn.assert_awaited_once()
        argv = spawn.await_args.args
        self.assertIn("--fix", argv)
        # By session id: the console never holds the human-readable name.
        self.assertIn("sess-1", argv)
        self.assertTrue(
            any(str(a).endswith("claude-transcript-doctor.py") for a in argv),
            f"the doctor script is not in the argv: {argv}",
        )

    async def test_the_chat_is_flagged_so_the_user_knows_to_resend(self):
        with self._doctor():
            await chat_routes._repair_after_refusal("c1", "sess-1")
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["degraded"], 1)
        self.assertIn("transcript:", chat["degraded_reason"])

    async def test_the_notice_tells_the_user_to_send_again(self):
        """A flag nobody can act on is just a red dot."""
        with self._doctor():
            await chat_routes._repair_after_refusal("c1", "sess-1")
        chat = await db.chat_get("c1", "admin")
        self.assertIn("send", chat["degraded_reason"].lower())

    async def test_the_notice_warns_that_a_live_terminal_can_undo_it(self):
        """The doctor's own banner: repairing a transcript underneath a
        running session changes nothing, because that session holds the
        conversation in memory and writes it back on its way past. Claiming
        an unconditional fix would be a lie for exactly that case."""
        with self._doctor():
            await chat_routes._repair_after_refusal("c1", "sess-1")
        chat = await db.chat_get("c1", "admin")
        self.assertIn("terminal", chat["degraded_reason"].lower())

    async def test_a_doctor_that_fails_still_flags_the_chat(self):
        """The turn is lost either way; silence is the one unacceptable
        outcome, because the chat is then permanently broken with no trace."""
        with self._doctor(returncode=2, output=b"could not install the result"):
            await chat_routes._repair_after_refusal("c1", "sess-1")
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["degraded"], 1)
        self.assertIn("could not", chat["degraded_reason"].lower())

    async def test_a_doctor_that_cannot_be_launched_does_not_raise(self):
        """This runs inside a turn's event handler. Raising here would turn a
        recoverable refusal into a 500 on top of it."""
        with patch.object(
            chat_routes.asyncio, "create_subprocess_exec",
            AsyncMock(side_effect=OSError("no such file")),
        ):
            await chat_routes._repair_after_refusal("c1", "sess-1")
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["degraded"], 1)

    async def test_a_chat_with_no_session_id_is_skipped(self):
        """No linked CLI session means no transcript to repair, so spawning
        the doctor would be a subprocess that can only report nothing."""
        with self._doctor() as spawn:
            await chat_routes._repair_after_refusal("c1", None)
        spawn.assert_not_awaited()
        chat = await db.chat_get("c1", "admin")
        self.assertEqual(chat["degraded"], 0)


if __name__ == "__main__":
    unittest.main()
