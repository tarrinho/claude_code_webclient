"""QA: the last request kept in view under the workspace strip.

Asked for so that "what did I ask here?" is answerable without scrolling, which
matters most on a phone where the conversation shows two or three messages at a
time.

Structure only. The behaviour -- which message is chosen, when the line updates,
whether it survives switching conversations -- is DOM behaviour and is covered in
``tests/smoke_last_command.py`` against a real browser, because a test that
asserts on markup cannot tell whether the page ever writes to it.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


class MarkupQA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text(encoding="utf-8")
        cls.css = (WEB / "assets" / "styles.css").read_text(encoding="utf-8")
        cls.app_js = (WEB / "assets" / "app.js").read_text(encoding="utf-8")
        cls.conv_js = (WEB / "assets" / "conversation.js").read_text(encoding="utf-8")

    def test_the_bar_exists_with_the_three_parts_the_script_writes_to(self):
        for element_id in ("lastCommandBar", "lastCommandText", "lastCommandWhen"):
            self.assertIn(f'id="{element_id}"', self.html, element_id)

    def test_it_starts_hidden(self):
        # An empty conversation must not show a bar with nothing in it.
        bar = re.search(r'<div class="workspace-lastcmd"[^>]*>', self.html)
        self.assertIsNotNone(bar)
        self.assertIn("hidden", bar.group(0))

    def test_it_sits_directly_below_the_strip(self):
        """Where it was asked to go, and the reason it is a sibling.

        The strip is a flex row; making it a two-row grid would move every
        control already in it. So this is the next element instead.
        """
        strip = self.html.index('id="workspaceStrip"')
        bar = self.html.index('id="lastCommandBar"')
        composer = self.html.index('id="composerArea"')
        messages = self.html.index('id="messagesArea"')
        self.assertLess(strip, bar, "the bar must come after the strip")
        self.assertLess(bar, messages, "and before the conversation")
        self.assertLess(bar, composer)

    def test_the_script_is_given_all_three_elements(self):
        # Missing one would leave the line half-written with no error.
        for key in ("lastCommandBar", "lastCommandText", "lastCommandWhen"):
            self.assertIn(f"{key}: byId('{key}')", self.app_js, key)

    def test_the_prompt_is_written_as_text_not_markup(self):
        """The user's own prompt comes back from the database.

        Rendered with innerHTML it would be interpreted, and a prompt is exactly
        the kind of content an operator pastes without thinking about it.
        """
        self.assertIn("elements.lastCommandText.textContent", self.conv_js)
        self.assertNotIn("lastCommandText.innerHTML", self.conv_js)

    def test_a_long_prompt_ellipsises_rather_than_widening_the_pane(self):
        rule = next(
            (line for line in self.css.splitlines()
             if line.startswith(".lastcmd-text{")), None)
        self.assertIsNotNone(rule, ".lastcmd-text rule missing")
        # min-width:0 is the half people forget; without it a grid child refuses
        # to shrink and the ellipsis never engages.
        for needed in ("text-overflow:ellipsis", "white-space:nowrap", "min-width:0"):
            self.assertIn(needed, rule, needed)

    def test_the_container_gives_the_text_a_shrinkable_column(self):
        rule = next(
            (line for line in self.css.splitlines()
             if line.startswith(".workspace-lastcmd{")), None)
        self.assertIsNotNone(rule)
        self.assertIn("minmax(0,1fr)", rule)

    def test_hidden_is_honoured_against_the_display_rule(self):
        # .workspace-lastcmd sets display:grid, which would override the hidden
        # attribute without this.
        self.assertIn(".workspace-lastcmd[hidden]{display:none}", self.css)

    def test_the_two_bars_read_as_one(self):
        # The strip drops its own bottom border while the line is showing.
        self.assertIn(
            ".workspace-strip:has(+ .workspace-lastcmd:not([hidden])){border-bottom:none}",
            self.css,
        )

    def test_closing_a_conversation_hides_it(self):
        """A stale request under an empty pane reads as work still in flight."""
        hide = self.app_js.index("byId('workspaceStrip').style.display = 'none'")
        window = self.app_js[hide:hide + 240]
        self.assertIn("lastCommandBar", window)
        self.assertIn("hidden = true", window)


class UpdatePointsQA(unittest.TestCase):
    """Every path that changes what was last asked must update the line.

    Listed explicitly because two of them are easy to miss: a routed request and
    a queued one never reach ``refreshCurrent``, so sending is the only moment
    the line can be set for either.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = (WEB / "assets" / "conversation.js").read_text(encoding="utf-8")

    def _body(self, name):
        start = self.js.index(f"function {name}(")
        return self.js[start:start + 1800]

    def test_opening_a_conversation_sets_it_from_what_was_loaded(self):
        self.assertIn("setLastCommand", self._body("selectChat"))

    def test_a_refresh_after_a_turn_updates_it(self):
        self.assertIn("setLastCommand", self._body("refreshCurrent"))

    def test_sending_sets_it_immediately(self):
        # Not only on completion: a routed or queued request produces no turn
        # here at all, so waiting for one would leave the line stale.
        self.assertIn("setLastCommand", self._body("send"))

    def test_the_newest_user_message_is_the_one_chosen(self):
        body = self._body("lastCommandFrom")
        self.assertIn("role === 'user'", body)
        # Walked backwards: the newest is wanted, and the list can be long.
        self.assertIn("index -= 1", body)

    def test_the_relative_time_is_refreshed(self):
        # Otherwise "2m ago" sits there saying 2m for an hour.
        self.assertIn("setInterval", self.js)
        self.assertIn("renderLastCommand", self.js)


if __name__ == "__main__":
    unittest.main()
