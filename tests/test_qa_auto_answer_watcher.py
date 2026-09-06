"""The auto-answer watcher, and the option choice it is allowed to make.

Design: docs/superpowers/specs/2026-09-02-auto-answer-knob-design.md, step 2.

Most of this file is about one decision: which option a machine may press on a
human's behalf. A permission prompt offers two affirmative answers --

    1. Yes
    2. Yes, and don't ask again for Bash(curl*) in this project

-- and only the first is the one the operator asked for. The second grants a
standing permission for every future command matching that rule, and because
`prompts.answer` navigates and confirms, a wrong choice is committed before
anybody sees it. So selection is by label, never by position, and when no single
unambiguous affirmative exists the watcher does not fire at all.

The other property under test is that a structured AskUserQuestion is never
auto-answered. It has no "yes": given `Delete the branch` / `Keep it`, any
automatic choice is a guess. `approval` is the flag both producers already set
(prompts.py and transcripts.py), so the gate reads that rather than guessing
from the prompt text -- a phrase heuristic on model-authored prose is what
produced false positives on "worth fixing" and "your call" elsewhere in this
tree.

`resolve_pending` and `deliver` are injected rather than imported. The canonical
pending-prompt lookup is `routes.chats._pending_prompt`, which is private to a
module split out an hour ago; injecting keeps this module free of a routes
import, avoids a cycle, and matches how `sysstats.start` already takes its
store as an argument.
"""
from __future__ import annotations

import asyncio
import contextlib
import tempfile
import time
import unittest
from unittest.mock import patch

import auth
import auto_answer
import config
import db

# ── The option choice ───────────────────────────────────────────────────────

class ChooseAffirmativeTests(unittest.TestCase):
    """Pure function, no I/O. The security-critical half of the feature."""

    def opt(self, *labels):
        return [{"index": i, "label": t} for i, t in enumerate(labels, start=1)]

    def test_the_plain_yes_is_chosen(self):
        chosen = auto_answer.choose_affirmative(self.opt(
            "Yes",
            "Yes, and don't ask again for Bash(curl*) in this project",
            "No, and tell Claude what to do differently",
        ))
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["index"], 1)
        self.assertEqual(chosen["label"], "Yes")

    def test_position_does_not_decide_it(self):
        """The same options, reordered. A position-based rule would take the
        broadening one here, which is the whole failure this guards.
        """
        chosen = auto_answer.choose_affirmative(self.opt(
            "Yes, and don't ask again for Bash(curl*) in this project",
            "Yes",
            "No, and tell Claude what to do differently",
        ))
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["label"], "Yes")
        self.assertEqual(chosen["index"], 2)

    def test_a_broadening_option_alone_is_refused(self):
        """Better to leave the prompt than to grant a standing permission."""
        self.assertIsNone(auto_answer.choose_affirmative(self.opt(
            "Yes, and don't ask again for Bash(curl*)",
            "No",
        )))

    def test_always_is_a_broadening_word(self):
        self.assertIsNone(auto_answer.choose_affirmative(self.opt(
            "Yes, always allow this", "No",
        )))

    def test_two_plain_affirmatives_are_ambiguous(self):
        """Two answers that both look right means the wording is not understood,
        and guessing between them is the thing this module must not do.
        """
        self.assertIsNone(auto_answer.choose_affirmative(self.opt(
            "Yes", "Approve", "No",
        )))

    def test_no_affirmative_at_all(self):
        self.assertIsNone(auto_answer.choose_affirmative(self.opt(
            "Edit the file first", "Cancel",
        )))

    def test_no_options(self):
        self.assertIsNone(auto_answer.choose_affirmative([]))

    def test_a_negative_containing_yes_is_not_affirmative(self):
        """"No, and tell Claude..." must not match on a substring."""
        chosen = auto_answer.choose_affirmative(self.opt(
            "No, don't do that", "Yes, proceed",
        ))
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["label"], "Yes, proceed")

    def test_case_and_padding_do_not_matter(self):
        chosen = auto_answer.choose_affirmative(self.opt("  YES  ", "No"))
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["index"], 1)


# ── The gate ────────────────────────────────────────────────────────────────

class ApprovalGateTests(unittest.TestCase):
    def test_an_approval_is_answerable(self):
        self.assertTrue(auto_answer.is_answerable({"approval": True}))

    def test_a_structured_question_is_not(self):
        """The property that keeps a machine out of multiple-choice answers."""
        self.assertFalse(auto_answer.is_answerable({
            "questions": [{"question": "Which branch?", "options": [
                {"label": "main"}, {"label": "develop"}]}],
        }))

    def test_nothing_pending_is_not_answerable(self):
        self.assertFalse(auto_answer.is_answerable(None))


# ── One pass over one chat ──────────────────────────────────────────────────

class ConsiderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        await db.user_create("alice", None, auth.hash_password("pw"))
        await db.chat_create("c1", "One", None, f"{self.tmp.name}/p", "alice")
        await db.chat_set_session("c1", "sess-1")
        await db.chat_auto_answer_set("c1", "alice", True)
        self.chat = {"id": "c1", "owner_id": "alice", "session_id": "sess-1"}
        self.delivered: list[tuple[str, int]] = []

    def deliver(self, session_id, index):
        self.delivered.append((session_id, index))
        return {"ok": True, "label": "Yes"}

    async def _consider(self, pending, options=None):
        return await auto_answer.consider(
            self.chat,
            resolve_pending=lambda _sid: pending,
            read_options=lambda _sid: options or [],
            deliver=self.deliver,
        )

    async def test_it_answers_a_permission_prompt(self):
        entry = await self._consider(
            {"approval": True, "questions": [{"question": "Permission rule "
                                              "Bash(curl*) requires confirmation",
                                              "header": "Permission"}]},
            [{"index": 1, "label": "Yes"}, {"index": 2, "label": "No"}],
        )
        self.assertEqual(self.delivered, [("sess-1", 1)])
        self.assertEqual(entry["outcome"], "answered")
        self.assertEqual(entry["kind"], "Permission")
        log = await db.chat_auto_answer_log_get("c1", "alice")
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["outcome"], "answered")

    async def test_it_leaves_a_structured_question_alone(self):
        entry = await self._consider(
            {"questions": [{"question": "Which one?", "options": [
                {"label": "Delete the branch"}, {"label": "Keep it"}]}]},
            [{"index": 1, "label": "Delete the branch"},
             {"index": 2, "label": "Keep it"}],
        )
        self.assertEqual(self.delivered, [], "a machine must not choose here")
        self.assertIsNone(entry, "not a decision worth logging")
        self.assertEqual(await db.chat_auto_answer_log_get("c1", "alice"), [])

    async def test_a_broadening_only_prompt_is_skipped_and_logged(self):
        entry = await self._consider(
            {"approval": True, "questions": [{"question": "Proceed?",
                                              "header": "Permission"}]},
            [{"index": 1, "label": "Yes, and don't ask again"},
             {"index": 2, "label": "No"}],
        )
        self.assertEqual(self.delivered, [])
        self.assertEqual(entry["outcome"], "skipped")
        self.assertIn("affirmative", entry["reason"])
        log = await db.chat_auto_answer_log_get("c1", "alice")
        self.assertEqual(log[0]["outcome"], "skipped",
                         "a skip is the entry worth having: it is still waiting")

    async def test_nothing_pending_records_nothing(self):
        self.assertIsNone(await self._consider(None))
        self.assertEqual(await db.chat_auto_answer_log_get("c1", "alice"), [])

    async def test_a_refused_delivery_is_recorded(self):
        """Not in tmux or screen: prompts.answer cannot reach the terminal."""
        def refuse(_sid, _index):
            return {"ok": False, "reason": "not running inside screen or tmux"}
        entry = await auto_answer.consider(
            self.chat,
            resolve_pending=lambda _sid: {"approval": True, "questions": [
                {"question": "Proceed?", "header": "Permission"}]},
            read_options=lambda _sid: [{"index": 1, "label": "Yes"}],
            deliver=refuse,
        )
        self.assertEqual(entry["outcome"], "skipped")
        self.assertIn("tmux", entry["reason"])


# ── The loop ────────────────────────────────────────────────────────────────

class WatcherLifecycleTests(unittest.IsolatedAsyncioTestCase):
    # Bounded AND timed, and both parts were needed. Mutation-testing this file
    # by deleting `_task.cancel()` first made the suite HANG -- `stop()` awaits
    # the task it cancelled, so with no cancel that await never returns.
    # Wrapping it in wait_for fixed the hang and destroyed the detection: the
    # timeout cancels `stop()`, `stop()` suppresses CancelledError by design
    # (matching sysstats.stop), so it completes, sets _task to None, and the
    # assertion below passes. The mutant then reported 21 passed in 16.4s.
    #
    # A hang is a bad signal; a slow pass is no signal. So the assertion is on
    # elapsed time: cancelling returns in milliseconds, and waiting out a live
    # loop cannot.
    STOP_TIMEOUT_S = 5
    STOP_BUDGET_S = 1.0

    async def _stop(self):
        started = time.monotonic()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(auto_answer.stop(), timeout=self.STOP_TIMEOUT_S)
        return time.monotonic() - started

    async def asyncTearDown(self):
        await self._stop()

    async def test_start_then_stop_leaves_nothing_running(self):
        """rules.md §4: a timer nothing can stop is the named failure. The
        orchestrator page shipped a bare setInterval for exactly this reason.
        """
        auto_answer.start(lambda _sid: None, interval_s=0.01)
        await asyncio.sleep(0.05)
        self.assertTrue(auto_answer.is_running())
        elapsed = await self._stop()
        self.assertFalse(auto_answer.is_running())
        self.assertLess(
            elapsed, self.STOP_BUDGET_S,
            f"stop() took {elapsed:.1f}s: it waited the loop out instead of "
            f"cancelling it, which is the failure rules.md §4 names",
        )

    async def test_starting_twice_does_not_double_the_rate(self):
        auto_answer.start(lambda _sid: None, interval_s=0.01)
        first = auto_answer._task
        auto_answer.start(lambda _sid: None, interval_s=0.01)
        self.assertIs(auto_answer._task, first, "second start must be a no-op")

    async def test_stop_is_safe_when_never_started(self):
        await auto_answer.stop()
        self.assertFalse(auto_answer.is_running())

    async def test_one_bad_pass_does_not_kill_the_watcher(self):
        """A watcher that dies on a single unreadable session stops answering
        every other chat too, silently.
        """
        def explode(_sid):
            raise RuntimeError("unreadable session")
        auto_answer.start(explode, interval_s=0.01)
        await asyncio.sleep(0.06)
        self.assertTrue(auto_answer.is_running(),
                        "the loop must outlive one failing chat")


if __name__ == "__main__":
    unittest.main()
