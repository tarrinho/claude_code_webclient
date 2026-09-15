"""QA: what a model change checks, and how it says so.

Two failures bite at exactly the moment a conversation's model changes, and
neither was visible before.

1. **Context size.** A context that fitted one model's window need not fit the
   next one's. Pedro hit it as:

       400 ContextWindowExceededError ... This model's maximum context length
       is 272144 tokens. However, you requested 32000 output tokens and your
       prompt contains at least 240145 input tokens

   240,145 + 32,000 = 272,145 against a 272,144 window -- over by one token,
   and the output budget is half the reason.

2. **Transcript pollution.** Empty text records (registry #26) are accepted by
   the backend that wrote them and refused by a strict one.

The trap these tests exist to pin is the *measurement*, not the reporting.
`input_tokens` alone is not the context: a cache-reporting model sends most of
the conversation from cache, so "local : 13 : models comparison" -- a real
411,000-token conversation -- has a newest row reading input_tokens=26,
cache_read_tokens=410,958. A check that reported 26 tokens would say "fits
comfortably" about a conversation that is 138,840 tokens over the Qwen window.

Windows are learned from refusals rather than tabulated. A hand-kept table
goes stale the way ai_machines.active_models does (CLAUDE.md §0.1), and a
wrong window is worse than none because it makes a claim about whether the
next turn can run at all.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import db
import shared
from routes.chats import _preflight_lines

REAL_REFUSAL = (
    "400 litellm.ContextWindowExceededError: litellm.BadRequestError: "
    'ContextWindowExceededError: Hosted_vllmException - {"error":{"message":'
    "\"This model's maximum context length is 272144 tokens. However, you "
    "requested 32000 output tokens and your prompt contains at least 240145 "
    'input tokens,"}}'
)


class WindowFromErrorTests(unittest.TestCase):
    """Reading a window out of the message that enforced it."""

    def test_the_real_refusal_is_parsed(self):
        found = shared.context_window_from_error(REAL_REFUSAL)
        self.assertEqual(found["window"], 272144)
        self.assertEqual(found["output"], 32000)
        self.assertEqual(found["input"], 240145)

    def test_the_arithmetic_the_message_describes(self):
        """Over by one token, which is why the output budget matters as much
        as the context does."""
        f = shared.context_window_from_error(REAL_REFUSAL)
        self.assertEqual(f["input"] + f["output"], f["window"] + 1)

    def test_an_unrelated_400_is_not_a_window_reading(self):
        """routes/chats.py warns that a context-window 400 "is also a 400
        about message content". Treating one as the other records a fiction."""
        self.assertIsNone(shared.context_window_from_error(
            "400 messages: text content blocks must be non-empty"))

    def test_junk_and_empty_are_refused(self):
        for text in ("", None, "no numbers here", "maximum context length is"):
            with self.subTest(text=text):
                self.assertIsNone(shared.context_window_from_error(text))

    def test_a_window_without_the_other_figures_still_reads(self):
        found = shared.context_window_from_error(
            "This model's maximum context length is 8192 tokens.")
        self.assertEqual(found, {"window": 8192})


class _DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()


class WindowStoreTests(_DbCase):

    async def test_an_unknown_model_reads_as_none_not_zero(self):
        """"Not known yet" and "fits comfortably" are opposite claims."""
        self.assertIsNone(await db.model_window_get("never-refused"))

    async def test_a_learned_window_round_trips(self):
        await db.model_window_learn("m1", 272144, source="refusal")
        got = await db.model_window_get("m1")
        self.assertEqual(got["window_tokens"], 272144)
        self.assertEqual(got["source"], "refusal")

    async def test_a_later_reading_replaces_an_earlier_one(self):
        """A gateway moving a model to different hardware changes the window;
        the newest refusal is the current truth."""
        await db.model_window_learn("m1", 32000)
        await db.model_window_learn("m1", 272144)
        self.assertEqual((await db.model_window_get("m1"))["window_tokens"], 272144)

    async def test_nonsense_is_refused(self):
        for model, window in (("", 100), ("m", 0), ("m", -5)):
            with self.subTest(model=model, window=window):
                self.assertFalse(await db.model_window_learn(model, window))


