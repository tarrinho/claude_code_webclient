"""The supervisor: which agents are waiting on the user.

It organises agents rather than doing work. "Waiting" means unread -- the agent
produced output after the last time the user looked at it, and is not mid-turn.
That definition is the whole feature: "finished at some point" would mark every
completed conversation forever, the badge would read twenty within a day, and a
number nobody believes is worse than no number.

Two things the tests hold firmly:
* Reading clears. The count has to go down when the user looks, or it is noise.
* Mid-turn is not waiting. A chat whose newest message is the user's own has a
  turn in flight; surfacing it as needing feedback would be backwards.

Covers:
* read_marks — round-trip, per-owner isolation, upsert on repeat.
* chat_last_activity — newest message per chat, owner-scoped.
* handle_supervisor — waiting vs working, unread arithmetic, archived skipped.
* CLI sessions — live sessions counted, webconsole shadows skipped.
* handle_supervisor_read — validation, and that it clears the badge.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import app
import auth
import config
import db

SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _request(body=None):
    return SimpleNamespace(
        method="GET",
        url=SimpleNamespace(path="/api/supervisor"),
        cookies={},
        headers={"accept": "*/*"},
        query_params={},
        client=SimpleNamespace(host="127.0.0.1"),
        state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
        json=AsyncMock(return_value=body if body is not None else {}),
    )


async def _setup(tc):
    td = tempfile.TemporaryDirectory()
    tc.tmpdir = td
    tc._db_patch = patch.object(config, "DB_PATH", f"{td.name}/db")
    tc._root_patch = patch.object(config, "PROJECTS_ROOT", f"{td.name}/projects")
    tc._db_patch.start()
    tc._root_patch.start()
    await db.init()
    await auth.bootstrap_admin()
    # No CLI sessions unless a test asks for them.
    tc._cli_patch = patch.object(db, "read_claude_sessions", AsyncMock(return_value=[]))
    tc._cli_patch.start()


async def _teardown(tc):
    tc._cli_patch.stop()
    await db.close()
    tc._db_patch.stop()
    tc._root_patch.stop()
    tc.tmpdir.cleanup()


async def _chat_with(chat_id, *messages, owner="admin", title="Work"):
    """messages: (role, created_at, content) newest last."""
    await db.chat_create(chat_id, title, None, "/tmp", owner)
    for role, created_at, content in messages:
        await db.db_conn.execute(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?,?,?,?)",
            (chat_id, role, content, created_at),
        )
    await db.db_conn.commit()


class ReadMarkTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup(self)

    async def asyncTearDown(self):
        await _teardown(self)

    async def test_round_trip(self):
        await db.read_mark_set("admin", "chat", "c1", "2026-08-29T10:00:00Z")
        marks = await db.read_marks_get("admin")
        self.assertEqual(marks[("chat", "c1")]["read_at"], "2026-08-29T10:00:00Z")

    async def test_repeat_updates_rather_than_duplicating(self):
        await db.read_mark_set("admin", "chat", "c1", "2026-08-29T10:00:00Z")
        await db.read_mark_set("admin", "chat", "c1", "2026-08-29T11:00:00Z")
        marks = await db.read_marks_get("admin")
        self.assertEqual(marks[("chat", "c1")]["read_at"], "2026-08-29T11:00:00Z")
        self.assertEqual(len(marks), 1)

    async def test_kinds_do_not_collide(self):
        """A chat and a session can share an id without sharing a mark."""
        await db.read_mark_set("admin", "chat", "x", "2026-08-29T10:00:00Z")
        await db.read_mark_set("admin", "session", "x", "2026-08-29T12:00:00Z")
        marks = await db.read_marks_get("admin")
        self.assertEqual(marks[("chat", "x")]["read_at"], "2026-08-29T10:00:00Z")
        self.assertEqual(marks[("session", "x")]["read_at"], "2026-08-29T12:00:00Z")

    async def test_marks_are_per_owner(self):
        await db.read_mark_set("admin", "chat", "c1", "2026-08-29T10:00:00Z")
        self.assertEqual(await db.read_marks_get("bob"), {})


class ChatLastActivityTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup(self)

    async def asyncTearDown(self):
        await _teardown(self)

    async def test_returns_the_newest_message_per_chat(self):
        await _chat_with(
            "c1",
            ("user", "2026-08-29T10:00:00Z", "first"),
            ("assistant", "2026-08-29T10:01:00Z", "second"),
        )
        activity = await db.chat_last_activity("admin")
        self.assertEqual(activity["c1"]["role"], "assistant")
        self.assertEqual(activity["c1"]["preview"], "second")

    async def test_chat_without_messages_is_absent(self):
        await db.chat_create("empty", "Empty", None, "/tmp", "admin")
        self.assertNotIn("empty", await db.chat_last_activity("admin"))

    async def test_owner_scoped(self):
        await _chat_with("c1", ("assistant", "2026-08-29T10:00:00Z", "hi"))
        self.assertEqual(await db.chat_last_activity("bob"), {})


class SupervisorChatTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup(self)

    async def asyncTearDown(self):
        await _teardown(self)

    async def _get(self):
        return json.loads((await app.handle_supervisor(_request())).body)

    async def test_unseen_assistant_reply_is_waiting(self):
        await _chat_with(
            "c1",
            ("user", "2026-08-29T10:00:00Z", "do it"),
            ("assistant", "2026-08-29T10:01:00Z", "Which way do you want it?"),
        )
        data = await self._get()
        self.assertEqual(data["counts"]["waiting"], 1)
        entry = data["waiting"][0]
        self.assertEqual(entry["kind"], "chat")
        self.assertEqual(entry["id"], "c1")
        self.assertEqual(entry["preview"], "Which way do you want it?")

    async def test_reading_does_not_retire_an_unanswered_question(self):
        """Opening a conversation is not answering it.

        Clearing on read let a question be dismissed by glancing at it, so the
        highlight vanished while the agent was still waiting.
        """
        await _chat_with(
            "c1",
            ("assistant", "2026-08-29T10:01:00Z", "Shall I continue?"),
        )
        self.assertEqual((await self._get())["counts"]["waiting"], 1)
        await db.read_mark_set("admin", "chat", "c1", "2026-08-29T10:02:00Z")
        self.assertEqual(
            (await self._get())["counts"]["waiting"], 1,
            "a question must stay listed until it is answered",
        )

    async def test_answering_retires_it(self):
        """Replying is what clears it -- the newest message stops being the
        agent's, so there is nothing outstanding."""
        await _chat_with(
            "c1",
            ("assistant", "2026-08-29T10:01:00Z", "Shall I continue?"),
        )
        self.assertEqual((await self._get())["counts"]["waiting"], 1)
        await db.db_conn.execute(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?,?,?,?)",
            ("c1", "user", "yes, go ahead", "2026-08-29T10:05:00Z"),
        )
        await db.db_conn.commit()
        self.assertEqual((await self._get())["counts"]["waiting"], 0)

    async def test_routine_output_is_still_retired_by_reading(self):
        """Only questions are sticky; seeing an update is the point of one."""
        await _chat_with("c1", ("assistant", "2026-08-29T10:01:00Z", "Done, all green."))
        self.assertEqual((await self._get())["counts"]["updated"], 1)
        await db.read_mark_set("admin", "chat", "c1", "2026-08-29T10:02:00Z")
        self.assertEqual((await self._get())["counts"]["updated"], 0)

    async def test_new_output_after_reading_waits_again(self):
        await _chat_with("c1", ("assistant", "2026-08-29T10:01:00Z", "Shall I continue?"))
        await db.read_mark_set("admin", "chat", "c1", "2026-08-29T10:02:00Z")
        await db.db_conn.execute(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?,?,?,?)",
            ("c1", "assistant", "One more thing -- shall I?", "2026-08-29T10:03:00Z"),
        )
        await db.db_conn.commit()
        self.assertEqual((await self._get())["counts"]["waiting"], 1)

    async def test_mid_turn_is_working_not_waiting(self):
        """Newest message is the user's own, so a turn is still in flight."""
        await _chat_with("c1", ("user", "2026-08-29T10:00:00Z", "do it"))
        data = await self._get()
        self.assertEqual(data["counts"]["waiting"], 0)
        self.assertEqual(data["counts"]["working"], 1)
        self.assertEqual(data["working"][0]["status"], "working")

    async def test_archived_chats_are_ignored(self):
        await _chat_with("c1", ("assistant", "2026-08-29T10:01:00Z", "Shall I continue?"))
        await db.chat_update("c1", "admin", archived=1)
        self.assertEqual((await self._get())["counts"]["waiting"], 0)

    async def test_empty_conversation_is_neither(self):
        await db.chat_create("empty", "Empty", None, "/tmp", "admin")
        data = await self._get()
        self.assertEqual(data["counts"], {"waiting": 0, "working": 0, "updated": 0})

    async def test_oldest_wait_is_listed_first(self):
        await _chat_with("new", ("assistant", "2026-08-29T12:00:00Z", "Shall I? (recent)"))
        await _chat_with("old", ("assistant", "2026-08-29T09:00:00Z", "Shall I? (ancient)"))
        self.assertEqual(
            [e["id"] for e in (await self._get())["waiting"]], ["old", "new"]
        )

    async def test_another_owner_sees_nothing(self):
        await _chat_with("c1", ("assistant", "2026-08-29T10:01:00Z", "Shall I continue?"))
        request = _request()
        request.state.session = {"user": "bob", "role": "user"}
        data = json.loads((await app.handle_supervisor(request)).body)
        self.assertEqual(data["counts"]["waiting"], 0)


