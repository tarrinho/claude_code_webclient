"""tests/test_qa_voice_conversation_ui.py — markup/wiring for the voice
conversation feature. Source-inspection style, matching
tests/test_qa_auto_answer_ui.py exactly -- no browser needed for markup
assertions (see tests/test_qa_voice_conversation_browser.py, Task 12, for
the live-browser counterpart).
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
ASSETS = WEB / "assets"


class VoiceConversationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text()

    def test_voice_settings_rows_exist_in_app_tab(self):
        panel = self.html.split('id="panelApp"')[1].split('</div>\n    </div>')[0]
        self.assertIn('id="voiceModelSelect"', panel)
        self.assertIn('id="voiceSpeechRate"', panel)

    def test_voice_settings_js_is_loaded(self):
        self.assertIn('voice-settings.js', self.html)

    def test_voice_settings_js_file_stays_under_300_lines(self):
        content = (ASSETS / "voice-settings.js").read_text()
        self.assertLess(len(content.splitlines()), 300)

    def test_mic_new_chat_button_exists_beside_new_conversation(self):
        occurrences = self.html.count('class="btn-new new-chat-btn"')
        voice_occurrences = self.html.count('voice-new-chat-btn')
        self.assertEqual(occurrences, 2, "expected mobile + desktop new-chat buttons")
        self.assertEqual(voice_occurrences, 2, "expected mobile + desktop voice buttons")

    def test_composer_row_has_voice_icons(self):
        composer = self.html.split('id="composerArea"')[1].split('</section>')[0]
        for control in ('voiceMicBtn', 'voiceLiveBtn', 'voiceStopBtn'):
            self.assertIn(f'id="{control}"', composer)

    def test_voice_conversation_js_files_stay_under_300_lines(self):
        """voice-conversation.js was split 2026-09-10 into three files along
        its own natural seams (engine/tooltip/handoff) once it crossed 300
        lines -- each successor stays under the same cap."""
        for name in ("voice-engine.js", "voice-tooltip.js", "voice-handoff.js"):
            content = (ASSETS / name).read_text()
            self.assertLess(len(content.splitlines()), 300, name)

    def test_voice_conversation_js_is_loaded(self):
        for name in ("voice-engine.js", "voice-tooltip.js", "voice-handoff.js"):
            self.assertIn(name, self.html)

    def test_conversation_js_calls_voice_hooks(self):
        conv = (ASSETS / "conversation.js").read_text()
        self.assertIn('window.voiceConversation?.onReplyChunk', conv)
        self.assertIn('window.voiceConversation?.onReplyDone', conv)
        self.assertIn('window.voiceConversation?.onReplyError', conv)


class ThinkingToneTests(unittest.TestCase):
    """Spec §6: a pulse while the model is thinking, silent once it speaks.

    The audible behaviour cannot be asserted without a speaker, so what is
    pinned here is the wiring and the two properties the spec makes
    load-bearing: the tone is ADDITIONAL to the visual indicator, and it has
    no in-app mute.
    """

    @classmethod
    def setUpClass(cls):
        cls.tone = (ASSETS / "voice-tone.js").read_text()
        cls.engine = (ASSETS / "voice-engine.js").read_text()

    def test_the_tone_starts_on_thinking_and_stops_on_anything_else(self):
        """Driven from setVoiceStatus, the one place status changes, so the
        tone cannot outlive the state that started it."""
        self.assertIn("if (next === 'thinking') startThinkingTone();", self.engine)
        self.assertIn("else stopThinkingTone();", self.engine)

    def test_the_visual_indicator_is_not_replaced_by_the_tone(self):
        """Spec §6 makes the tone's absence the fault signal, which only works
        if the tone is additional -- a user watching the screen must still see
        Thinking… and a user listening must still hear it."""
        self.assertIn("renderThinkingIndicator(next === 'thinking');", self.engine)

    def test_there_is_no_in_app_mute(self):
        """A control that silences a fault signal defeats the reason for
        having one. The system volume is the mute."""
        for word in ("muted", "setMute", "toggleTone", "toneEnabled"):
            self.assertNotIn(word, self.tone, word)

    def test_it_synthesises_rather_than_loading_an_audio_file(self):
        """A file that fails to load is silence, and silence is the fault
        signal -- so the failure mode would be indistinguishable from the
        thing it reports."""
        self.assertIn("createOscillator", self.tone)
        for asset in (".mp3", ".wav", ".ogg", "new Audio("):
            self.assertNotIn(asset, self.tone, asset)

    def test_the_pulse_is_about_one_per_second(self):
        """Requirement 9's rate, as a number rather than a promise."""
        import re
        interval = int(re.search(r"PULSE_INTERVAL_MS = (\d+)", self.tone).group(1))
        self.assertGreaterEqual(interval, 700)
        self.assertLessEqual(interval, 1300)

    def test_starting_twice_does_not_double_the_beat(self):
        """setVoiceStatus can fire repeatedly for the same state."""
        self.assertIn("if (timer) return;", self.tone)

    def test_voice_engine_is_still_under_its_line_cap(self):
        """It is at 299 of 300 after this change. Recorded explicitly so the
        next addition extracts rather than discovering the cap by failing."""
        self.assertLess(len(self.engine.splitlines()), 300)


class VoiceStartupOrderingTests(unittest.TestCase):
    """The panel must become usable without waiting for its summary.

    Reported from use on 2026-09-21: a voice session "stopped replying and
    also listening". The cause was `await runVoiceContext(...)` sitting before
    the two calls that enable the microphone, so the session was deaf for as
    long as the summary took -- tens of seconds for a CLI turn over a window of
    up to 40,000 characters. The server log showed no message requests at all,
    only polling, because the client never got as far as sending.

    Source inspection rather than a browser, matching this file's style. What
    is pinned is an ordering, which is the whole of the defect: the feature
    worked, the summary arrived, and the microphone was simply unreachable
    until it did.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = (ASSETS / "voice-tooltip.js").read_text()

    def test_the_context_stream_is_not_awaited(self):
        """`await runVoiceContext` is the regression, precisely."""
        self.assertNotIn("await runVoiceContext", self.src)

    def test_the_summary_is_read_per_turn_so_not_gating_is_safe(self):
        """What makes it safe not to wait: `stream_voice_turn` re-reads the
        summary off the chat row on every turn, rather than being handed it
        once at open. So an utterance made before the summary lands costs that
        one turn its overview and nothing after it. If this ever became a
        value captured at session start, not gating would silently mean the
        whole session ran without context."""
        voice_py = (ROOT / "routes" / "voice.py").read_text()
        turn = voice_py[voice_py.index("async def stream_voice_turn"):]
        turn = turn[:turn.index("async def ", 10)]
        self.assertIn('chat.get("voice_context")', turn)

    def test_a_failure_in_the_stream_cannot_reject_into_the_open_path(self):
        """Unawaited promises that reject become unhandled rejections, and an
        unhandled rejection here would surface as a broken session rather than
        as a session that merely has no summary."""
        tail = self.src[self.src.index("runVoiceContext("):]
        self.assertIn(".catch(", tail.split("\n")[0] + tail.split("\n")[1])
