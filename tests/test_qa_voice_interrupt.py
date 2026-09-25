"""QA: the voice interrupt words, and the one echo they must not answer.

Design: docs/superpowers/specs/2026-09-21-voice-session-context-design.md §7

Two layers, because the feature has two halves that fail differently.

`InterruptMatchingTests` drives the real `voice-interrupt.js` in a blank page
and asserts the matching rules directly: which words trigger, which
substrings must not, and the three-second self-echo window. No server, no
recogniser -- the module is pure, which is why it was split out of the engine.

`InterruptWiringTests` reads `voice-engine.js` for the wiring the first layer
cannot see: that the recogniser now runs while *thinking* and not only while
speaking, and that a trigger in that state cancels the reply rather than
merely muting speech that has not begun. Source reading is a weak test and is
used here only where the alternative is a live speech recogniser, which this
suite cannot drive.

The echo rule is the part most worth testing. Without it the model saying
"wait a moment" cuts itself off, on laptop speakers, every time -- and the
failure looks like a bug in the model rather than in the microphone.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.test_frontend_browser import DRIVER_OK, DRIVER_WHY

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"


def _harness() -> str:
    """The real voice-interrupt.js in a page, with its clock controllable.

    `Date.now` is replaced rather than the test sleeping for three real
    seconds per case: the window is a duration, and asserting a duration by
    waiting it out makes a suite slow enough that somebody eventually deletes
    the test.
    """
    source = (ASSETS / "voice-interrupt.js").read_text()
    source = source.replace("export const", "const").replace("export function", "function")
    return (
        "<!DOCTYPE html><html><body><script type='module'>\n"
        "window.__now = 1000000;\n"
        "Date.now = () => window.__now;\n"
        + source
        + "\nwindow.__interrupt = {"
        "matchInterrupt, isSelfEcho, rememberSpoken, clearSpokenHistory,"
        "INTERRUPT_STATES, ECHO_WINDOW_MS};"
        "\n</script></body></html>"
    )


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
class InterruptMatchingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

    def setUp(self):
        self.page = self._browser.new_page()
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.set_content(_harness())
        self.page.wait_for_timeout(100)
        self.addCleanup(self.page.close)

    def _match(self, transcript):
        return self.page.evaluate(
            "t => window.__interrupt.matchInterrupt(t)", transcript)

    def _spoke(self, text):
        self.page.evaluate("t => window.__interrupt.rememberSpoken(t)", text)

    def _advance(self, ms):
        self.page.evaluate("ms => { window.__now += ms; }", ms)

    def test_each_of_the_three_words_triggers(self):
        for word in ("stop", "wait", "pause"):
            self.assertEqual(self._match(f"okay {word} please"), word, word)

    def test_matching_ignores_case(self):
        self.assertEqual(self._match("STOP"), "stop")

    def test_a_substring_does_not_trigger(self):
        """"Stopping" and "waitress" are ordinary words in ordinary replies."""
        for phrase in ("stopping there", "the waitress arrived", "paused it"):
            self.assertIsNone(self._match(phrase), phrase)

    def test_an_unrelated_phrase_does_not_trigger(self):
        self.assertIsNone(self._match("tell me about the database"))

    def test_the_model_s_own_word_is_suppressed(self):
        """The systematic false trigger, and the only one that is filtered."""
        self._spoke("Wait a moment while I look that up.")
        self.assertIsNone(self._match("wait"))

    def test_suppression_expires_with_the_window(self):
        self._spoke("Wait a moment while I look that up.")
        self._advance(3100)
        self.assertEqual(self._match("wait"), "wait",
                         "an old utterance still suppressed a live interrupt")

    def test_suppression_holds_inside_the_window(self):
        """The boundary, asserted from the inside as well as the outside."""
        self._spoke("Wait a moment.")
        self._advance(2900)
        self.assertIsNone(self._match("wait"))

    def test_a_different_word_is_not_suppressed(self):
        """Echo suppression is per word, not a blanket mute.

        The model saying "wait" must not swallow the user saying "stop" --
        that would turn one narrow filter into a three-second deaf spot.
        """
        self._spoke("Wait a moment while I look that up.")
        self.assertEqual(self._match("stop"), "stop")

    def test_the_model_saying_a_longer_word_does_not_suppress(self):
        """"Stopped" is not "stop"; whole-word matching applies both ways."""
        self._spoke("I stopped at the second file.")
        self.assertEqual(self._match("stop"), "stop")

    def test_clearing_the_history_reopens_everything(self):
        self._spoke("Wait a moment.")
        self.page.evaluate("() => window.__interrupt.clearSpokenHistory()")
        self.assertEqual(self._match("wait"), "wait")

    def test_the_interruptible_states_are_thinking_and_speaking(self):
        """Not `listening`: "stop" said to an open mic is a word, not a command."""
        self.assertEqual(
            self.page.evaluate("() => window.__interrupt.INTERRUPT_STATES"),
            ["thinking", "speaking"])
        self.assertEqual(self.errors, [])


class InterruptWiringTests(unittest.TestCase):
    """What the engine does with a match, read from source.

    A weak kind of test, used only because the alternative is driving a live
    SpeechRecognition instance, which needs a microphone and a human voice.
    Each assertion below names the specific regression it would catch.
    """

    @classmethod
    def setUpClass(cls):
        cls.engine = (ASSETS / "voice-engine.js").read_text()
        cls.conversation = (ASSETS / "conversation.js").read_text()
        # The speech queue moved out of the engine when this work pushed that
        # file past its 300-line cap; the utterance hook moved with it.
        cls.speech = (ASSETS / "voice-speech.js").read_text()

    def test_the_engine_no_longer_matches_stop_on_its_own(self):
        """The old `/\\bstop\\b/i` must be gone, not merely supplemented."""
        self.assertNotIn(r"/\bstop\b/i", self.engine,
                         "the single-word matcher is still in the engine")

    def test_the_barge_in_recogniser_uses_the_shared_matcher(self):
        self.assertIn("matchInterrupt(event.results[i][0].transcript)", self.engine)

    def test_the_recogniser_lifecycle_covers_both_states(self):
        """The defect this half exists to fix.

        Under the old lifecycle the recogniser started on entering `speaking`,
        so "stop" said during the pause between asking and the first spoken
        word reached nothing at all.
        """
        self.assertNotIn("if (next === 'speaking' && previous !== 'speaking') "
                         "startBargeInListening();", self.engine)
        self.assertIn("INTERRUPT_STATES.includes(previous)", self.engine)
        self.assertIn("INTERRUPT_STATES.includes(next)", self.engine)

    def test_an_interrupt_while_thinking_cancels_the_reply(self):
        """Muting speech that has not started leaves the reply to arrive
        seconds later and start talking, which is the failure that makes
        people stop trusting the word."""
        handler = re.search(r"export function handleInterrupt\(\)\s*\{.*?\n\}",
                            self.engine, re.S)
        self.assertIsNotNone(handler, "handleInterrupt is gone")
        body = handler.group(0)
        self.assertIn("voiceStatus === 'thinking'", body)
        self.assertIn("__webConsoleCancel", body)

    def test_an_interrupt_does_not_end_the_conversation(self):
        """`performVoiceStop(true)` would close the session and offer the
        handoff panel -- "wait" must leave the conversation running."""
        handler = re.search(r"export function handleInterrupt\(\)\s*\{.*?\n\}",
                            self.engine, re.S)
        self.assertIn("performVoiceStop(false)", handler.group(0))

    def test_the_cancel_hook_exists_on_the_conversation_side(self):
        """handleInterrupt calls it through window; nothing else would fail
        if it were never defined -- the optional call would silently do
        nothing, which is the shape of defect §12 of the spec is about."""
        self.assertIn("window.__webConsoleCancel = ", self.conversation)

    def test_spoken_sentences_are_recorded_when_they_become_audible(self):
        """From `onstart`, not from `speak()`: a queued sentence can sit for
        seconds, and the window has to start when it is heard."""
        self.assertIn("utterance.onstart = () => rememberSpoken(trimmed);", self.speech)
        self.assertNotIn("rememberSpoken", self.engine,
                         "the engine should reach the echo history only through "
                         "the speech queue that produces it")

    def test_ending_the_conversation_clears_the_spoken_history(self):
        self.assertIn("clearSpokenHistory()", self.engine)


if __name__ == "__main__":
    unittest.main()