class ContextSizeTests(_DbCase):
    """The measurement, which is where the trap is."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await db.chat_create("c1", "sized", None, "/tmp/w", "admin")

    async def test_cache_reads_count_towards_the_context(self):
        """The real shape of "local : 13 : models comparison": 26 tokens sent,
        410,958 served from cache. Reporting 26 would be wrong by four orders
        of magnitude and would read as "plenty of room"."""
        await db.usage_record("c1", "admin", "claude-opus-5", "anthropic",
                              input_tokens=26, cache_read_tokens=410958)
        size = await db.context_size_of("c1")
        self.assertEqual(size["input_tokens"], 26)
        self.assertEqual(size["total_tokens"], 410984)

    async def test_the_newest_turn_is_the_one_measured(self):
        await db.usage_record("c1", "admin", "m", "p", input_tokens=10)
        await db.usage_record("c1", "admin", "m", "p", input_tokens=999)
        self.assertEqual((await db.context_size_of("c1"))["total_tokens"], 999)

    async def test_a_conversation_with_no_turns_is_unknown_not_zero(self):
        await db.chat_create("c2", "fresh", None, "/tmp/w", "admin")
        self.assertIsNone(await db.context_size_of("c2"))


class PreflightNoteTests(unittest.TestCase):
    """What lands in the conversation."""

    BIG = {"model": "claude-opus-5", "measured_at": "2026-09-14T19:30:15Z",
           "input_tokens": 26, "cache_read_tokens": 410958,
           "cache_creation_tokens": 0, "total_tokens": 410984}
    QWEN = {"model": "vllm/Qwen3.6-35B-A3B-NVFP4", "window_tokens": 272144,
            "learned_at": "2026-09-15T09:00:00Z", "source": "refusal"}

    def test_it_reports_the_cache_inclusive_total(self):
        note = "\n".join(_preflight_lines(self.BIG, None, {"repaired": False}))
        self.assertIn("410,984", note)
        self.assertNotRegex(note, r"about \*\*26 tokens")

    def test_a_context_over_the_window_says_by_how_much(self):
        note = "\n".join(_preflight_lines(self.BIG, self.QWEN, {"repaired": False}))
        self.assertIn("138,840 tokens over", note)

    def test_a_context_under_the_window_says_the_headroom(self):
        small = {**self.BIG, "input_tokens": 1000, "cache_read_tokens": 0,
                 "total_tokens": 1000}
        note = "\n".join(_preflight_lines(small, self.QWEN, {"repaired": False}))
        self.assertIn("271,144 tokens of headroom", note)

    def test_an_unknown_window_makes_no_claim_either_way(self):
        """The failure worth avoiding: asserting a fit from a default."""
        note = "\n".join(_preflight_lines(self.BIG, None, {"repaired": False}))
        self.assertIn("not known for this model", note)
        for claim in ("headroom", "over"):
            self.assertNotIn(claim, note.split("**Window**")[1])

    def test_a_repair_reports_what_it_changed(self):
        note = "\n".join(_preflight_lines(
            self.BIG, None,
            {"repaired": True, "removed": 2074, "trimmed": 3, "relinked": 5,
             "backup": "t.jsonl.bak-1"}))
        self.assertIn("2,074 empty records removed", note)
        self.assertIn("3 refused blocks trimmed", note)
        self.assertIn("5 replies relinked", note)
        self.assertIn("t.jsonl.bak-1", note)

    def test_a_clean_transcript_says_so(self):
        note = "\n".join(_preflight_lines(self.BIG, None, {"repaired": False}))
        self.assertIn("clean, nothing to repair", note)

    def test_an_unmeasured_conversation_is_not_reported_as_empty(self):
        note = "\n".join(_preflight_lines(None, None, {"repaired": False}))
        self.assertIn("not measured yet", note)
        self.assertNotIn("0 tokens", note)


if __name__ == "__main__":
    unittest.main()


class PreflightEndpointTests(_DbCase):
    """The route, end to end: what it returns and what it leaves behind."""

    # A uuid-shaped owner: shared.owner_of returns a 32-hex value unchanged,
    # so no user row or admin bootstrap is needed to exercise the handler.
    OWNER = "a" * 32

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.owner = self.OWNER
        await db.chat_create("c1", "switching", None, "/tmp/w", self.owner)

    def _request(self):
        from types import SimpleNamespace
        return SimpleNamespace(
            state=SimpleNamespace(session={"user": self.owner, "role": "admin"}))

    async def test_another_owners_chat_is_404(self):
        from fastapi import HTTPException
        from routes import chats as chat_routes
        await db.chat_create("c2", "theirs", None, "/tmp/w", "b" * 32)
        with self.assertRaises(HTTPException) as ctx:
            await chat_routes.handle_chat_preflight(self._request(), "c2")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_the_note_is_persisted_into_the_conversation(self):
        """The whole point: it has to be readable in the chat afterwards, not
        only in the response to a request nobody kept."""
        from routes import chats as chat_routes
        await db.usage_record("c1", self.owner, "claude-opus-5", "anthropic",
                              input_tokens=26, cache_read_tokens=410958)
        resp = await chat_routes.handle_chat_preflight(self._request(), "c1")
        self.assertEqual(resp.status_code, 200)

        cur = await db.db_conn.execute(
            "SELECT role, content FROM messages WHERE chat_id='c1' "
            "ORDER BY id DESC LIMIT 1")
        row = await cur.fetchone()
        self.assertIn("410,984", row["content"])
        self.assertIn("Transcript", row["content"])

    async def test_it_reports_the_measured_context_in_its_payload(self):
        import json
        from routes import chats as chat_routes
        await db.usage_record("c1", self.owner, "m", "p",
                              input_tokens=100, cache_read_tokens=900)
        resp = await chat_routes.handle_chat_preflight(self._request(), "c1")
        body = json.loads(resp.body)
        self.assertEqual(body["context"]["total_tokens"], 1000)
        self.assertTrue(body["ok"])

    async def test_a_chat_with_no_session_still_reports_rather_than_failing(self):
        """No linked session means nothing to repair -- that is a clean
        result, not an error, and the context still gets sized."""
        import json
        from routes import chats as chat_routes
        await db.usage_record("c1", self.owner, "m", "p", input_tokens=5)
        resp = await chat_routes.handle_chat_preflight(self._request(), "c1")
        body = json.loads(resp.body)
        self.assertFalse(body["repair"]["repaired"])
        self.assertIn("clean", body["note"])

    async def test_a_known_window_reaches_the_note(self):
        from routes import chats as chat_routes
        await db.chat_update("c1", self.owner, model="vllm/Qwen3.6-35B-A3B-NVFP4")
        await db.model_window_learn("vllm/Qwen3.6-35B-A3B-NVFP4", 272144, "refusal")
        await db.usage_record("c1", self.owner, "claude-opus-5", "anthropic",
                              input_tokens=26, cache_read_tokens=410958)
        resp = await chat_routes.handle_chat_preflight(self._request(), "c1")
        import json
        body = json.loads(resp.body)
        self.assertEqual(body["window"]["window_tokens"], 272144)
        self.assertIn("138,840 tokens over", body["note"])


class WindowLearningHookTests(_DbCase):
    """The hook that makes a window known in the first place."""

    async def test_a_context_window_refusal_is_learned(self):
        from routes.chats import _learn_context_window
        self.assertTrue(await _learn_context_window(REAL_REFUSAL, "qwen-x"))
        self.assertEqual((await db.model_window_get("qwen-x"))["window_tokens"],
                         272144)

    async def test_an_unrelated_failure_teaches_nothing(self):
        """A context-window 400 is also a 400 about message content; learning
        from the wrong one records a fiction that then gets reported as fact."""
        from routes.chats import _learn_context_window
        self.assertFalse(await _learn_context_window(
            "400 messages: text content blocks must be non-empty", "qwen-x"))
        self.assertIsNone(await db.model_window_get("qwen-x"))

    async def test_no_model_means_nothing_to_attribute_it_to(self):
        from routes.chats import _learn_context_window
        self.assertFalse(await _learn_context_window(REAL_REFUSAL, None))
