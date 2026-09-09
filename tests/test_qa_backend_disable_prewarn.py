"""QA: a Disable that would be refused is greyed out, not clicked and 409'd.

Pedro reported this as an exception in the browser console:

    PATCH .../api/machines/eca777e... 409 (Conflict)
        apiFetch@api.js:27
        _setMachineEnabled@machines.js:614
        (anonymous)@machines.js:460

Nothing threw. `apiFetch` raises only on 401/303, and `_setMachineEnabled`
handles the 409 and shows the server's message. Those three frames are the
initiator stack the browser attaches to its *own* network log, which it writes
for any non-2xx response however completely the handler deals with it. It
cannot be suppressed from JavaScript.

So the fix is not in the handler. The refusal was entirely predictable -- one
conversation was pinned to that backend -- and the request should never have
been sent. The default-backend case was already pre-empted this way; the pinned
case was not, because the list payload carried no pin count. It does now.

The 409 path is deliberately kept and is still tested by
test_qa_backend_disable_route.py: this check is derived from an earlier load, so
a conversation pinned in another tab between load and click leaves the button
live, and that genuine race is what a 409 is for.
"""
from __future__ import annotations

import importlib
import os
import re
import tempfile
import unittest
from pathlib import Path

from tests.testing_model import TESTING_MODEL

ROOT = Path(__file__).resolve().parents[1]
MACHINES_JS = ROOT / "web" / "assets" / "machines.js"


class PinnedCountsQueryTests(unittest.IsolatedAsyncioTestCase):
    """db.chats_pinned_counts: one GROUP BY answering for every backend."""

    async def asyncSetUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wc-prewarn-")) / "wc.db"
        os.environ["WC_DB_PATH"] = str(tmp)
        os.environ["WC_SESSION_DB_PATH"] = str(tmp.parent / "sessions.db")
        os.environ.setdefault("WC_PROXY_ENABLED", "0")
        import config

        importlib.reload(config)
        import db

        db.config = config
        self.db = db
        await db.init()
        for mid, name in (("m-1", "One"), ("m-2", "Two")):
            await db.ai_machine_create(
                mid, name, "api.anthropic.com", 443, None, TESTING_MODEL,
                "https://api.anthropic.com", None, "admin",
                provider="claude_code",
            )

    async def asyncTearDown(self):
        await self.db.close()

    async def _chat(self, chat_id: str, machine_id: str | None, owner="admin"):
        await self.db.chat_create(
            chat_id, f"chat {chat_id}", None, f"/tmp/{chat_id}", owner)
        if machine_id:
            await self.db.chat_set_machine(chat_id, owner, machine_id)
        return chat_id

    async def test_a_machine_with_no_pins_is_absent_not_zero(self):
        """Callers read this with .get(id, 0); absent keeps the query small."""
        counts = await self.db.chats_pinned_counts("admin")
        self.assertEqual(counts, {})

    async def test_it_counts_per_machine(self):
        await self._chat("c1", "m-1")
        await self._chat("c2", "m-1")
        await self._chat("c3", "m-2")
        counts = await self.db.chats_pinned_counts("admin")
        self.assertEqual(counts.get("m-1"), 2)
        self.assertEqual(counts.get("m-2"), 1)

    async def test_an_unpinned_chat_is_counted_against_nothing(self):
        """`ai_machine_id IS NOT NULL` in the WHERE: without it, every chat with
        no backend would group under a single NULL key and be reported as a
        machine's pin count by any caller that did not special-case it."""
        await self._chat("c1", None)
        counts = await self.db.chats_pinned_counts("admin")
        self.assertNotIn(None, counts)
        self.assertEqual(counts, {})

    async def test_it_is_scoped_by_owner(self):
        await self._chat("c1", "m-1", owner="admin")
        await self._chat("c2", "m-1", owner="someone-else")
        self.assertEqual((await self.db.chats_pinned_counts("admin")).get("m-1"), 1)

    async def test_a_deleted_conversation_stops_counting(self):
        """chat_delete writes a tombstone rather than removing the row. A
        conversation that is not coming back must not keep a backend shelved."""
        await self._chat("c1", "m-1")
        await self.db.chat_delete("c1", "admin")
        self.assertEqual(await self.db.chats_pinned_counts("admin"), {})

    async def test_it_agrees_with_the_refusal_query(self):
        """The load-bearing one. `chats_pinned_counts` decides whether the
        button is clickable and `chats_pinned_to_machine` decides whether the
        server refuses. If the two apply different inclusion rules, the user is
        either blocked from something that would have worked or invited to do
        something that will not."""
        await self._chat("c1", "m-1")
        await self._chat("c2", "m-1")
        await self._chat("c3", "m-1")
        await self.db.chat_delete("c3", "admin")
        counts = await self.db.chats_pinned_counts("admin")
        named = await self.db.chats_pinned_to_machine("m-1", "admin")
        self.assertEqual(counts.get("m-1", 0), named["total"])


