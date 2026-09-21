"""QA: the chat_subagents table — schema and its idempotency guarantee."""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import db


class _SubagentDbFixture:
    """Database fixture shared with the later subagent test classes.

    Deliberately NOT a TestCase: a class that subclasses one re-runs its
    tests inside every subclass, so the schema cases below would execute
    again for each of the accessor and retention suites.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for attr, value in (("DB_PATH", f"{self.tmp.name}/db"),
                            ("PROJECTS_ROOT", f"{self.tmp.name}/p")):
            patcher = patch.object(config, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        await db.init()
        self.addAsyncCleanup(db.close)


class ChatSubagentsSchemaTests(_SubagentDbFixture, unittest.IsolatedAsyncioTestCase):
    async def test_the_table_exists_with_every_column(self):
        cur = await db.db_conn.execute("PRAGMA table_info(chat_subagents)")
        cols = {r["name"] for r in await cur.fetchall()}
        self.assertEqual(cols, {
            "id", "chat_id", "tool_use_id", "agent_type", "description",
            "status", "started_at", "ended_at",
        })

    async def test_the_same_tool_use_id_cannot_be_recorded_twice(self):
        """The idempotency guarantee. A transcript is re-scanned for reasons
        unrelated to this feature, so a second scan must be a no-op."""
        for _ in range(2):
            await db.db_conn.execute(
                "INSERT OR IGNORE INTO chat_subagents "
                "(chat_id, tool_use_id, agent_type, description, status, started_at) "
                "VALUES ('c1', 'tu_1', 'code-review', 'd', 'running', '2026-09-21T00:00:00Z')")
        await db.db_conn.commit()
        cur = await db.db_conn.execute("SELECT COUNT(*) FROM chat_subagents")
        self.assertEqual((await cur.fetchone())[0], 1)

    async def test_the_same_tool_use_id_in_another_chat_is_a_separate_row(self):
        for chat in ("c1", "c2"):
            await db.db_conn.execute(
                "INSERT OR IGNORE INTO chat_subagents "
                "(chat_id, tool_use_id, agent_type, description, status, started_at) "
                f"VALUES ('{chat}', 'tu_1', 'x', 'd', 'running', '2026-09-21T00:00:00Z')")
        await db.db_conn.commit()
        cur = await db.db_conn.execute("SELECT COUNT(*) FROM chat_subagents")
        self.assertEqual((await cur.fetchone())[0], 2)


class SubagentAccessorTests(_SubagentDbFixture, unittest.IsolatedAsyncioTestCase):
    """Uses its own base classes, not ChatSubagentsSchemaTests: subclassing a
    TestCase re-runs its tests inside every subclass, so the schema cases
    above would otherwise execute again here too."""

    async def test_record_then_read_back_grouped_by_chat(self):
        from routes.db_subagents import subagent_record, subagents_for_chats
        await subagent_record("c1", [
            {"tool_use_id": "tu_2", "agent_type": "test-writer",
             "description": "write tests", "status": "running",
             "started_at": "2026-09-21T00:00:02Z", "ended_at": None},
            {"tool_use_id": "tu_1", "agent_type": "code-review",
             "description": "review", "status": "done",
             "started_at": "2026-09-21T00:00:01Z",
             "ended_at": "2026-09-21T00:00:09Z"},
        ])
        got = await subagents_for_chats(["c1"])
        self.assertEqual([r["tool_use_id"] for r in got["c1"]], ["tu_1", "tu_2"],
                         "must be ordered by started_at, not insertion order")
        self.assertEqual(got["c1"][0]["status"], "done")

    async def test_recording_the_same_rows_twice_changes_nothing(self):
        from routes.db_subagents import subagent_record, subagents_for_chats
        row = {"tool_use_id": "tu_1", "agent_type": "x", "description": "d",
               "status": "running", "started_at": "2026-09-21T00:00:01Z",
               "ended_at": None}
        await subagent_record("c1", [row])
        await subagent_record("c1", [row])
        got = await subagents_for_chats(["c1"])
        self.assertEqual(len(got["c1"]), 1)

    async def test_a_rescan_promotes_running_to_done(self):
        """The tool_result arrives in a later record, so the second scan of the
        same transcript must be able to finish a row it already inserted."""
        from routes.db_subagents import subagent_record, subagents_for_chats
        await subagent_record("c1", [
            {"tool_use_id": "tu_1", "agent_type": "x", "description": "d",
             "status": "running", "started_at": "2026-09-21T00:00:01Z",
             "ended_at": None}])
        await subagent_record("c1", [
            {"tool_use_id": "tu_1", "agent_type": "x", "description": "d",
             "status": "done", "started_at": "2026-09-21T00:00:01Z",
             "ended_at": "2026-09-21T00:00:09Z"}])
        got = await subagents_for_chats(["c1"])
        self.assertEqual(len(got["c1"]), 1)
        self.assertEqual(got["c1"][0]["status"], "done")
        self.assertEqual(got["c1"][0]["ended_at"], "2026-09-21T00:00:09Z")

    async def test_an_empty_row_list_is_a_no_op(self):
        from routes.db_subagents import subagent_record, subagents_for_chats
        await subagent_record("c1", [])
        self.assertEqual(await subagents_for_chats(["c1"]), {})

    async def test_an_empty_chat_id_list_queries_nothing(self):
        from routes.db_subagents import subagents_for_chats
        self.assertEqual(await subagents_for_chats([]), {})

    async def test_only_the_requested_chats_come_back(self):
        from routes.db_subagents import subagent_record, subagents_for_chats
        for chat in ("c1", "c2"):
            await subagent_record(chat, [
                {"tool_use_id": "tu_1", "agent_type": "x", "description": "d",
                 "status": "running", "started_at": "2026-09-21T00:00:01Z",
                 "ended_at": None}])
        got = await subagents_for_chats(["c1"])
        self.assertEqual(set(got), {"c1"})

    async def test_a_done_row_is_never_reopened(self):
        """The UPDATE is narrowed to running -> done on purpose.

        A transcript is re-scanned from the start, so an older region gets read
        again after a subagent has finished. Without the `status != 'done'`
        guard that re-read would write `running` back over a completed row, and
        the sidebar would show finished work as still in flight -- with no
        second source to correct it, because the transcript is the only record.
        """
        from routes.db_subagents import subagent_record, subagents_for_chats
        done = {"tool_use_id": "tu_1", "agent_type": "x", "description": "d",
                "status": "done", "started_at": "2026-09-21T00:00:01Z",
                "ended_at": "2026-09-21T00:00:09Z"}
        await subagent_record("c1", [done])
        # The same subagent as an earlier scan saw it: still running, no end.
        await subagent_record("c1", [{**done, "status": "running",
                                      "ended_at": None}])
        got = await subagents_for_chats(["c1"])
        self.assertEqual(got["c1"][0]["status"], "done",
                         "a re-scan must not un-finish completed work")
        self.assertEqual(got["c1"][0]["ended_at"], "2026-09-21T00:00:09Z",
                         "and must not clear the end time either")


class CaptureOrderingTests(unittest.TestCase):
    """The capture call's placement, asserted on source.

    Placement is the whole property here: the comment already in this block
    says a failure must never risk the turn's transcript write, because that
    is the primary artifact and a subagent row is a secondary index. A
    behavioural test cannot see ordering; this can.
    """

    SOURCE = __import__("pathlib").Path("routes/chats.py").read_text()

    def test_capture_runs_after_the_transcript_write(self):
        batch = self.SOURCE.index("await db.messages_batch(")
        capture = self.SOURCE.index("await db.subagent_record(")
        self.assertLess(batch, capture,
                        "subagent capture must not precede messages_batch")

    def test_capture_sits_with_the_other_secondary_index(self):
        """Next to generated_image_record, which is the same kind of thing
        and already carries the rule in a comment."""
        images = self.SOURCE.index("await db.generated_image_record(")
        capture = self.SOURCE.index("await db.subagent_record(")
        self.assertLess(images, capture,
                        "capture must follow the image record, not precede it")
        self.assertLess(abs(images - capture), 1200,
                        "the two secondary indexes should stay adjacent")


class SubagentRetentionTests(_SubagentDbFixture, unittest.IsolatedAsyncioTestCase):
    """Uses the plain mixin, not ChatSubagentsSchemaTests: subclassing a
    TestCase re-runs its tests inside every subclass, so the schema cases
    would otherwise execute again here too."""

    async def test_deleting_a_chat_removes_its_subagents(self):
        """A subagent row's only lifetime is its chat's. There is no
        independent expiry: a row is small, and 'probably dead' is a guess."""
        import pathlib
        from routes.db_subagents import subagent_record, subagents_for_chats
        pathlib.Path(f"{self.tmp.name}/p").mkdir(parents=True, exist_ok=True)
        await db.user_create("bob", None, "x")
        owner = (await db.user_get_by_name("bob"))["id"]
        await db.chat_create("c1", "C", None, f"{self.tmp.name}/p", owner)
        await subagent_record("c1", [
            {"tool_use_id": "tu_1", "agent_type": "x", "description": "d",
             "status": "running", "started_at": "2026-09-21T10:00:00Z",
             "ended_at": None}])
        await db.chat_delete("c1", owner)
        self.assertEqual(await subagents_for_chats(["c1"]), {})
