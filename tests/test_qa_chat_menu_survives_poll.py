"""QA: the conversation ⋯ menu must outlive the sidebar's poll.

The report was "I click the three dots and nothing happens". The menu was
opening correctly every time -- measured on the live page, the click set
`.chat-menu.open`, `aria-expanded="true"` and a 360px-tall `display:grid`
panel -- and then it vanished on its own:

    t+1s  open menus=1  trigger still in DOM=True
    t+2s  open menus=0  trigger still in DOM=False

refreshChats() polls every CHAT_POLL_MS (6 seconds) and calls
listController.render() unconditionally, and render rebuilds every row. The
open menu hangs off a row, so the rebuild throws it away along with the
trigger it belongs to. A menu therefore survived somewhere between 0 and 6
seconds depending on where the click fell in the poll cycle, and a poll
landing immediately after the click looked exactly like a dead button.

This is the same churn that shows up in the browser suite as "ElementHandle
click: Element is not attached to the DOM", so the poll was destroying
interactions there too.

Asserted from source, this repo's convention for the frontend modules: there
is no JS runner for chat-list.js (the quickjs harness in tests/js/ serves
supervisor-map.js and the d3 stub). What this buys is the ordering, which is
where this class of bug lives -- that the guard exists, that it defers rather
than drops, and that the deferred draw happens after the menus are actually
closed rather than while the guard would still see one open.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAT_LIST = ROOT / "web" / "assets" / "chat-list.js"


def _strip_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


class RenderDefersWhileAMenuIsOpenTests(unittest.TestCase):
    def setUp(self):
        self.js = _strip_comments(CHAT_LIST.read_text(encoding="utf-8"))

    def _render(self) -> str:
        match = re.search(
            r"function render\(chats = lastChats.*?\n  \}", self.js, re.DOTALL)
        self.assertIsNotNone(match, "render() moved or was renamed")
        return match.group(0)

    def _close_menus(self) -> str:
        match = re.search(
            r"function closeMenus\(.*?\n  \}", self.js, re.DOTALL)
        self.assertIsNotNone(match, "closeMenus() moved or was renamed")
        return match.group(0)

    def test_render_refuses_to_rebuild_under_an_open_menu(self):
        body = self._render()
        self.assertIn("'.chat-menu.open'", body,
                      "render no longer checks for an open menu, so the 6s "
                      "poll can destroy one mid-click again")
        guard = body.index(".chat-menu.open")
        self.assertIn("return", body[guard:guard + 120],
                      "the open-menu check does not actually stop the rebuild")

    def test_the_skipped_draw_is_remembered_rather_than_dropped(self):
        """Deferring must not mean losing: the sidebar would otherwise sit on
        stale rows until something unrelated triggered another render."""
        body = self._render()
        self.assertIn("renderDeferred = true", body)
        self.assertRegex(
            body, r"renderDeferred = false",
            "nothing clears the deferred flag, so the first deferral would "
            "leave it set forever",
        )

    def test_closing_the_menu_draws_what_was_deferred(self):
        body = self._close_menus()
        self.assertIn("renderDeferred", body)
        self.assertRegex(body, r"render\(\)",
                         "closing a menu never redraws, so the list stays "
                         "frozen at whatever it showed when the menu opened")

    def test_the_deferred_draw_runs_after_the_menus_are_closed(self):
        """Ordering is the whole fix. Redrawing before the `open` class is
        removed would hit render's own guard and defer again, which is a
        sidebar that never updates while looking like it should."""
        body = self._close_menus()
        removal = body.index("classList.remove('open')")
        redraw = body.index("render()")
        self.assertLess(
            removal, redraw,
            "the deferred render runs before the menu is closed, so it will "
            "be deferred again by its own guard",
        )

    def test_the_flag_is_declared_before_render_uses_it(self):
        """A `let` read above its declaration is a temporal-dead-zone throw,
        which would break the whole sidebar rather than just the menu."""
        source = CHAT_LIST.read_text(encoding="utf-8")
        declared = source.index("let renderDeferred")
        used = source.index("function render(chats = lastChats")
        self.assertLess(declared, used)


if __name__ == "__main__":
    unittest.main()
