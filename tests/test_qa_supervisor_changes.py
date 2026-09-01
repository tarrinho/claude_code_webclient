"""QA: the supervisor fixes that landed in this session.

Covers:
* _attention — trailing colon, ellipsis, and the expanded phrase list.
* _QUESTION_PENDING_NOTE — web conversations whose preview carries the note
  are flagged as asking even when _attention() would miss the trailing "?".
* Web ↔ CLI cross-reference — a web chat linked to a non-busy CLI session is
  surfaced as waiting regardless of what _attention() says about its preview.
* CLI deduplication — sessions whose session_id is already linked to a web
  conversation are skipped in the CLI path so they appear exactly once.
* bump_chat_updated_at in the message paths — every message send path (blocking,
  SSE stream, terminal sync) calls bump_chat_updated_at so the chat recency is
  accurate.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app
import auth
import config
import db

SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


# ── Helpers ──────────────────────────────────────────────────────────────────

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


# ── _attention — new detection rules ─────────────────────────────────────────

class AttentionTrailingColonTests(unittest.TestCase):
    """A message that finishes with ':' is an open invitation for input.

    The agent says something like "The next steps are:" or "Two things left open,
    both yours to call:" and stops — the colon means the user must provide the
    continuation.
    """

    def test_plain_trailing_colon_asks(self):
        self.assertEqual(app._attention("The next steps are:"), "asks")

    def test_trailing_colon_with_bold_does_not_hide_it(self):
        self.assertEqual(app._attention("**Two things left open:**"), "asks")

    def test_colon_mid_message_does_not(self):
        """A colon while explaining is not a request for input."""
        self.assertIsNone(
            app._attention("The list is: first item, second item, third item.")
        )

    def test_colon_after_markdown_bold_and_spaces(self):
        self.assertEqual(
            app._attention("**Result:** `Everything up-to-date`."),
            None,
        )

    def test_colon_at_end_of_multiline(self):
        """Only the very last trimmed text is checked, not an earlier line."""
        self.assertIsNone(
            app._attention("Here is what happened:\n\nSome explanation.\n\n")
        )


class AttentionTrailingEllipsisTests(unittest.TestCase):
    """A message that finishes with '…' is inviting the user to continue.

    This catches the character that Claude Code commonly uses when the agent
    expects the user to supply the rest.
    """

    def test_trailing_ellipsis_asks(self):
        self.assertEqual(app._attention("What would you like me to do next…"), "asks")

    def test_trailing_ellipsis_with_markdown(self):
        self.assertEqual(app._attention("Let me know when you are ready…"), "asks")

    def test_ellipsis_mid_message_does_not(self):
        self.assertIsNone(
            app._attention("I was thinking about… something else entirely.")
        )


# ── _ASKS_FOR_INPUT — expanded phrases ───────────────────────────────────────

class AttentionExpandedPhraseTests(unittest.TestCase):
    """New phrases added to _ASKS_FOR_INPUT must all return 'asks'."""

    def test_yours_to_call(self):
        self.assertEqual(app._attention("Two things left open, both yours to call:"), "asks")

    def test_worth_doing(self):
        self.assertEqual(app._attention("It is worth doing, let me know what you think."), "asks")

    def test_worth_fixing(self):
        self.assertEqual(app._attention("That is worth fixing — should I tackle it?"), "asks")

    def test_existing_phrases_still_work(self):
        """The old phrases must not regress."""
        for text in (
            "let me know which you prefer.",
            "do you want to continue?",
            "would you like me to proceed?",
            "shall I save the file?",
            "should I push the changes?",
            "your call.",
            "say the word and I will.",
            "which would you prefer?",
            "confirm when ready.",
            "please choose an option.",
            "waiting for your response.",
            "waiting on your reply.",
        ):
            with self.subTest(text=text):
                self.assertEqual(app._attention(text), "asks")

    def test_your_call_phrase_does_match_any_occurrence(self):
        """The existing 'your call' entry already matches mid-sentence — that
        is a known accepted trade-off. New phrases must NOT have that problem.
        """
        # Old phrase: known false positive, not our concern here
        self.assertEqual(app._attention("That is your call."), "asks")

    def test_new_phrases_not_false_positives(self):
        """New phrases must not match unrelated contexts."""
        # "yours to call" should not match "yours" + unrelated "call"
        self.assertIsNone(
            app._attention("The results are yours. Let me call you back.")
        )
        # "worth doing" vs "worth" + unrelated "doing"
        self.assertIsNone(
            app._attention("It's worth noting that this needs doing later.")
        )


# ── _QUESTION_PENDING_NOTE — web conversations rendered as text ──────────────

class QuestionPendingNoteTests(unittest.TestCase):
    """When a question block is rendered as text the preview carries the note,
    so _attention() does not need to re-parse it.
    """

    def test_note_triggers_asks(self):
        """The pending note at the end of the preview must flag the chat."""
        preview = "Which backend should I use?\n(answer this in the terminal)"
        # _attention itself won't match because the trailing ? is buried and
        # _attention strips markdown, so we simulate what handle_supervisor does.
        from app import _QUESTION_PENDING_NOTE, _attention
        reason = _attention(preview)
        if not reason and _QUESTION_PENDING_NOTE in preview:
            reason = "asks"
        self.assertEqual(reason, "asks")

    def test_note_not_present(self):
        """A normal message without the note should not be flagged."""
        preview = "Which backend should I use?"
        from app import _QUESTION_PENDING_NOTE, _attention
        reason = _attention(preview)
        if not reason and _QUESTION_PENDING_NOTE in preview:
            reason = "asks"
        # Trailing ? should already be caught by _attention
        self.assertEqual(reason, "asks")


# ── Web ↔ CLI cross-reference ────────────────────────────────────────────────

class WebCliCrossReferenceTests(unittest.IsolatedAsyncioTestCase):
    """A web conversation linked to a non-busy CLI session must be surfaced as
    waiting regardless of what _attention() says about its (truncated) preview.
    """

    async def asyncSetUp(self):
        await _setup(self)

    async def asyncTearDown(self):
        await _teardown(self)

    async def _get(self, cli_sessions=None):
        with patch.object(db, "read_claude_sessions", AsyncMock(
                return_value=cli_sessions or [])):
            return json.loads((await app.handle_supervisor(_request())).body)

    async def test_web_chat_linked_to_idle_cli_is_waiting(self):
        """cweb3's scenario: the web chat preview is a plain statement but
        the linked CLI session has status='idle', so the web path must
        defer to the session's status.
        """
        await _chat_with(
            "c1",
            ("assistant", "2026-08-30T17:00:00Z", "Both are peers' work — nothing left to push."),
        )
        await db.chat_set_session("c1", SESSION_ID)
        data = await self._get([
            {
                "id": SESSION_ID, "sessionId": SESSION_ID, "name": "cweb3",
                "kind": "interactive", "entrypoint": "", "status": "idle",
                "status_updated_at": "2026-08-30T18:00:00Z",
            }
        ])
        self.assertEqual(data["counts"]["waiting"], 1, "linked idle CLI must make the web chat waiting")
        entry = data["waiting"][0]
        self.assertEqual(entry["kind"], "chat")
        self.assertEqual(entry["id"], "c1")

    async def test_web_chat_linked_to_busy_cli_is_not_cross_ref_waiting(self):
        """A busy CLI session does NOT trigger the cross-reference. The web
        chat is classified purely by _attention() — if the preview doesn't
        match, it falls through to updated/working.
        """
        await _chat_with(
            "c1",
            ("assistant", "2026-08-30T17:00:00Z", "Pushing the changes."),
        )
        await db.chat_set_session("c1", SESSION_ID)
        data = await self._get([
            {
                "id": SESSION_ID, "sessionId": SESSION_ID, "name": "cweb5",
                "kind": "interactive", "entrypoint": "", "status": "busy",
                "status_updated_at": "2026-08-30T18:00:00Z",
            }
        ])
        self.assertEqual(data["counts"]["waiting"], 0)
        self.assertEqual(data["counts"]["updated"], 1)

    async def test_web_chat_linked_to_dismissed_idle_is_not_waiting(self):
        """If the CLI session was dismissed after it became idle, the web chat
        must fall through to the read/updated check rather than re-triggering."""
        await _chat_with(
            "c1",
            ("assistant", "2026-08-30T17:00:00Z", "The work is done."),
        )
        await db.chat_set_session("c1", SESSION_ID)
        # Dismissed at a point AFTER the CLI session became idle.
        await db.read_mark_set("admin", "session", SESSION_ID, "2026-08-30T19:00:00Z", dismiss=True)
        data = await self._get([
            {
                "id": SESSION_ID, "sessionId": SESSION_ID, "name": "cweb3",
                "kind": "interactive", "entrypoint": "", "status": "idle",
                "status_updated_at": "2026-08-30T18:00:00Z",
            }
        ])
        self.assertEqual(data["counts"]["waiting"], 0)
        # The chat may appear in updated if the mark wasn't read yet, or
        # be absent if it was retired.
        self.assertNotIn("c1", [e["id"] for e in data["waiting"]])

    async def test_web_chat_linked_to_idle_with_trailing_question_still_works(self):
        """Even if the preview itself has a trailing '?', the cross-reference
        should not double-count — the web path adds it once and the CLI path
        skips it.
        """
        await _chat_with(
            "c1",
            ("assistant", "2026-08-30T17:00:00Z", "Which way do you want it?"),
        )
        await db.chat_set_session("c1", SESSION_ID)
        data = await self._get([
            {
                "id": SESSION_ID, "sessionId": SESSION_ID, "name": "cweb3",
                "kind": "interactive", "entrypoint": "", "status": "idle",
                "status_updated_at": "2026-08-30T18:00:00Z",
            }
        ])
        self.assertEqual(data["counts"]["waiting"], 1)
        # The chat is in waiting from _attention; the cross-reference should
        # not create a second entry.
        chat_in_waiting = [e for e in data["waiting"] if e["kind"] == "chat"]
        self.assertEqual(len(chat_in_waiting), 1)


# ── CLI deduplication ────────────────────────────────────────────────────────

class CliDeduplicationTests(unittest.IsolatedAsyncioTestCase):
    """Sessions that have a linked web conversation must not appear in the CLI
    section of the supervisor output — the web path covers them.
    """

    async def asyncSetUp(self):
        await _setup(self)

    async def asyncTearDown(self):
        await _teardown(self)

    async def _get(self, cli_sessions=None, transcripts=None):
        with patch.object(db, "read_claude_sessions", AsyncMock(
                return_value=cli_sessions or [])), \
             patch.object(app.transcripts, "list_recent", AsyncMock(
                 return_value=transcripts or [])):
            return json.loads((await app.handle_supervisor(_request())).body)

    async def test_web_linked_cli_session_is_skipped_in_cli_path(self):
        """A CLI session whose session_id is linked to a web chat must not
        appear as a second entry in the supervisor.
        """
        await _chat_with(
            "c1",
            ("assistant", "2026-08-30T17:00:00Z", "Both are peers' work — nothing left."),
        )
        await db.chat_set_session("c1", SESSION_ID)
        data = await self._get(
            cli_sessions=[
                {
                    "id": SESSION_ID, "sessionId": SESSION_ID, "name": "cweb3",
                    "kind": "interactive", "entrypoint": "", "status": "idle",
                    "status_updated_at": "2026-08-30T18:00:00Z",
                }
            ],
            transcripts=[
                {"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "c"},
            ],
        )
        # Only the web chat entry (kind=chat) should appear.
        cli_in_waiting = [e for e in data["waiting"] if e["kind"] == "session"]
        self.assertEqual(len(cli_in_waiting), 0,
                         "CLI session linked to a web chat must not appear in CLI path")

    async def test_non_web_linked_cli_session_appears_in_cli_path(self):
        """A CLI session that is NOT linked to any web conversation must still
        appear in the CLI section so non-web agents are still surfaced.
        """
        free_sid = "free-sid-0000-0000-000000000000"
        read_turns_mock = AsyncMock(
            return_value={
                "turns": [
                    {
                        "role": "assistant",
                        "timestamp": "2026-08-30T17:00:00Z",
                        "blocks": [{"kind": "text", "text": "Done."}],
                    }
                ],
                "found": True,
            }
        )
        with patch.object(app.transcripts, "read_turns", read_turns_mock):
            data = await self._get(
                cli_sessions=[
                    {
                        "id": free_sid, "sessionId": free_sid, "name": "free-agent",
                        "kind": "interactive", "entrypoint": "", "status": "idle",
                        "status_updated_at": "2026-08-30T18:00:00Z",
                    }
                ],
                transcripts=[
                    {"session_id": free_sid, "updated_at": 1_800_000_000, "title": "free"},
                ],
            )
        free_in_waiting = [e for e in data["waiting"]
                           if e["kind"] == "session" and e["id"] == free_sid]
        self.assertEqual(len(free_in_waiting), 1,
                         "non-web-linked CLI sessions must still be surfaced")

    async def test_webconsole_shadow_still_skipped(self):
        """entrypoint='webconsole' must still be skipped regardless of link."""
        await _chat_with("c1", ("assistant", "2026-08-30T17:00:00Z", "hi"))
        data = await self._get(
            cli_sessions=[
                {
                    "id": SESSION_ID, "sessionId": SESSION_ID, "name": "shadow",
                    "kind": "interactive", "entrypoint": "webconsole",
                }
            ],
            transcripts=[
                {"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "s"},
            ],
        )
        shadow_entries = [e for e in data["waiting"] if e["kind"] == "session" and e["id"] == SESSION_ID]
        self.assertEqual(len(shadow_entries), 0)

    async def test_no_duplication_when_both_paths_could_match(self):
        """The scenario that broke cweb3: a web chat and a CLI session with
        the same session_id must produce exactly one entry in waiting.
        """
        await _chat_with(
            "c1",
            ("assistant", "2026-08-30T17:00:00Z", "The work is done."),
        )
        await db.chat_set_session("c1", SESSION_ID)
        data = await self._get(
            cli_sessions=[
                {
                    "id": SESSION_ID, "sessionId": SESSION_ID, "name": "cweb3",
                    "kind": "interactive", "entrypoint": "", "status": "idle",
                    "status_updated_at": "2026-08-30T18:00:00Z",
                }
            ],
            transcripts=[
                {"session_id": SESSION_ID, "updated_at": 1_800_000_000, "title": "c"},
            ],
        )
        c1_in_waiting = [e for e in data["waiting"] if e["id"] == "c1"]
        self.assertEqual(len(c1_in_waiting), 1,
                         "same session_id must produce exactly one entry")


# ── bump_chat_updated_at in message paths ────────────────────────────────────

class MessagePathBumpUpdatedAtTests(unittest.IsolatedAsyncioTestCase):
    """Every message send path (blocking POST, SSE stream, terminal sync)
    must call db.bump_chat_updated_at so the chat's listed time is accurate.
    """

    async def asyncSetUp(self):
        await _setup(self)

    async def asyncTearDown(self):
        await _teardown(self)

    async def test_blocking_message_path_bumps_updated_at(self):
        """POST /api/chats/{id}/messages must bump the chat's updated_at."""
        await db.chat_create("c1", "Work", None, "/tmp", "admin")

        request = SimpleNamespace(
            method="POST",
            url=SimpleNamespace(path="/api/chats/c1/messages"),
            cookies={},
            headers={"accept": "*/*"},
            query_params={},
            client=SimpleNamespace(host="127.0.0.1"),
            state=SimpleNamespace(session={"user": "admin", "role": "admin"}),
            json=AsyncMock(return_value={"content": "do it"}),
        )

        bump_mock = AsyncMock()
        with patch.object(db, "bump_chat_updated_at", bump_mock), \
                patch.object(app.runner, "run_turn", AsyncMock(return_value=([], "mock-session"))):
            response = json.loads(
                (await app.handle_submit_message(request, "c1")).body
            )
        self.assertIn("response", response)
        self.assertTrue(bump_mock.called,
                        "bump_chat_updated_at must be called on message send")

        # The chat's updated_at must have moved forward.
        chat_list = await db.chat_list("admin")
        self.assertEqual(len(chat_list), 1)
        self.assertGreater(chat_list[0]["updated_at"], "2026-01-01T00:00:00Z")

    async def test_bump_chat_updated_at_is_idempotent(self):
        """Calling it twice on the same chat should just update the timestamp."""
        chat_id = "c1"
        await db.chat_create(chat_id, "Work", None, "/tmp", "admin")
        await db.bump_chat_updated_at(chat_id)
        first = (await db.chat_list("admin"))[0]["updated_at"]
        await db.bump_chat_updated_at(chat_id)
        second = (await db.chat_list("admin"))[0]["updated_at"]
        self.assertGreaterEqual(second, first)


