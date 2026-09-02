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
import classification
import config
import db
import shared
from routes import supervisors as supervisor_routes

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
    """A message that finishes with ':' is NOT a request for input.

    This class asserted the opposite, on the reading that "The next steps are:"
    leaves the user to supply the continuation. Reversed on Pedro's instruction:
    highlight only when a question genuinely needs a person, or when the action
    has ended -- never merely because the agent emitted text.

    The reading was not unreasonable, it was just outweighed. Ordinary output
    ends with a colon constantly ("Here is what I found:", "Changes:"), so this
    summoned the user for prose, and a badge that fires on prose is a badge that
    gets ignored -- which costs the real asks buried among them.

    Nothing is hidden by the reversal. A finished turn is now surfaced in its own
    right with ``reason="done"``, so a reply ending in a colon still appears; it
    appears labelled as finished instead of as a question nobody asked.
    """

    def test_plain_trailing_colon_does_not_ask(self):
        self.assertIsNone(classification._attention("The next steps are:"))

    def test_trailing_colon_with_bold_does_not_ask(self):
        self.assertIsNone(classification._attention("**Two things left open:**"))

    def test_a_real_question_still_asks(self):
        """Guards the reversal: dropping the colon must not silence everything.

        Without this the class above passes against an ``_attention`` that has
        stopped detecting anything at all.
        """
        self.assertEqual(classification._attention("Which branch should I use?"), "asks")

    def test_colon_mid_message_does_not(self):
        """A colon while explaining is not a request for input."""
        self.assertIsNone(
            classification._attention("The list is: first item, second item, third item.")
        )

    def test_colon_after_markdown_bold_and_spaces(self):
        self.assertEqual(
            classification._attention("**Result:** `Everything up-to-date`."),
            None,
        )

    def test_colon_at_end_of_multiline(self):
        """Only the very last trimmed text is checked, not an earlier line."""
        self.assertIsNone(
            classification._attention("Here is what happened:\n\nSome explanation.\n\n")
        )


class AttentionTrailingEllipsisTests(unittest.TestCase):
    """A trailing '…' does NOT on its own mean the user is being asked.

    Reversed with the trailing colon above, and for the same reason: an ellipsis
    is as often narration trailing off as it is an invitation.

    The two cases below still return "asks", and the docstring here used to
    credit the ellipsis for that. They do not: "What would you like me to do
    next…" and "Let me know when you are ready…" both contain phrases from
    ``_ASKS_FOR_INPUT`` ("what would you like", "let me know"), so they passed
    on the phrase list and would have passed with the ellipsis rule already
    removed. Kept, because they are genuine asks and worth pinning -- but named
    for the rule that actually decides them, since a test credited to the wrong
    mechanism is a test nobody can reason about.
    """

    def test_an_explicit_invitation_asks_whatever_it_ends_with(self):
        self.assertEqual(classification._attention("What would you like me to do next…"), "asks")

    def test_a_second_explicit_invitation_also_asks(self):
        self.assertEqual(classification._attention("Let me know when you are ready…"), "asks")

    def test_an_ellipsis_alone_does_not_ask(self):
        """The reversal itself: narration trailing off summons nobody."""
        self.assertIsNone(classification._attention("Still working through the files…"))

    def test_ellipsis_mid_message_does_not(self):
        self.assertIsNone(
            classification._attention("I was thinking about… something else entirely.")
        )


# ── _ASKS_FOR_INPUT — expanded phrases ───────────────────────────────────────

class AttentionExpandedPhraseTests(unittest.TestCase):
    """New phrases added to _ASKS_FOR_INPUT must all return 'asks'."""

    def test_yours_to_call(self):
        self.assertEqual(classification._attention("Two things left open, both yours to call:"), "asks")

    def test_worth_doing(self):
        self.assertEqual(classification._attention("It is worth doing, let me know what you think."), "asks")

    def test_worth_fixing(self):
        self.assertEqual(classification._attention("That is worth fixing — should I tackle it?"), "asks")

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
                self.assertEqual(classification._attention(text), "asks")

    def test_your_call_phrase_does_match_any_occurrence(self):
        """The existing 'your call' entry already matches mid-sentence — that
        is a known accepted trade-off. New phrases must NOT have that problem.
        """
        # Old phrase: known false positive, not our concern here
        self.assertEqual(classification._attention("That is your call."), "asks")

    def test_new_phrases_not_false_positives(self):
        """New phrases must not match unrelated contexts."""
        # "yours to call" should not match "yours" + unrelated "call"
        self.assertIsNone(
            classification._attention("The results are yours. Let me call you back.")
        )
        # "worth doing" vs "worth" + unrelated "doing"
        self.assertIsNone(
            classification._attention("It's worth noting that this needs doing later.")
        )


# ── _QUESTION_PENDING_NOTE — web conversations rendered as text ──────────────

