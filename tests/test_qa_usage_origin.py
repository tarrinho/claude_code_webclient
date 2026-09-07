"""QA: where a turn's tokens came from, and which of them mean anything.

Pedro spent a day working from a phone through the website and found the Usage
page reporting vastly more "terminal" usage than website usage. Both halves of
that were real defects, and neither was double counting:

1. **Attribution.** Terminal usage was dominated by the agent sessions the
   console had adopted -- 175 million tokens for one of them in a day against
   271 thousand typed into the website -- all filed under the operator's own
   account as though he had typed it. One bar labelled "terminal" gave no way to
   see that.

2. **Magnitude.** A model that reports no cache breakdown puts the whole
   conversation into ``input_tokens`` on every turn. One session averaged
   106,769 input tokens a turn with no cache line, so summing it reported
   409 million tokens for work that mostly re-sent the same context.

Origin is now recorded rather than inferred from "does this row have a session
id", which was true in practice and a guess in principle -- every web turn runs
against a session-linked conversation, so nothing but the absence of a column
kept them apart.
"""
from __future__ import annotations

import datetime
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import config
import db
import transcripts
from routes import misc as misc_routes


def _record(model="claude-opus-5", *, cache=True, inp=1000, out=50):
    """An assistant transcript record, with or without cache accounting."""
    usage = {"input_tokens": inp, "output_tokens": out}
    if cache:
        usage["cache_read_input_tokens"] = 900
        usage["cache_creation_input_tokens"] = 10
    return {"type": "assistant", "message": {"model": model, "usage": usage}}


class UnsplitDetectionQA(unittest.TestCase):
    """Detected from the transcript stating it, not from a size threshold."""

    def test_a_model_reporting_cache_is_split(self):
        row = transcripts._usage_from_record(_record(cache=True))
        self.assertFalse(row["context_unsplit"])
        self.assertEqual(row["cache_read_tokens"], 900)

    def test_a_model_reporting_no_cache_is_flagged(self):
        row = transcripts._usage_from_record(
            _record("nvidia/Qwen3.6-35B-A3B-NVFP4", cache=False, inp=106769)
        )
        self.assertTrue(row["context_unsplit"])
        self.assertEqual(row["cache_read_tokens"], 0)

    def test_a_zero_cache_line_still_counts_as_split(self):
        # Reporting zero is a statement; omitting the key is the absence of one.
        record = _record(cache=False, inp=5)
        record["message"]["usage"]["cache_read_input_tokens"] = 0
        self.assertFalse(transcripts._usage_from_record(record)["context_unsplit"])

    def test_a_large_input_that_reports_cache_is_not_flagged(self):
        """Key presence, not size. This is the case that separates the two.

        A long Anthropic-served turn has a large input AND a cache line; a size
        threshold would flag it and quietly remove real spend from the total.
        The first version of this test only used small inputs, so a
        threshold-based implementation passed it -- the test was weak, not the
        rule.
        """
        row = transcripts._usage_from_record(_record(cache=True, inp=200_000))
        self.assertFalse(row["context_unsplit"])

    def test_a_synthetic_reply_is_not_spend(self):
        record = _record()
        record["message"]["model"] = "<synthetic>"
        self.assertIsNone(transcripts._usage_from_record(record))


