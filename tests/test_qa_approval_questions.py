"""QA: an approval prompt is a question, and has to show like one.

AskUserQuestion was the only tool the transcript renderer treated as a question.
Plan-mode approvals -- EnterPlanMode and ExitPlanMode -- also stop and wait for
a person, but they declare nothing: the CLI draws the prompt itself, so the tool
call carries an empty ``input``. They therefore fell through to the generic tool
branch and rendered as the bare word "ExitPlanMode": no ask, no options, and
nothing to tell the reader the session was blocked on them.

They are not an edge case. Across this machine's transcripts there are 60 of
them against 47 AskUserQuestion, so most of what was waiting on an answer never
appeared in the web chat -- which is exactly the report that prompted this.

Options stay empty on purpose. The real choices exist only on the live prompt,
and the question endpoint reads them off the terminal with visible_options();
answering is by index, so inventing labels here would put choices in front of
the user that the terminal never offered.
"""
from __future__ import annotations

import unittest

import transcripts


def call(name: str, block_id: str = "call_1") -> dict:
    """A plan-mode tool call, shaped as the CLI actually writes it."""
    return {"type": "tool_use", "id": block_id, "name": name, "input": {}}


def result(block_id: str, text: str) -> dict:
    return {"type": "tool_result", "tool_use_id": block_id, "content": text}


class RenderingTests(unittest.TestCase):
    def test_both_plan_tools_render_as_questions(self):
        for name in ("EnterPlanMode", "ExitPlanMode"):
            with self.subTest(tool=name):
                blocks = transcripts._blocks_from_content([call(name)])
                self.assertEqual([b["kind"] for b in blocks], ["question"])

    def test_the_ask_is_real_text_rather_than_the_tool_name(self):
        """The whole defect: "ExitPlanMode" is not a question anybody can answer."""
        block = transcripts._blocks_from_content([call("ExitPlanMode")])[0]
        ask = block["questions"][0]["question"]
        self.assertNotIn("ExitPlanMode", ask)
        self.assertGreater(len(ask), 20, ask)
        self.assertTrue(ask.endswith("?"), ask)

    def test_no_options_are_invented(self):
        """Answering is by index; a wrong label would select the wrong thing."""
        block = transcripts._blocks_from_content([call("EnterPlanMode")])[0]
        self.assertEqual(block["questions"][0]["options"], [])

    def test_the_result_renders_as_an_answer_not_as_tool_output(self):
        blocks = transcripts._blocks_from_content([
            call("ExitPlanMode"),
            result("call_1", "User has approved your plan. You can now start coding"),
        ])
        self.assertEqual([b["kind"] for b in blocks], ["question", "answer"])
        self.assertEqual(blocks[1]["status"], "answered")

    def test_a_rejection_reads_as_declined(self):
        blocks = transcripts._blocks_from_content([
            call("ExitPlanMode"),
            result("call_1", "The user doesn't want to proceed with this tool use. "
                             "The tool use was rejected"),
        ])
        self.assertEqual(blocks[1]["status"], "declined")

    def test_an_unrelated_tool_is_untouched(self):
        """The change must not turn every tool call into a question."""
        blocks = transcripts._blocks_from_content([
            {"type": "tool_use", "id": "b1", "name": "Bash",
             "input": {"command": "ls"}},
        ])
        self.assertEqual([b["kind"] for b in blocks], ["tool"])


class PendingTests(unittest.TestCase):
    """pending_question() is what raises the answerable bar in the web UI."""

    def _pending(self, records: list[dict], tmp) -> dict | None:
        path = tmp / "t.jsonl"
        import json
        path.write_text("".join(
            json.dumps({"message": {"content": [r]}}) + "\n" for r in records))
        original = transcripts.transcript_path
        transcripts.transcript_path = lambda _sid: path
        try:
            return transcripts.pending_question("sid")
        finally:
            transcripts.transcript_path = original

    def test_an_unanswered_approval_is_pending(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            pending = self._pending([call("ExitPlanMode")], Path(tmp))
        self.assertIsNotNone(pending, "the bar never appears, so nobody can answer")
        self.assertTrue(pending["approval"])

    def test_an_answered_approval_is_not_pending(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            pending = self._pending(
                [call("ExitPlanMode"), result("call_1", "approved")], Path(tmp))
        self.assertIsNone(pending)

    def test_an_approval_carries_no_needle(self):
        """The ask is ours, so it is not on screen and cannot be matched.

        With a needle, find_target() fails every time and the endpoint reports
        the prompt unreachable, blaming screen/tmux -- which would be false.
        """
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            pending = self._pending([call("EnterPlanMode")], Path(tmp))
        self.assertEqual(pending["needle"], "")

    def test_a_real_question_still_carries_its_needle(self):
        """The needle must not be dropped for questions that do have text."""
        import tempfile
        from pathlib import Path
        ask = {"type": "tool_use", "id": "q1", "name": "AskUserQuestion",
               "input": {"questions": [{"question": "Ship it?", "header": "Go",
                                        "options": [{"label": "Yes"}]}]}}
        with tempfile.TemporaryDirectory() as tmp:
            pending = self._pending([ask], Path(tmp))
        self.assertEqual(pending["needle"], "Ship it?")
        self.assertFalse(pending["approval"])


if __name__ == "__main__":
    unittest.main()
