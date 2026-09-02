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

import shared
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


class GatewayPayloadShapeQA(unittest.TestCase):
    """A question must survive whichever wire shape the backend used.

    Anthropic sends ``questions`` as a JSON array. An OpenAI-compatible gateway
    (LiteLLM, vLLM -- tool ids like ``chatcmpl-tool-*``) sends tool arguments as
    a *string* of JSON. Iterating that string walked it one character at a time,
    none of which is a dict, so every entry was skipped and the question vanished
    with nothing logged. Questions therefore showed in some conversations and not
    others purely by which backend served the turn.
    """

    def _ask_with(self, payload, qid="gw1"):
        return {"type": "tool_use", "id": qid, "name": "AskUserQuestion",
                "input": {"questions": payload}}

    def test_a_json_string_payload_is_parsed_into_a_question(self):
        payload = json.dumps([{
            "question": "Which feature next?", "header": "Next",
            "multiSelect": False,
            "options": [{"label": "Highlighting", "description": "Prism.js"},
                        {"label": "Retry", "description": "Re-submit"}],
        }])
        blocks = _blocks([_record("assistant", [self._ask_with(payload)])])
        self.assertEqual(blocks[0]["kind"], "question")
        entry = blocks[0]["questions"][0]
        self.assertEqual(entry["question"], "Which feature next?")
        self.assertEqual(entry["header"], "Next")
        self.assertEqual([o["label"] for o in entry["options"]],
                         ["Highlighting", "Retry"])

    def test_a_single_question_sent_unwrapped_still_renders(self):
        payload = json.dumps({
            "question": "Proceed?", "header": "Confirm", "multiSelect": False,
            "options": [{"label": "Yes", "description": ""}],
        })
        blocks = _blocks([_record("assistant", [self._ask_with(payload)])])
        self.assertEqual(blocks[0]["kind"], "question")
        self.assertEqual(blocks[0]["questions"][0]["question"], "Proceed?")

    def test_multi_select_survives_the_string_round_trip(self):
        payload = json.dumps([{
            "question": "Pick any", "header": "Many", "multiSelect": True,
            "options": [{"label": "A", "description": ""}],
        }])
        blocks = _blocks([_record("assistant", [self._ask_with(payload)])])
        self.assertTrue(blocks[0]["questions"][0]["multi_select"])

    def test_a_corrupt_but_question_shaped_payload_stays_visible(self):
        # Observed in a real transcript: a gateway emitted mismatched escaping,
        # so the options cannot be recovered. Dropping it hid the fact that the
        # agent had stopped and was waiting on an answer.
        payload = ('[{"question": "Which one?", "options": [{"label": '
                   '\\\"Broken\\\", \\\"description\\\": \\\"bad\\\"}]}]')
        blocks = _blocks([_record("assistant", [self._ask_with(payload)])])
        self.assertEqual(blocks[0]["kind"], "question")
        self.assertTrue(blocks[0].get("unreadable"))
        self.assertIn("could not be read", blocks[0]["questions"][0]["question"])

    def test_a_scrap_that_names_no_question_is_not_promoted(self):
        # The counterpart guarantee: garbage must not be invented into an ask.
        for scrap in ("nope", "", "   ", json.dumps("nope"), json.dumps(5)):
            blocks = _blocks([_record("assistant", [self._ask_with(scrap)])])
            kinds = [b["kind"] for b in blocks]
            self.assertNotIn("question", kinds, repr(scrap))

    def test_the_anthropic_list_shape_is_unchanged(self):
        # Regression guard: the fix must not alter the path that already worked.
        blocks = _blocks([_record("assistant", [_ask()])])
        self.assertEqual(blocks[0]["kind"], "question")
        self.assertEqual(blocks[0]["questions"][0]["question"], "Pick one")
        self.assertEqual(len(blocks[0]["questions"][0]["options"]), 2)
        self.assertNotIn("unreadable", blocks[0])

    def test_a_string_payload_is_answerable_and_pairs_by_id(self):
        # The whole point of surfacing it: the answer must still pair up, or the
        # question would stay listed as pending forever.
        payload = json.dumps([{
            "question": "Pick one", "header": "H", "multiSelect": False,
            "options": [{"label": "A", "description": ""}],
        }])
        blocks = _blocks([
            _record("assistant", [self._ask_with(payload, qid="gwPair")]),
            _record("user", [_result("gwPair")]),
        ])
        answer = next(b for b in blocks if b["kind"] == "answer")
        self.assertEqual(answer["id"], "gwPair")
        self.assertEqual(answer["status"], "answered")


