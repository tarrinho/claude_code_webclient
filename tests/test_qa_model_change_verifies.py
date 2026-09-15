"""QA: changing a conversation's model asks what actually answers.

Configuration cannot settle which model serves a conversation, and on
2026-09-15 it demonstrably did not. "local : 13 : models comparison" was set
to vllm/Qwen3.6-35B-A3B-NVFP4; the usage row for the turn recorded exactly
that, and the reply in the chat said "Claude Opus 5". Both were true. The
conversation is linked to a live CLI session, so the headless turn and that
terminal share one transcript: the turn ran on Qwen, the terminal answered
from its own context on Opus, and transcript sync imported the terminal's
reply into the web chat. Nothing in the UI could show the disagreement.

So the model picker now asks, as its first action after a change, and the
answer lands in the conversation where it can be read against the selection.
It goes through the ordinary send path deliberately -- whatever answers this
is whatever would answer real work, which is the only thing that settles it.

Asserted from source, this repo's convention for app.js: there is no JS
runner for it (the quickjs harness in tests/js/ serves supervisor-map.js
only). See test_qa_backend_status_on_first_paint.py, whose comment-stripping
these mirror. What that buys and what it does not: these pin the wiring --
that the handler awaits the store, sends only on success, and sends only on
a real change -- and they cannot prove the browser dispatches it. The
behaviour they guard is the ordering, which is where the mistakes live.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "web" / "assets" / "app.js"


def _strip_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


def _model_change_handler() -> str:
    """The body of the conversationModel change listener."""
    body = _strip_comments(APP_JS.read_text(encoding="utf-8"))
    match = re.search(
        r"byId\('conversationModel'\)\?\.addEventListener\('change',(.*?)\n  \}\);",
        body,
        re.DOTALL,
    )
    assert match, "the conversationModel change handler moved or was renamed"
    return match.group(1)


class ModelChangeAsksWhatAnsweredTests(unittest.TestCase):

    def test_the_handler_sends_a_verification_prompt(self):
        self.assertIn("MODEL_CHECK_PROMPT", _model_change_handler(),
                      "changing the model no longer asks what answers")

    def test_the_prompt_asks_for_the_model_id(self):
        body = _strip_comments(APP_JS.read_text(encoding="utf-8"))
        match = re.search(r"const MODEL_CHECK_PROMPT = '([^']+)'", body)
        self.assertIsNotNone(match, "MODEL_CHECK_PROMPT is gone")
        prompt = match.group(1).lower()
        self.assertIn("model", prompt)
        # Short and unambiguous: it is answered by every backend and cannot be
        # mistaken for a request to do work.
        self.assertLess(len(prompt), 120)

    def test_it_goes_through_the_ordinary_send_path(self):
        """Not a side channel. A bespoke request would report what the server
        believes rather than what answers, which is the thing in doubt."""
        self.assertIn("conversationController?.send(", _model_change_handler())

    def test_it_only_sends_once_the_change_is_stored(self):
        """Asking after a failed PATCH checks a model the conversation is not
        on, and the failure path puts the picker back to the stored value."""
        handler = _model_change_handler()
        self.assertRegex(
            handler,
            r"const ok = await setConversationRouting",
            "the handler no longer waits for the store to succeed",
        )
        self.assertRegex(
            handler,
            r"if \(ok &&[^)]*\)\s*conversationController\?\.send",
            "the send is no longer guarded by the store succeeding",
        )

    def test_it_only_sends_on_a_real_change(self):
        """Re-selecting the same entry fires a change event in some browsers;
        a turn per non-change is spend for nothing."""
        handler = _model_change_handler()
        self.assertIn("previous", handler)
        self.assertRegex(handler, r"model !== previous")

    def test_the_previous_model_is_read_before_the_store_overwrites_it(self):
        """setConversationRouting assigns the new value onto state.currentChat,
        so reading `previous` afterwards would always equal `model` and the
        real-change guard would never fire."""
        handler = _model_change_handler()
        previous_at = handler.index("const previous")
        store_at = handler.index("setConversationRouting")
        self.assertLess(
            previous_at, store_at,
            "previous is read after the store, so it can never differ",
        )


class SetConversationRoutingReportsOutcomeTests(unittest.TestCase):
    """The handler's guard needs a truthful answer from the store."""

    def test_it_returns_true_on_success_and_false_on_failure(self):
        body = _strip_comments(APP_JS.read_text(encoding="utf-8"))
        match = re.search(
            r"async function setConversationRouting\(.*?\n\}", body, re.DOTALL)
        self.assertIsNotNone(match, "setConversationRouting moved or was renamed")
        fn = match.group(0)
        self.assertIn("return true;", fn)
        self.assertIn("return false;", fn)


if __name__ == "__main__":
    unittest.main()
