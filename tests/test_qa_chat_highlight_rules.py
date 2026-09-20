"""Which conversations the sidebar is allowed to highlight, and which it is not.

Pedro's rule, given on 2026-09-20 while explaining why he keeps working in the
terminal instead of the console:

    "the chat highlights are strange and they should be only highlighted when
     there is something to answer or if it has ended the task. if it is
     working, just do the blue pulse"

Three states, and nothing else:

    needs an answer   -> .chat-needs-answer   (amber, still)
    working           -> .chat-running        (blue, pulsing)
    task ended        -> .chat-ended          (green arrow)

What that replaced, and why each went:

* ``.chat-unread`` fired whenever a conversation's ``updated_at`` moved while
  the user was looking elsewhere. That marks output arriving, and
  ``renderSupervisor``'s own comment in chat-list.js already said output is not
  a summons -- "a badge that fires on prose is a badge that gets ignored, which
  costs the real asks buried among them". It was the noise Pedro was objecting
  to.
* ``.chat-terminal-busy`` was a second, quieter mark for a conversation working
  in its own terminal. Working is working; the reader does not have to care
  where it happens.

And what was missing, which is the half that matters: nothing on the row said
"this one asked you something". GET /api/orchestrator already classifies web
conversations into waiting/working/updated, and device-alerts.js already polls
it and hands it to the sidebar -- but the sidebar spent it on a section badge
and never marked the row. The signal was in the browser the whole time.

The CSS/source tests below run everywhere and carry most of the value; the
browser tests need a driver and are skipped without one.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from urllib.parse import urlparse

from tests.test_frontend_browser import DRIVER_OK, DRIVER_WHY, _BrowserFixture

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "web" / "assets"
CSS = (ASSETS / "styles.css").read_text(encoding="utf-8")
CHAT_LIST = (ASSETS / "chat-list.js").read_text(encoding="utf-8")
APP = (ASSETS / "app.js").read_text(encoding="utf-8")


class RetiredIndicatorsAreGoneTests(unittest.TestCase):
    """Removed means removed -- in the CSS and in every caller.

    A class left behind in one of the three places is the shape of bug that
    leaves a dead rule styling nothing, or worse, a live rule nothing styles.
    """

    def test_the_unread_dot_is_gone_from_the_stylesheet(self):
        self.assertNotIn(".chat-unread", CSS)

    def test_the_unread_dot_is_gone_from_the_renderer(self):
        self.assertNotIn("chat-unread", CHAT_LIST)

    def test_nothing_still_calls_setUnread(self):
        # The controller no longer exports it; a caller left behind would be a
        # TypeError on every refresh, which is invisible until someone opens
        # the console.
        self.assertNotIn("setUnread", CHAT_LIST)
        self.assertNotIn("setUnread", APP)

    def test_the_seen_bookkeeping_went_with_it(self):
        # The per-chat "last time I looked" keys in localStorage existed only
        # to feed the unread dot. Keeping the writes after deleting the reads
        # would accumulate keys no code consults.
        #
        # Asserted on the call sites rather than on the key prefix: the prefix
        # is named in a comment explaining why it was removed, and a test that
        # cannot tell live code from the note describing its removal fails on
        # its own documentation.
        self.assertNotIn("storageSet(seenKey", APP)
        self.assertNotIn("storageGet(seenKey", APP)
        self.assertNotIn("unreadChatIds(", APP)
        self.assertNotIn("markSeen(", APP)

    def test_the_separate_terminal_busy_dot_is_gone(self):
        self.assertNotIn(".chat-terminal-busy", CSS.replace(
            ".chat-terminal-busy was removed", ""))
        self.assertNotIn("chat-terminal-busy", CHAT_LIST)


class NeedsAnswerIndicatorTests(unittest.TestCase):

    def test_the_stylesheet_defines_it(self):
        self.assertIn(".chat-needs-answer{", CSS)

    def test_it_is_not_animated(self):
        # Motion means "running", colour means "you". Two animations side by
        # side compete for one glance, which is how the previous set became
        # the noise this change exists to remove.
        rule = CSS.split(".chat-needs-answer{", 1)[1].split("}", 1)[0]
        self.assertNotIn("animation", rule)

    def test_it_is_the_same_footprint_as_the_running_dot(self):
        # So a row does not shift when a conversation stops working and starts
        # waiting -- movement in a list is itself a distraction.
        rule = CSS.split(".chat-needs-answer{", 1)[1].split("}", 1)[0]
        self.assertIn("width:10px", rule)
        self.assertIn("height:10px", rule)

    def test_it_is_fed_from_the_orchestrator_waiting_bucket(self):
        self.assertIn("waitingIds", CHAT_LIST)
        self.assertIn("chat-needs-answer", CHAT_LIST)

    def test_only_chat_entries_are_taken_from_that_bucket(self):
        # The bucket also carries CLI/terminal sessions, whose ids are session
        # ids. Filtering by kind says so deliberately rather than relying on
        # two id spaces happening never to collide.
        self.assertIn("kind === 'chat'", CHAT_LIST)


class IndicatorPrecedenceTests(unittest.TestCase):
    """Order asserted, because reversing it is invisible to every other test.

    A conversation that asked a question and then started another turn still
    needs the person. Ranking "running" first would hide the one signal the
    sidebar exists to surface behind the most common state there is -- and it
    would still pass every test that only checks the classes exist.
    """

    def test_needs_answer_is_checked_before_running(self):
        needs = CHAT_LIST.index("waitingIds.has(chat.id)")
        running = CHAT_LIST.index("activeTurnIds.has(chat.id) || chat.terminal_busy")
        self.assertLess(
            needs, running,
            "the needs-an-answer tier must be tested before the running tier",
        )

    def test_terminal_work_shares_the_running_tier(self):
        # Pedro's rule: if it is working, just pulse. One branch, both signals.
        self.assertIn("activeTurnIds.has(chat.id) || chat.terminal_busy", CHAT_LIST)


@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
class ChatHighlightBrowserTests(_BrowserFixture):
    """The rendered result, against the real modules."""

    DESKTOP = "#chatListDesktop"

    def _chat(self, chat_id, title, running=False, terminal_busy=False):
        return {
            "id": chat_id, "title": title, "session_id": f"sess-{chat_id}",
            "work_dir": "/tmp", "model": None, "pinned": False,
            "archived": False, "updated_at": "2026-01-01T00:00:00Z",
            "running": running, "terminal_busy": terminal_busy, "queued": 0,
        }

    def _login(self):
        # One of each state the rule names, side by side: a per-row decision
        # cannot be told from a global one with fewer.
        self._chats = {"chats": [
            self._chat("c1", "Asked me something"),
            self._chat("c2", "Working here", running=True),
            self._chat("c3", "Working in its terminal", terminal_busy=True),
            self._chat("c4", "Idle"),
        ]}
        self._supervisor = {
            "waiting": [{"kind": "chat", "id": "c1", "title": "Asked me something",
                         "status": "waiting", "reason": "asks", "since": "1"}],
            "working": [], "updated": [],
        }

        def handler(route):
            request = route.request
            path = urlparse(request.url).path
            if request.method == "GET" and path == "/api/chats":
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(self._chats))
            elif request.method == "GET" and path == "/api/orchestrator":
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(self._supervisor))
            else:
                route.continue_()

        self.page.route("**/api/chats*", handler)
        self.page.route("**/api/orchestrator*", handler)
        page = self.page
        page.goto(f"{self.base}/login", wait_until="domcontentloaded")
        page.fill("#username", "admin")
        page.fill("#password", self.password)
        page.click("#submitBtn")
        page.wait_for_selector("#settingsBtn", timeout=15_000)

    def _dot_class(self, chat_id):
        """The row's leading dot class, read in one evaluate.

        One round trip on purpose: the list re-renders on a timer, so taking a
        row handle and then querying it separately can land on a detached node
        -- the flakiness the running-dot suite documents at length.
        """
        return self.page.evaluate(
            """(id) => {
                const row = document.querySelector(
                    `#chatListDesktop [data-chat-id="${id}"]`);
                if (!row) return null;
                const dot = row.querySelector('span[class^="chat-"]');
                return dot ? dot.className : '';
            }""",
            chat_id,
        )

    def test_a_chat_that_asked_something_is_highlighted(self):
        self._login()
        self.page.wait_for_selector(f"{self.DESKTOP} .chat-needs-answer", timeout=15_000)
        self.assertEqual(self._dot_class("c1"), "chat-needs-answer")

    def test_a_working_chat_only_pulses(self):
        self._login()
        self.page.wait_for_selector(f"{self.DESKTOP} .chat-running", timeout=15_000)
        self.assertEqual(self._dot_class("c2"), "chat-running")

    def test_terminal_work_pulses_the_same_way(self):
        self._login()
        self.page.wait_for_selector(f"{self.DESKTOP} .chat-running", timeout=15_000)
        self.assertEqual(self._dot_class("c3"), "chat-running")

    def test_an_idle_chat_is_not_highlighted(self):
        self._login()
        self.page.wait_for_selector(f"{self.DESKTOP} .chat-running", timeout=15_000)
        self.assertNotIn("needs-answer", self._dot_class("c4") or "")
        self.assertNotIn("running", self._dot_class("c4") or "")

    def test_no_unread_dot_is_rendered_anywhere(self):
        self._login()
        self.page.wait_for_selector(f"{self.DESKTOP} .chat-running", timeout=15_000)
        self.assertEqual(
            self.page.evaluate("document.querySelectorAll('.chat-unread').length"), 0)


if __name__ == "__main__":
    unittest.main()
