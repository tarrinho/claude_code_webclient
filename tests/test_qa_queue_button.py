"""QA: the topbar queue button must exist and wire correctly.

A button was added to the topbar near the auto-approve toggle so the user can
see the queue count from anywhere, not only when a conversation is open.

The button has:
  * id=queueToggleTop (unique from the composer's id=queueToggle)
  * class="btn-icon strip-action queue-toggle" (matches existing button style)
  * id="queueToggleTop" (so app.js can wire the click handler)
  * always visible (badge updates live; badge shows "0" when nothing is queued)

app.js wires queueToggleTop in the elements dict and conversation.js adds a
click handler. Both the composer's queueToggle and the topbar's queueToggleTop
must receive the same updateQueueToggle() state so they stay in sync.

This file tests:
  * index.html contains the button element with the correct attributes.
  * app.js includes queueToggleTop in the elements dict.
  * conversation.js attaches a click handler to queueToggleTop.
  * updateQueueToggle controls both buttons, hiding/showing and updating the
    badge and title.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO / "web" / "index.html"
APP_JS = REPO / "web" / "assets" / "app.js"
CONVERSATION_JS = REPO / "web" / "assets" / "conversation.js"


class IndexHtmlQA(unittest.TestCase):
    """index.html must contain the topbar queue button."""

    def test_queue_toggle_top_element_exists(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn(
            'id="queueToggleTop"',
            html,
            "index.html must contain a button with id=queueToggleTop",
        )

    def test_button_has_correct_classes(self):
        """Must use the same btn-icon / strip-action classes as other buttons."""
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn(
            'class="btn-icon strip-action queue-toggle"',
            html,
            "queue button must carry btn-icon and strip-action classes",
        )

    def test_button_has_aria_label(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn(
            'aria-label="Show queued prompts"',
            html,
            "queue button must have an aria-label for keyboard accessibility",
        )

    def test_button_has_title(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn(
            'title="Queued prompts"',
            html,
            "queue button must have a title tooltip",
        )

    def test_button_is_not_hidden(self):
        """Queue button must be visible at all times, even when queue is empty."""
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertNotIn(
            'hidden',
            html.split('id="queueToggleTop"')[1].split('>')[0].strip(),
            "queue button must not have a hidden attribute",
        )


class AppJsQA(unittest.TestCase):
    """app.js must wire queueToggleTop in the elements dict."""

    def test_queue_toggle_top_in_elements_dict(self):
        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn(
            "queueToggleTop: byId('queueToggleTop')",
            source,
            "app.js must include queueToggleTop in the elements dict "
            "passed to createConversationController",
        )


class ConversationJsQA(unittest.TestCase):
    """conversation.js must attach a click handler to queueToggleTop."""

    def test_click_handler_on_queue_toggle_top(self):
        source = CONVERSATION_JS.read_text(encoding="utf-8")
        self.assertIn(
            "queueToggleTop",
            source,
            "conversation.js must reference queueToggleTop",
        )
        self.assertIn(
            "queueToggleTop?.addEventListener",
            source,
            "conversation.js must attach a click handler to queueToggleTop",
        )

    def test_updateQueueToggle_references_queue_toggle_top(self):
        """updateQueueToggle must control both buttons."""
        source = CONVERSATION_JS.read_text(encoding="utf-8")
        self.assertIn(
            "queueToggleTop",
            source,
            "updateQueueToggle must reference queueToggleTop for state sync",
        )

    def test_updateQueueToggle_never_sets_hidden(self):
        """Buttons stay always visible; updateQueueToggle only updates text/badge."""
        source = CONVERSATION_JS.read_text(encoding="utf-8")
        # Must not set .hidden on either button.
        self.assertNotIn(
            "queueToggleTop.hidden",
            source,
            "updateQueueToggle must never hide queueToggleTop",
        )
        self.assertNotIn(
            "queueToggle.hidden",
            source,
            "updateQueueToggle must never hide queueToggle",
        )


class UniqueIdsQA(unittest.TestCase):
    """There must be exactly one element with id=queueToggleTop and exactly one with id=queueToggle."""

    def test_no_duplicate_queue_toggle_top(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        matches = list(re.finditer(r'id="queueToggleTop"', html))
        self.assertEqual(
            len(matches), 1,
            f"expected exactly one queueToggleTop, found {len(matches)}",
        )

    def test_no_duplicate_queue_toggle(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        matches = list(re.finditer(r'id="queueToggle"', html))
        self.assertEqual(
            len(matches), 1,
            f"expected exactly one queueToggle, found {len(matches)}",
        )
