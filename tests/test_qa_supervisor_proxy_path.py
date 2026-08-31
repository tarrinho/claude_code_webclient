"""QA coverage for the supervisor reaching its backend through the proxy.

The owner fallback that let the supervisor authenticate at all was added and
then verified with ``WC_PROXY_ENABLED=false`` -- the direct path. Production
runs the proxy, and two calls inside ``_execute_proxy`` still resolved without
an owner:

    "model": model or await get_default_model(chat_id),
    backend = await get_backend(chat_id)

So in the only configuration that matters the supervisor was still
unauthenticated and still asking for a model the gateway does not serve. The
tests passed and the feature stayed broken, because they exercised the branch
production does not take.

These tests speak the proxy's wire protocol to a fake listener and assert what
was actually put on the socket. That is the only way to tell: the frame is
built in ``runner.py`` and consumed in ``claude_proxy.py``, and the fault lived
in the gap between them.

The lesson, worth more than the fix: a configuration flag is a branch, and a
test that pins one branch says nothing about the other. Both paths are covered
here for exactly that reason.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import uuid
from unittest.mock import patch

import config
import db
import runner


class FakeProxy:
    """Accepts one connection, answers the handshake, records the turn frame."""

    def __init__(self):
        self.turn: dict | None = None
        self.error: Exception | None = None
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        try:
            await reader.readline()                      # handshake
            writer.write(b'{"type": "ack"}\n')
            await writer.drain()
            line = await reader.readline()               # the turn frame
            self.turn = json.loads(line)
            # A minimal successful turn so the caller does not error out.
            for frame in (
                {"type": "assistant", "text": "ok"},
                {"type": "result", "session_id": "s", "usage": {}},
                {"type": "done"},
            ):
                writer.write((json.dumps(frame) + "\n").encode())
            await writer.drain()
        except Exception as exc:  # noqa: BLE001
            # Recorded, not swallowed. A bare pass here would hide the reason a
            # frame never arrived and leave the assertion blaming the code --
            # which is the same mistake this whole file exists because of.
            self.error = exc
        finally:
            writer.close()

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()


class ProxyTurnFrameTests(unittest.IsolatedAsyncioTestCase):
    """What reaches the socket when the caller has no conversation."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(config, "DB_PATH", f"{self.tmp.name}/db"),
            patch.object(config, "PROJECTS_ROOT", self.tmp.name),
            patch.object(config, "PROXY_ENABLED", True),
            patch.object(config, "PROXY_HOST", "127.0.0.1"),
            patch.object(config, "PROXY_TOKEN", "t"),
        ]
        for p in self.patches:
            p.start()
        await db.init()
        machine_id = uuid.uuid4().hex
        await db.ai_machine_create(
            machine_id=machine_id, name="Gateway", host="gw.example.com",
            port=443, api_key="secret-key", model="vllm/Local-Model",
            base_url="https://gw.example.com", description="",
            owner_id="alice", provider="anthropic",
        )
        await db.ai_machine_activate(machine_id, "alice")
        self.proxy = FakeProxy()
        await self.proxy.start()
        self.port_patch = patch.object(config, "PROXY_PORT", self.proxy.port)
        self.port_patch.start()

    async def asyncTearDown(self):
        self.port_patch.stop()
        await self.proxy.stop()
        try:
            await db.close()
        except Exception:  # noqa: BLE001,S110 -- must not mask the real failure
            pass
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    async def _send(self, owner):
        """One supervisor-shaped turn: an id that is not a conversation."""
        await runner.run_turn(
            "plan this", f"supervisor_{uuid.uuid4().hex}", self.tmp.name,
            uuid.uuid4().hex, None, owner,
        )
        return self.proxy.turn

    async def test_the_frame_carries_the_backend_when_an_owner_is_given(self):
        """Without this the proxy built an env with no credentials at all."""
        turn = await self._send("alice")
        self.assertIsNotNone(
            turn, f"no turn frame reached the proxy: {self.proxy.error!r}")
        self.assertIn("backend", turn,
                      "the proxy has no other source for the backend")
        self.assertEqual(turn["backend"].get("base_url"), "https://gw.example.com")
        self.assertEqual(turn["backend"].get("api_key"), "secret-key")

    async def test_the_frame_carries_the_backends_model(self):
        """Omitting it let the CLI pick one the gateway answers 429 for."""
        turn = await self._send("alice")
        self.assertEqual(turn.get("model"), "vllm/Local-Model")

    async def test_without_an_owner_the_frame_carries_no_backend(self):
        """The pre-fix behaviour, kept as the contract for ordinary callers.

        A real conversation resolves its own backend from its chats row; only a
        caller without one needs the fallback.
        """
        turn = await self._send(None)
        self.assertIsNotNone(turn)
        self.assertNotIn("backend", turn)

    async def test_another_owners_backend_is_not_sent(self):
        turn = await self._send("bob")
        self.assertNotIn("backend", turn,
                         "the fallback must stay scoped to the owner given")


class BothBranchesAreCoveredTests(unittest.TestCase):
    """A configuration flag is a branch; pinning one says nothing about the other.

    This is the structural guard for the mistake itself: every place the runner
    resolves a backend or a model must accept the owner, on both the direct and
    the proxy path. The fix was applied to two of four sites and verified on the
    branch that was already working.
    """

    def test_no_resolution_site_ignores_the_owner(self):
        with open(runner.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for pattern in ("get_backend(chat_id)", "get_default_model(chat_id)"):
            self.assertNotIn(
                pattern, source,
                f"{pattern} resolves without an owner; the supervisor's ids are "
                f"not conversations, so it will silently get nothing",
            )

    def test_every_execution_helper_accepts_an_owner(self):
        with open(runner.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for helper in ("_execute_direct", "_execute_proxy", "_do_direct_stream",
                       "_do_proxy_stream", "_proxy_turn", "run_turn"):
            block = source.split(f"async def {helper}(", 1)[1].split(") ->", 1)[0]
            self.assertIn("owner", block, f"{helper} cannot pass an owner through")


if __name__ == "__main__":
    unittest.main()
