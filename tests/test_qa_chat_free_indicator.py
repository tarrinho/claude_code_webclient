"""The sidebar's "nothing outstanding" indicator (.chat-free).

Requested live: a symbol for chats that have no work running, are not
waiting on anything queued to send, and (with one deliberate exception, see
below) have no question sitting unanswered.

Sits as the lowest tier in chat-list.js's existing if/else chain
(.chat-running > .chat-terminal-busy > .chat-unread > .chat-ended >
.chat-free), gated on `!chat.queued` specifically -- queued is otherwise
rendered as its own trailing badge, independent of this chain, so without
that extra gate a chat with prompts queued to send but none of the tiers
above active would incorrectly read as free.

Deliberately does NOT check for a pending, unanswered question. That was a
real option -- discussed and rejected. Checking it would mean reading each
chat's transcript (some are tens of MB, per CLAUDE.md) or its live terminal,
for every chat, on the same 6s poll that GET /api/chats already serves --
exactly the shape of per-poll cost that was already reported live as making
the whole page slow, for a case (an unanswered question with no live process
behind it at all) rare enough that opening the chat and seeing the question
bar as normal was judged an acceptable gap. `chats_with_auto_answer` in
db.py takes the opposite tradeoff for a different, already-armed subset of
chats; this indicator is unscoped to every chat in the sidebar and does not
get to make that trade the same way.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.test_frontend_browser import CHROMIUM, DRIVER_OK, DRIVER_WHY, _BrowserFixture

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
ASSETS = WEB / "assets"


# ── Source-level: the branch and its CSS ─────────────────────────────────────

class ChatFreeIndicatorSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chat_list = (ASSETS / "chat-list.js").read_text(encoding="utf-8")
        cls.css = (ASSETS / "styles.css").read_text(encoding="utf-8")
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        # chat-list.js has no <script> tag of its own -- it is only ever
        # reached via a bare import in app.js -- so its own cache-buster
        # lives there, not in index.html or in the file's own text.
        cls.app = (ASSETS / "app.js").read_text(encoding="utf-8")

    def test_the_branch_is_gated_on_no_queue_too(self):
        """The bug this guards: without checking !chat.queued, a chat with
        nothing running but prompts still queued to send would read as free
        even though it is plainly still waiting for something to finish.
        """
        match = re.search(
            r"else if \(endedIds\.has\(chat\.id\)\) \{.*?\}\s*else if "
            r"\((![\w.]+)\)\s*\{[^}]*chat-free", self.chat_list, re.DOTALL,
        )
        self.assertIsNotNone(
            match, "could not find the chat-free branch guarded by a "
            "negated condition right after the chat-ended branch",
        )
        self.assertEqual(match.group(1), "!chat.queued")

    def test_the_branch_comes_after_every_busier_tier(self):
        """Source order is the priority order here -- an earlier if/else
        branch wins, so chat-free must be textually last in the chain.
        """
        order = [self.chat_list.index(marker) for marker in (
            "activeTurnIds.has(chat.id)", "chat.terminal_busy",
            "unreadIds.has(chat.id)", "endedIds.has(chat.id)", "chat-free",
        )]
        self.assertEqual(order, sorted(order))

    def test_it_does_not_read_or_await_any_question_state(self):
        """The rejected, more-accurate option. Guards against it creeping
        back in as a per-chat cost on the list poll. Checked against the
        branch's CODE lines only, with `//` comments stripped -- this file's
        own explanation of *why* the check was rejected necessarily uses the
        word, so a bare substring check over the whole branch would trip on
        its own docstring rather than on the thing it is meant to catch.
        """
        branch = self.chat_list.split("chat-free", 1)[0]
        branch = branch[branch.rindex("else if"):]
        code_only = "\n".join(
            line for line in branch.splitlines()
            if not line.strip().startswith("//")
        )
        self.assertNotIn("question", code_only.lower())
        self.assertNotIn("await", code_only.lower())
        self.assertNotIn("fetch(", code_only.lower())

    def test_the_css_uses_no_animation(self):
        """Every busy indicator here moves or glows on purpose; this is the
        one state that should read as calm, not another thing competing for
        attention next to a title.
        """
        match = re.search(r"\.chat-free\{([^}]*)\}", self.css)
        self.assertIsNotNone(match, ".chat-free rule not found")
        self.assertNotIn("animation", match.group(1))

    def test_styles_css_carries_a_cache_buster_at_or_above_the_fix(self):
        # Numbering reset after an uncommitted-work incident on 2026-09-03;
        # this floor tracks the post-incident reapplication, not the
        # original pre-incident history.
        match = re.search(r"styles\.css\?v=(\d+)", self.html)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(int(match.group(1)), 30)

    def test_chat_list_js_carries_a_cache_buster_at_or_above_the_fix(self):
        match = re.search(r"from '\./chat-list\.js\?v=(\d+)'", self.app)
        self.assertIsNotNone(
            match, "app.js's import of chat-list.js must carry a version "
            "query -- that bare specifier is its own cache key, untouched "
            "by bumping app.js's own <script> tag ?v=",
        )
        self.assertGreaterEqual(int(match.group(1)), 2)


# ── Live, multi-chat behaviour ───────────────────────────────────────────────

@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class ChatFreeIndicatorBrowserTests(_BrowserFixture):
    DESKTOP = "#chatListDesktop"

    def _chat(self, chat_id, title, running=False, terminal_busy=False, queued=0):
        return {
            "id": chat_id, "title": title, "session_id": f"sess-{chat_id}",
            "work_dir": "/tmp", "model": None, "pinned": False,
            "archived": False, "updated_at": "2026-01-01T00:00:00Z",
            "running": running, "terminal_busy": terminal_busy, "queued": queued,
        }

    def _login(self):
        # One of each tier, so the free chat's own absence of every other
        # marker is proven by contrast, not assumed.
        self._payload = {"chats": [
            self._chat("c1", "Running", running=True),
            self._chat("c2", "Busy terminal", terminal_busy=True),
            self._chat("c3", "Queued only", queued=2),
            self._chat("c4", "Free"),
        ]}

        def handler(route):
            from urllib.parse import urlparse
            request = route.request
            if request.method != "GET" or urlparse(request.url).path != "/api/chats":
                route.continue_()
                return
            import json
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(self._payload))

        self.page.route("**/api/chats*", handler)
        page = self.page
        page.goto(f"{self.base}/login", wait_until="domcontentloaded")
        page.fill("#username", "admin")
        page.fill("#password", self.password)
        page.click("#submitBtn")
        page.wait_for_selector("#settingsBtn", timeout=15_000)

    def _row(self, chat_id):
        return self.page.query_selector(
            f'{self.DESKTOP} .chat-item[data-chat-id="{chat_id}"]')

    def test_only_the_chat_with_nothing_outstanding_gets_the_free_icon(self):
        self.page.wait_for_selector(
            f'{self.DESKTOP} .chat-item[data-chat-id="c4"]', timeout=15_000)
        self.assertIsNone(self._row("c1").query_selector(".chat-free"))
        self.assertIsNone(self._row("c2").query_selector(".chat-free"))
        self.assertIsNone(
            self._row("c3").query_selector(".chat-free"),
            "a chat with prompts queued must not read as free even though "
            "nothing is currently running",
        )
        self.assertIsNotNone(self._row("c4").query_selector(".chat-free"))
        self.assertEqual(self.errors, [])

    def test_the_free_icon_is_the_only_marker_on_that_row(self):
        """It must not double up with .chat-running or .chat-terminal-busy --
        this is the tier that only ever applies when nothing else does.
        """
        self.page.wait_for_selector(
            f'{self.DESKTOP} .chat-item[data-chat-id="c4"] .chat-free',
            timeout=15_000)
        row = self._row("c4")
        self.assertIsNone(row.query_selector(".chat-running"))
        self.assertIsNone(row.query_selector(".chat-terminal-busy"))
        self.assertIsNone(row.query_selector(".chat-queued"))

    def test_starting_a_turn_removes_the_free_icon_on_the_next_poll(self):
        self.page.wait_for_selector(
            f'{self.DESKTOP} .chat-item[data-chat-id="c4"] .chat-free',
            timeout=15_000)

        self._payload["chats"][3]["running"] = True
        self.page.wait_for_timeout(7_000)  # past CHAT_POLL_MS (6000)

        row = self._row("c4")
        self.assertIsNone(
            row.query_selector(".chat-free"),
            "a chat that started running must lose the free icon on the "
            "very next poll, not keep showing 'nothing outstanding'",
        )
        self.assertIsNotNone(row.query_selector(".chat-running"))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
