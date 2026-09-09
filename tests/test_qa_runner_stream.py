"""QA coverage for the streaming half of runner.py.

A name-level audit found the entire streaming path untested: _proxy_stream_turn,
_do_proxy_stream, _execute_direct_stream, _do_direct_stream, _collect_chunks and
_kill_process were never referenced by any test. Streaming is the path every
interactive turn actually takes, and its failure modes are quiet -- a stream
that ends without a done frame, or a turn routed to the wrong backend, still
renders as a plausible-looking conversation.

Scope note: the turn payload for the *blocking* path is covered by
tests/test_machine_provider.py::TurnPayloadTests, and backend resolution by
tests/test_qa_model_backend.py (cweb3). This file covers the streaming path.

Covers:
* _do_proxy_stream — handshake, turn payload incl. backend, frame relay,
  unknown/non-JSON frames, truncated streams, connect failure, timeout.
* _proxy_stream_turn — TurnError surfaced as an error event.
* _collect_chunks / take_last_model — text assembly and model capture.
* _kill_process — termination of a process that outlives its turn.
* Skill recording from streamed frames.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch

import auth
import config
import db
import runner
from tests.testing_model import TESTING_MODEL

CHAT = "chat-stream-1"


class _Writer:
    """Captures frames the runner sends to the proxy."""

    def __init__(self):
        self.frames: list[bytes] = []
        self.closed = False

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None

    def sent(self) -> list[dict]:
        return [json.loads(frame.decode()) for frame in self.frames]


class _Reader:
    async def readuntil(self, _sep):
        return b'{"type":"ack"}\n'


def _stream_of(*frames: dict):
    """Build a _read_lines replacement yielding *frames* as NDJSON bytes."""

    async def _lines(_reader):
        for frame in frames:
            yield json.dumps(frame).encode()

    return _lines


def _raw_stream_of(*lines: bytes):
    async def _lines(_reader):
        for line in lines:
            yield line

    return _lines


async def _setup_db(tc):
    td = tempfile.TemporaryDirectory()
    tc.tmpdir = td
    tc._db_patch = patch.object(config, "DB_PATH", f"{td.name}/db")
    tc._root_patch = patch.object(config, "PROJECTS_ROOT", f"{td.name}/projects")
    tc._db_patch.start()
    tc._root_patch.start()
    await db.init()
    await auth.bootstrap_admin()
    await db.chat_create(CHAT, "Chat", None, "/tmp", "admin")


async def _teardown_db(tc):
    await db.close()
    tc._db_patch.stop()
    tc._root_patch.stop()
    tc.tmpdir.cleanup()


class ProxyStreamTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _setup_db(self)
        self.writer = _Writer()

        async def _open(*_a, **_kw):
            return _Reader(), self.writer

        self._open = patch.object(runner.asyncio, "open_connection", _open)
        self._open.start()

    async def asyncTearDown(self):
        self._open.stop()
        await _teardown_db(self)

    async def _drain(self, *frames):
        with patch.object(runner, "_read_lines", _stream_of(*frames)):
            return [event async for event in runner._do_proxy_stream(
                "hi", None, "/tmp", CHAT, TESTING_MODEL)]

    async def test_handshake_precedes_the_turn(self):
        await self._drain({"type": "done"})
        sent = self.writer.sent()
        self.assertEqual(sent[0]["type"], "handshake")
        self.assertEqual(sent[0]["protocol"], config.PROTOCOL)
        self.assertEqual(sent[1]["type"], "turn")

    async def test_turn_carries_prompt_and_model(self):
        await self._drain({"type": "done"})
        turn = self.writer.sent()[1]
        self.assertEqual(turn["prompt"], "hi")
        self.assertEqual(turn["model"], TESTING_MODEL)
        self.assertEqual(turn["work_dir"], "/tmp")

    async def test_no_backend_key_when_no_machine_is_active(self):
        await self._drain({"type": "done"})
        self.assertNotIn("backend", self.writer.sent()[1])

    async def test_backend_travels_with_the_streaming_turn(self):
        """Without it a chat on a custom gateway failed on every streamed turn
        while the same prompt through the blocking path succeeded."""
        await db.ai_machine_create(
            "m1", "Gateway", "gw.example.com", 443, "sk-test", TESTING_MODEL,
            "https://gw.example.com", None, "admin", provider="claude_code",
        )
        await db.ai_machine_activate("m1", "admin")
        await self._drain({"type": "done"})
        backend = self.writer.sent()[1]["backend"]
        self.assertEqual(backend["provider"], "claude_code")
        self.assertEqual(backend["base_url"], "https://gw.example.com")
        self.assertEqual(backend["api_key"], "sk-test")

    async def test_conversational_frames_are_relayed(self):
        events = await self._drain(
            {"type": "session_id", "session_id": "s1"},
            {"type": "model", "model": TESTING_MODEL},
            {"type": "text", "content": "hello"},
            {"type": "done"},
        )
        self.assertEqual([e["type"] for e in events],
                         ["session_id", "model", "text", "done"])

    async def test_error_frame_is_relayed(self):
        events = await self._drain({"type": "error", "error": "boom"}, {"type": "done"})
        self.assertEqual(events[0], {"type": "error", "error": "boom"})

    async def test_status_frame_is_relayed(self):
        events = await self._drain({"type": "status", "status": "api_retry"},
                                   {"type": "done"})
        self.assertEqual(events[0]["type"], "status")

    async def test_unknown_frame_types_are_dropped(self):
        events = await self._drain({"type": "telemetry", "x": 1}, {"type": "done"})
        self.assertEqual([e["type"] for e in events], ["done"])

    async def test_non_json_lines_are_skipped_not_fatal(self):
        with patch.object(runner, "_read_lines", _raw_stream_of(
            b"not json", b'{"type":"text","content":"ok"}', b'{"type":"done"}')):
            events = [e async for e in runner._do_proxy_stream(
                "hi", None, "/tmp", CHAT, None)]
        self.assertEqual([e["type"] for e in events], ["text", "done"])

    async def test_blank_lines_are_skipped(self):
        with patch.object(runner, "_read_lines", _raw_stream_of(
            b"", b"   ", b'{"type":"done"}')):
            events = [e async for e in runner._do_proxy_stream(
                "hi", None, "/tmp", CHAT, None)]
        self.assertEqual([e["type"] for e in events], ["done"])

    async def test_frames_after_done_are_not_relayed(self):
        events = await self._drain(
            {"type": "done"}, {"type": "text", "content": "late"})
        self.assertEqual([e["type"] for e in events], ["done"])

    async def test_stream_without_done_reports_truncation(self):
        """A proxy that dies mid-turn must not look like a finished answer."""
        events = await self._drain({"type": "text", "content": "partial"})
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("before completion", events[-1]["error"])

    async def test_skill_frames_are_recorded_and_relayed(self):
        with patch.object(runner, "_read_lines", _stream_of(
            {"type": "skill", "name": "pentesting"}, {"type": "done"})):
            events = [e async for e in runner._do_proxy_stream(
                "hi", "sess-1", "/tmp", CHAT, None)]
        self.assertEqual(events[0]["type"], "skill")
        self.assertIn("pentesting", runner.active_skills("sess-1"))

    async def test_connection_is_always_closed(self):
        await self._drain({"type": "done"})
        self.assertTrue(self.writer.closed)

    async def test_connect_failure_yields_an_error_event(self):
        self._open.stop()

        async def _refuse(*_a, **_kw):
            raise ConnectionRefusedError("nope")

        try:
            with patch.object(runner.asyncio, "open_connection", _refuse):
                events = [e async for e in runner._do_proxy_stream(
                    "hi", None, "/tmp", CHAT, None)]
        finally:
            self._open.start()
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("Cannot connect", events[0]["error"])

    async def test_timeout_yields_an_error_event(self):
        async def _hang(_reader):
            await asyncio.sleep(3600)
            yield b""  # pragma: no cover - never reached

        with patch.object(config, "PROXY_TURN_TIMEOUT_S", 0.05), \
                patch.object(runner, "_read_lines", _hang):
            events = [e async for e in runner._do_proxy_stream(
                "hi", None, "/tmp", CHAT, None)]
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("timed out", events[-1]["error"])


class ProxyStreamTurnTests(unittest.IsolatedAsyncioTestCase):
    """The semaphore wrapper converts TurnError into an error event."""

    async def asyncSetUp(self):
        await _setup_db(self)

    async def asyncTearDown(self):
        await _teardown_db(self)

    async def test_turn_error_becomes_an_error_event(self):
        async def _raise(*_a, **_kw):
            raise runner.TurnError("proxy exploded", fatal=False)
            yield  # pragma: no cover - generator marker

        with patch.object(runner, "_do_proxy_stream", _raise):
            events = [e async for e in runner._proxy_stream_turn(
                "hi", None, "/tmp", CHAT, None)]
        self.assertEqual(events, [{"type": "error", "error": "proxy exploded"}])

    async def test_events_pass_through_unchanged(self):
        async def _ok(*_a, **_kw):
            yield {"type": "text", "content": "hello"}
            yield {"type": "done"}

        with patch.object(runner, "_do_proxy_stream", _ok):
            events = [e async for e in runner._proxy_stream_turn(
                "hi", None, "/tmp", CHAT, None)]
        self.assertEqual([e["type"] for e in events], ["text", "done"])


class _FakeProc:
    """A finished subprocess whose stdout replays *lines*."""

    def __init__(self, lines: list[bytes] | None):
        self.stdout = object() if lines is not None else None
        self._lines = lines or []
        self.waited = False

    async def wait(self):
        self.waited = True
        return 0


def _proc_lines(proc: _FakeProc):
    async def _lines(_stdout):
        for line in proc._lines:
            yield line

    return _lines


class CollectChunksTests(unittest.IsolatedAsyncioTestCase):
    """_collect_chunks turns the CLI's NDJSON stdout into a blocking answer."""

    async def _collect(self, *lines: bytes):
        proc = _FakeProc(list(lines))
        with patch.object(runner, "_read_lines", _proc_lines(proc)):
            return await runner._collect_chunks(proc, CHAT)

    async def test_text_is_assembled_in_order(self):
        chunks, sid = await self._collect(
            json.dumps({"type": "assistant", "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "one "}]}}).encode(),
            json.dumps({"type": "assistant", "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "two"}]}}).encode(),
        )
        self.assertEqual("".join(chunks), "one two")
        self.assertIsNone(sid)

    async def test_session_id_is_captured(self):
        _chunks, sid = await self._collect(
            json.dumps({"type": "system", "subtype": "init",
                        "session_id": "abc"}).encode())
        self.assertEqual(sid, "abc")

    async def test_model_is_recorded_then_cleared_by_take_last_model(self):
        """A stale model must not be reported against a later turn."""
        await self._collect(
            json.dumps({"type": "system", "subtype": "init",
                        "session_id": "s", "model": TESTING_MODEL}).encode())
        self.assertEqual(runner.take_last_model(CHAT), TESTING_MODEL)
        self.assertEqual(runner.take_last_model(CHAT), "")

    async def test_non_json_lines_are_skipped(self):
        chunks, _sid = await self._collect(
            b"not json",
            json.dumps({"type": "assistant", "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}]}}).encode(),
        )
        self.assertEqual(chunks, ["ok"])

    async def test_error_frame_raises_turn_error(self):
        with self.assertRaises(runner.TurnError) as ctx:
            await self._collect(
                json.dumps({"type": "result", "is_error": True,
                            "error": "backend refused"}).encode())
        self.assertIn("backend refused", str(ctx.exception))

    async def test_error_is_raised_after_the_whole_stream_is_read(self):
        """Raising early would discard text the CLI had already produced."""
        with self.assertRaises(runner.TurnError):
            await self._collect(
                json.dumps({"type": "result", "is_error": True,
                            "error": "late failure"}).encode(),
                json.dumps({"type": "assistant", "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "trailing"}]}}).encode(),
            )

    async def test_process_without_stdout_returns_empty(self):
        proc = _FakeProc(None)
        chunks, sid = await runner._collect_chunks(proc, CHAT)
        self.assertEqual((chunks, sid), ([], None))


class KillProcessTests(unittest.IsolatedAsyncioTestCase):
    """A subprocess that outlives its turn must actually be stopped."""

    async def test_terminate_is_attempted(self):
        class _Proc:
            def __init__(self):
                self.terminated = False
                self.killed = False

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

            async def wait(self):
                return 0

        proc = _Proc()
        await runner._kill_process(proc)
        self.assertTrue(proc.terminated or proc.killed)

    async def test_already_dead_process_does_not_raise(self):
        class _Dead:
            def terminate(self):
                raise ProcessLookupError

            def kill(self):
                raise ProcessLookupError

            async def wait(self):
                return 0

        await runner._kill_process(_Dead())  # must not raise


if __name__ == "__main__":
    unittest.main()
