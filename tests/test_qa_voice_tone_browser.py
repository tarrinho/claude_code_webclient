"""Live-browser QA: the thinking tone actually makes sound, and stops.

Design: docs/superpowers/specs/2026-09-21-voice-session-context-design.md §6

`test_qa_voice_conversation_ui.py` asserts the wiring by reading the source:
that setVoiceStatus starts and stops it, that there is no mute, that nothing
loads an audio file. None of that proves a single oscillator is ever created,
and an audible cue that is wired but silent is exactly as useless as one that
was never written -- more so, because the source reads as finished.

So this drives the real module in a real browser and counts oscillators. It
needs no server: the module is loaded into a blank page, which is why this
does not use `_BrowserFixture` like the other live-browser tests here.

Skipped, not failed, where no usable browser exists -- the same rule the rest
of the browser suite follows, so a host without chromium reports a skip rather
than a defect that is not there.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from tests.test_frontend_browser import DRIVER_OK, DRIVER_WHY

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"


def _harness() -> str:
    """The real voice-tone.js in a page, with createOscillator counted.

    The `export` keywords are stripped so the module can be inlined in one
    script tag; nothing else about it is altered, so what runs here is the
    shipped code rather than a copy that drifts.
    """
    source = (ASSETS / "voice-tone.js").read_text().replace("export function", "function")
    return (
        "<!DOCTYPE html><html><body><script type='module'>\n"
        + source
        + "\nwindow.__tone = {startThinkingTone, stopThinkingTone, thinkingToneRunning};"
        "\nwindow.__oscillators = 0;"
        "\nconst Ctor = window.AudioContext;"
        "\nconst real = Ctor.prototype.createOscillator;"
        "\nCtor.prototype.createOscillator = function () {"
        "\n  window.__oscillators += 1; return real.call(this); };"
        "\n</script></body></html>"
    )


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
class ThinkingToneBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        # Chromium refuses to start audio without a user gesture. A real voice
        # session always has one -- the mic or live button -- but a test page
        # has none, so the policy is relaxed here. That is the one thing this
        # test cannot vouch for, and it is called out rather than hidden.
        cls._browser = cls._pw.chromium.launch(
            args=["--autoplay-policy=no-user-gesture-required"])

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

    def setUp(self):
        self.page = self._browser.new_page()
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.set_content(_harness())
        self.page.wait_for_timeout(150)
        self.addCleanup(self.page.close)

    def _oscillators(self) -> int:
        return self.page.evaluate("window.__oscillators")

    def test_it_is_silent_until_started(self):
        self.assertFalse(self.page.evaluate("window.__tone.thinkingToneRunning()"))
        self.assertEqual(self._oscillators(), 0)

    def test_it_pulses_about_once_a_second_while_thinking(self):
        """Requirement 9's rate, measured rather than asserted about a
        constant: two seconds of thinking should produce two or three pulses,
        and the first is immediate so the wait is never silent."""
        self.page.evaluate("window.__tone.startThinkingTone()")
        self.page.wait_for_timeout(2100)
        self.assertTrue(self.page.evaluate("window.__tone.thinkingToneRunning()"))
        self.assertIn(self._oscillators(), (2, 3),
                      "expected roughly one pulse per second")

    def test_stopping_really_stops_it(self):
        """The failure that would matter most: a beep still pulsing under the
        model's reply is worse than no beep at all."""
        self.page.evaluate("window.__tone.startThinkingTone()")
        self.page.wait_for_timeout(1200)
        self.page.evaluate("window.__tone.stopThinkingTone()")
        settled = self._oscillators()
        self.page.wait_for_timeout(1500)
        self.assertEqual(self._oscillators(), settled,
                         "the tone kept firing after it was stopped")
        self.assertFalse(self.page.evaluate("window.__tone.thinkingToneRunning()"))

    def test_starting_twice_does_not_double_the_beat(self):
        """setVoiceStatus can fire repeatedly for the same state, and two
        intervals would beat at twice the rate with no way to stop one."""
        self.page.evaluate("window.__tone.startThinkingTone()")
        self.page.evaluate("window.__tone.startThinkingTone()")
        self.page.wait_for_timeout(2100)
        self.assertLessEqual(self._oscillators(), 3)

    def test_it_raises_nothing(self):
        self.page.evaluate("window.__tone.startThinkingTone()")
        self.page.wait_for_timeout(1200)
        self.page.evaluate("window.__tone.stopThinkingTone()")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
