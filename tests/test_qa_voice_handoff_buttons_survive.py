"""QA: the voice handoff buttons survive being used.

Reported 2026-09-16. The conclusion panel holds a label and three buttons --
Agree & Apply, Summarize Only, Reject & Discard -- as its children. Every
handler wrote its output by clearing that panel's innerHTML first, which
destroyed the buttons along with the label. After one "Summarize Only" the
panel showed a summary and nothing to click, and only a page reload brought
the buttons back.

Rebuilding the markup would not have been enough either: voice-handoff.js
captures the three buttons once at module load and binds click listeners to
those elements, so re-created buttons would be listener-less and silently do
nothing -- a worse failure than visibly missing ones.

The fix is that output goes to its own element inside the panel and the panel
is never cleared. These tests pin both halves.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class VoiceHandoffButtonsSurviveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text()
        cls.handoff = (WEB / "assets" / "voice-handoff.js").read_text()

    def test_the_panel_is_never_cleared(self):
        """The defect in one line: any innerHTML write to the panel takes the
        three buttons with it."""
        self.assertNotIn(
            "voiceTooltipConclusion.innerHTML", self.handoff,
            "clearing the conclusion panel destroys the handoff buttons "
            "inside it; write to #voiceConclusionOutput instead",
        )

    def test_there_is_a_separate_output_element(self):
        self.assertIn('id="voiceConclusionOutput"', self.html)

    def test_the_output_element_is_inside_the_panel(self):
        """Outside it, the output would show while the panel is hidden -- the
        bug fixed separately the same day, where the panel's label appeared
        mid-conversation."""
        panel = self.html.split('id="voiceTooltipConclusion"', 1)[1].split("</div>", 1)[0]
        self.assertIn('id="voiceConclusionOutput"', panel)

    def test_all_three_buttons_are_still_in_the_markup(self):
        for button_id in ("voiceAgreeBtn", "voiceSummarizeBtn", "voiceRejectBtn"):
            self.assertIn(f'id="{button_id}"', self.html)

    def test_a_new_session_clears_the_previous_output(self):
        """It used to happen for free when the panel was wiped wholesale, so
        removing the wipe removes the reset unless it is done explicitly."""
        reset = self.handoff.split("export function resetVoiceHandoffState()", 1)[1]
        reset = reset.split("\n}", 1)[0]
        self.assertIn("clearConclusionOutput()", reset)
