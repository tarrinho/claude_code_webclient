"""QA: questions asked by a terminal session must reach the web page.

A question used to render as the bare string "AskUserQuestion" -- no question
text, no options, no indication that an answer was wanted -- because
_tool_summary falls back to the tool name when none of its summary keys match,
and nothing in AskUserQuestion's input matches them. So a question asked in the
terminal was invisible to anyone reading the conversation in the browser.

Answers live in a later record's tool_result, which is dropped for every other
tool because it is replayed tool output. A question's result is the answer, so
it is the one worth keeping, and it has to be paired by tool_use_id.
"""
from __future__ import annotations

import json
import unittest

import transcripts


def _record(role, content, rtype=None):
    return {"type": rtype or role, "message": {"role": role, "content": content}}


def _ask(qid="q1", question="Pick one", header="Choice", options=(("A", "first"), ("B", "second")),
         multi=False):
    return {
        "type": "tool_use", "id": qid, "name": "AskUserQuestion",
        "input": {"questions": [{
            "question": question, "header": header, "multiSelect": multi,
            "options": [{"label": l, "description": d} for l, d in options],
        }]},
    }


def _result(qid="q1", text="Your questions have been answered: \"Pick one\"=\"A\"", error=False):
    return {"type": "tool_result", "tool_use_id": qid, "content": text, "is_error": error}


def _blocks(records):
    raw = b"\n".join(json.dumps(r).encode() for r in records)
    return [b for turn in transcripts._turns_from_bytes(raw) for b in turn["blocks"]]


class QuestionExtractionQA(unittest.TestCase):
    def test_a_question_is_no_longer_a_bare_tool_name(self):
        blocks = _blocks([_record("assistant", [_ask()])])
        kinds = [b["kind"] for b in blocks]
        self.assertIn("question", kinds)
        self.assertNotIn("tool", kinds)

    def test_the_question_text_and_header_survive(self):
        blocks = _blocks([_record("assistant", [_ask(question="Which backend?",
                                                     header="Routing")])])
        entry = blocks[0]["questions"][0]
        self.assertEqual(entry["question"], "Which backend?")
        self.assertEqual(entry["header"], "Routing")

    def test_every_option_survives_with_its_description(self):
        blocks = _blocks([_record("assistant", [_ask(
            options=(("Anthropic", "official API"), ("AI Machine", "self hosted"),
                     ("Follow active", "")))])])
        options = blocks[0]["questions"][0]["options"]
        self.assertEqual([o["label"] for o in options],
                         ["Anthropic", "AI Machine", "Follow active"])
        self.assertEqual(options[0]["description"], "official API")

    def test_multi_select_is_reported(self):
        blocks = _blocks([_record("assistant", [_ask(multi=True)])])
        self.assertTrue(blocks[0]["questions"][0]["multi_select"])
        blocks = _blocks([_record("assistant", [_ask(multi=False)])])
        self.assertFalse(blocks[0]["questions"][0]["multi_select"])

    def test_several_questions_in_one_call_all_survive(self):
        ask = _ask()
        ask["input"]["questions"].append({
            "question": "And the model?", "header": "Model",
            "options": [{"label": "opus", "description": ""}],
        })
        blocks = _blocks([_record("assistant", [ask])])
        self.assertEqual(len(blocks[0]["questions"]), 2)

    def test_options_without_a_label_are_skipped(self):
        ask = _ask()
        ask["input"]["questions"][0]["options"].append({"description": "no label"})
        blocks = _blocks([_record("assistant", [ask])])
        self.assertEqual(len(blocks[0]["questions"][0]["options"]), 2)

    def test_malformed_input_falls_back_to_the_tool_line(self):
        for bad in ("not a dict", None, {"questions": "nope"}, {"questions": []}):
            ask = {"type": "tool_use", "id": "x", "name": "AskUserQuestion", "input": bad}
            kinds = [b["kind"] for b in _blocks([_record("assistant", [ask])])]
            self.assertIn("tool", kinds, repr(bad))
            self.assertNotIn("question", kinds, repr(bad))

    def test_other_tools_are_untouched(self):
        tool = {"type": "tool_use", "id": "t1", "name": "Bash",
                "input": {"command": "git status"}}
        blocks = _blocks([_record("assistant", [tool])])
        self.assertEqual(blocks[0]["kind"], "tool")
        self.assertIn("git status", blocks[0]["text"])


class AnswerPairingQA(unittest.TestCase):
    def test_an_answer_is_kept_and_paired_by_id(self):
        blocks = _blocks([
            _record("assistant", [_ask("qA")]),
            _record("user", [_result("qA")]),
        ])
        answer = next(b for b in blocks if b["kind"] == "answer")
        self.assertEqual(answer["id"], "qA")
        self.assertEqual(answer["status"], "answered")
        self.assertIn("Pick one", answer["text"])

    def test_a_declined_question_is_reported_as_declined(self):
        blocks = _blocks([
            _record("assistant", [_ask("qB")]),
            _record("user", [_result("qB", "The tool use was rejected by the user")]),
        ])
        self.assertEqual(
            next(b for b in blocks if b["kind"] == "answer")["status"], "declined")

    def test_an_error_result_is_declined(self):
        blocks = _blocks([
            _record("assistant", [_ask("qC")]),
            _record("user", [_result("qC", "boom", error=True)]),
        ])
        self.assertEqual(
            next(b for b in blocks if b["kind"] == "answer")["status"], "declined")

    def test_an_unanswered_question_produces_no_answer_block(self):
        # This is the pending case: the terminal is still waiting.
        blocks = _blocks([_record("assistant", [_ask("qD")])])
        self.assertFalse([b for b in blocks if b["kind"] == "answer"])

    def test_results_for_other_tools_are_still_dropped(self):
        # Tool output is replayed into the next record and dwarfs the
        # conversation; only a question's result earns its place.
        blocks = _blocks([
            _record("assistant", [{"type": "tool_use", "id": "t9", "name": "Read",
                                   "input": {"file_path": "/a"}}]),
            _record("user", [{"type": "tool_result", "tool_use_id": "t9",
                              "content": "x" * 5000}]),
        ])
        self.assertFalse([b for b in blocks if b["kind"] == "answer"])
        self.assertNotIn("xxxx", json.dumps(blocks))

    def test_answer_text_is_capped(self):
        blocks = _blocks([
            _record("assistant", [_ask("qE")]),
            _record("user", [_result("qE", "have been answered " + "y" * 4000)]),
        ])
        self.assertLessEqual(
            len(next(b for b in blocks if b["kind"] == "answer")["text"]), 600)

    def test_two_questions_pair_with_their_own_answers(self):
        blocks = _blocks([
            _record("assistant", [_ask("q1", question="First")]),
            _record("user", [_result("q1", "have been answered: First=A")]),
            _record("assistant", [_ask("q2", question="Second")]),
            _record("user", [_result("q2", "was rejected")]),
        ])
        answers = {b["id"]: b["status"] for b in blocks if b["kind"] == "answer"}
        self.assertEqual(answers, {"q1": "answered", "q2": "declined"})


if __name__ == "__main__":
    unittest.main()
