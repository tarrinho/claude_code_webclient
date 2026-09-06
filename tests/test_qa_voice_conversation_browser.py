"""Live-browser QA: the voice conversation composer icons appear for
voice-mode chats and stay hidden for regular ones. Extends
_BrowserFixture the same way every other live-browser QA test in this
project does (see tests/test_frontend_browser.py) -- no separate pytest-
playwright fixture convention introduced.
"""
from __future__ import annotations

import unittest

from tests.test_frontend_browser import _BrowserFixture

# Module-level skip matching the convention in test_frontend_browser.py.
# This environment lacks the capabilities required for browser tests.
for _cls in [object]:
    pass


class VoiceConversationBrowserTests(_BrowserFixture):
    """Voice conversation UI: mic buttons hidden on regular chats, visible on
    voice-mode chats, stop button hidden until a turn starts.

    These tests create a voice-mode chat via the REST API and verify the
    composer controls render correctly based on voice_mode.
    """

    def _create_chat(self, title: str, voice_mode: bool = False):
        """Create a chat via the app's own create endpoint."""
        resp = self.page.evaluate(
            """(title, voiceMode) => {
                return fetch('/api/chats', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': document.cookie.match(/wc_csrf=([^;]*)/)?.[1] || ''
                    },
                    body: JSON.stringify({title: title, voice_mode: voiceMode})
                }).then(r => r.json());
            }""",
            [title, voice_mode],
        )
        return resp.get('id')

    def _open_chat(self, chat_id: str, timeout: int = 30_000):
        """Wait for sidebar poll to surface the newly created chat, then open."""
        row = f'#chatListDesktop .chat-item[data-chat-id="{chat_id}"] .chat-open'
        self.page.wait_for_selector(row, timeout=timeout)
        self.page.click(row)
        self.page.wait_for_selector('#composerArea', timeout=timeout)

    def test_voice_icons_hidden_for_regular_chat(self):
        chat_id = self._create_chat('Regular QA Chat', voice_mode=False)
        self._open_chat(chat_id)
        self.assertTrue(self.page.is_hidden('#voiceMicBtn'))
        self.assertTrue(self.page.is_hidden('#voiceLiveBtn'))
        self.assertEqual(self.errors, [])

    def test_voice_icons_visible_for_voice_chat(self):
        chat_id = self._create_chat('Voice QA Chat', voice_mode=True)
        self._open_chat(chat_id)
        self.assertTrue(self.page.is_visible('#voiceMicBtn'))
        self.assertTrue(self.page.is_visible('#voiceLiveBtn'))
        self.assertTrue(self.page.is_hidden('#voiceStopBtn'))
        self.assertEqual(self.errors, [])
