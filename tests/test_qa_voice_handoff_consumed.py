"""QA: one handoff consumes the voice conversation, and the UI says so.

From the code review of 9deea3d. Making the handoff buttons survive being
used was correct, but it left them live against a conversation that no longer
exists: routes/voice.py's voice_handoff calls db.chat_delete on *every* path
it has -- success, missing credentials, and the exception handler -- so a
single POST consumes the chat whatever the outcome. Before the buttons
survived, a second click was impossible; afterwards it POSTed against a
deleted chat and got a 400.

Three properties are pinned here:

* the buttons are disabled once a handoff has run, whichever way it went;
* a new voice session re-arms them, or the first fix becomes a permanent
  version of the bug it replaced;
* the failure text says the conversation was discarded, because it was.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDOFF = ROOT / "web" / "assets" / "voice-handoff.js"
VOICE_PY = ROOT / "routes" / "voice.py"


class VoiceHandoffConsumedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = HANDOFF.read_text()
        cls.py = VOICE_PY.read_text()

    def _summarize(self) -> str:
        body = self.js.split("async function voiceHandoffSummarize()", 1)[1]
        return body.split("\n}", 1)[0]

    def test_the_backend_really_does_delete_on_every_path(self):
        """The premise the rest of this file rests on. If voice_handoff stops
        deleting the chat, disabling the buttons becomes wrong and these tests
        should be revisited rather than quietly kept passing."""
        self.assertGreaterEqual(
            self.py.count("await db.chat_delete(chat_id, owner)"), 3,
            "voice_handoff is expected to delete the chat on the success, "
            "fallback and exception paths",
        )

    def test_a_handoff_disables_the_buttons(self):
        self.assertIn("markHandoffConsumed()", self._summarize())

    def test_consuming_disables_all_three(self):
        body = self.js.split("function markHandoffConsumed()", 1)[1].split("\n}", 1)[0]
        for name in ("voiceAgreeBtn", "voiceSummarizeBtn", "voiceRejectBtn"):
            self.assertIn(f"{name}.disabled = true", body)

    def test_a_new_session_re_arms_them(self):
        body = self.js.split("export function resetVoiceHandoffState()", 1)[1]
        body = body.split("\n}", 1)[0]
        for name in ("voiceAgreeBtn", "voiceSummarizeBtn", "voiceRejectBtn"):
            self.assertIn(f"{name}.disabled = false", body)

    def test_the_failure_message_admits_the_conversation_is_gone(self):
        self.assertIn("discarded", self._summarize())

    def test_the_summary_is_parsed_as_json(self):
        """handle_voice_handoff returns JSONResponse({"ok":..., "summary":...});
        reading it as text rendered the raw JSON into the panel."""
        summarize = self._summarize()
        self.assertIn("response.json()", summarize)
        self.assertNotIn("response.text()", summarize)
