"""QA: naming the conversations that pin a backend.

A refusal that says "8 conversations are pinned" sends the reader hunting
through the sidebar for which eight. Naming them is the whole value, so the
shape of this helper is asserted rather than left to the caller.

Design: docs/superpowers/specs/2026-09-08-default-and-enabled-backends-design.md
"""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path


class PinnedChatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wc-pins-")) / "wc.db"
        os.environ["WC_DB_PATH"] = str(tmp)
        os.environ["WC_SESSION_DB_PATH"] = str(tmp.parent / "sessions.db")
        os.environ.setdefault("WC_PROXY_ENABLED", "0")
        import config

        importlib.reload(config)
        import db

        db.config = config
        self.db = db
        await db.init()
        await db.ai_machine_create(
            "m-1", "Gateway", "gw.example.com", 443, None, "vllm/x",
            "https://gw.example.com", None, "admin", provider="claude_code",
        )

    async def asyncTearDown(self):
        await self.db.close()

    async def _chat(self, chat_id: str, title: str, machine: str | None):
        # chat_set_machine's real signature is (chat_id, owner_id, machine_id)
        # -- owner second, machine third. Transposing them silently pins the
        # chat to a machine id of "admin", which resolves to nothing and makes
        # every assertion below pass for the wrong reason.
        await self.db.chat_create(chat_id, title, None, "/tmp", "admin")
        if machine:
            await self.db.chat_set_machine(chat_id, "admin", machine)

    async def test_no_pins_reports_zero(self):
        result = await self.db.chats_pinned_to_machine("m-1", "admin")
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["titles"], [])

    async def test_pins_are_named(self):
        await self._chat("c1", "cweb2", "m-1")
        await self._chat("c2", "voice test", "m-1")
        await self._chat("c3", "unpinned", None)
        result = await self.db.chats_pinned_to_machine("m-1", "admin")
        self.assertEqual(result["total"], 2)
        self.assertEqual(set(result["titles"]), {"cweb2", "voice test"})
        self.assertEqual(set(result["ids"]), {"c1", "c2"})

    async def test_the_list_is_capped_but_the_total_is_not(self):
        """40 pins must not produce a 40-line error message."""
        for i in range(12):
            await self._chat(f"c{i}", f"chat {i}", "m-1")
        result = await self.db.chats_pinned_to_machine("m-1", "admin", limit=8)
        self.assertEqual(result["total"], 12)
        self.assertEqual(len(result["titles"]), 8)

    async def test_it_is_scoped_by_owner(self):
        await self._chat("c1", "mine", "m-1")
        result = await self.db.chats_pinned_to_machine("m-1", "someone-else")
        self.assertEqual(result["total"], 0)

    async def test_an_archived_chat_still_counts(self):
        """Archived is not deleted: it can be restored and would then be
        pinned to a backend that had been shelved underneath it."""
        await self._chat("c1", "archived one", "m-1")
        await self.db.chat_archive("c1", "admin", 1)
        result = await self.db.chats_pinned_to_machine("m-1", "admin")
        self.assertEqual(result["total"], 1)

    async def test_a_deleted_chat_does_not_count(self):
        """deleted_at is a tombstone; that conversation is not coming back."""
        await self._chat("c1", "deleted one", "m-1")
        await self.db.chat_delete("c1", "admin")
        result = await self.db.chats_pinned_to_machine("m-1", "admin")
        self.assertEqual(result["total"], 0)


if __name__ == "__main__":
    unittest.main()