class ListPayloadTests(unittest.IsolatedAsyncioTestCase):
    """GET /api/machines must carry the count the button needs."""

    async def test_the_listing_reports_pinned_total_per_machine(self):
        from unittest.mock import AsyncMock, patch

        from routes import machines as mr

        machines = [
            {"id": "m-1", "name": "One", "provider": "claude_code",
             "host": "api.anthropic.com", "base_url": "https://api.anthropic.com",
             "api_key": "secret-should-not-appear", "active": 1, "enabled": 1},
            {"id": "m-2", "name": "Two", "provider": "claude_code",
             "host": "api.anthropic.com", "base_url": "https://api.anthropic.com",
             "api_key": None, "active": 0, "enabled": 1},
        ]
        request = type("R", (), {})()
        request.state = type("S", (), {"session": {"user": "admin"}})()

        with patch.object(mr.db, "ai_machine_seed_anthropic", AsyncMock()), \
                patch.object(mr.db, "ai_machines_list",
                             AsyncMock(return_value=machines)), \
                patch.object(mr.db, "chats_pinned_counts",
                             AsyncMock(return_value={"m-1": 3})):
            response = await mr.handle_machines_list(request)

        import json

        body = json.loads(bytes(response.body))
        by_id = {m["id"]: m for m in body["machines"]}
        self.assertEqual(by_id["m-1"]["pinned_total"], 3)
        self.assertEqual(by_id["m-2"]["pinned_total"], 0,
                         "a machine absent from the counts must report 0")
        self.assertNotIn("api_key", by_id["m-1"],
                         "the listing must still not leak credentials")


class DisableObstacleTests(unittest.TestCase):
    """The client-side check, read from the source.

    Asserted against the text rather than executed: this repo has no JS test
    runner, and the property that matters is that both refusal reasons are
    consulted here. A check that only reads `active` is the defect.
    """

    def setUp(self):
        source = MACHINES_JS.read_text(encoding="utf-8")
        match = re.search(
            r"function _disableObstacle\(m\)\s*\{(.*?)\n\}", source, re.DOTALL)
        self.assertIsNotNone(match, "_disableObstacle not found in machines.js")
        self.body = match.group(1)

    def test_it_checks_the_default_flag(self):
        self.assertIn("m.active", self.body)

    def test_it_checks_the_pinned_count(self):
        """The regression. Without this the button accepts a click on a pinned
        backend and the refusal arrives as a console-logged 409."""
        self.assertIn("pinned_total", self.body)

    def test_the_pinned_message_is_singular_for_one(self):
        """'1 conversations are pinned' is the kind of detail that makes a
        message read as machine output rather than an explanation."""
        self.assertIn("One conversation is pinned", self.body)

    def test_the_button_is_disabled_when_there_is_an_obstacle(self):
        """_disableObstacle is only useful if its return value gates the
        button; a caller that ignored it would leave every assertion above
        passing against nothing."""
        source = MACHINES_JS.read_text(encoding="utf-8")
        match = re.search(
            r"_disableObstacle\(m\);(.*?)\}", source, re.DOTALL)
        self.assertIsNotNone(match)
        self.assertIn("disabled = true", match.group(1))
        self.assertIn("title", match.group(1),
                      "the reason must reach the user, not just grey it out")


class NineOhNineIsStillHandledTests(unittest.TestCase):
    """Pre-warning does not replace the server refusal.

    The check is derived from a list loaded earlier, so a conversation pinned
    in another tab between load and click leaves the button live. That race is
    what the 409 is for, and removing its handler would turn a rare correct
    refusal into an unexplained failure.
    """

    def test_set_machine_enabled_still_reads_the_409_body(self):
        source = MACHINES_JS.read_text(encoding="utf-8")
        match = re.search(
            r"export async function _setMachineEnabled\(.*?\n\}",
            source, re.DOTALL)
        self.assertIsNotNone(match)
        body = match.group(0)
        self.assertIn("409", body)
        self.assertIn("data.error", body)


if __name__ == "__main__":
    unittest.main()