class PendingQuestionShapeQA(unittest.TestCase):
    """pending_question() drives the sidebar badge and the answer controls.

    It shares _question_block with the render path, so a shape that path could
    not read also left the conversation looking idle while it was in fact
    blocked -- no badge, no controls, nothing to answer.
    """

    def _write(self, tmp, records):
        from pathlib import Path
        path = Path(tmp) / "t.jsonl"
        path.write_bytes(b"\n".join(json.dumps(r).encode() for r in records))
        return path

    def _pending_via_scan(self, records):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, records)
            return transcripts._scan_questions_sync(path)

    def test_an_unanswered_string_payload_is_reported_pending(self):
        payload = json.dumps([{
            "question": "Still waiting?", "header": "H", "multiSelect": False,
            "options": [{"label": "Yes", "description": ""}],
        }])
        ask = {"type": "tool_use", "id": "p1", "name": "AskUserQuestion",
               "input": {"questions": payload}}
        found = self._pending_via_scan([_record("assistant", [ask])])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["questions"][0]["question"], "Still waiting?")

    def test_an_answered_string_payload_is_not_reported_pending(self):
        payload = json.dumps([{
            "question": "Done?", "header": "H", "multiSelect": False,
            "options": [{"label": "Yes", "description": ""}],
        }])
        ask = {"type": "tool_use", "id": "p2", "name": "AskUserQuestion",
               "input": {"questions": payload}}
        found = self._pending_via_scan([
            _record("assistant", [ask]),
            _record("user", [_result("p2")]),
        ])
        self.assertEqual(found, [])


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

    def test_results_for_other_tools_never_become_answers(self):
        """Ordinary tool output must not be mistaken for a question's answer.

        This used to assert that other tools' results were dropped entirely.
        1cfc978 ("Show what a tool call actually ran, and what came back")
        deliberately started rendering them as `result` blocks, so that premise
        is gone -- but the part that matters is not: answer blocks are paired
        with an AskUserQuestion by id, and a Read result arriving in the next
        record must not pair with anything or the pending-question state would
        resolve itself on unrelated tool traffic.
        """
        blocks = _blocks([
            _record("assistant", [{"type": "tool_use", "id": "t9", "name": "Read",
                                   "input": {"file_path": "/a"}}]),
            _record("user", [{"type": "tool_result", "tool_use_id": "t9",
                              "content": "x" * 5000}]),
        ])
        self.assertFalse([b for b in blocks if b["kind"] == "answer"])
        # It is shown, but capped: replayed tool output dwarfs the conversation.
        result = next(b for b in blocks if b["kind"] == "result")
        self.assertTrue(result["truncated"])
        self.assertLess(len(result["text"]), 5000)

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


# ── The conversation on the site, not just the transcript viewer ──────────────


