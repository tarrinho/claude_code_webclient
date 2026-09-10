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
