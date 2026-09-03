"""The sidebar's per-chat "agent is working" indicator (.chat-running).

Reported live: the dot existed already (chat-list.js has carried it for a
while, driven by the real `running` flag GET /api/chats already returns per
chat) but read as barely-there next to a chat title -- easy to miss across a
sidebar full of rows. Bumped from a plain 6px dot to a bigger, glowing,
harder-pulsing one; see styles.css's own comment on `.chat-running` for the
exact values.

Two claims get separate tests here, on purpose:

  * the CSS itself carries the bumped values (a regression could revert the
    size without anyone noticing, since nothing else in the app depends on
    the exact pixel count);
  * the dot is actually keyed to the right row, for more than one chat at
    once, and disappears once that chat's turn ends. A single-chat check
    cannot prove the first half of that -- "the dot is per-row, not shown
    globally whenever anything is running" is only visible with at least two
    chats in different states side by side, which is what "in all chats" in
    the request that asked for this file means.

The live test intercepts GET /api/chats rather than driving a real turn: the
dot is entirely a function of that response's `running` field
(app.js:refreshChats -> listController.setActiveTurns ->
chat-list.js:renderSection), so faking the response tests exactly the
contract the rendering relies on without needing a live backend to run
anything.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from urllib.parse import urlparse

from tests.test_frontend_browser import CHROMIUM, DRIVER_OK, DRIVER_WHY, _BrowserFixture

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
ASSETS = WEB / "assets"


# ── CSS values ───────────────────────────────────────────────────────────────

class ChatRunningDotCssTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = (ASSETS / "styles.css").read_text(encoding="utf-8")
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")

    def _rule(self, selector: str) -> str:
        match = re.search(re.escape(selector) + r"\{([^}]*)\}", self.css)
        self.assertIsNotNone(match, f"{selector} not found in styles.css")
        return match.group(1)

    def test_the_dot_is_bigger_than_the_original_6px(self):
        rule = self._rule(".chat-running")
        match = re.search(r"width:(\d+)px", rule)
        self.assertIsNotNone(match, "the rule must set an explicit width")
        self.assertGreater(
            int(match.group(1)), 6,
            "6px is the original size this was reported as too easy to miss "
            "at -- a value at or below it is the regression this guards",
        )

    def test_the_dot_glows(self):
        """box-shadow is what turns "a slightly bigger dot" into something
        that reads as active rather than merely present.
        """
        self.assertIn("box-shadow", self._rule(".chat-running"))

    def test_the_dot_still_pulses(self):
        rule = self._rule(".chat-running")
        self.assertIn("animation", rule)
        self.assertIn("chat-pulse", rule)

    def test_the_pulse_swing_is_narrow_enough_to_survive_a_rebuild(self):
        """Reported live as "flashing": the sidebar list is fully rebuilt
        every 6s poll (chat-list.js render() -- list.replaceChildren()),
        which restarts this animation's timeline on a brand new element each
        time. A rebuild landing mid-dip snaps straight back to full opacity
        -- the wider the swing, the more that snap reads as a flash rather
        than a pulse. 1 -> .25 (this file's own earlier bump) was reported
        live as flashing; 1 -> .35 (the original) was not.
        """
        # Not through _rule(): the keyframes block has nested braces
        # (0%,100%{...} and 50%{...} both inside @keyframes chat-pulse{...}),
        # and _rule()'s "up to the first }" extraction only captures the
        # first inner block. Searched directly in the full stylesheet instead.
        match = re.search(r"50%\{opacity:([\d.]+)\}", self.css)
        self.assertIsNotNone(match, "the keyframe must set 50% opacity")
        self.assertGreaterEqual(
            float(match.group(1)), 0.5,
            "a dip below .5 is where a rebuild-triggered reset back to full "
            "opacity starts reading as a flash rather than a pulse",
        )

    def test_the_pulse_still_respects_reduced_motion(self):
        """Pre-existing accessibility rule; the bump must not have dropped it
        along the way.
        """
        self.assertIn(
            "@media (prefers-reduced-motion:reduce){.chat-running{animation:none}",
            self.css,
        )

    def test_styles_css_carries_a_cache_buster_at_or_above_the_fix(self):
        match = re.search(r"styles\.css\?v=(\d+)", self.html)
        self.assertIsNotNone(match, "styles.css must carry a version query")
        self.assertGreaterEqual(
            int(match.group(1)), 30,
            "styles.css?v= must be at least 30 -- the size bump and the "
            "pulse-amplitude fix both changed this file (numbering reset "
            "after an uncommitted-work incident on 2026-09-03; the floor "
            "here tracks the post-incident reapplication, not the original "
            "history), and each needed its own version bump",
        )


# ── Live, multi-chat behaviour ───────────────────────────────────────────────

@unittest.skipUnless(DRIVER_OK, f"playwright driver unusable ({DRIVER_WHY})")
@unittest.skipIf(CHROMIUM is None, "no Chromium binary on PATH")
class ChatRunningDotBrowserTests(_BrowserFixture):
    DESKTOP = "#chatListDesktop"

    def _chat(self, chat_id, title, running, terminal_busy=False):
        return {
            "id": chat_id, "title": title, "session_id": f"sess-{chat_id}",
            "work_dir": "/tmp", "model": None, "pinned": False,
            "archived": False, "updated_at": "2026-01-01T00:00:00Z",
            "running": running, "terminal_busy": terminal_busy, "queued": 0,
        }

    def _login(self):
        # Two running at once and one idle: the minimum shape that can prove
        # the dot is decided per row rather than flipped on for the whole
        # list whenever anything, anywhere, is running.
        self._payload = {"chats": [
            self._chat("c1", "First agent", running=True),
            self._chat("c2", "Second agent", running=True),
            self._chat("c3", "Idle agent", running=False),
        ]}

        def handler(route):
            request = route.request
            if request.method != "GET" or urlparse(request.url).path != "/api/chats":
                route.continue_()
                return
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(self._payload))

        self.page.route("**/api/chats*", handler)
        page = self.page
        page.goto(f"{self.base}/login", wait_until="domcontentloaded")
        page.fill("#username", "admin")
        page.fill("#password", self.password)
        page.click("#submitBtn")
        page.wait_for_selector("#settingsBtn", timeout=15_000)

    def _has_dot(self, chat_id):
        row = self.page.query_selector(
            f'{self.DESKTOP} .chat-item[data-chat-id="{chat_id}"]')
        self.assertIsNotNone(row, f"chat {chat_id} is not in the list at all")
        return row.query_selector(".chat-running") is not None

    def test_running_agents_each_get_their_own_dot(self):
        self.page.wait_for_selector(
            f'{self.DESKTOP} .chat-item[data-chat-id="c3"]', timeout=15_000)
        self.assertTrue(self._has_dot("c1"), "c1 is running and must show the dot")
        self.assertTrue(self._has_dot("c2"), "c2 is running and must show the dot")
        self.assertFalse(self._has_dot("c3"), "c3 is idle and must not show one")

    def test_two_running_agents_are_not_collapsed_into_one_dot(self):
        """Guards against a bug shaped like "the dot is global": if the
        client only tracked whether *anything* was running, both c1 and c2
        would still individually show a dot here, so this only fails if a
        future change makes the indicator per-list rather than per-row --
        caught by asserting the count matches the number of running chats,
        not just that each individually named one has one.
        """
        self.page.wait_for_selector(
            f'{self.DESKTOP} .chat-item[data-chat-id="c3"]', timeout=15_000)
        dots = self.page.query_selector_all(f'{self.DESKTOP} .chat-running')
        self.assertEqual(len(dots), 2)

    def test_the_dot_disappears_once_that_chats_turn_ends(self):
        """The other half of the claim: not just "appears while running", but
        "goes away on its own once the turn is over" -- with c2 left running,
        so this also proves the disappearance is per-row, not the list
        deciding nothing is running anymore.
        """
        self.page.wait_for_selector(
            f'{self.DESKTOP} .chat-item[data-chat-id="c3"]', timeout=15_000)
        self.assertTrue(self._has_dot("c1"))

        self._payload["chats"][0]["running"] = False
        # CHAT_POLL_MS is 6000; generous margin over that for the next poll
        # to land rather than racing it.
        self.page.wait_for_timeout(7_000)

        self.assertFalse(
            self._has_dot("c1"),
            "the dot for a finished turn must be removed on the next poll, "
            "not left showing until the page is reloaded",
        )
        self.assertTrue(
            self._has_dot("c2"),
            "c2 is still running and must not have been cleared along with c1",
        )
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