class ConversationSyncQA(unittest.TestCase):
    """A synced question must reach the conversation's own messages.

    _turn_to_message reads block["text"], and a question block has no such key
    -- its content is the question and its options -- so questions were dropped
    from the conversation even after the transcript viewer learned to render
    them. A question the user has to answer is the last thing that should go
    missing on the way in.
    """

    def _msg(self, blocks, role="assistant"):
        return shared._turn_to_message({"role": role, "blocks": blocks})

    def _question(self, **over):
        entry = {
            "question": "Should I keep this session named cweb5?",
            "header": "Session name",
            "multi_select": False,
            "options": [
                {"label": "Yes, keep it", "description": "Keep the name cweb5"},
                {"label": "No, rename it", "description": "Pick a different name"},
            ],
        }
        entry.update(over)
        return {"kind": "question", "id": "q1", "questions": [entry]}

    def test_a_question_is_no_longer_dropped(self):
        result = self._msg([self._question()])
        self.assertIsNotNone(result)
        self.assertIn("Should I keep this session named cweb5?", result[1])

    def test_every_option_and_description_reaches_the_message(self):
        _role, body = self._msg([self._question()])
        self.assertIn("Yes, keep it — Keep the name cweb5", body)
        self.assertIn("No, rename it — Pick a different name", body)

    def test_the_header_is_shown(self):
        self.assertIn("Question — Session name", self._msg([self._question()])[1])

    def test_a_question_says_where_to_answer(self):
        # Otherwise it reads as a rhetorical question in the log.
        self.assertIn("answer this in the terminal", self._msg([self._question()])[1])

    def test_the_note_does_not_claim_the_question_is_still_waiting(self):
        # The body is fixed at import time and cannot be revised when the answer
        # lands in a later sync, so it must not assert a state that will age.
        body = self._msg([self._question()])[1]
        self.assertNotIn("waiting", body.lower())

    def test_multi_select_is_stated(self):
        self.assertIn("choose one or more",
                      self._msg([self._question(multi_select=True)])[1])

    def test_an_option_without_a_description_still_appears(self):
        body = self._msg([self._question(
            options=[{"label": "Just do it", "description": ""}])])[1]
        self.assertIn("Just do it", body)

    def test_text_and_question_both_survive_in_one_turn(self):
        _role, body = self._msg([
            {"kind": "text", "text": "One thing before I start."},
            self._question(),
        ])
        self.assertIn("One thing before I start.", body)
        self.assertIn("Session name", body)

    def test_an_answer_is_recorded_in_the_conversation(self):
        _role, body = self._msg([{
            "kind": "answer", "id": "q1", "status": "answered",
            "text": 'Your questions have been answered: "Session name"="Yes, keep it"',
        }], role="user")
        self.assertIn("Answered in the terminal", body)
        self.assertIn("Yes, keep it", body)

    def test_a_declined_answer_says_declined(self):
        _role, body = self._msg([{"kind": "answer", "id": "q1",
                                  "status": "declined", "text": "was rejected"}])
        self.assertIn("Declined in the terminal", body)

    def test_a_malformed_question_does_not_produce_an_empty_message(self):
        for bad in ({"kind": "question", "id": "q", "questions": []},
                    {"kind": "question", "id": "q", "questions": ["nope"]},
                    {"kind": "question", "id": "q"}):
            self.assertIsNone(self._msg([bad]), repr(bad))

    def test_subagent_turns_are_still_dropped(self):
        self.assertIsNone(shared._turn_to_message(
            {"role": "assistant", "sidechain": True, "blocks": [self._question()]}))

    def test_a_real_transcript_question_reaches_the_conversation(self):
        # End to end over the reader and the converter, from raw records.
        ask = {"type": "tool_use", "id": "qz", "name": "AskUserQuestion",
               "input": {"questions": [{
                   "question": "Proceed?", "header": "Confirm",
                   "options": [{"label": "Yes", "description": "go ahead"}]}]}}
        raw = json.dumps({"type": "assistant",
                          "message": {"role": "assistant", "content": [ask]}}).encode()
        turns = transcripts._turns_from_bytes(raw)
        rows = [r for r in (shared._turn_to_message(t) for t in turns) if r]
        self.assertEqual(len(rows), 1)
        self.assertIn("Proceed?", rows[0][1])
        self.assertIn("Yes — go ahead", rows[0][1])
