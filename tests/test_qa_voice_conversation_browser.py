"""Live-browser QA: the voice conversation composer icons appear for
voice-mode chats and stay hidden for regular ones. Extends
_BrowserFixture the same way every other live-browser QA test in this
project does (see tests/test_frontend_browser.py) -- no separate pytest-
playwright fixture convention introduced.
"""
from __future__ import annotations

import unittest

from tests.test_frontend_browser import _BrowserFixture, CHROMIUM, DRIVER_OK, DRIVER_WHY


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class VoiceConversationBrowserTests(_BrowserFixture):
    """Voice conversation UI: mic buttons hidden on regular chats, visible on
    voice-mode chats, stop button hidden until a turn starts.

    Uses the same create-chat flow as the other browser tests (seed
    via DB, open by data-chat-id selector). The sidebar poller surfaces
    the seeded rows on its first tick (~6s), which the 30s timeout covers.
    """

    DESKTOP = "#chatListDesktop"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        stamp = cls._now()
        cls.voice_chat_id = f"vc-{__import__('secrets').token_hex(4)}"
        cls.regular_chat_id = f"rc-{__import__('secrets').token_hex(4)}"
        con = __import__('sqlite3').connect(str(__import__('pathlib').Path(cls.tmp.name) / "wc.db"))
        con.execute(
            "INSERT INTO chats (id,title,description,work_dir,owner_id,"
            "created_at,updated_at,voice_mode,ai_machine_id,model) VALUES "
            "(?,?,?,?,NULL,?,?,1,'voice-machine-test','azure_ai/gpt-5.6-luna')",
            (cls.voice_chat_id, "Voice Test Chat", None, "/tmp", "admin", stamp, stamp),
        )
        con.execute(
            "INSERT INTO chats (id,title,description,work_dir,owner_id,"
            "created_at,updated_at,voice_mode) VALUES "
            "(?,?,?,?,NULL,?,0)",
            (cls.regular_chat_id, "Regular Test Chat", None, "/tmp", "admin", stamp, stamp),
        )
        con.commit()
        con.close()
        # Wait for sidebar poll to surface the rows before tests run.
        cls.page.wait_for_timeout(8000)

    @staticmethod
    def _now():
        import datetime
        return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_voice_icons_hidden_for_regular_chat(self):
        row = f'{self.DESKTOP} .chat-item[data-chat-id="{self.regular_chat_id}"] .chat-open'
        self.page.wait_for_selector(row, timeout=30_000)
        self.page.click(row)
        self.page.wait_for_selector('#composerArea', timeout=10_000)
        self.assertTrue(self.page.is_hidden('#voiceMicBtn'))
        self.assertTrue(self.page.is_hidden('#voiceLiveBtn'))
        self.assertEqual(self.errors, [])

    def test_voice_icons_visible_for_voice_chat(self):
        row = f'{self.DESKTOP} .chat-item[data-chat-id="{self.voice_chat_id}"] .chat-open'
        self.page.wait_for_selector(row, timeout=30_000)
        self.page.click(row)
        self.page.wait_for_selector('#composerArea', timeout=10_000)
        self.assertTrue(self.page.is_visible('#voiceMicBtn'))
        self.assertTrue(self.page.is_visible('#voiceLiveBtn'))
        self.assertTrue(self.page.is_hidden('#voiceStopBtn'))
        self.assertEqual(self.errors, [])
