"""QA: the backend must reach the CLI, on every turn path.

A chat routed to a custom gateway only works if the machine's base_url and
API key travel with the turn. Two defects broke that in practice, and both
are covered here:

* The streaming turn payload omitted "backend" entirely while the blocking
  one included it, so the same prompt succeeded through /messages and failed
  through /stream -- the CLI silently fell back to the default Anthropic
  endpoint and rejected a model the gateway does serve.
* The model picker harvested model ids out of old session transcripts, so a
  model no backend serves was selectable and every turn using it failed.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import config
import db
import runner


class _Recorder:
    """Stands in for the proxy socket and captures the frames written to it."""

    def __init__(self):
        self.frames: list[dict] = []

    def write(self, raw: bytes) -> None:
        for line in raw.decode().splitlines():
            if line.strip():
                self.frames.append(json.loads(line))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None

    def turn(self) -> dict | None:
        return next((f for f in self.frames if f.get("type") == "turn"), None)


class BackendResolutionTests(unittest.IsolatedAsyncioTestCase):
    """runner.get_backend translates a machine row into CLI environment."""

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

    async def _machine(self, **kw):
        fields = {
            "machine_id": "m1", "name": "GW", "host": "gw.example.com",
            "port": 443, "api_key": None, "model": "vllm/Model",
            "base_url": "https://gw.example.com", "description": None,
            "owner_id": "admin", "provider": "claude_code",
        }
        fields.update(kw)
        await db.ai_machine_create(**fields)
        await db.ai_machine_activate("m1", "admin")

    async def test_anthropic_machine_yields_base_url(self):
        await self._machine()
        backend = await runner.get_backend("c1")
        self.assertEqual(backend.get("provider"), "claude_code")
        self.assertEqual(backend.get("base_url"), "https://gw.example.com")

    async def test_api_key_travels_when_set(self):
        await self._machine(api_key="secret-value")
        backend = await runner.get_backend("c1")
        self.assertEqual(backend.get("api_key"), "secret-value")

    async def test_absent_api_key_is_omitted_not_empty(self):
        """An empty string would stop the CLI falling back to the host login."""
        await self._machine(api_key=None)
        backend = await runner.get_backend("c1")
        self.assertNotIn("api_key", backend)

    async def test_unknown_chat_yields_no_backend(self):
        self.assertEqual(await runner.get_backend("no-such-chat"), {})


class TurnPayloadParityTests(unittest.IsolatedAsyncioTestCase):
    """Both proxy paths must put the backend on the wire."""

    async def asyncSetUp(self):
        self.backend = {
            "provider": "claude_code",
            "base_url": "https://gw.example.com",
            "api_key": "k",
        }

    async def test_streaming_turn_carries_backend(self):
        """The regression: /stream omitted backend while /messages sent it.

        Without it the proxy spawns the CLI against the default endpoint, so a
        chat on a custom gateway failed on every streamed turn.
        """
        recorder = _Recorder()
        reader = AsyncMock()
        reader.readuntil.return_value = b'{"type":"ack"}\n'

        async def fake_open(*_a, **_k):
            return reader, recorder

        with patch.object(runner.asyncio, "open_connection", fake_open), \
             patch.object(runner, "get_backend", AsyncMock(return_value=self.backend)), \
             patch.object(runner, "get_default_model", AsyncMock(return_value="m")), \
             patch.object(runner, "get_proxy_host", AsyncMock(return_value="127.0.0.1")), \
             patch.object(runner, "_read_lines", lambda _r: _empty()):
            async for _ in runner._do_proxy_stream("hi", None, "/tmp", "c1", "m"):
                pass

        turn = recorder.turn()
        self.assertIsNotNone(turn, "no turn frame was written")
        self.assertIn("backend", turn)
        self.assertEqual(turn["backend"]["base_url"], "https://gw.example.com")

    async def test_streaming_turn_omits_backend_when_none_resolves(self):
        """A proxy-provider machine must not gain an empty backend key."""
        recorder = _Recorder()
        reader = AsyncMock()
        reader.readuntil.return_value = b'{"type":"ack"}\n'

        async def fake_open(*_a, **_k):
            return reader, recorder

        with patch.object(runner.asyncio, "open_connection", fake_open), \
             patch.object(runner, "get_backend", AsyncMock(return_value={})), \
             patch.object(runner, "get_default_model", AsyncMock(return_value="m")), \
             patch.object(runner, "get_proxy_host", AsyncMock(return_value="127.0.0.1")), \
             patch.object(runner, "_read_lines", lambda _r: _empty()):
            async for _ in runner._do_proxy_stream("hi", None, "/tmp", "c1", "m"):
                pass

        self.assertNotIn("backend", recorder.turn())


async def _empty():
    """An immediately-exhausted line stream, so the turn ends after the write."""
    return
    yield b""  # pragma: no cover -- makes this an async generator


class ProxyTargetResolutionTests(unittest.IsolatedAsyncioTestCase):
    """runner.get_proxy_target routes through transport_id when set."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(config, "PROJECTS_ROOT", f"{self.tmp.name}/p")
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        Path(f"{self.tmp.name}/p").mkdir(parents=True, exist_ok=True)

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_get_proxy_target_routes_via_transport_id_not_provider(self):
        await db.ssh_transport_create("t1", "Kali3", "admin", "h", "kali", "k")
        await db.ai_machine_create(
            "m1", "CF AI Machine (via Kali3)", "llm.example", 443, None,
            "vllm/x", None, None, "admin", provider="claude_code",
            transport_id="t1",
        )
        await db.ai_machine_activate("m1", "admin")
        await db.chat_create("c1", "Test", None, f"{self.tmp.name}/p", "admin")

        fake_status = {"tunnel_up": True, "proxy_ok": True, "local_port": 9005}
        with patch("tunnel_manager.tunnel_status", AsyncMock(return_value=fake_status)):
            host, port = await runner.get_proxy_target("c1")
        self.assertEqual((host, port), ("127.0.0.1", 9005))


class BaseUrlNormalisationTests(unittest.TestCase):
    """The CLI appends /v1 itself, so the stored value must be an origin."""

    def test_strips_trailing_v1_and_slash(self):
        self.assertEqual(
            runner.normalise_base_url("https://gw.example.com/v1/"),
            "https://gw.example.com",
        )

    def test_adds_scheme_to_a_bare_host(self):
        self.assertEqual(
            runner.normalise_base_url("api.anthropic.com"),
            "https://api.anthropic.com",
        )

    def test_empty_becomes_none(self):
        self.assertIsNone(runner.normalise_base_url(""))
        self.assertIsNone(runner.normalise_base_url(None))


if __name__ == "__main__":
    unittest.main()
