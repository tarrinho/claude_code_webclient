"""Live-browser QA: the voice conversation composer icons appear for
voice-mode chats and stay hidden for regular ones. Extends
_BrowserFixture the same way every other live-browser QA test in this
project does (see tests/test_frontend_browser.py) -- no separate pytest-
playwright fixture convention introduced.
"""
from __future__ import annotations

import unittest

from tests.test_frontend_browser import CHROMIUM, DRIVER_OK, DRIVER_WHY, _BrowserFixture


def _admin_id(conn) -> str:
    """The admin's real user id, for seeding `chats.owner_id` directly.

    A literal "admin" used to work here and silently stopped: app.py's login
    calls `auth.session_new(user["id"], ...)`, so `session["user"]` is a user
    id and `GET /api/chats` lists with `db.chat_list(session["user"])`. A chat
    owned by the string "admin" belongs to nobody the session can be, so the
    sidebar never renders it and both tests here failed waiting 30s for
    `.chat-item[data-chat-id=...] .chat-open` -- a timeout that reads as a
    browser or host problem rather than as one wrong string.
    """
    row = conn.execute("SELECT id FROM users WHERE name = 'admin'").fetchone()
    assert row, "no admin user; the server seeds one from WC_ADMIN_PASSWORD"
    return row[0]


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
        # No backend pin. These tests are about which composer controls a voice
        # conversation shows, and that follows voice_mode alone. Pinning an id
        # here made the page fetch that machine's model list, which answered
        # 404 for a machine that was never seeded and 400 for a seeded one
        # whose hostname cannot resolve -- a console error unrelated to the
        # buttons, in a suite that asserts the console stays clean.
        # Every column carries its own placeholder. Writing owner_id as a
        # literal NULL while still passing "admin" left more values than
        # placeholders, so both inserts raised ProgrammingError in setUpClass
        # and neither test ever ran -- the suite reported two errors rather
        # than an opinion about the buttons.
        con.execute(
            "INSERT INTO chats (id,title,description,work_dir,owner_id,"
            "created_at,updated_at,voice_mode,model) VALUES "
            "(?,?,?,?,?,?,?,1,'azure_ai/gpt-5.6-luna')",
            (cls.voice_chat_id, "Voice Test Chat", None, "/tmp", _admin_id(con),
             stamp, stamp),
        )
        con.execute(
            "INSERT INTO chats (id,title,description,work_dir,owner_id,"
            "created_at,updated_at,voice_mode) VALUES "
            "(?,?,?,?,?,?,?,0)",
            (cls.regular_chat_id, "Regular Test Chat", None, "/tmp", _admin_id(con),
             stamp, stamp),
        )
        con.commit()
        con.close()
        # No wait here: _BrowserFixture opens the page in setUp, so there is no
        # cls.page yet and touching one raised AttributeError before either
        # test ran. Each test already waits up to 30s for its own row to be
        # surfaced by the sidebar poll, which is the same wait done properly.

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
