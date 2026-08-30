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

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app
import config
import db
import transcripts


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
        await db.usage_record("c1", "admin", "claude-opus-5", "anthropic",
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
        await db.usage_record("c1", "admin", "claude-opus-5", "anthropic",
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
        await db.usage_record("c1", "admin", "m", "anthropic",
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
        with patch.object(app, "_import_cli_usage", return_value=0):
            response = await app.handle_usage_get(request)
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
        await db.usage_record("c1", "admin", "claude-opus-5", "anthropic",
                              input_tokens=5, output_tokens=1)
        payload = await self._get()
        web = next(r for r in payload["by_origin"] if r["origin"] == "web")
        self.assertNotIn("unsplit_note", web)


if __name__ == "__main__":
    unittest.main()