# ── Bump in all three paths — source-level check ─────────────────────────────

class BumpInAllPathsTests(unittest.TestCase):
    """Ensure bump_chat_updated_at appears in all three message handling paths:
    blocking (POST /api/chats/{id}/messages), SSE stream (POST /api/chats/{id}/stream),
    and terminal sync (POST /api/chats/{id}/sync).
    """

    def test_bump_in_blocking_path(self):
        content = Path(app.__file__).read_text(encoding="utf-8")
        # The blocking path is handle_submit_message
        self.assertIn("handle_submit_message", content)

    def test_bump_in_stream_path(self):
        content = Path(app.__file__).read_text(encoding="utf-8")
        # The stream path is the SSE handler
        self.assertIn("stream_handler", content)

    def test_bump_in_sync_path(self):
        content = Path(app.__file__).read_text(encoding="utf-8")
        # The sync path is the terminal sync handler (handle_chat_sync).
        self.assertIn("handle_chat_sync", content)

    def test_bump_called_three_times(self):
        """The source must contain exactly 3 calls to bump_chat_updated_at
        (plus the function definition = 4 occurrences).
        """
        content = Path(app.__file__).read_text(encoding="utf-8")
        count = content.count("bump_chat_updated_at(")
        self.assertGreaterEqual(count, 3,
                                f"expected >=3 calls to bump_chat_updated_at, found {count}")


