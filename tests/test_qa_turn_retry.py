"""QA: automatic retry when a turn completes cleanly but answers with nothing.

Small gateway models occasionally return an empty text block while still
reporting a normal, error-free completion -- observed on `vllm/Qwen3.5-0.8`
asked to self-identify (see CLAUDE.md). Before this, that surfaced to the user
as a turn that silently produced nothing, indistinguishable from a real
failure except that nothing was logged as one. `runner.run_turn` and
`runner.stream_turn` now retry such a completion (never a real error --
CLAUDE.md rule 4 says an error is an event, not an exception on the blocking
paths and an explicit `{"type": "error"}` event on the streaming ones, and
this feature must not touch either) up to `config.TURN_RETRY_MAX` times.

Each discarded attempt still spent real tokens against the backend, so
CLAUDE.md rule 5 ("record failures too") applies to it exactly as to a normal
turn -- these tests assert that spend surfaces through `take_retried_usage`
rather than silently disappearing.
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
import runner


def _usage(output_tokens: int, model: str = "m") -> dict:
    return {
        "type": "usage",
        "models": {
            model: {
                "input_tokens": 5,
                "output_tokens": output_tokens,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "cost_basis": None,
            }
        },
        "cost_usd": 0.001,
        "duration_ms": 10,
        "is_error": False,
    }


class IsNonAnswerTests(unittest.TestCase):
    """Pure function, no transport involved."""

    def test_empty_text_below_the_floor_is_a_non_answer(self):
        self.assertTrue(runner._is_non_answer([""], _usage(2)))

    def test_empty_text_at_the_floor_is_not_a_non_answer(self):
        # TURN_RETRY_MIN_TOKENS default is 5; "below" excludes the boundary.
        self.assertFalse(runner._is_non_answer([""], _usage(config.TURN_RETRY_MIN_TOKENS)))

    def test_empty_text_above_the_floor_is_not_a_non_answer(self):
        self.assertFalse(runner._is_non_answer([""], _usage(20)))

    def test_any_real_text_is_never_a_non_answer_regardless_of_tokens(self):
        self.assertFalse(runner._is_non_answer(["ok"], _usage(0)))

    def test_whitespace_only_text_counts_as_empty(self):
        self.assertTrue(runner._is_non_answer(["   \n"], _usage(0)))

    def test_missing_usage_with_no_text_is_a_non_answer(self):
        self.assertTrue(runner._is_non_answer([], {}))

    def test_multiple_models_sum_their_output_tokens(self):
        frame = {
            "models": {
                "a": {"output_tokens": 2},
                "b": {"output_tokens": 2},
            }
        }
        self.assertTrue(runner._is_non_answer([""], frame))
        frame["models"]["b"]["output_tokens"] = 3
        self.assertFalse(runner._is_non_answer([""], frame))


class RunTurnRetryTests(unittest.IsolatedAsyncioTestCase):
    """The blocking path: `_execute_direct` is the seam under `run_turn`."""

    async def test_retries_once_then_returns_the_good_attempt(self):
        calls: list[int] = []

        async def fake_execute_direct(prompt, session_id, work_dir, chat_id, model, owner):
            calls.append(1)
            if len(calls) == 1:
                runner.record_usage_frame(chat_id, _usage(1))
                return [], "sid-bad"
            runner.record_usage_frame(chat_id, _usage(20))
            return ["real answer"], "sid-good"

        chat_id = "chat-retry-success"
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(config, "PROJECTS_ROOT", root),
            patch.object(config, "PROXY_ENABLED", False),
            patch.object(config, "TURN_RETRY_MAX", 2),
            patch.object(runner, "_execute_direct", fake_execute_direct),
        ):
            chunks, sid = await runner.run_turn("hi", None, root, chat_id)

        self.assertEqual(chunks, ["real answer"])
        self.assertEqual(sid, "sid-good")
        self.assertEqual(len(calls), 2)

        retried = runner.take_retried_usage(chat_id)
        self.assertEqual(len(retried), 1)
        self.assertEqual(retried[0]["models"]["m"]["output_tokens"], 1)

        kept = runner.take_last_usage(chat_id)
        self.assertEqual(kept["models"]["m"]["output_tokens"], 20)

    async def test_cap_exhausted_returns_the_last_attempt_anyway(self):
        calls: list[int] = []

        async def fake_execute_direct(prompt, session_id, work_dir, chat_id, model, owner):
            calls.append(1)
            runner.record_usage_frame(chat_id, _usage(0))
            return [], f"sid-{len(calls)}"

        chat_id = "chat-retry-exhausted"
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(config, "PROJECTS_ROOT", root),
            patch.object(config, "PROXY_ENABLED", False),
            patch.object(config, "TURN_RETRY_MAX", 2),
            patch.object(runner, "_execute_direct", fake_execute_direct),
        ):
            chunks, sid = await runner.run_turn("hi", None, root, chat_id)

        self.assertEqual(chunks, [])
        self.assertEqual(sid, "sid-3")
        self.assertEqual(len(calls), 3, "TURN_RETRY_MAX=2 must allow exactly 3 attempts total")

        retried = runner.take_retried_usage(chat_id)
        self.assertEqual(len(retried), 2, "the first two discarded attempts, not the kept last one")

    async def test_a_good_first_attempt_never_retries(self):
        calls: list[int] = []

        async def fake_execute_direct(prompt, session_id, work_dir, chat_id, model, owner):
            calls.append(1)
            runner.record_usage_frame(chat_id, _usage(20))
            return ["fine on the first try"], "sid-1"

        chat_id = "chat-retry-not-needed"
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(config, "PROJECTS_ROOT", root),
            patch.object(config, "PROXY_ENABLED", False),
            patch.object(config, "TURN_RETRY_MAX", 2),
            patch.object(runner, "_execute_direct", fake_execute_direct),
        ):
            chunks, _sid = await runner.run_turn("hi", None, root, chat_id)

        self.assertEqual(chunks, ["fine on the first try"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(runner.take_retried_usage(chat_id), [])

    async def test_retry_max_zero_disables_retry(self):
        """A deployment that sets WC_TURN_RETRY_MAX=0 keeps today's behaviour."""
        calls: list[int] = []

        async def fake_execute_direct(prompt, session_id, work_dir, chat_id, model, owner):
            calls.append(1)
            runner.record_usage_frame(chat_id, _usage(0))
            return [], "sid-1"

        chat_id = "chat-retry-disabled"
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(config, "PROJECTS_ROOT", root),
            patch.object(config, "PROXY_ENABLED", False),
            patch.object(config, "TURN_RETRY_MAX", 0),
            patch.object(runner, "_execute_direct", fake_execute_direct),
        ):
            chunks, _sid = await runner.run_turn("hi", None, root, chat_id)

        self.assertEqual(chunks, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(runner.take_retried_usage(chat_id), [])


class StreamTurnRetryTests(unittest.IsolatedAsyncioTestCase):
    """The streaming path: `_execute_direct_stream` is the seam under `stream_turn`."""

    async def test_retries_once_then_streams_the_good_attempt(self):
        calls: list[int] = []

        async def fake_stream(prompt, session_id, work_dir, chat_id, model, owner):
            calls.append(1)
            if len(calls) == 1:
                yield _usage(1)
                yield {"type": "done"}
            else:
                yield {"type": "text", "content": "real answer"}
                yield _usage(20)
                yield {"type": "done"}

        chat_id = "chat-stream-retry-success"
        events = []
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(config, "PROJECTS_ROOT", root),
            patch.object(config, "PROXY_ENABLED", False),
            patch.object(config, "TURN_RETRY_MAX", 2),
            patch.object(runner, "_execute_direct_stream", fake_stream),
        ):
            async for event in runner.stream_turn("hi", None, root, chat_id):
                events.append(event)

        self.assertEqual(len(calls), 2)
        types = [e["type"] for e in events]
        self.assertEqual(types, ["status", "text", "usage", "done"])
        self.assertEqual(events[0]["status"], "api_retry")
        # Matches the existing api_retry shape conversation.js already renders
        # via setStreamState('retrying') -- see web/assets/conversation.js.
        self.assertEqual(events[0]["attempt"], 2)
        self.assertEqual(events[0]["max_retries"], 2)
        self.assertEqual(events[1]["content"], "real answer")
        self.assertEqual(events[2]["models"]["m"]["output_tokens"], 20)

        retried = runner.take_retried_usage(chat_id)
        self.assertEqual(len(retried), 1)
        self.assertEqual(retried[0]["models"]["m"]["output_tokens"], 1)

    async def test_cap_exhausted_streams_the_last_attempt_anyway(self):
        calls: list[int] = []

        async def fake_stream(prompt, session_id, work_dir, chat_id, model, owner):
            calls.append(1)
            yield _usage(0)
            yield {"type": "done"}

        chat_id = "chat-stream-retry-exhausted"
        events = []
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(config, "PROJECTS_ROOT", root),
            patch.object(config, "PROXY_ENABLED", False),
            patch.object(config, "TURN_RETRY_MAX", 2),
            patch.object(runner, "_execute_direct_stream", fake_stream),
        ):
            async for event in runner.stream_turn("hi", None, root, chat_id):
                events.append(event)

        self.assertEqual(len(calls), 3)
        # Two retry statuses (before attempt 2 and attempt 3), then the third
        # attempt's usage/done delivered as-is.
        statuses = [e for e in events if e["type"] == "status"]
        self.assertEqual(len(statuses), 2)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-2]["type"], "usage")

        retried = runner.take_retried_usage(chat_id)
        self.assertEqual(len(retried), 2)

    async def test_a_real_error_is_never_retried(self):
        """CLAUDE.md rule 4: an error is an event.  A clean, empty completion is
        retried; an explicit error event must pass straight through instead."""
        calls: list[int] = []

        async def fake_stream(prompt, session_id, work_dir, chat_id, model, owner):
            calls.append(1)
            yield {"type": "error", "error": "boom"}

        chat_id = "chat-stream-real-error"
        events = []
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(config, "PROJECTS_ROOT", root),
            patch.object(config, "PROXY_ENABLED", False),
            patch.object(config, "TURN_RETRY_MAX", 2),
            patch.object(runner, "_execute_direct_stream", fake_stream),
        ):
            async for event in runner.stream_turn("hi", None, root, chat_id):
                events.append(event)

        self.assertEqual(len(calls), 1, "a real error must not be retried")
        self.assertEqual([e["type"] for e in events], ["error"])

    async def test_a_good_first_attempt_never_retries(self):
        calls: list[int] = []

        async def fake_stream(prompt, session_id, work_dir, chat_id, model, owner):
            calls.append(1)
            yield {"type": "text", "content": "fine on the first try"}
            yield _usage(20)
            yield {"type": "done"}

        chat_id = "chat-stream-retry-not-needed"
        events = []
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(config, "PROJECTS_ROOT", root),
            patch.object(config, "PROXY_ENABLED", False),
            patch.object(config, "TURN_RETRY_MAX", 2),
            patch.object(runner, "_execute_direct_stream", fake_stream),
        ):
            async for event in runner.stream_turn("hi", None, root, chat_id):
                events.append(event)

        self.assertEqual(len(calls), 1)
        self.assertNotIn("status", [e["type"] for e in events])
        self.assertEqual(runner.take_retried_usage(chat_id), [])


if __name__ == "__main__":
    unittest.main()
