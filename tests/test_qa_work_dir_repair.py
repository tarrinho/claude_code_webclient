"""QA: the stale-work_dir repair must actually write, not merely be called.

`_fix_stale_work_dir` shipped in f92464b as a *sync* function calling
`db.db_conn.execute(...).fetchone()`. `db.db_conn` is aiosqlite: `execute`
returns an awaitable `Result` with no `fetchone`, so the call raised, the
function's own `except Exception: return` swallowed it, and the repair silently
never happened. It left an un-awaited coroutine behind, which is the only trace
it produced -- a RuntimeWarning, surfaced by the remote suite run on
2026-09-12 rather than by any test, because there were none.

It had looked like it worked: the 72 rows repaired that day were fixed by a
separate direct-sqlite3 script, so the endpoint returning 200 was taken as
evidence for a function that had done nothing.

So these assert the *effect* on the row, never that the call returned. And
`test_no_coroutine_is_left_unawaited` is the one that fails against the
original: a sync body cannot await, so the warning fires.
"""
from __future__ import annotations

import os
import tempfile
import unittest
import warnings
from unittest.mock import patch

import auth
import config
import db
from routes.misc import _fix_stale_work_dir


class WorkDirRepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "p")
        os.makedirs(self.root, exist_ok=True)
        self._db = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self._root = patch.object(config, "PROJECTS_ROOT", self.root)
        self._db.start()
        self._root.start()
        self.addCleanup(self._db.stop)
        self.addCleanup(self._root.stop)
        await db.init()
        self.addAsyncCleanup(db.close)
        await db.user_create("u", None, auth.hash_password("x"), role="admin")
        self.owner = (await db.user_get_by_name("u"))["id"]

    async def _work_dir(self, chat_id: str) -> str:
        return (await db.chat_get(chat_id, self.owner))["work_dir"]

    async def test_a_work_dir_outside_the_root_is_rewritten(self):
        """The case runner.py rejects with "escapes PROJECTS_ROOT"."""
        await db.chat_create("c1", "Chat", None, "/tmp", self.owner)
        await _fix_stale_work_dir("c1")
        after = await self._work_dir("c1")
        self.assertNotEqual(after, "/tmp", "the repair did not write anything")
        self.assertTrue(after.startswith(self.root), after)
        self.assertTrue(os.path.isdir(after), "the new work_dir was not created")

    async def test_a_work_dir_inside_the_root_is_left_alone(self):
        """Repairing a healthy row would move a conversation for no reason."""
        good = os.path.join(self.root, "keepme")
        os.makedirs(good, exist_ok=True)
        await db.chat_create("c2", "Chat", None, good, self.owner)
        await _fix_stale_work_dir("c2")
        self.assertEqual(await self._work_dir("c2"), good)

    async def test_no_coroutine_is_left_unawaited(self):
        """Fails against the original: a sync body cannot await aiosqlite, so
        `Connection.execute` is created and dropped. That warning was the only
        evidence the repair was inert."""
        await db.chat_create("c3", "Chat", None, "/tmp", self.owner)
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            await _fix_stale_work_dir("c3")
        self.assertNotEqual(await self._work_dir("c3"), "/tmp")

    async def test_an_unknown_chat_is_not_an_error(self):
        """Called from the resume path for whatever row it found; a missing one
        must not take the request down."""
        await _fix_stale_work_dir("nosuchchat")  # must not raise


if __name__ == "__main__":
    unittest.main()
