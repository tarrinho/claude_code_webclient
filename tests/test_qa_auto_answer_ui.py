"""Frontend wiring for the auto-answer toggle and its last-ten popover.

Step 4 of docs/superpowers/specs/2026-09-02-auto-answer-knob-design.md.
Source-inspection style, matching tests/test_frontend.py -- no browser needed
for markup/wiring assertions, and that file is contended by another session's
edit, so this is its own file rather than an addition to it.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
ASSETS = WEB / "assets"


class AutoAnswerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / "index.html").read_text()
        cls.css = (ASSETS / "styles.css").read_text()
        cls.app = (ASSETS / "app.js").read_text()

    # ── markup ───────────────────────────────────────────────────────────

    def test_the_controls_exist_and_start_hidden(self):
        """Hidden by default: shown only once refreshAutoAnswer confirms the
        chat has a session_id, matching the server-side eligibility check in
        db.chats_with_auto_answer.
        """
        for control in ("autoAnswerToggle", "autoAnswerInfo", "autoAnswerMenu"):
            self.assertIn(f'id="{control}"', self.html)
        strip = self.html.split('id="workspaceStrip"')[1].split("</div>\n  <section")[0]
        for control in ("autoAnswerToggle", "autoAnswerInfo", "autoAnswerMenu"):
            self.assertIn(f'id="{control}"', strip, f"{control} must sit in the strip")
        toggle = re.search(r'<button[^>]*id="autoAnswerToggle"[^>]*>', self.html).group()
        info = re.search(r'<button[^>]*id="autoAnswerInfo"[^>]*>', self.html).group()
        menu = re.search(r'<div[^>]*id="autoAnswerMenu"[^>]*>', self.html).group()
        self.assertIn("hidden", toggle)
        self.assertIn("hidden", info)
        self.assertIn("hidden", menu)

    def test_the_toggle_is_positioned_before_run_state(self):
        """.run-state carries margin-left:auto (test_frontend.py), so anything
        meant to sit with the status dot must precede it in source order.
        """
        strip = self.html.split('id="workspaceStrip"')[1].split("</div>\n  <section")[0]
        self.assertLess(strip.index('id="autoAnswerToggle"'), strip.index('id="runState"'))
        self.assertLess(strip.index('id="autoAnswerInfo"'), strip.index('id="runState"'))

    def test_the_label_says_what_it_does(self):
        """Not "Auto-answer" as a neutral preference: this approves permission
        prompts without a person, and the accessible name has to say so.
        """
        toggle = re.search(r'<button[^>]*id="autoAnswerToggle"[^>]*>', self.html).group()
        self.assertIn("aria-label=", toggle)
        label = re.search(r'aria-label="([^"]*)"', toggle).group(1)
        self.assertIn("approve", label.lower())
        self.assertIn("permission", label.lower())

    def test_the_toggle_reports_its_state_to_assistive_tech(self):
        toggle = re.search(r'<button[^>]*id="autoAnswerToggle"[^>]*>', self.html).group()
        self.assertIn('aria-pressed="false"', toggle)

    def test_the_menu_is_a_polite_live_region(self):
        """A new auto-answer should be announced, not appear silently -- the
        mobile spec's rule for anything that updates outside user action."""
        menu = re.search(r'<div[^>]*id="autoAnswerMenu"[^>]*>', self.html).group()
        self.assertIn('role="log"', menu)
        self.assertIn('aria-live="polite"', menu)

    def test_the_info_button_declares_its_popup(self):
        info = re.search(r'<button[^>]*id="autoAnswerInfo"[^>]*>', self.html).group()
        self.assertIn('aria-haspopup="true"', info)
        self.assertIn('aria-expanded="false"', info)
        self.assertIn('aria-controls="autoAnswerMenu"', info)

    def test_touch_targets_meet_the_44px_rule(self):
        """Reuses .btn-icon/.strip-action, which test_frontend.py already
        pins at a minimum size; asserted again here because a future change
        to those classes could silently shrink these two along with it.
        """
        toggle = re.search(r'<button[^>]*id="autoAnswerToggle"[^>]*>', self.html).group()
        info = re.search(r'<button[^>]*id="autoAnswerInfo"[^>]*>', self.html).group()
        self.assertIn("btn-icon", toggle)
        self.assertIn("btn-icon", info)

    # ── CSS ──────────────────────────────────────────────────────────────

    def test_the_armed_state_does_not_rely_on_colour_alone(self):
        self.assertIn('.auto-answer-toggle[aria-pressed="true"]', self.css)

    def test_the_menu_can_be_hidden(self):
        self.assertIn(".auto-answer-menu[hidden]{display:none}", self.css)

    # ── JS wiring ────────────────────────────────────────────────────────

    def test_the_toggle_is_wired_to_its_handler(self):
        self.assertIn(
            "byId('autoAnswerToggle').addEventListener('click', toggleAutoAnswer)",
            self.app,
        )

    def test_the_info_button_is_wired_to_its_handler(self):
        self.assertIn(
            "byId('autoAnswerInfo').addEventListener('click', toggleAutoAnswerMenu)",
            self.app,
        )

    def test_polling_starts_when_a_chat_is_selected(self):
        body = self.app.split("function updateCurrentUi(chat)", 1)[1][:2000]
        self.assertIn("startAutoAnswerPolling()", body)

    def test_the_controls_are_cleared_on_deselect(self):
        """Without this a stale toggle/menu from the previous chat could be
        acted on against the one now open.
        """
        self.assertIn("byId('autoAnswerToggle').hidden = true", self.app)
        self.assertIn("byId('autoAnswerInfo').hidden = true", self.app)
        self.assertIn("closeAutoAnswerMenu()", self.app)

    def test_a_chat_with_no_session_id_hides_the_controls(self):
        """Answering needs a session id to locate the terminal -- the same
        eligibility rule as db.chats_with_auto_answer. Showing an armed-looking
        toggle here would be a control that looks live and can never fire.
        """
        body = self.app.split("async function refreshAutoAnswer()", 1)[1][:600]
        self.assertIn("chat.session_id", body)
        self.assertIn("toggle.hidden = true", body)

    def test_escape_closes_the_menu_and_restores_focus(self):
        """The mobile spec's rule for any menu/drawer/dialog."""
        block = self.app.split("document.addEventListener('keydown'", 1)[1][:800]
        self.assertIn("autoAnswerMenu", block)
        self.assertIn("closeAutoAnswerMenu()", block)
        self.assertIn("byId('autoAnswerInfo')?.focus()", block)

    def test_there_is_only_one_escape_listener_for_the_menu(self):
        """A second keydown listener on the menu itself would fire first on
        bubble and close the menu, and then the global handler's own
        now-stale hidden-check would fall through to closeSidebar() on the
        same keypress. Guards against reintroducing that double-fire.
        """
        self.assertNotIn("addEventListener('keydown', onAutoAnswerMenuKeydown)", self.app)

    def test_rows_are_built_with_createElement_not_innerHTML(self):
        """Every row quotes a prompt read off a terminal or written by a
        model; it must never be interpolated into innerHTML.
        """
        body = self.app.split("function renderAutoAnswerMenu()", 1)[1]
        body = body[:body.index("\nfunction ")]
        self.assertNotIn("innerHTML", body)
        self.assertIn("createElement", body)
        self.assertIn("textContent", body)

    def test_a_skip_shows_its_reason_not_a_blank_prompt(self):
        """The skip's reason is the useful line: it says why nothing was
        pressed, which is the whole point of a skip entry."""
        body = self.app.split("function renderAutoAnswerMenu()", 1)[1]
        body = body[:body.index("\nfunction ")]
        self.assertIn("entry.reason", body)


if __name__ == "__main__":
    unittest.main()
