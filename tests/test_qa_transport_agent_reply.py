"""QA: transport-aware agent-reply — the pieces that were easy to get wrong.

Design: docs/superpowers/specs/2026-09-08-transport-aware-agent-reply-design.md

Three properties pinned here, each one a way the earlier design (or a later
"simplification") could silently regress:

1. build_remote_reply_command never shell-interpolates the target/text raw —
   only a base64 blob (safe alphabet, no shell metacharacters) reaches the
   command string. A regression here is a remote command-injection hole.
2. runner._execute_proxy's proxy_target override actually bypasses
   get_proxy_target — the wake-up-turn addressing bug the design fixes.
   Without this, a transport-resolved reply would connect to whatever the
   *replying* chat happens to be pinned to, not the target's own host.
3. The DB-backed cooldown rejects a second rapid call to the same
   (chat_id, target) pair and allows one to a different target.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import config
import db
import runner
import transcripts


class BuildRemoteReplyCommandTests(unittest.TestCase):
    """Pure function -- no mocks needed."""

    def test_payload_is_base64_json_not_raw_text(self):
        evil = "'; rm -rf ~ #"
        cmd = transcripts.build_remote_reply_command(
            "~/projects/claude-code-webconsole", "cweb6", evil)
        # The raw attacker string must never appear in the command -- only its
        # base64 encoding does.
        self.assertNotIn(evil, cmd)
        self.assertNotIn("rm -rf", cmd)

    def test_decoding_the_embedded_payload_recovers_target_and_text(self):
        cmd = transcripts.build_remote_reply_command(
            "/home/kali/projects/claude-code-webconsole", "cweb6", "hello there")
        # Extract the base64 blob between the single quotes following
        # base64.b64decode(' ... ').
        start = cmd.index("b64decode('") + len("b64decode('")
        end = cmd.index("'", start)
        blob = cmd[start:end]
        decoded = json.loads(base64.b64decode(blob))
        self.assertEqual(decoded, {"to": "cweb6", "text": "hello there"})

    def test_remote_path_is_used_verbatim_in_the_cd(self):
        cmd = transcripts.build_remote_reply_command(
            "/home/kali/projects/claude-code-webconsole", "x", "y")
        self.assertTrue(cmd.startswith("cd /home/kali/projects/claude-code-webconsole && "))

    def test_base64_alphabet_has_no_shell_metacharacters(self):
        # Defence in depth: even if the surrounding quoting were ever changed,
        # the payload alphabet itself must stay injection-safe.
        cmd = transcripts.build_remote_reply_command(
            "~/p", "target", "text with $(command) substitution `and backticks`")
        start = cmd.index("b64decode('") + len("b64decode('")
        end = cmd.index("'", start)
        blob = cmd[start:end]
        self.assertRegex(blob, r"^[A-Za-z0-9+/=]+$")


class ProxyTargetOverrideTests(unittest.IsolatedAsyncioTestCase):
    """Pins the §2 wake-up-turn addressing fix: proxy_target bypasses
    get_proxy_target's chat/owner-based resolution entirely."""

    async def test_proxy_target_override_skips_get_proxy_target(self):
        get_proxy_target_called = False

        async def fake_get_proxy_target(chat_id, owner=None):
            nonlocal get_proxy_target_called
            get_proxy_target_called = True
            # If this were used, the test would observe the "wrong" host.
            return ("wrong-host", 1)

        connected_to: dict[str, object] = {}

        async def fake_open_connection(host, port):
            connected_to["host"] = host
            connected_to["port"] = port
            raise OSError("stubbed: no real transport in this test")

        with (
            patch.object(runner, "get_proxy_target", fake_get_proxy_target),
            patch.object(asyncio, "open_connection", fake_open_connection),
            contextlib.suppress(OSError, runner.TurnError),
        ):
            await runner._execute_proxy(
                " ", "some-session-id", "/tmp", "chat1", None, "owner1",
                proxy_target=("127.0.0.1", 55123),
            )

        self.assertFalse(
            get_proxy_target_called,
            "proxy_target override must bypass get_proxy_target entirely",
        )
        self.assertEqual(connected_to.get("host"), "127.0.0.1")
        self.assertEqual(connected_to.get("port"), 55123)

    async def test_without_override_falls_back_to_get_proxy_target(self):
        """The default (no proxy_target) must keep today's behaviour for
        every other caller of _execute_proxy/_proxy_turn."""
        async def fake_get_proxy_target(chat_id, owner=None):
            return ("resolved-host", 9999)

        connected_to: dict[str, object] = {}

        async def fake_open_connection(host, port):
            connected_to["host"] = host
            connected_to["port"] = port
            raise OSError("stubbed")

        with (
            patch.object(runner, "get_proxy_target", fake_get_proxy_target),
            patch.object(asyncio, "open_connection", fake_open_connection),
            contextlib.suppress(OSError, runner.TurnError),
        ):
            await runner._execute_proxy(" ", "sid", "/tmp", "chat1")

        self.assertEqual(connected_to.get("host"), "resolved-host")
        self.assertEqual(connected_to.get("port"), 9999)


class CooldownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.root_patch.stop)
        await db.init()
        self.addAsyncCleanup(db.close)

    async def test_first_call_to_a_target_is_allowed(self):
        allowed = await db.agent_reply_cooldown_check("chat1", "cweb6")
        self.assertTrue(allowed)

    async def test_second_rapid_call_to_same_target_is_rejected(self):
        await db.agent_reply_log_add("chat1", "admin", "cweb6", "local", True)
        allowed = await db.agent_reply_cooldown_check("chat1", "cweb6")
        self.assertFalse(allowed)

    async def test_a_failed_attempt_still_starts_the_cooldown(self):
        """A broken remote must not be hammered in a tight retry loop either."""
        await db.agent_reply_log_add("chat1", "admin", "cweb6", "local", False, "boom")
        allowed = await db.agent_reply_cooldown_check("chat1", "cweb6")
        self.assertFalse(allowed)

    async def test_a_different_target_is_unaffected(self):
        await db.agent_reply_log_add("chat1", "admin", "cweb6", "local", True)
        allowed = await db.agent_reply_cooldown_check("chat1", "cweb7")
        self.assertTrue(allowed)

    async def test_a_different_chat_to_same_target_is_unaffected(self):
        await db.agent_reply_log_add("chat1", "admin", "cweb6", "local", True)
        allowed = await db.agent_reply_cooldown_check("chat2", "cweb6")
        self.assertTrue(allowed)


if __name__ == "__main__":
    unittest.main()
