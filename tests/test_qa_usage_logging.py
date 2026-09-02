"""QA: every way usage accounting can fail must say so.

The Usage tab read empty for a whole session while turns plainly succeeded.
The cause was a claude_proxy process older than its own source, so it never
emitted a usage frame — but nothing anywhere logged that, because each link in
the chain returned quietly on missing data:

    CLI result frame -> claude_proxy.usage_frame -> runner -> _record_turn_usage
    -> db.usage_record

Five bare returns, one swallowed exception, and no logger in db.py at all.
These tests pin the diagnostics rather than the happy path, since the happy
path was never what failed.
"""
from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import config
import db
from routes import chats as chat_routes


class UsageRecordDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        Path(f"{self.tmp.name}/p").mkdir(parents=True, exist_ok=True)
        await db.chat_create("c1", "Chat", None, f"{self.tmp.name}/p", "admin")

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_db_module_has_a_logger(self):
        """It had none, so every swallowed failure in it was unobservable."""
        self.assertTrue(hasattr(db, "_log"))
        self.assertIsInstance(db._log, logging.Logger)

    async def test_rejected_row_is_logged_with_the_missing_field(self):
        with self.assertLogs("wc.db", level="WARNING") as caught:
            self.assertIsNone(await db.usage_record("c1", "admin", "", "anthropic"))
        self.assertIn("usage_record_rejected", "\n".join(caught.output))

    async def test_each_required_field_is_named_when_missing(self):
        for args in (("", "admin", "m"), ("c1", "", "m"), ("c1", "admin", "")):
            with (
                self.subTest(args=args),
                self.assertLogs("wc.db", level="WARNING"),
            ):
                self.assertIsNone(await db.usage_record(*args, "anthropic"))

    async def test_write_failure_is_logged_not_only_swallowed(self):
        """The write still must not break the turn -- but it must be visible."""
        with (
            patch.object(db.db_conn, "execute", AsyncMock(side_effect=RuntimeError("disk"))),
            self.assertLogs("wc.db", level="ERROR") as caught,
        ):
            result = await db.usage_record("c1", "admin", "m", "anthropic")
        self.assertIsNone(result)          # swallowed, as designed
        self.assertIn("usage_record_failed", "\n".join(caught.output))

    async def test_successful_write_still_returns_a_row_id(self):
        self.assertIsInstance(
            await db.usage_record("c1", "admin", "m", "anthropic", input_tokens=5), int
        )


class RecordTurnUsageDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    """_record_turn_usage's two bare returns are the empty-tab symptom."""

    async def test_absent_frame_names_the_stale_proxy_as_a_cause(self):
        with self.assertLogs("wc.app", level="WARNING") as caught:
            await chat_routes._record_turn_usage("c1", "admin", {})
        text = "\n".join(caught.output)
        self.assertIn("usage_missing", text)
        self.assertIn("claude_proxy", text)

    async def test_frame_without_models_is_logged(self):
        with self.assertLogs("wc.app", level="WARNING") as caught:
            await chat_routes._record_turn_usage("c1", "admin", {"models": {}, "cost_usd": 1})
        self.assertIn("usage_frame_has_no_models", "\n".join(caught.output))

    async def test_a_usable_frame_logs_what_it_recorded(self):
        frame = {"models": {"m": {"input_tokens": 1, "output_tokens": 2}}}
        with (
            patch.object(db, "ai_machine_active", AsyncMock(return_value=None)),
            patch.object(db, "usage_record", AsyncMock(return_value=1)),
            self.assertLogs("wc.app", level="INFO") as caught,
        ):
            await chat_routes._record_turn_usage("c1", "admin", frame)
        self.assertIn("usage_recorded", "\n".join(caught.output))


class ProxyStalenessTests(unittest.TestCase):
    """A proxy older than its source has now broken three separate features."""

    def test_startup_stamps_the_source_it_is_running(self):
        src = (Path(__file__).resolve().parents[1] / "claude_proxy.py").read_text()
        self.assertIn("source_mtime", src)

    def test_result_frame_without_usage_is_logged(self):
        src = (Path(__file__).resolve().parents[1] / "claude_proxy.py").read_text()
        self.assertIn("carried no usage", src)


if __name__ == "__main__":
    unittest.main()
