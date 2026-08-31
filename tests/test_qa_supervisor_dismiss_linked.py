"""QA: dismissing a highlighted conversation actually removes it.

Pedro clicked the row's dismiss control and the row stayed. The click was fine,
the request was fine, and the mark was written -- three checks that all passed
while the feature did not work.

A web conversation linked to a CLI session that has stopped being busy is
listed as waiting by a *deferred* branch of classify_chat, which read only the
session's dismissal mark. The row is rendered as a conversation, so dismissing
it writes ("chat", chat_id). The branch never consulted that key, so the
dismissal was recorded faithfully and then ignored, and the row returned on the
next poll.

The browser tests written with this control seeded a static chat: one message,
never updated, no linked session. That shape is suppressed by the *earlier*
branch, which does read the chat mark -- so they passed against code that could
not work for the rows the user actually has. This drives the classifier with
the shape production has.
"""
from __future__ import annotations

import unittest

import app

CHAT_ID = "c0ffee00c0ffee00c0ffee00c0ffee00"
SESSION_ID = "8db15c35-74bc-4670-a617-ad2ff0426ec4"


def classify(marks, status_updated, cli_status="waiting", last_role="assistant"):
    """Run the classifier for a conversation linked to a CLI session."""
    return app.classify_chat(
        chat={"id": CHAT_ID, "title": "cweb2", "session_id": SESSION_ID},
        last={"role": last_role, "created_at": "2026-08-31T19:12:04Z",
              "preview": "Let me study the existing test files."},
        live_ids=frozenset(),
        queued={},
        marks=marks,
        cli_status_map={SESSION_ID: cli_status},
        cli_dismiss_map={SESSION_ID: ""},
        cli_status_updated_map={SESSION_ID: status_updated},
    )


class LinkedConversationDismissTests(unittest.TestCase):
    """The shape Pedro actually had: a chat whose CLI session went quiet."""

    def test_without_a_dismissal_it_is_listed(self):
        """Guards the rest: if this stopped being waiting, nothing below means
        anything."""
        entry = classify(marks={}, status_updated="2026-08-31T19:00:00Z")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "waiting")

    def test_dismissing_the_conversation_removes_it(self):
        """The regression proper. This is the mark the dismiss control writes."""
        marks = {("chat", CHAT_ID): {"dismissed_at": "2026-08-31T19:06:28Z"}}
        entry = classify(marks=marks, status_updated="2026-08-31T19:00:00Z")
        # Not None: it drops to "updated", which the sidebar renders as a quiet
        # count rather than a highlighted row. Leaving the highlights is the
        # contract; disappearing from the console entirely is not.
        self.assertNotEqual(
            (entry or {}).get("status"), "waiting",
            "the conversation's own dismissal was written and then ignored",
        )

    def test_dismissing_the_session_still_removes_it(self):
        """The path that already worked must keep working."""
        entry = app.classify_chat(
            chat={"id": CHAT_ID, "title": "cweb2", "session_id": SESSION_ID},
            last={"role": "assistant", "created_at": "2026-08-31T19:12:04Z",
                  "preview": "Let me study the existing test files."},
            live_ids=frozenset(), queued={}, marks={},
            cli_status_map={SESSION_ID: "waiting"},
            cli_dismiss_map={SESSION_ID: "2026-08-31T19:06:28Z"},
            cli_status_updated_map={SESSION_ID: "2026-08-31T19:00:00Z"},
        )
        self.assertNotEqual((entry or {}).get("status"), "waiting")

    def test_a_newer_ask_raises_it_again(self):
        """Dismissing silences what was there, not everything that follows.

        The session changing status after the dismissal is a fresh summons and
        must reappear, or the control becomes a permanent mute.
        """
        marks = {("chat", CHAT_ID): {"dismissed_at": "2026-08-31T19:06:28Z"}}
        entry = classify(marks=marks, status_updated="2026-08-31T19:09:00Z")
        self.assertIsNotNone(entry, "a new ask after the dismissal must relist")
        self.assertEqual(entry["status"], "waiting")

    def test_the_later_of_the_two_dismissals_wins(self):
        """Either identity may silence the row; neither may un-silence it."""
        marks = {("chat", CHAT_ID): {"dismissed_at": "2026-08-31T19:06:28Z"}}
        entry = app.classify_chat(
            chat={"id": CHAT_ID, "title": "cweb2", "session_id": SESSION_ID},
            last={"role": "assistant", "created_at": "2026-08-31T19:12:04Z",
                  "preview": "Let me study the existing test files."},
            live_ids=frozenset(), queued={}, marks=marks,
            cli_status_map={SESSION_ID: "waiting"},
            # Stale session dismissal: the chat's newer one has to win.
            cli_dismiss_map={SESSION_ID: "2026-08-30T08:00:00Z"},
            cli_status_updated_map={SESSION_ID: "2026-08-31T19:00:00Z"},
        )
        self.assertNotEqual((entry or {}).get("status"), "waiting",
                            "an older session mark masked a newer chat one")

    def test_a_busy_session_is_not_waiting_at_all(self):
        """Busy means working; the deferred branch must not fire."""
        entry = classify(marks={}, status_updated="2026-08-31T19:00:00Z",
                         cli_status="busy")
        self.assertNotEqual((entry or {}).get("status"), "waiting")

    def test_a_session_with_no_status_timestamp_can_still_be_dismissed(self):
        """Every non-busy session on this machine has no status_updated_at.

        The guard required one, so `dismissed and status_updated and ...` was
        falsy and the branch relisted unconditionally. The dismissal could not
        suppress anything, whichever mark it read -- which is why fixing the
        mark alone left the row on screen.
        """
        marks = {("chat", CHAT_ID): {"dismissed_at": "2026-08-31T19:15:00Z"}}
        entry = classify(marks=marks, status_updated="")
        self.assertNotEqual(
            (entry or {}).get("status"), "waiting",
            "with no session timestamp the dismissal was ignored entirely",
        )

    def test_activity_after_the_dismissal_still_relists_it(self):
        """The fallback must not turn dismissal into a permanent mute."""
        marks = {("chat", CHAT_ID): {"dismissed_at": "2026-08-31T19:00:00Z"}}
        # `last` is stamped 19:12:04, i.e. after the dismissal.
        entry = classify(marks=marks, status_updated="")
        self.assertEqual((entry or {}).get("status"), "waiting")


if __name__ == "__main__":
    unittest.main()