# Filler with no "?" and nothing from _ASKS_FOR_INPUT or _REPORTS_A_BLOCKER, so
# `_attention` cannot flag it and only the note can. Long enough that the note
# lands past the 200-character preview window.
_QUIET_FILLER = (
    "I reviewed the module and applied the changes we settled on, including "
    "the helper refactor and the extra tests around it. Everything is "
    "committed and the suite is green. "
) * 3


class IncidentalPhraseTests(unittest.TestCase):
    """A phrase list matched inside words, and said the opposite of the truth.

    `_ASKS_FOR_INPUT` and `_REPORTS_A_BLOCKER` were tested with
    `phrase in text`, so `"should i"` matched `"should include"` and
    `"blocked"` matched `"unblocked"`. Every case below was live in 0.9.4.

    It was reported as a regression in the change that made the classifier read
    the *end* of a message as well as its opening, and it is not one: the window
    only widened the exposure. `_attention(preview)` had the same fault for any
    message whose opening happened to contain "should include", so a fix aimed
    at the windows would have left the cause in place -- which is why the
    proposed reversion was rejected. Anchoring on word boundaries removes it for
    both windows, and no true positive is lost.
    """

    # A neutral pad, asserted neutral below rather than assumed: a pad that
    # happens to contain one of the phrases invalidates every case built on it.
    _PAD = (
        "I reviewed the module and applied the changes we settled on, then ran "
        "the suite and the linter over the result. Everything is committed. "
    ) * 3

    def test_the_pad_is_neutral(self):
        """Guards every case in this class."""
        self.assertIsNone(classification._attention(self._PAD))

    def test_should_include_is_not_an_ask(self):
        """The reported case: 'should i' inside 'should include'."""
        text = self._PAD + "I documented whether I should include the rebuild."
        self.assertIsNone(classification._attention(text))

    def test_confirmed_is_not_a_request_to_confirm(self):
        self.assertIsNone(classification._attention(self._PAD + "I confirmed the tests pass."))

    def test_unblocked_is_not_a_blocker(self):
        """The sharpest: the opposite claim, read as the claim."""
        self.assertIsNone(classification._attention(self._PAD + "The task is unblocked now."))

    def test_a_finished_report_whose_tail_holds_a_phrase_is_done(self):
        """The end-to-end form, which is how this reached a release.

        A long finished report classified as `reason: "asks"` with no question
        on it -- the exact over-reporting the done/asks split exists to remove,
        reappearing at the other end of the message. Asserted through
        `classify_chat`, because `_attention` alone would not have caught the
        consequence.
        """
        body = self._PAD + "I noted whether I should include it. The work is complete."
        self.assertGreater(len(body), 400)
        entry = classification.classify_chat(
            chat={"id": "c1", "title": "t", "session_id": ""},
            last={"role": "assistant", "created_at": "2026-08-31T19:12:04Z",
                  "preview": body[:200], "tail": body[-200:]},
            live_ids=frozenset(), queued={}, marks={},
            cli_status_map={}, cli_dismiss_map={}, cli_status_updated_map={},
        )
        self.assertEqual(entry["reason"], "done")
        self.assertFalse(entry["question"])

    def test_real_asks_and_blockers_still_match(self):
        """The other half. Anchoring must not silence the lists it anchors.

        Without this the class passes against an `_attention` that has stopped
        matching phrases altogether, which is the failure mode anchoring could
        plausibly introduce.
        """
        for text, expected in (
            ("Should I proceed", "asks"),
            ("Please confirm before I continue", "asks"),
            ("Let me know which you prefer.", "asks"),
            ("Your call.", "asks"),
            ("I cannot proceed without the token.", "blocked"),
            ("Note: blocked here.", "blocked"),
            ("Permission denied on that path.", "blocked"),
            ("This needs your approval.", "blocked"),
        ):
            with self.subTest(text=text):
                self.assertEqual(classification._attention(text), expected)

    def test_a_blocker_only_in_the_tail_is_still_found(self):
        """Why the reversion was refused, pinned so it is not tried again.

        Keeping `_attention(preview)` and adding an anchored question test to the
        tail loses blockers that appear only at the end -- and by the rule in
        `_CLI_STATUS_NOT_BLOCKED`, under-reporting a stuck agent is the more
        expensive direction.
        """
        body = self._PAD + "Note: blocked here."
        self.assertIsNone(classification._attention(body[:200]),
                          "the pad must not carry the signal on its own")
        self.assertEqual(classification._attention(body[-200:]), "blocked")

    def test_every_phrase_is_anchored_at_both_ends(self):
        """The property, rather than a sample of its consequences."""
        for phrase in (*classification._ASKS_FOR_INPUT, *classification._REPORTS_A_BLOCKER):
            with self.subTest(phrase=phrase):
                # Glued to a letter on either side, it must not match.
                self.assertIsNone(classification._attention(f"x{phrase}"))
                self.assertIsNone(classification._attention(f"{phrase}x"))


