"""QA: identity is resolved once, where it enters -- not at the write.

`chat_create` rejects a login name as owner_id (344cf00). `session["user"]`
has been the user's *id* since login switched from `user["name"]`, but
sessions are persisted and survive restarts, so one minted before that fix
carries the name for its whole TTL.

Two endpoints write a chat. The resume endpoint had a name-to-id translation
inline and survived; `handle_chat_create` did not and answered 500 on the same
session. That asymmetry is what these tests pin: one helper answers "who owns
this?", both writers use it, and the write layer keeps rejecting what it
cannot store.

Deliberately not tested here: that the guard exists. It does, unchanged, and
`test_the_write_layer_still_rejects_a_raw_name` states that this helper does
not weaken it -- translating is not validating.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import auth
import config
import db
from shared import owner_of


class OwnerOfTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._db = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self._root = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self._db.start()
        self._root.start()
        self.addCleanup(self._db.stop)
        self.addCleanup(self._root.stop)
        await db.init()
        # Registered immediately, so a failure below still closes the
        # connection: aiosqlite's worker is non-daemon and an unclosed one
        # blocks interpreter shutdown. See the fixture in tests/conftest.py.
        self.addAsyncCleanup(db.close)
        await db.user_create("admin", None, auth.hash_password("x"), role="admin")
        self.admin_id = (await db.user_get_by_name("admin"))["id"]

    async def test_a_legacy_name_session_resolves_to_the_real_id(self):
        """The case that produced the 500."""
        self.assertEqual(await owner_of({"user": "admin"}), self.admin_id)

    async def test_an_id_is_returned_untouched(self):
        self.assertEqual(await owner_of({"user": self.admin_id}), self.admin_id)

    async def test_an_id_costs_no_database_lookup(self):
        """The normal path is one regex. A query per chat write would be a
        silent cost paid on every request for a case that no longer happens."""
        with patch.object(db, "user_get_by_name") as lookup:
            await owner_of({"user": self.admin_id})
        lookup.assert_not_called()

    async def test_an_unknown_name_is_passed_through_not_invented(self):
        """It translates when it can and otherwise returns what it was given:
        deciding a value is unacceptable belongs to the write, which is the
        only place that knows what it can store."""
        self.assertEqual(await owner_of({"user": "nobody"}), "nobody")

    async def test_empty_and_missing_sessions_do_not_raise(self):
        for session in ({"user": ""}, {}, None):
            with self.subTest(session=session):
                self.assertEqual(await owner_of(session), "")

    async def test_a_chat_can_be_created_through_a_legacy_name_session(self):
        """End to end: the write that used to raise ValueError now lands."""
        owner = await owner_of({"user": "admin"})
        await db.chat_create("c1", "Chat", None, f"{self.tmp.name}/p", owner)
        chat = await db.chat_get("c1", self.admin_id)
        self.assertIsNotNone(chat)
        self.assertEqual(chat["owner_id"], self.admin_id)

    async def test_the_write_layer_still_rejects_a_raw_name(self):
        """Translating must not become a way to smuggle a name into the table.
        Six chats owned by the literal strings 'admin' and 'pedro' were
        repaired on 2026-09-12; the guard is what stops more being written."""
        with self.assertRaises(ValueError):
            await db.chat_create("c2", "Chat", None, f"{self.tmp.name}/p", "admin")


if __name__ == "__main__":
    unittest.main()
