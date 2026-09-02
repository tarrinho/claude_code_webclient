"""Storage for the per-chat auto-answer knob and its rolling log.

Design: docs/superpowers/specs/2026-09-02-auto-answer-knob-design.md

This is step 1 of that design -- the two columns and their helpers, with no
watcher and no routes yet. It is deliberately the uncontended piece: `db.py` is
on the file-structure reorganisation's leave-alone list, so it can land while
`app.py` is being split.

Two properties carry the most weight here, because both fail silently:

  * the knob is **owner-scoped**. It arms an automatic approver, so a write that
    ignored `owner_id` would let one user turn on silent approval of permission
    prompts in another user's chat. Five `db.py` functions once accepted
    `owner_id` and never used it (fixed in ec548d1); this is the shape of thing
    that regression would have made dangerous rather than merely wrong.
  * the log is **capped and ordered**. Uncapped, a long-running chat grows a
    column without bound. Mis-ordered, the tooltip shows the ten oldest
    approvals instead of the ten that just happened, which is worse than
    showing nothing because it reads as current.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import auth
import config
import db


class AutoAnswerStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

        await db.user_create("alice", None, auth.hash_password("pw-alice"))
        await db.user_create("bob", None, auth.hash_password("pw-bob"), role="user")
        await db.chat_create("c1", "Alice's", None, f"{self.tmp.name}/p", "alice")

    # ── the knob ────────────────────────────────────────────────────────

    async def test_it_is_off_by_default(self):
        """Nothing arms an automatic approver implicitly."""
        self.assertFalse(await db.chat_auto_answer_get("c1", "alice"))

    async def test_the_owner_can_turn_it_on_and_off(self):
        self.assertTrue(await db.chat_auto_answer_set("c1", "alice", True))
        self.assertTrue(await db.chat_auto_answer_get("c1", "alice"))
        self.assertTrue(await db.chat_auto_answer_set("c1", "alice", False))
        self.assertFalse(await db.chat_auto_answer_get("c1", "alice"))

    async def test_another_user_cannot_arm_it(self):
        """The security property. A cross-owner write must not land."""
        self.assertFalse(await db.chat_auto_answer_set("c1", "bob", True))
        self.assertFalse(
            await db.chat_auto_answer_get("c1", "alice"),
            "bob's write reached alice's chat",
        )

    async def test_another_user_cannot_read_it(self):
        await db.chat_auto_answer_set("c1", "alice", True)
        self.assertFalse(await db.chat_auto_answer_get("c1", "bob"))

    async def test_an_unknown_chat_is_not_an_error(self):
        """The watcher polls whatever the list gave it; a chat can be deleted
        between the read and the write, and that is not exceptional.
        """
        self.assertFalse(await db.chat_auto_answer_set("nope", "alice", True))
        self.assertFalse(await db.chat_auto_answer_get("nope", "alice"))

    # ── the rolling log ─────────────────────────────────────────────────

    async def test_the_log_starts_empty(self):
        self.assertEqual(await db.chat_auto_answer_log_get("c1", "alice"), [])

    async def test_an_answer_is_recorded(self):
        await db.chat_auto_answer_log_append("c1", {
            "kind": "Permission",
            "prompt": "Permission rule Bash(curl*) requires confirmation",
            "index": 1,
            "label": "Yes",
            "outcome": "answered",
        })
        log = await db.chat_auto_answer_log_get("c1", "alice")
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["label"], "Yes")
        self.assertEqual(log[0]["outcome"], "answered")
        self.assertTrue(log[0]["at"], "every entry needs a timestamp")

    async def test_a_skip_is_recorded_with_its_reason(self):
        """Skips are the entries worth having: they are the prompts still
        waiting for a person.
        """
        await db.chat_auto_answer_log_append("c1", {
            "kind": "Permission",
            "prompt": "Do you want to proceed?",
            "outcome": "skipped",
            "reason": "no unambiguously affirmative option",
        })
        entry = (await db.chat_auto_answer_log_get("c1", "alice"))[0]
        self.assertEqual(entry["outcome"], "skipped")
        self.assertIn("affirmative", entry["reason"])

    async def test_newest_first(self):
        for i in range(3):
            await db.chat_auto_answer_log_append("c1", {"label": f"e{i}"})
        self.assertEqual(
            [e["label"] for e in await db.chat_auto_answer_log_get("c1", "alice")],
            ["e2", "e1", "e0"],
        )

    async def test_it_caps_at_ten_and_drops_the_oldest(self):
        for i in range(14):
            await db.chat_auto_answer_log_append("c1", {"label": f"e{i}"})
        log = await db.chat_auto_answer_log_get("c1", "alice")
        self.assertEqual(len(log), 10, "the log must not grow without bound")
        self.assertEqual(log[0]["label"], "e13", "newest kept")
        self.assertEqual(log[-1]["label"], "e4", "oldest dropped")

    async def test_another_user_cannot_read_the_log(self):
        """It quotes prompt text out of someone else's session."""
        await db.chat_auto_answer_log_append("c1", {"label": "Yes"})
        self.assertEqual(await db.chat_auto_answer_log_get("c1", "bob"), [])

    async def test_a_damaged_log_reads_as_empty(self):
        """Hand-edited or half-written JSON must not break the chat view. The
        column is written by this process alone, but a crash mid-write and a
        manual fix are both possible, and a tooltip is not worth a 500.
        """
        await db.db_conn.execute(
            "UPDATE chats SET auto_answer_log = ? WHERE id = ?", ("{not json", "c1"))
        await db.db_conn.commit()
        self.assertEqual(await db.chat_auto_answer_log_get("c1", "alice"), [])

    # ── the enabled-chat query the watcher will poll ────────────────────

    async def test_only_enabled_chats_are_listed(self):
        await db.chat_create("c2", "Second", None, f"{self.tmp.name}/p", "alice")
        await db.chat_set_session("c2", "sess-c2")
        await db.chat_auto_answer_set("c2", "alice", True)
        rows = await db.chats_with_auto_answer()
        self.assertEqual([r["id"] for r in rows], ["c2"])
        self.assertEqual(rows[0]["session_id"], "sess-c2")

    async def test_a_chat_with_no_session_is_not_listed(self):
        """Answering needs a session id to locate the terminal, so a chat
        without one can never be answered and polling it every tick would be
        pure cost.
        """
        await db.chat_auto_answer_set("c1", "alice", True)
        self.assertEqual(await db.chats_with_auto_answer(), [])

    async def test_a_deleted_chat_is_not_listed(self):
        await db.chat_set_session("c1", "sess-c1")
        await db.chat_auto_answer_set("c1", "alice", True)
        await db.chat_delete("c1", "alice")
        self.assertEqual(await db.chats_with_auto_answer(), [])


if __name__ == "__main__":
    unittest.main()