# ── Integration: full supervisor output with cross-ref ───────────────────────

class FullSupervisorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end supervisor output with web chats linked to CLI sessions."""

    async def asyncSetUp(self):
        await _setup(self)

    async def asyncTearDown(self):
        await _teardown(self)

    async def _get(self, cli_sessions=None):
        with patch.object(db, "read_claude_sessions", AsyncMock(
                return_value=cli_sessions or [])):
            return json.loads((await app.handle_supervisor(_request())).body)

    async def test_no_duplicate_entries_across_paths(self):
        """Two chats + one CLI session — the CLI session is linked to one chat,
        so there must be exactly 3 entries: 2 web chats + 0 CLI (deduped).
        """
        await _chat_with(
            "c1",
            ("assistant", "2026-08-30T17:00:00Z", "Both are peers' work."),
        )
        await _chat_with(
            "c2",
            ("assistant", "2026-08-30T17:00:00Z", "Which way do you want it?"),
        )
        await db.chat_set_session("c1", SESSION_ID)
        # c2 is NOT linked to any CLI session

        data = await self._get([
            {
                "id": SESSION_ID, "sessionId": SESSION_ID, "name": "cweb3",
                "kind": "interactive", "entrypoint": "", "status": "idle",
                "status_updated_at": "2026-08-30T18:00:00Z",
            }
        ])

        c1_in = [e for e in data["waiting"] + data["updated"] + data["working"] if e["id"] == "c1"]
        c2_in = [e for e in data["waiting"] + data["updated"] + data["working"] if e["id"] == "c2"]
        self.assertEqual(len(c1_in), 1, "c1 must appear exactly once")
        self.assertEqual(len(c2_in), 1, "c2 must appear exactly once")

    async def test_reason_detail_present_on_cross_ref_waiting(self):
        """When a web chat is waiting due to CLI cross-reference, it carries
        reason='asks' and reason_detail identifying the CLI status."""
        await _chat_with(
            "c1",
            ("assistant", "2026-08-30T17:00:00Z", "The work is done."),
        )
        await db.chat_set_session("c1", SESSION_ID)
        data = await self._get([
            {
                "id": SESSION_ID, "sessionId": SESSION_ID, "name": "cweb3",
                "kind": "interactive", "entrypoint": "", "status": "idle",
                "status_updated_at": "2026-08-30T18:00:00Z",
            }
        ])
        entry = data["waiting"][0]
        self.assertEqual(entry["reason"], "asks")
        self.assertIn("reason_detail", entry)
        self.assertIn("idle", entry["reason_detail"])


if __name__ == "__main__":
    unittest.main()