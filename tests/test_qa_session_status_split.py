"""QA: `idle` and `waiting` are not the same thing.

Claude Code writes its own state into ``~/.claude/sessions/<pid>.json``. On
2.1.252 three values occur:

    busy     working
    idle     nothing further to do -- the task concluded
    waiting  blocked, a person is needed

The orchestrator tested ``status != "busy"``, which put both of the last two into
the waiting feed. So every agent that *finished* was reported as one blocked on a
question, with its closing sentence presented as the thing it was asking -- and
the badge that exists to say "someone needs you" counted rows that needed
nobody. A badge is worth having only while every row in it is real.

The comment the code was written against said the observed value was ``busy``,
singular. It was accurate when written; the CLI grew two more values and nothing
re-read it. So these cases pin the distinction rather than the wording.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

import app
import classification

CHAT_ID = "c0ffee00c0ffee00c0ffee00c0ffee00"
SESSION_ID = "8db15c35-74bc-4670-a617-ad2ff0426ec4"
SPOKE_AT = "2026-08-31T19:12:04Z"


def classify(cli_status, marks=None, last_role="assistant"):
    """Classify a conversation linked to a CLI session in *cli_status*."""
    return classification.classify_chat(
        chat={"id": CHAT_ID, "title": "cweb2", "session_id": SESSION_ID},
        last={"role": last_role, "created_at": SPOKE_AT,
              "preview": "I have finished the refactor and the tests pass."},
        live_ids=frozenset(),
        queued={},
        marks=marks or {},
        cli_status_map={SESSION_ID: cli_status},
        cli_dismiss_map={SESSION_ID: ""},
        cli_status_updated_map={SESSION_ID: SPOKE_AT},
    )


def _code_only(source: str) -> str:
    """*source* with comments and docstrings removed, so only code is scanned.

    Stripping `#` comments was not enough. The note recording why this rule
    changed lives in a **docstring** -- `_session_needs_a_person` explains that
    the check used to compare against "busy" alone -- and a line-based strip
    does not reach inside a triple-quoted string. So the scan found the old
    expression inside its own postmortem for a second time, in a second form,
    after the first had already been fixed.

    `ast.unparse` drops comments for free; the docstrings have to be popped
    explicitly. The alternative, forbidding the explanation from naming the bug
    it explains, trades the reason for the guard and is the wrong way round.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body.pop(0)
    return ast.unparse(tree)


class StatusMeaningTests(unittest.TestCase):
    def test_waiting_reaches_the_user(self):
        """Guards everything else: if this stopped being waiting, the rest of
        this file would pass against a orchestrator that reports nothing at all."""
        entry = classify("waiting")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "waiting")

    def test_idle_is_surfaced_but_not_as_a_question(self):
        """The defect, stated directly: a concluded task is not a question.

        An earlier version of this case asserted that idle must not reach the
        attention feed at all, and routed it to the quiet "updated" bucket. That
        was half the answer and it was superseded: Pedro's rule is to highlight
        when a person is genuinely needed **or when the action has ended**, so a
        finished agent is surfaced deliberately -- just labelled `done` rather
        than presented as asking something.

        What survives from the original is the part that was actually wrong:
        `reason: "asks"` on a session whose status said it had finished.
        """
        entry = classify("idle")
        self.assertIsNotNone(entry, "a finished agent stopped being surfaced")
        self.assertEqual(entry["reason"], "done")
        self.assertFalse(
            entry.get("question"),
            "a finished agent is still being presented as having asked something",
        )

    def test_idle_does_not_claim_to_be_asking_anything(self):
        """`reason: "asks"` on an idle session was a fabricated question.

        It came from the status field, not from reading the transcript, so the
        orchestrator asserted an ask it had no evidence for.
        """
        entry = classify("idle")
        self.assertNotEqual(entry.get("reason"), "asks")

    def test_busy_is_working_not_waiting(self):
        entry = classify("busy")
        if entry is not None:
            self.assertNotEqual(entry["status"], "waiting")

    def test_an_unknown_status_is_treated_as_blocked(self):
        """Fail towards the human.

        A value this code has never seen might mean stuck. Over-reporting costs
        a dismissal; under-reporting leaves an agent waiting with nobody told,
        so the allowlist names what is safe rather than what is not.
        """
        entry = classify("some-future-state")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "waiting")

    def test_a_read_mark_retires_an_idle_session(self):
        """A finished row is retired by being seen; a question is not.

        This is what keeps "the action has ended" from becoming a permanent
        badge on every conversation that ever completed. The claim is narrower
        than "finished": *finished since you last looked*. A question is
        different -- glancing at one does not answer it -- so that still needs
        answering or dismissing.
        """
        marks = {("chat", CHAT_ID): {"read_at": "2026-09-01T00:00:00Z"}}
        self.assertIsNone(classify("idle", marks=marks))

    def test_a_read_mark_does_not_retire_a_blocked_session(self):
        """Reading a question does not answer it."""
        marks = {("chat", CHAT_ID): {"read_at": "2026-09-01T00:00:00Z"}}
        entry = classify("waiting", marks=marks)
        self.assertIsNotNone(entry, "a blocked agent was retired by being read")
        self.assertEqual(entry["status"], "waiting")


class AllowlistShapeTests(unittest.TestCase):
    def test_the_not_blocked_set_holds_exactly_busy_and_idle(self):
        """Named as a set so adding a state is a decision, not a side effect.

        If the CLI grows a fourth value, the choice of which side it falls on
        should be made here, deliberately, with the reason written down.
        """
        self.assertEqual(classification._CLI_STATUS_NOT_BLOCKED, frozenset({"busy", "idle"}))

    def test_waiting_is_not_in_it(self):
        self.assertNotIn("waiting", classification._CLI_STATUS_NOT_BLOCKED)

    def test_the_check_is_membership_rather_than_a_comparison_to_busy(self):
        """`status != "busy"` is the bug; it must not come back.

        Asserted on the source because the behavioural cases above can all be
        satisfied by a comparison that happens to enumerate today's values --
        and the next value added would then silently rejoin the two.

        Comments are stripped first. The note recording *why* this changed
        quotes the old expression, so a scan over the raw text finds the bug
        inside its own postmortem -- which it did on the first run here. The
        obvious repair, deleting the explanation, loses the reason; the right one
        is to look only at code. `tests/test_qa_timer_handles.py` strips
        comments for exactly this.
        """
        # classification.py as well: the attention cluster, including
        # _CLI_STATUS_NOT_BLOCKED, moved there in 0.10.0.
        code = _code_only("\n".join(
            Path(mod.__file__).read_text(encoding="utf-8")
            for mod in (app, classification)))
        self.assertNotIn('cli_status != "busy"', code)
        self.assertIn("_CLI_STATUS_NOT_BLOCKED", code)


if __name__ == "__main__":
    unittest.main()
