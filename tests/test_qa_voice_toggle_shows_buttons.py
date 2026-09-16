"""QA: toggling voice mode shows the voice buttons without a page reload.

Reported 2026-09-16: clicking the voice-mode toggle turned the chat into a
voice chat, and the mic and live-conversation buttons stayed hidden until the
page was hard-refreshed.

updateVoiceButtonVisibility() in voice-engine.js is the only thing that sets
`hidden` on those two buttons, and it reads window.state.currentChat.voice_mode.
toggleChatVoiceMode() in app.js updated that field and then refreshed the
backend picker, the model display and the chat list -- but never re-ran the
visibility check, and never imported it. A reload was what re-evaluated
visibility from scratch, which is why refreshing "fixed" it.

Source-inspection style, matching test_qa_voice_conversation_ui.py: the bug is
a missing call, which is visible in the source and does not need a browser.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web" / "assets"


def _toggle_body(app_js: str) -> str:
    """The body of toggleChatVoiceMode, up to the next top-level function."""
    after = app_js.split("async function toggleChatVoiceMode()", 1)[1]
    return after.split("\nfunction ", 1)[0]


class VoiceToggleShowsButtonsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ASSETS / "app.js").read_text()
        cls.engine = (ASSETS / "voice-engine.js").read_text()

    def test_app_imports_the_visibility_helper(self):
        self.assertRegex(
            self.app,
            r"import\s*\{[^}]*updateVoiceButtonVisibility[^}]*\}\s*from\s*'\./voice-engine\.js",
            "app.js must import updateVoiceButtonVisibility; without it the "
            "toggle cannot show the voice buttons",
        )

    def test_toggling_voice_mode_refreshes_button_visibility(self):
        self.assertIn(
            "updateVoiceButtonVisibility()", _toggle_body(self.app),
            "toggleChatVoiceMode must re-run updateVoiceButtonVisibility, or "
            "the mic and live buttons stay hidden until a page reload",
        )

    def test_the_visibility_helper_is_what_unhides_both_buttons(self):
        """Pins the assumption the other two rest on. If visibility moves out
        of this function, they would keep passing while the bug returned."""
        body = self.engine.split("export function updateVoiceButtonVisibility()", 1)[1]
        body = body.split("\n}", 1)[0]
        self.assertIn("voiceMicBtn.hidden", body)
        self.assertIn("voiceLiveBtn.hidden", body)
        self.assertIn("voice_mode", body)

    def test_state_is_read_and_written_through_the_same_object(self):
        """updateVoiceButtonVisibility reads window.state; toggleChatVoiceMode
        mutates app.js's own `state`. They must be one object or the refreshed
        check would read a stale value."""
        self.assertIn("window.state = state;", self.app)

    def test_the_toggle_icon_is_updated_after_the_state_changes(self):
        """It used to be the first line of the function, so the title showed
        the state the chat had just left."""
        body = _toggle_body(self.app)
        self.assertLess(
            body.index("chat.voice_mode ="), body.index("updateVoiceToggleIcon()"),
            "updateVoiceToggleIcon must run after voice_mode is updated",
        )