class OriginRecordedQA(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.db_patch.start()
        await db.init()
        await db.chat_create("c1", "Website chat", None, "/tmp/w", "admin")
        await db.chat_set_session("c1", "sess-agent")

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.tmp.cleanup()

    async def test_a_web_turn_records_origin_web(self):
        await db.usage_record("c1", "admin", "claude-opus-5", "claude_code",
                              input_tokens=10, output_tokens=5)
        rows = await db.usage_by_origin("admin", days=None)
        self.assertEqual([r["origin"] for r in rows], ["web"])

    async def test_a_row_carrying_a_session_id_is_still_web_if_it_says_so(self):
        """Why the column exists, stated accurately.

        The old rule -- "the row has a session id, therefore a terminal" -- was
        never actually wrong on live data, because ``usage_record`` does not
        write one. Replacing inference with a stored value is defensive rather
        than a bug fix, and mutating it back passes every other test here; that
        is worth admitting rather than dressing up.

        What it buys is this case: a row that carries both a session id and an
        explicit origin is labelled by what it says, so attributing a web turn
        to a session -- which the queue and reattach work makes plausible --
        cannot silently reclassify a day's usage.
        """
        await db.db_conn.execute(
            "INSERT INTO usage_events (chat_id, session_id, owner_id, model, "
            " provider, input_tokens, output_tokens, cache_read_tokens, "
            " cache_creation_tokens, is_error, created_at, origin) "
            "VALUES ('c1', 'sess-agent', 'admin', 'm', 'anthropic', 10, 5, 4, "
            "        1, 0, ?, 'web')",
            (db._now(),),
        )
        await db.db_conn.commit()
        rows = await db.usage_by_origin("admin", days=None)
        self.assertEqual([r["origin"] for r in rows], ["web"])

    async def test_an_imported_turn_records_origin_terminal_and_the_flag(self):
        await db.usage_import("admin", "sess-agent", [
            {"model": "gw/model", "input_tokens": 106769, "output_tokens": 40,
             "cache_read_tokens": 0, "cache_creation_tokens": 0,
             "context_unsplit": True, "offset": 10},
        ], 10)
        rows = await db.usage_by_origin("admin", days=None)
        self.assertEqual(rows[0]["origin"], "terminal")
        self.assertEqual(rows[0]["unsplit_requests"], 1)
        self.assertEqual(rows[0]["unsplit_tokens"], 106769)

    async def test_unsplit_tokens_are_reported_apart_from_the_rest(self):
        # Both origins present; only the gateway rows are flagged.
        await db.usage_record("c1", "admin", "claude-opus-5", "claude_code",
                              input_tokens=100, output_tokens=20)
        await db.usage_import("admin", "sess-agent", [
            {"model": "gw/model", "input_tokens": 90000, "output_tokens": 10,
             "cache_read_tokens": 0, "cache_creation_tokens": 0,
             "context_unsplit": True, "offset": 1},
            {"model": "claude-opus-5", "input_tokens": 500, "output_tokens": 30,
             "cache_read_tokens": 400, "cache_creation_tokens": 5,
             "context_unsplit": False, "offset": 2},
        ], 2)
        by_origin = {r["origin"]: r for r in await db.usage_by_origin("admin", None)}
        self.assertEqual(by_origin["web"]["unsplit_tokens"], 0)
        self.assertEqual(by_origin["terminal"]["unsplit_tokens"], 90000)
        # The comparable figure is what is left once the re-counted context is
        # taken out: 500 + 30, not 90,540.
        terminal = by_origin["terminal"]
        comparable = (terminal["input_tokens"] + terminal["output_tokens"]
                      - terminal["unsplit_tokens"])
        self.assertEqual(comparable, 540)

    async def test_sessions_are_named_and_ranked(self):
        await db.chat_create("c2", "cweb7", None, "/tmp/x", "admin")
        await db.chat_set_session("c2", "sess-big")
        await db.usage_import("admin", "sess-big", [
            {"model": "gw/model", "input_tokens": 175_000_000, "output_tokens": 1,
             "cache_read_tokens": 0, "cache_creation_tokens": 0,
             "context_unsplit": True, "offset": 1}], 1)
        await db.usage_import("admin", "sess-agent", [
            {"model": "gw/model", "input_tokens": 100, "output_tokens": 1,
             "cache_read_tokens": 0, "cache_creation_tokens": 0,
             "context_unsplit": False, "offset": 1}], 1)
        rows = await db.usage_by_session("admin", days=None)
        # Named, so a surprising total is explainable rather than anonymous.
        self.assertEqual([r["title"] for r in rows], ["cweb7", "Website chat"])
        self.assertEqual(rows[0]["requests"], 1)

    async def test_a_session_with_no_conversation_still_appears(self):
        await db.usage_import("admin", "orphan-sess", [
            {"model": "m", "input_tokens": 5, "output_tokens": 1,
             "cache_read_tokens": 0, "cache_creation_tokens": 0,
             "context_unsplit": False, "offset": 1}], 1)
        rows = await db.usage_by_session("admin", days=None)
        self.assertEqual(rows[0]["session_id"], "orphan-sess")
        self.assertIsNone(rows[0]["title"])

    async def test_another_owner_sees_none_of_it(self):
        await db.usage_import("admin", "sess-agent", [
            {"model": "m", "input_tokens": 5, "output_tokens": 1,
             "cache_read_tokens": 0, "cache_creation_tokens": 0,
             "context_unsplit": False, "offset": 1}], 1)
        self.assertEqual(await db.usage_by_origin("someone-else", None), [])
        self.assertEqual(await db.usage_by_session("someone-else", None), [])


class BackfillQA(unittest.IsolatedAsyncioTestCase):
    """An existing database must not lose its history to the new column."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.db_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.tmp.cleanup()

    async def test_rows_written_before_the_column_are_labelled(self):
        # Simulate pre-migration rows: origin blank, distinguishable only by the
        # old rule.
        await db.db_conn.execute(
            "INSERT INTO usage_events (chat_id, session_id, owner_id, model, "
            " provider, input_tokens, output_tokens, cache_read_tokens, "
            " cache_creation_tokens, is_error, created_at, origin) "
            "VALUES ('', 'old-sess', 'admin', 'm', 'cli', 50000, 5, 0, 0, 0, "
            "        '2026-08-01T00:00:00Z', '')"
        )
        await db.db_conn.execute(
            "INSERT INTO usage_events (chat_id, owner_id, model, provider, "
            " input_tokens, output_tokens, cache_read_tokens, "
            " cache_creation_tokens, is_error, created_at, origin) "
            "VALUES ('c9', 'admin', 'm', 'anthropic', 10, 5, 4, 1, 0, "
            "        '2026-08-01T00:00:00Z', '')"
        )
        await db.db_conn.commit()

        await db._ensure_usage_columns()

        rows = {r["origin"]: r for r in await db.usage_by_origin("admin", None)}
        self.assertEqual(sorted(rows), ["terminal", "web"])
        self.assertEqual(rows["terminal"]["requests"], 1)
        self.assertEqual(rows["web"]["requests"], 1)
        # The old terminal row had no cache line and a large input, so it is
        # flagged: otherwise the backfilled history keeps the inflated total.
        self.assertEqual(rows["terminal"]["unsplit_tokens"], 50000)
        self.assertEqual(rows["web"]["unsplit_tokens"], 0)

    async def test_the_backfill_does_not_relabel_an_explicit_origin(self):
        await db.usage_record("c1", "admin", "m", "claude_code",
                              input_tokens=1, output_tokens=1, origin="web")
        await db._ensure_usage_columns()
        await db._ensure_usage_columns()   # idempotent
        rows = await db.usage_by_origin("admin", None)
        self.assertEqual([r["origin"] for r in rows], ["web"])


class UsageApiQA(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.db_patch.start()
        await db.init()
        await db.chat_create("c1", "cweb7", None, "/tmp/w", "admin")
        await db.chat_set_session("c1", "sess-agent")
        await db.usage_import("admin", "sess-agent", [
            {"model": "gw/model", "input_tokens": 175_000, "output_tokens": 3,
             "cache_read_tokens": 0, "cache_creation_tokens": 0,
             "context_unsplit": True, "offset": 1}], 1)

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.tmp.cleanup()

    async def _get(self):
        request = SimpleNamespace(
            state=SimpleNamespace(session={"user": "admin"}), query_params={}
        )
        with patch.object(misc_routes, "_import_cli_usage", return_value=0):
            response = await misc_routes.handle_usage_get(request)
        return json.loads(response.body)

    async def test_the_response_carries_both_breakdowns(self):
        payload = await self._get()
        self.assertIn("by_origin", payload)
        self.assertIn("by_session", payload)
        self.assertEqual(payload["by_origin"][0]["origin"], "terminal")
        self.assertEqual(payload["by_session"][0]["title"], "cweb7")

    async def test_the_unsplit_rows_carry_an_explanation(self):
        payload = await self._get()
        note = payload["by_origin"][0].get("unsplit_note") or ""
        # The number is startling on its own; the response has to say why rather
        # than leaving the reader to guess at a billion tokens.
        self.assertIn("cache", note.lower())
        self.assertIn("whole conversation", note.lower())

    async def test_no_note_when_nothing_was_unsplit(self):
        await db.usage_record("c1", "admin", "claude-opus-5", "claude_code",
                              input_tokens=5, output_tokens=1)
        payload = await self._get()
        web = next(r for r in payload["by_origin"] if r["origin"] == "web")
        self.assertNotIn("unsplit_note", web)


if __name__ == "__main__":
    unittest.main()


class RoutedAttributionQA(unittest.IsolatedAsyncioTestCase):
    """A request made in the website that ran in a terminal.

    The case Pedro hit: a conversation linked to a live terminal has its web
    requests *typed into that terminal* rather than run by the server, so the
    tokens land in the terminal's transcript and were imported as the terminal's
    own work. Confirmed from the log --

        prompt delivered to live terminal chat=283a9a5b... session=8e2e8bd0...

    -- so "asked in the web, counted as terminal" was accurate, and neither
    plain label describes it. It now gets a third origin of its own.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.db_patch.start()
        await db.init()
        await db.chat_create("c1", "cweb5", None, "/tmp/w", "admin")
        await db.chat_set_session("c1", "sess-live")

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def _row(offset, when, prompt="", inp=1000):
        return {"model": "claude-opus-5", "input_tokens": inp, "output_tokens": 10,
                "cache_read_tokens": 5, "cache_creation_tokens": 0,
                "context_unsplit": False, "offset": offset, "timestamp": when,
                # What the session was working on when this turn ran.
                "after_prompt": prompt}

    async def test_a_turn_is_claimed_only_by_the_prompt_that_caused_it(self):
        """The bug the offset-only version had, caught live.

        Typing into a busy session queues the input, so work can begin long
        after the mark and everything the agent does meanwhile belongs to
        whatever it was already doing. The first version credited a routed
        request with a peer message the agent happened to answer in between --
        observed on the real machine, not theorised.
        """
        await db.routed_request_add("sess-live", "c1", "admin", 0, "please do X")
        marks = await db.routed_markers("sess-live")
        when = marks[0]["created_at"]
        await db.usage_import("admin", "sess-live", [
            self._row(100, when, "please do X"),        # caused by the request
            self._row(200, when, "something else"),     # the agent's own work
        ], 200)
        rows = {r["origin"]: r for r in await db.usage_by_origin("admin", None)}
        self.assertEqual(rows["web-routed"]["requests"], 1)
        self.assertEqual(rows["terminal"]["requests"], 1)

    async def test_queued_work_is_still_attributed_however_late_it_runs(self):
        # The offset cannot express this: the turns arrive after other work.
        await db.routed_request_add("sess-live", "c1", "admin", 0, "queued ask")
        marks = await db.routed_markers("sess-live")
        asked = datetime.datetime.fromisoformat(
            marks[0]["created_at"].replace("Z", "+00:00"))
        later = (asked + datetime.timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
        await db.usage_import("admin", "sess-live", [
            self._row(50, marks[0]["created_at"], "other business"),
            self._row(900, later, "queued ask"),
        ], 900)
        rows = {r["origin"]: r for r in await db.usage_by_origin("admin", None)}
        self.assertEqual(rows["web-routed"]["requests"], 1)

    async def test_whitespace_and_case_do_not_break_the_match(self):
        # Two records of one prompt: what the console sent, and what the CLI
        # received. They differ in formatting.
        await db.routed_request_add("sess-live", "c1", "admin", 0, "Do   The Thing")
        marks = await db.routed_markers("sess-live")
        await db.usage_import("admin", "sess-live", [
            self._row(10, marks[0]["created_at"], "do the thing")], 10)
        rows = await db.usage_by_origin("admin", None)
        self.assertEqual([r["origin"] for r in rows], ["web-routed"])

    async def test_a_routed_row_carries_the_conversation_it_came_from(self):
        await db.routed_request_add("sess-live", "c1", "admin", 0, "trace me")
        marks = await db.routed_markers("sess-live")
        await db.usage_import("admin", "sess-live", [
            self._row(10, marks[0]["created_at"], "trace me")], 10)
        cur = await db.db_conn.execute(
            "SELECT chat_id FROM usage_events WHERE origin = 'web-routed'")
        row = await cur.fetchone()
        # Without the chat id the row cannot be traced back to the request.
        self.assertEqual(row["chat_id"], "c1")

    async def test_a_marker_cannot_claim_turns_that_predate_it(self):
        await db.routed_request_add("sess-live", "c1", "admin", 500, "same words")
        marks = await db.routed_markers("sess-live")
        await db.usage_import("admin", "sess-live", [
            self._row(400, marks[0]["created_at"], "same words")], 400)
        rows = await db.usage_by_origin("admin", None)
        self.assertEqual([r["origin"] for r in rows], ["terminal"])

    async def test_attribution_expires_as_a_backstop(self):
        """Bounded even when the prompt matches, for a session that goes quiet.

        The window is no longer the mechanism -- the prompt match is -- but a
        marker must not be able to claim turns forever.
        """
        await db.routed_request_add("sess-live", "c1", "admin", 0, "old ask")
        marks = await db.routed_markers("sess-live")
        asked = datetime.datetime.fromisoformat(
            marks[0]["created_at"].replace("Z", "+00:00"))
        late = (asked + datetime.timedelta(seconds=db.ROUTED_WINDOW_S + 60)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
        await db.usage_import("admin", "sess-live", [
            self._row(10, late, "old ask")], 10)
        rows = await db.usage_by_origin("admin", None)
        self.assertEqual([r["origin"] for r in rows], ["terminal"])

    async def test_a_session_never_routed_to_is_untouched(self):
        await db.usage_import("admin", "sess-live", [
            self._row(10, "2026-08-30T10:00:00Z", "anything")], 10)
        rows = await db.usage_by_origin("admin", None)
        self.assertEqual([r["origin"] for r in rows], ["terminal"])

    async def test_two_requests_are_told_apart_by_what_they_asked(self):
        await db.chat_create("c2", "second ask", None, "/tmp/y", "admin")
        await db.chat_set_session("c2", "sess-live")
        await db.routed_request_add("sess-live", "c1", "admin", 0, "first thing")
        await db.routed_request_add("sess-live", "c2", "admin", 0, "second thing")
        marks = await db.routed_markers("sess-live")
        when = marks[0]["created_at"]
        await db.usage_import("admin", "sess-live", [
            self._row(500, when, "first thing"),
            self._row(1000, when, "second thing")], 1000)
        cur = await db.db_conn.execute(
            "SELECT chat_id FROM usage_events WHERE origin='web-routed' ORDER BY id")
        self.assertEqual([r["chat_id"] for r in await cur.fetchall()], ["c1", "c2"])

    async def test_marking_needs_a_session_and_a_chat(self):
        self.assertIsNone(await db.routed_request_add("", "c1", "admin", 0, "x"))
        self.assertIsNone(await db.routed_request_add("s", "", "admin", 0, "x"))
        self.assertEqual(await db.routed_markers("s"), [])
