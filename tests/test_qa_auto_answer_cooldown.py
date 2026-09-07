"""QA: auto-answer cooldown gates repeated questions.

The cooldown lives in `auto_answer.py` and uses `_auto_answer_cooldown` dict
mapping (chat_id, hash[:12]) → monotonic_timestamp. The window is 300s.

  * `_cooldown_check(key)` -- returns True if safe to answer, False if in
    cooldown. Sets the timestamp on first call. Async.

  * `_cooldown_cleanup()` -- removes entries older than 2x COOLDOWN_S.

  * `_start_cooldown_cleanup()` / `_stop_cooldown_cleanup()` -- start/stop
    background task that runs every 600s.

This file tests:
  * _cooldown_check -- returns cooldown status and updates state.
  * _cooldown_cleanup -- removes stale entries.
  * _start_cooldown_cleanup / _stop_cooldown_cleanup -- background task.
"""
from __future__ import annotations

import asyncio
import hashlib
import time as _time_mod
import unittest

import auto_answer


class CooldownCheckTests(unittest.IsolatedAsyncioTestCase):
    """_cooldown_check -- returns cooldown status."""

    def _hash(self, chat_id: str, text: str) -> str:
        raw = f"message|{text}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def _set_cooldown(self, chat_id: str, text: str, value: float) -> None:
        key = (chat_id, self._hash(chat_id, text))
        auto_answer._auto_answer_cooldown[key] = value

    async def asyncSetUp(self):
        auto_answer._auto_answer_cooldown.clear()

    async def asyncTearDown(self):
        auto_answer._auto_answer_cooldown.clear()

    async def test_first_call_returns_true(self):
        key = ("chat-1", self._hash("chat-1", "what is 2+2?"))
        result = await auto_answer._cooldown_check(key)
        self.assertTrue(result, "first call must return True (safe to answer)")

    async def test_second_call_within_cooldown_returns_false(self):
        chat_id = "chat-1"
        text = "what is 2+2?"
        key = (chat_id, self._hash(chat_id, text))
        await auto_answer._cooldown_check(key)  # first -- sets timestamp
        self._set_cooldown(chat_id, text, _time_mod.monotonic())
        result = await auto_answer._cooldown_check(key)
        self.assertFalse(result, "call within cooldown must return False")

    async def test_expires_after_cooldown_window(self):
        chat_id = "chat-1"
        text = "what is 2+2?"
        key = (chat_id, self._hash(chat_id, text))
        await auto_answer._cooldown_check(key)  # first
        self._set_cooldown(chat_id, text, _time_mod.monotonic())
        await auto_answer._cooldown_check(key)  # now in cooldown
        # Fast-forward past cooldown window (300s)
        self._set_cooldown(chat_id, text, _time_mod.monotonic() - 310)
        result = await auto_answer._cooldown_check(key)
        self.assertTrue(result, "cooldown must expire after 300 seconds")


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    """_cooldown_cleanup -- removes stale entries (2x COOLDOWN_S = 600s)."""

    async def asyncSetUp(self):
        auto_answer._auto_answer_cooldown.clear()

    async def asyncTearDown(self):
        auto_answer._auto_answer_cooldown.clear()

    async def test_removes_stale_entries(self):
        auto_answer._auto_answer_cooldown[("chat-1", "stale_hash")] = (
            _time_mod.monotonic() - 601
        )
        auto_answer._auto_answer_cooldown[("chat-1", "fresh_hash")] = _time_mod.monotonic()
        self.assertEqual(len(auto_answer._auto_answer_cooldown), 2)
        auto_answer._cooldown_cleanup()
        self.assertEqual(len(auto_answer._auto_answer_cooldown), 1)
        self.assertIn(("chat-1", "fresh_hash"), auto_answer._auto_answer_cooldown)

    async def test_removes_all_stale(self):
        auto_answer._auto_answer_cooldown[("chat-1", "h1")] = (
            _time_mod.monotonic() - 700
        )
        auto_answer._auto_answer_cooldown[("chat-1", "h2")] = (
            _time_mod.monotonic() - 650
        )
        self.assertEqual(len(auto_answer._auto_answer_cooldown), 2)
        auto_answer._cooldown_cleanup()
        self.assertEqual(len(auto_answer._auto_answer_cooldown), 0)

    async def test_keeps_recent_entries(self):
        auto_answer._auto_answer_cooldown[("chat-1", "h1")] = _time_mod.monotonic()
        auto_answer._cooldown_cleanup()
        self.assertEqual(len(auto_answer._auto_answer_cooldown), 1)


class CooldownCleanupTaskTests(unittest.IsolatedAsyncioTestCase):
    """_start_cooldown_cleanup / _stop_cooldown_cleanup -- background task."""

    async def asyncSetUp(self):
        self.orig_task = auto_answer._cooldown_cleanup_task
        auto_answer._cooldown_cleanup_task = None
        auto_answer._auto_answer_cooldown.clear()

    async def asyncTearDown(self):
        if auto_answer._cooldown_cleanup_task:
            try:
                auto_answer._cooldown_cleanup_task.cancel()
                await auto_answer._cooldown_cleanup_task
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                pass
        auto_answer._cooldown_cleanup_task = self.orig_task
        auto_answer._auto_answer_cooldown.clear()

    async def test_start_creates_task(self):
        await auto_answer._start_cooldown_cleanup()
        self.assertIsNotNone(auto_answer._cooldown_cleanup_task)
        self.assertFalse(auto_answer._cooldown_cleanup_task.done())

    async def test_stop_cancels_task(self):
        await auto_answer._start_cooldown_cleanup()
        self.assertIsNotNone(auto_answer._cooldown_cleanup_task)
        await auto_answer._stop_cooldown_cleanup()
        self.assertIsNone(auto_answer._cooldown_cleanup_task)

    async def test_task_prunes_stale(self):
        auto_answer._auto_answer_cooldown[("chat-1", "h1")] = (
            _time_mod.monotonic() - 700
        )
        self.assertEqual(len(auto_answer._auto_answer_cooldown), 1)
        auto_answer._cooldown_cleanup()
        self.assertEqual(
            len(auto_answer._auto_answer_cooldown),
            0,
            "background cleanup must prune stale entries",
        )


if __name__ == "__main__":
    unittest.main()