class SupervisorSessionTests(unittest.IsolatedAsyncioTestCase):
    """CLI/terminal agents, the ones running outside the portal."""

    async def asyncSetUp(self):
        await _setup(self)
        self._cli_patch.stop()

    async def asyncTearDown(self):
        self._cli_patch.start()
        await _teardown(self)

    def _cli(self, **over):
        base = {
            "id": SESSION_ID, "sessionId": SESSION_ID, "name": "cweb3",
            "kind": "interactive", "entrypoint": "", "live": True,
            # Absent by default: older builds never wrote it, and the tests
            # that predate the status field exercise the transcript fallback.
            "status": "", "status_updated_at": "",
        }
        base.update(over)
        return base

    def _turns(self, role, text, timestamp="2026-08-29T10:00:00Z"):
        return {
            "turns": [{
                "role": role,
                "timestamp": timestamp,
                "blocks": [{"kind": "text", "text": text}],
            }],
            "found": True,
        }

    async def _get(self, cli, listing, turns):
        with patch.object(db, "read_claude_sessions", AsyncMock(return_value=cli)), \
                patch.object(app.transcripts, "list_recent", AsyncMock(return_value=listing)), \
                patch.object(app.transcripts, "read_turns", AsyncMock(return_value=turns)):
            return json.loads((await app.handle_supervisor(_request())).body)

    async def test_session_awaiting_a_reply_is_waiting(self):
        data = await self._get(
            [self._cli()],
            [{"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "t"}],
            self._turns("assistant", "Which way do you want it?"),
        )
        self.assertEqual(data["counts"]["waiting"], 1)
        entry = data["waiting"][0]
        self.assertEqual(entry["kind"], "session")
        self.assertEqual(entry["title"], "cweb3")
        self.assertEqual(entry["preview"], "Which way do you want it?")

    async def test_session_mid_turn_is_working(self):
        data = await self._get(
            [self._cli()],
            [{"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "t"}],
            self._turns("user", "go"),
        )
        self.assertEqual(data["counts"]["waiting"], 0)
        self.assertEqual(data["counts"]["working"], 1)

    async def test_webconsole_shadow_records_are_skipped(self):
        """That session is already listed as the web chat it belongs to."""
        data = await self._get(
            [self._cli(entrypoint="webconsole")],
            [{"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "t"}],
            self._turns("assistant", "Shall I continue?"),
        )
        self.assertEqual(data["counts"], {"waiting": 0, "working": 0, "updated": 0})

    async def test_session_without_a_transcript_is_skipped(self):
        data = await self._get([self._cli()], [], self._turns("assistant", "Shall I continue?"))
        self.assertEqual(data["counts"]["waiting"], 0)

    async def test_reading_a_session_clears_it(self):
        listing = [{"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "t"}]
        turns = self._turns("assistant", "Shall I continue?")
        self.assertEqual((await self._get([self._cli()], listing, turns))["counts"]["waiting"], 1)
        # A mark later than the transcript's mtime.
        await db.read_mark_set("admin", "session", SESSION_ID, "2036-01-01T00:00:00Z")
        self.assertEqual((await self._get([self._cli()], listing, turns))["counts"]["waiting"], 0)

    async def test_read_sessions_are_not_re_read_from_disk(self):
        """The mtime check must gate the transcript read, or every poll walks
        every transcript on disk."""
        listing = [{"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "t"}]
        await db.read_mark_set("admin", "session", SESSION_ID, "2036-01-01T00:00:00Z")
        read = AsyncMock(return_value=self._turns("assistant", "Shall I continue?"))
        with patch.object(db, "read_claude_sessions", AsyncMock(return_value=[self._cli()])), \
                patch.object(app.transcripts, "list_recent", AsyncMock(return_value=listing)), \
                patch.object(app.transcripts, "read_turns", read):
            await app.handle_supervisor(_request())
        read.assert_not_called()

    async def test_busy_status_means_working_whatever_the_transcript_says(self):
        """Claude Code reports its own state; trust it over the transcript.

        "busy" is the value actually observed in ~/.claude/sessions/*.json.
        """
        data = await self._get(
            [self._cli(status="busy")],
            [{"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "t"}],
            self._turns("assistant", "Shall I continue?"),
        )
        self.assertEqual(data["counts"]["waiting"], 0)
        self.assertEqual(data["counts"]["working"], 1)

    async def test_any_non_busy_status_means_it_is_waiting_on_a_human(self):
        """The idle value is unobserved, so nothing hard-codes a guess at it.

        Only "busy" is trusted as the negative; every other present value is
        taken to mean the agent has stopped. If a future build writes "idle",
        "waiting" or anything else, this keeps working.
        """
        for value in ("waiting", "idle", "input", "paused"):
            with self.subTest(status=value):
                data = await self._get(
                    [self._cli(status=value, status_updated_at="2026-08-29T15:00:00Z")],
                    [{"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "t"}],
                    self._turns("assistant", "Which way do you want it?"),
                )
                self.assertEqual(data["counts"]["waiting"], 1, value)
                self.assertEqual(data["waiting"][0]["reason"], "asks")

    async def test_a_stopped_agent_waits_even_without_an_explicit_question(self):
        """A terminal sitting at its prompt is waiting on the user by
        definition -- there is no turn in flight and nobody else will type."""
        data = await self._get(
            [self._cli(status="waiting", status_updated_at="2026-08-29T15:00:00Z")],
            [{"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "t"}],
            self._turns("assistant", "Done. All green."),
        )
        self.assertEqual(data["counts"]["waiting"], 1)
        self.assertEqual(data["waiting"][0]["reason"], "idle")

    async def test_status_absent_falls_back_to_reading_the_transcript(self):
        """An older CLI never wrote the field; absence is unknown, not idle."""
        data = await self._get(
            [self._cli(status="")],
            [{"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "t"}],
            self._turns("assistant", "Which way do you want it?"),
        )
        self.assertEqual(data["counts"]["waiting"], 1)

    async def test_a_trailing_tool_call_is_working_not_silence(self):
        """The bug that hid every terminal agent.

        A turn carrying only a tool call still has role="assistant", so reading
        the last turn found no text, concluded the agent had said nothing, and
        filed it under "updated" where it was never seen. Real transcripts end
        on a tool call constantly.
        """
        turns = {
            "turns": [
                {"role": "assistant", "timestamp": "2026-08-29T09:00:00Z",
                 "blocks": [{"kind": "text", "text": "Which way do you want it?"}]},
                {"role": "assistant", "timestamp": "2026-08-29T10:00:00Z",
                 "blocks": [{"kind": "tool", "text": "Bash(ls)"}]},
            ],
            "found": True,
        }
        data = await self._get(
            [self._cli(status="")],
            [{"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "t"}],
            turns,
        )
        self.assertEqual(data["counts"]["working"], 1)
        self.assertEqual(data["counts"]["waiting"], 0)

    async def test_the_question_is_found_behind_a_tool_call(self):
        """When the status field says it stopped, the preview must still find
        what it said -- which is rarely the very last turn."""
        turns = {
            "turns": [
                {"role": "assistant", "timestamp": "2026-08-29T09:00:00Z",
                 "blocks": [{"kind": "text", "text": "Which way do you want it?"}]},
                {"role": "assistant", "timestamp": "2026-08-29T10:00:00Z",
                 "blocks": [{"kind": "tool", "text": "Bash(ls)"}]},
            ],
            "found": True,
        }
        data = await self._get(
            [self._cli(status="waiting", status_updated_at="2026-08-29T15:00:00Z")],
            [{"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "t"}],
            turns,
        )
        self.assertEqual(data["counts"]["waiting"], 1)
        self.assertEqual(data["waiting"][0]["preview"], "Which way do you want it?")

    async def test_a_terminal_agent_that_merely_reported_is_quiet(self):
        """The literal complaint: four terminals badged for having spoken.

        A session that finished a task and said so is an update, not a summons.
        """
        data = await self._get(
            [self._cli()],
            [{"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "t"}],
            self._turns("assistant", "Done. Suite is green, ruff clean."),
        )
        self.assertEqual(data["counts"]["waiting"], 0)
        self.assertEqual(data["counts"]["updated"], 1)

    async def test_a_pending_structured_question_waits(self):
        turns = {"turns": [{"role": "assistant", "timestamp": "2026-08-29T10:00:00Z",
                            "blocks": [{"kind": "question", "id": "q1",
                                        "questions": [{"question": "Which backend?"}]}]}],
                 "found": True}
        data = await self._get(
            [self._cli(status="waiting", status_updated_at="2026-08-29T15:00:00Z")],
            [{"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "t"}],
            turns,
        )
        self.assertEqual(data["counts"]["waiting"], 1)
        self.assertEqual(data["waiting"][0]["reason"], "asks")
        self.assertEqual(data["waiting"][0]["preview"], "Which backend?")

    async def test_answering_a_structured_question_removes_the_highlight(self):
        """Once it is answered the agent is not blocked, so it must drop off."""
        turns = {"turns": [
            {"role": "assistant", "timestamp": "2026-08-29T10:00:00Z",
             "blocks": [{"kind": "question", "id": "q1",
                         "questions": [{"question": "Which backend?"}]}]},
            {"role": "assistant", "timestamp": "2026-08-29T10:01:00Z",
             "blocks": [{"kind": "answer", "id": "q1", "status": "answered",
                         "text": "Your questions have been answered"}]},
        ], "found": True}
        data = await self._get(
            [self._cli(status="waiting", status_updated_at="2026-08-29T15:00:00Z")],
            [{"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "t"}],
            turns,
        )
        self.assertEqual(data["waiting"][0]["reason"], "idle",
                         "an answered question must not still read as an ask")

    async def test_a_terminal_agent_reporting_a_blocker_does_wait(self):
        data = await self._get(
            [self._cli()],
            [{"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "t"}],
            self._turns("assistant", "I am blocked: the endpoint needs your API key."),
        )
        self.assertEqual(data["counts"]["waiting"], 1)
        self.assertEqual(data["waiting"][0]["reason"], "blocked")

    async def test_a_touched_transcript_alone_does_not_mean_waiting(self):
        """The false positive that would have made the badge worthless.

        A transcript is written by things that are not conversation. A single
        cross-session message deposits dozens of queue-operation and attachment
        records into the receiving session's file, so its mtime moves without
        the agent having said anything. With several agents messaging each
        other the badge would never go out. The decision has to come from the
        last assistant turn's timestamp, not the file's.
        """
        listing = [{"session_id": SESSION_ID, "updated_at": 2_000_000_000, "title": "t"}]
        # Already read at a point AFTER the agent last spoke...
        await db.read_mark_set("admin", "session", SESSION_ID, "2026-08-29T11:00:00Z")
        # ...but the file has been touched since, by non-conversation records.
        data = await self._get(
            [self._cli()],
            listing,
            self._turns("assistant", "Shall I continue?", timestamp="2026-08-29T10:00:00Z"),
        )
        self.assertEqual(
            data["counts"]["waiting"], 0,
            "a file write with no new assistant turn must not raise the badge",
        )

    async def test_a_genuinely_new_assistant_turn_still_waits(self):
        """The other side of the same rule: real output must still count."""
        listing = [{"session_id": SESSION_ID, "updated_at": 2_000_000_000, "title": "t"}]
        await db.read_mark_set("admin", "session", SESSION_ID, "2026-08-29T11:00:00Z")
        data = await self._get(
            [self._cli()],
            listing,
            self._turns("assistant", "Shall I continue?", timestamp="2026-08-29T12:00:00Z"),
        )
        self.assertEqual(data["counts"]["waiting"], 1)
        self.assertEqual(data["waiting"][0]["since"], "2026-08-29T12:00:00Z")

    async def test_unreadable_session_registry_does_not_break_the_view(self):
        with patch.object(db, "read_claude_sessions", AsyncMock(side_effect=OSError("nope"))), \
                patch.object(app.transcripts, "list_recent", AsyncMock(return_value=[])):
            data = json.loads((await app.handle_supervisor(_request())).body)
        self.assertEqual(data["counts"], {"waiting": 0, "working": 0, "updated": 0})


class SupervisorReadEndpointTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup(self)

    async def asyncTearDown(self):
        await _teardown(self)

    async def test_marks_a_chat(self):
        response = await app.handle_supervisor_read(_request({"kind": "chat", "id": "c1"}))
        self.assertTrue(json.loads(response.body)["ok"])
        marks = await db.read_marks_get("admin")
        self.assertIn(("chat", "c1"), marks)

    async def test_rejects_an_unknown_kind(self):
        with self.assertRaises(HTTPException) as ctx:
            await app.handle_supervisor_read(_request({"kind": "robot", "id": "c1"}))
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_rejects_a_traversal_id(self):
        for bad in ("../../etc/passwd", "a/b", "", "a b"):
            with self.subTest(ref=bad), self.assertRaises(HTTPException) as ctx:
                await app.handle_supervisor_read(_request({"kind": "chat", "id": bad}))
            self.assertEqual(ctx.exception.status_code, 400)


class AttentionTests(unittest.TestCase):
    """What earns a badge.

    The rule Pedro set after seeing the first version light up for four
    terminals that had merely finished speaking: alert when information is
    required or something important is reported, not on every new message.
    """

    def test_a_trailing_question_asks(self):
        self.assertEqual(app._attention("Which way do you want it?"), "asks")

    def test_a_question_mid_message_does_not(self):
        """Quoting a question while explaining is not a request for input."""
        self.assertIsNone(
            app._attention("I wondered whether it was cached? It was not. Fixed.")
        )

    def test_trailing_markdown_does_not_hide_the_question(self):
        self.assertEqual(app._attention("Shall I go ahead?**"), "asks")

    def test_explicit_asks_without_a_question_mark(self):
        for text in (
            "Say the word and I will build it.",
            "Let me know which you prefer.",
            "Your call.",
        ):
            with self.subTest(text=text):
                self.assertEqual(app._attention(text), "asks")

    def test_blockers_are_flagged(self):
        for text in (
            "I am blocked on the credentials.",
            "Cannot proceed without the API key.",
            "This needs your approval first.",
        ):
            with self.subTest(text=text):
                self.assertEqual(app._attention(text), "blocked")

    def test_routine_output_is_silent(self):
        for text in (
            "Done. 931 passed, ruff clean.",
            "Added the migration and wired the endpoint.",
            "Here is the summary of what changed.",
            "",
        ):
            with self.subTest(text=text):
                self.assertIsNone(app._attention(text))


class RoutineOutputTests(unittest.IsolatedAsyncioTestCase):
    """An agent that merely finished talking must not summon anyone."""

    async def asyncSetUp(self):
        await _setup(self)

    async def asyncTearDown(self):
        await _teardown(self)

    async def _get(self):
        return json.loads((await app.handle_supervisor(_request())).body)

    async def test_a_plain_reply_is_an_update_not_a_wait(self):
        await _chat_with("c1", ("assistant", "2026-08-29T10:01:00Z", "Done, all green."))
        data = await self._get()
        self.assertEqual(data["counts"]["waiting"], 0)
        self.assertEqual(data["counts"]["updated"], 1)
        self.assertEqual(data["updated"][0]["status"], "updated")

    async def test_a_question_still_waits(self):
        await _chat_with("c1", ("assistant", "2026-08-29T10:01:00Z", "Shall I continue?"))
        data = await self._get()
        self.assertEqual(data["counts"]["waiting"], 1)
        self.assertEqual(data["waiting"][0]["reason"], "asks")

    async def test_a_blocker_waits(self):
        await _chat_with(
            "c1", ("assistant", "2026-08-29T10:01:00Z", "I am blocked without the key.")
        )
        data = await self._get()
        self.assertEqual(data["counts"]["waiting"], 1)
        self.assertEqual(data["waiting"][0]["reason"], "blocked")

    async def test_many_chatty_agents_leave_the_badge_at_zero(self):
        """The exact complaint: four terminals that had simply spoken."""
        for index in range(4):
            await _chat_with(
                f"c{index}",
                ("assistant", "2026-08-29T10:01:00Z", f"Finished task {index}."),
            )
        self.assertEqual((await self._get())["counts"]["waiting"], 0)


class StructuredQuestionTests(unittest.TestCase):
    """AskUserQuestion is recorded as a question block and an answer block.

    Pairing them is what makes the highlight go away when the question is
    actually answered, rather than lingering because prose detection cannot
    tell a resolved question from an open one.
    """

    def _q(self, qid, text="Which way do you want it?"):
        return {"role": "assistant", "blocks": [
            {"kind": "question", "id": qid, "questions": [{"question": text}]}]}

    def _a(self, qid, status="answered"):
        return {"role": "assistant", "blocks": [
            {"kind": "answer", "id": qid, "status": status, "text": "..."}]}

    def test_an_unanswered_question_is_pending(self):
        self.assertEqual(
            app._pending_question([self._q("q1")]), "Which way do you want it?"
        )

    def test_an_answered_question_is_not_pending(self):
        self.assertIsNone(app._pending_question([self._q("q1"), self._a("q1")]))

    def test_a_declined_question_is_also_resolved(self):
        """Declining is answering: the agent is no longer blocked on it."""
        self.assertIsNone(
            app._pending_question([self._q("q1"), self._a("q1", "declined")])
        )

    def test_the_newest_question_decides(self):
        turns = [self._q("q1"), self._a("q1"), self._q("q2", "And now?")]
        self.assertEqual(app._pending_question(turns), "And now?")

    def test_an_answer_to_a_different_question_does_not_resolve_it(self):
        turns = [self._q("q1"), self._a("q99")]
        self.assertEqual(app._pending_question(turns), "Which way do you want it?")

    def test_no_questions_at_all(self):
        self.assertIsNone(app._pending_question([
            {"role": "assistant", "blocks": [{"kind": "text", "text": "hi"}]}]))


class ClearAllTests(unittest.IsolatedAsyncioTestCase):
    """The clear control: a deliberate act, so it silences questions too."""

    async def asyncSetUp(self):
        await _setup(self)

    async def asyncTearDown(self):
        await _teardown(self)

    async def _get(self):
        return json.loads((await app.handle_supervisor(_request())).body)

    async def test_clearing_silences_an_unanswered_question(self):
        await _chat_with("c1", ("assistant", "2026-08-29T10:01:00Z", "Shall I continue?"))
        self.assertEqual((await self._get())["counts"]["waiting"], 1)
        response = await app.handle_supervisor_read(_request({"all": True}))
        self.assertTrue(json.loads(response.body)["ok"])
        self.assertEqual((await self._get())["counts"]["waiting"], 0)

    async def test_clearing_reports_how_many_it_cleared(self):
        await _chat_with("c1", ("assistant", "2026-08-29T10:01:00Z", "Shall I?"))
        await _chat_with("c2", ("assistant", "2026-08-29T10:01:00Z", "Done, all green."))
        body = json.loads((await app.handle_supervisor_read(_request({"all": True}))).body)
        self.assertEqual(body["cleared"], 2)
        counts = (await self._get())["counts"]
        self.assertEqual((counts["waiting"], counts["updated"]), (0, 0))

    async def test_a_later_question_returns_after_clearing(self):
        """Clearing silences what is there now, not the agent forever."""
        await _chat_with("c1", ("assistant", "2026-08-29T10:01:00Z", "Shall I continue?"))
        await app.handle_supervisor_read(_request({"all": True}))
        self.assertEqual((await self._get())["counts"]["waiting"], 0)
        await db.db_conn.execute(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?,?,?,?)",
            ("c1", "assistant", "Actually -- which one?", "2036-01-01T00:00:00Z"),
        )
        await db.db_conn.commit()
        self.assertEqual((await self._get())["counts"]["waiting"], 1)

    async def test_reading_alone_never_dismisses(self):
        """The distinction the whole change rests on."""
        await _chat_with("c1", ("assistant", "2026-08-29T10:01:00Z", "Shall I continue?"))
        await app.handle_supervisor_read(_request({"kind": "chat", "id": "c1"}))
        self.assertEqual((await self._get())["counts"]["waiting"], 1)
        marks = await db.read_marks_get("admin")
        self.assertFalse(marks[("chat", "c1")]["dismissed_at"])


class OneLineTests(unittest.TestCase):

    def test_collapses_whitespace(self):
        self.assertEqual(app._one_line("a\n  b\tc"), "a b c")

    def test_truncates_with_an_ellipsis(self):
        out = app._one_line("x" * 500, limit=50)
        self.assertEqual(len(out), 50)
        self.assertTrue(out.endswith("…"))

    def test_empty_input_is_empty(self):
        self.assertEqual(app._one_line(""), "")
        self.assertEqual(app._one_line(None), "")


if __name__ == "__main__":
    unittest.main()
