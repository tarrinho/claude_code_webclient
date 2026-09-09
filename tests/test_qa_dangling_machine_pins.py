"""QA: a deleted backend must not leave conversations pinned to it.

`chats.ai_machine_id` pins a conversation to one backend, and this schema has
no foreign keys, so `ai_machine_delete` used to remove the machine row and
leave every chat naming it pointing at an id that resolves to nothing. The
frontend's `ensurePinnedModels` asks `/api/models?machine_id=<pin>` whenever
such a conversation is opened, and the server answers 404 "Machine not found".

Found live rather than by reading: two 404s in the production log after a
deploy, traced to five chats all pinned to the same deleted machine, each
logging an error every time it was opened.

Two halves, one test class each: `ai_machine_delete` unpins at the source, and
`_clear_dangling_machine_pins` repairs rows written before that existed. Both
are needed -- the repair alone would let new ones appear, and the source fix
alone would leave the five already there.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import db
from tests.testing_model import TESTING_MODEL


async def _sandbox(tc):
    """Own database and projects root. Registry #41: never db.init() the real one."""
    tc.tmp = tempfile.TemporaryDirectory()
    tc._db = patch.object(config, "DB_PATH", f"{tc.tmp.name}/db")
    tc._root = patch.object(config, "PROJECTS_ROOT", f"{tc.tmp.name}/projects")
    tc._db.start()
    tc._root.start()
    await db.init()


async def _teardown(tc):
    await db.close()
    tc._db.stop()
    tc._root.stop()
    tc.tmp.cleanup()


async def _machine(machine_id: str, owner: str = "admin") -> None:
    await db.ai_machine_create(
        machine_id, f"Box {machine_id}", "gateway.example.com", 443, None,
        TESTING_MODEL, "https://gateway.example.com", None, owner,
        provider="claude_code",
    )


async def _chat(chat_id: str, machine_id: str | None, owner: str = "admin") -> None:
    await db.chat_create(chat_id, f"chat {chat_id}", None, "/tmp", owner)
    if machine_id is not None:
        await db.chat_update(chat_id, owner, ai_machine_id=machine_id)


async def _pin_of(chat_id: str, owner: str = "admin") -> str | None:
    row = await db.chat_get(chat_id, owner)
    return None if row is None else row["ai_machine_id"]


class DeleteUnpinsChatsQA(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await _sandbox(self)

    async def asyncTearDown(self):
        await _teardown(self)

    async def test_deleting_a_machine_unpins_the_chats_that_named_it(self):
        await _machine("m-doomed")
        await _chat("c-pinned", "m-doomed")
        self.assertEqual(await _pin_of("c-pinned"), "m-doomed", "precondition")

        self.assertTrue(await db.ai_machine_delete("m-doomed", "admin"))

        self.assertIsNone(
            await _pin_of("c-pinned"),
            "the chat still names a machine that no longer exists -- every "
            "open of it will ask /api/models for that id and get a 404",
        )

    async def test_other_chats_keep_their_own_pins(self):
        """The unpin must be scoped to the deleted machine, not a blanket clear."""
        await _machine("m-doomed")
        await _machine("m-kept")
        await _chat("c-doomed", "m-doomed")
        await _chat("c-kept", "m-kept")

        await db.ai_machine_delete("m-doomed", "admin")

        self.assertIsNone(await _pin_of("c-doomed"))
        self.assertEqual(
            await _pin_of("c-kept"), "m-kept",
            "a delete cleared a pin belonging to a different, still-existing "
            "backend",
        )

    async def test_another_owners_pin_is_untouched(self):
        """ai_machine_delete is owner-scoped and the unpin must be too."""
        await _machine("m-shared", owner="admin")
        await _machine("m-shared-b", owner="bob")
        await _chat("c-bob", "m-shared-b", owner="bob")

        await db.ai_machine_delete("m-shared", "admin")

        self.assertEqual(
            await _pin_of("c-bob", owner="bob"), "m-shared-b",
            "another owner's chat lost its pin",
        )

    async def test_a_delete_that_matches_nothing_changes_no_pins(self):
        await _machine("m-real")
        await _chat("c-real", "m-real")

        self.assertFalse(await db.ai_machine_delete("m-never-existed", "admin"))

        self.assertEqual(
            await _pin_of("c-real"), "m-real",
            "a no-op delete must not clear an unrelated pin",
        )


class ClearDanglingPinsMigrationQA(unittest.IsolatedAsyncioTestCase):
    """The repair for rows written before the delete fix existed."""

    async def asyncSetUp(self):
        await _sandbox(self)

    async def asyncTearDown(self):
        await _teardown(self)

    async def test_a_pin_to_a_missing_machine_is_cleared(self):
        # Written straight to the table: the point is a row the current code
        # paths can no longer produce, which is what the production database
        # actually contained.
        await _chat("c-dangling", None)
        await db.db_conn.execute(
            "UPDATE chats SET ai_machine_id = 'm-vanished' WHERE id = 'c-dangling'"
        )
        await db.db_conn.commit()
        self.assertEqual(await _pin_of("c-dangling"), "m-vanished", "precondition")

        await db._clear_dangling_machine_pins()

        self.assertIsNone(await _pin_of("c-dangling"))

    async def test_a_valid_pin_survives_the_repair(self):
        await _machine("m-alive")
        await _chat("c-alive", "m-alive")

        await db._clear_dangling_machine_pins()

        self.assertEqual(
            await _pin_of("c-alive"), "m-alive",
            "the repair cleared a pin whose machine exists -- it must only "
            "touch ids that resolve to nothing",
        )

    async def test_running_it_twice_changes_nothing_further(self):
        """Idempotent: it runs on every startup, not once."""
        await _machine("m-alive")
        await _chat("c-alive", "m-alive")
        await _chat("c-dangling", None)
        await db.db_conn.execute(
            "UPDATE chats SET ai_machine_id = 'm-vanished' WHERE id = 'c-dangling'"
        )
        await db.db_conn.commit()

        await db._clear_dangling_machine_pins()
        await db._clear_dangling_machine_pins()

        self.assertIsNone(await _pin_of("c-dangling"))
        self.assertEqual(await _pin_of("c-alive"), "m-alive")

    async def test_init_applies_the_repair_on_startup(self):
        """It has to be wired into init(), not merely defined."""
        await _chat("c-dangling", None)
        await db.db_conn.execute(
            "UPDATE chats SET ai_machine_id = 'm-vanished' WHERE id = 'c-dangling'"
        )
        await db.db_conn.commit()

        # Re-running init() against the same sandboxed database is what a
        # restart does; the repair must fire from there.
        await db.init()

        self.assertIsNone(
            await _pin_of("c-dangling"),
            "init() did not apply the repair -- defining the function is not "
            "the same as calling it, and nothing else would ever run it",
        )


if __name__ == "__main__":
    unittest.main()