class QuestionPendingNoteTests(unittest.TestCase):
    """The pending note flags a chat even when it lands past the preview.

    `_QUESTION_PENDING_NOTE` is appended to the END of a rendered question, so on
    any message longer than ~400 characters it appears in the tail and never in
    the preview. That is what made this worth testing at all -- and the previous
    version of this class could not have caught it, for three reasons found by
    cweb4:

    * Both test bodies **copied** the production branch instead of calling it, so
      neither touched `classify_chat`. Deleting the note handling from production
      left both passing.
    * Both fixtures were "Which backend should I use?", which `_attention` flags
      via the phrase `should i`. So `if not reason:` was False and the copied
      note logic never executed even inside the test.
    * `test_note_not_present` asserted `reason == "asks"` under a docstring
      saying the message "should not be flagged".

    The class docstring asserted the premise the bug depended on -- "the preview
    carries the note" -- which is true only for short messages.

    These go through `classify_chat`, with filler chosen so that `_attention`
    alone cannot flag the preview. Mutation-checked: reverting the production
    line to `_attention(preview)` alone fails the first two.
    """

    @staticmethod
    def _classify(body, *, session=""):
        """Real classifier, real preview/tail split (db.py: first 200, last 200)."""
        return classification.classify_chat(
            chat={"id": "c1", "title": "cweb2", "session_id": session},
            last={"role": "assistant", "created_at": "2026-08-31T19:12:04Z",
                  "preview": body[:200], "tail": body[-200:]},
            live_ids=frozenset(), queued={}, marks={},
            cli_status_map={}, cli_dismiss_map={}, cli_status_updated_map={},
        )

    def test_a_note_past_the_preview_still_asks(self):
        """The defect this class exists for, through the real call site."""
        body = _QUIET_FILLER + "Pick one " + shared._QUESTION_PENDING_NOTE
        self.assertGreater(len(body), 400, "fixture too short to test the split")
        self.assertNotIn(
            shared._QUESTION_PENDING_NOTE, body[:200],
            "the note is inside the preview, so this tests nothing",
        )
        self.assertIsNone(
            classification._attention(body[:200]),
            "_attention flags the preview on its own; only the note may flag it",
        )
        entry = self._classify(body)
        self.assertEqual(entry["reason"], "asks")
        self.assertTrue(entry["question"])

    def test_a_question_mark_past_the_preview_still_asks(self):
        """Same window, without the structured note."""
        body = _QUIET_FILLER + "Which branch do you prefer?"
        self.assertIsNone(classification._attention(body[:200]))
        entry = self._classify(body)
        self.assertEqual(entry["reason"], "asks")
        self.assertTrue(entry["question"])

    def test_a_long_message_with_no_question_is_done(self):
        """The control that keeps the two above honest.

        Without it, a classifier that returned "asks" for everything long would
        satisfy them both -- and the whole point of the `done` promotion is that
        ordinary finished work is not reported as a question.
        """
        body = _QUIET_FILLER + "All tests pass."
        entry = self._classify(body)
        self.assertEqual(entry["reason"], "done")
        self.assertFalse(entry["question"])

    def test_a_short_note_is_flagged_too(self):
        """The case the old fixtures were reaching for: preview == tail."""
        entry = self._classify("Pick one " + shared._QUESTION_PENDING_NOTE)
        self.assertEqual(entry["reason"], "asks")
        self.assertTrue(entry["question"])

    def test_a_plain_trailing_question_is_flagged_without_the_note(self):
        """Replaces `test_note_not_present`, which asserted the opposite of its
        own docstring. The behaviour it meant to check is that `_attention`
        catches a trailing "?" unaided -- nothing to do with the note.
        """
        body = "Which backend should I use?"
        self.assertNotIn(shared._QUESTION_PENDING_NOTE, body)
        entry = self._classify(body)
        self.assertEqual(entry["reason"], "asks")


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
            return json.loads((await supervisor_routes.handle_supervisor(_request())).body)

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
            return json.loads((await supervisor_routes.handle_supervisor(_request())).body)

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
            return json.loads((await supervisor_routes.handle_supervisor(_request())).body)

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

    async def test_an_idle_linked_session_is_reported_as_finished_not_asking(self):
        """An idle CLI session is surfaced, but as finished rather than asking.

        This asserted ``reason == "asks"`` with a ``reason_detail`` naming the
        CLI status. The row is still listed -- an ended action is exactly what
        the user wants told -- but the reason has changed, on Pedro's
        instruction: highlight when a person is genuinely needed, or when the
        work has ended, and label each as what it is.

        ``asks`` here was asserted about a session whose status was ``idle``,
        which meant "finished". Nothing had read the transcript, so the claim
        that it was asking something came from the status field alone and had no
        evidence behind it.
        """
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
        self.assertEqual(len(data["waiting"]), 1, "a finished agent stopped being surfaced")
        entry = data["waiting"][0]
        self.assertEqual(entry["reason"], "done")
        self.assertFalse(
            entry.get("question"),
            "a finished agent must not be presented as having asked something",
        )


if __name__ == "__main__":
    unittest.main()