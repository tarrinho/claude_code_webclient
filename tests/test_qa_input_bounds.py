"""Bounds and row-access on two paths that had neither.

Both fixes here are small; both were reachable from a normal request.

The orchestrator config blob had no ceiling at all, while the prompt beside it
was capped at 8000 and title and description were sliced to 200 and 500. It is
not the §2 subprocess boundary -- config is stored and read back, never passed
to the CLI -- so this is unbounded input rather than execution. The reason it
still matters is the asymmetry: every other field on the same two routes was
bounded, so the one that was not looks deliberate to a reader and is not.

`chat_get_question_ids` called `.get()` on a `sqlite3.Row`, which has no such
method, so the defensive form raised AttributeError on every call -- stricter
than the absence it was defending against, and it failed 11 tests across three
files once the surrounding feature started running.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import config
import db
from routes import supervisors as supervisor_routes


class SupervisorConfigBoundTests(unittest.TestCase):
    """The ceiling, and the 400 that replaces a 500."""

    def test_none_passes_through(self):
        self.assertIsNone(supervisor_routes._validated_supervisor_config(None))

    def test_a_small_config_is_accepted_unchanged(self):
        value = {"model": "claude-opus-5", "tasks": [1, 2, 3]}
        self.assertEqual(supervisor_routes._validated_supervisor_config(value), value)

    def test_a_config_at_the_limit_is_accepted(self):
        # Serialised length is what is measured, so build against that.
        padding = "x" * (supervisor_routes._SUPERVISOR_CONFIG_MAX - 20)
        value = {"k": padding}
        self.assertLessEqual(len(json.dumps(value)), supervisor_routes._SUPERVISOR_CONFIG_MAX)
        self.assertEqual(supervisor_routes._validated_supervisor_config(value), value)

    def test_an_oversized_config_is_rejected(self):
        value = {"k": "x" * (supervisor_routes._SUPERVISOR_CONFIG_MAX + 1000)}
        with self.assertRaises(HTTPException) as caught:
            supervisor_routes._validated_supervisor_config(value)
        self.assertEqual(caught.exception.status_code, 400)

    def test_an_unserialisable_config_is_a_400_not_a_500(self):
        """It used to reach json.dumps inside the DB layer, where the
        TypeError surfaced as a server error rather than a bad request."""
        with self.assertRaises(HTTPException) as caught:
            supervisor_routes._validated_supervisor_config({"when": object()})
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("serialisable", caught.exception.detail)

    def test_the_limit_is_below_the_upload_cap_and_above_a_real_config(self):
        """A sanity bound in both directions: large enough that a genuine plan
        fits, small enough to be a limit rather than a formality."""
        self.assertGreater(supervisor_routes._SUPERVISOR_CONFIG_MAX, 8 * 1024)
        self.assertLess(supervisor_routes._SUPERVISOR_CONFIG_MAX, config.MAX_UPLOAD_BYTES)


class QuestionIdsRowAccessTests(unittest.IsolatedAsyncioTestCase):
    """Reading a column off a sqlite3.Row, which has no .get()."""

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
        await db.chat_create("c1", "Ledger", None, f"{self.tmp.name}/p", "admin")

    async def asyncTearDown(self):
        await db.close()

    async def test_a_chat_with_no_question_ids_returns_empty(self):
        """The call that raised AttributeError. sqlite3.Row supports
        subscripting and keys() and has no .get() at all, so the defensive
        form failed on every chat rather than only on an unusual one."""
        self.assertEqual(await db.chat_get_question_ids("c1"), [])

    async def test_stored_ids_are_returned(self):
        await db.db_conn.execute(
            "UPDATE chats SET question_ids = ? WHERE id = ?",
            (json.dumps(["q1", "q2"]), "c1"),
        )
        await db.db_conn.commit()
        self.assertEqual(await db.chat_get_question_ids("c1"), ["q1", "q2"])

    async def test_an_unknown_chat_returns_empty(self):
        self.assertEqual(await db.chat_get_question_ids("nope"), [])

    async def test_a_corrupt_value_does_not_break_the_caller(self):
        """One unreadable column must not break the sync of every other
        conversation -- and must not do it silently either."""
        await db.db_conn.execute(
            "UPDATE chats SET question_ids = ? WHERE id = ?", ("{not json", "c1")
        )
        await db.db_conn.commit()
        with self.assertLogs("wc.db", level="WARNING") as logs:
            self.assertEqual(await db.chat_get_question_ids("c1"), [])
        self.assertTrue(any("question_ids" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
