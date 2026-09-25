"""QA: proxy_ok means a turn can get through, not that a process exists.

`probe_proxy` sets `proxy_ok`, which runner routes turns on and machines.js
renders the 'active' badge from. It used to run `pgrep -f claude_proxy` over
SSH and return whether anything matched, while its docstring described a
handshake through the tunnel. The gap between those two is what shipped.

RETRACTION, 2026-09-25. This file first claimed the change was forced by a live
failure: that all four of this deployment's transport backends answered EOF to
a real handshake while being reported healthy. That was wrong. The probe making
those measurements ran outside the console with an empty `config.PROXY_TOKEN`,
and `claude_proxy.py` closes the connection without replying when the token
does not match -- so every EOF was a correct authentication refusal, read as a
dead tunnel. With the token from the settings table, all four answer `ack`.

What tipped it over was a contradiction that could not be argued away: the
deployed probe reported proxy_ok while an identical handshake from another
process got EOF on the same port. Two clients, one port, opposite answers --
the difference had to be the client.

The change stands on the argument alone. An `ssh -L` forward accepts
connections locally whether or not anything is listening at the far end; it
accepts, then closes. So neither a matching remote process nor a listening
local port establishes that a turn can get through, and those were the only two
things being checked. Testing the path is stronger than testing a process name
whether or not anything happens to be broken today.

The probe now completes the real handshake -- `{"type": "handshake", ...}` out,
`{"type": "ack"}` back -- against the forwarded port.
"""
from __future__ import annotations

import asyncio
import json
import unittest

import tunnel_manager
from tunnel_manager_health import probe_proxy


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeProxy:
    """A listener on 127.0.0.1 that answers the handshake, or does not."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.port = None
        self._server = None
        self._loop = None

    async def _handle(self, reader, writer):
        try:
            await reader.readline()
            if self.behaviour == "ack":
                writer.write(json.dumps({"type": "ack"}).encode() + b"\n")
                await writer.drain()
            elif self.behaviour == "refuse":
                writer.write(json.dumps(
                    {"type": "error", "error": "bad token"}).encode() + b"\n")
                await writer.drain()
            # "eof": reply with nothing, exactly as a dangling ssh -L forward
            # does -- accept, then close.
        finally:
            writer.close()

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()


class ProxyProbeProvesThePathQA(unittest.TestCase):
    def setUp(self):
        tunnel_manager._STATE.clear()

    def tearDown(self):
        tunnel_manager._STATE.clear()

    def _probe_against(self, behaviour):
        async def go():
            proxy = _FakeProxy(behaviour)
            await proxy.start()
            tunnel_manager._STATE["m1"] = {
                "tunnel_up": 1, "state": "connected", "local_port": proxy.port}
            try:
                return await probe_proxy("m1")
            finally:
                await proxy.stop()
        return _run(go())

    def test_a_forward_with_nothing_behind_it_is_not_proxy_ok(self):
        """A forward that accepts and then closes.

        Not, as this docstring first claimed, what production was doing -- see
        the retraction at the top of this file. It is what a dangling `ssh -L`
        does when nothing listens at the far end, which is a real shape worth
        holding the line on whether or not any deployment is in it today.
        """
        self.assertFalse(self._probe_against("eof"))

    def test_a_refused_handshake_is_not_proxy_ok(self):
        """A proxy that answers but rejects the token cannot carry a turn
        either -- and pgrep would have matched it happily."""
        self.assertFalse(self._probe_against("refuse"))

    def test_a_completed_handshake_is_proxy_ok(self):
        """The control. Without it, returning False unconditionally would pass
        both cases above while marking every healthy tunnel dead -- which is
        the opposite failure and just as damaging."""
        self.assertTrue(self._probe_against("ack"))

    def test_a_running_remote_process_does_not_make_it_proxy_ok(self):
        """The only case here that discriminates between the two checks.

        Reverting to the old `pgrep -f claude_proxy` leaves the EOF, refused,
        no-port and closed-port cases all passing, because in a test
        environment that pgrep cannot reach SSH and returns False for
        everything -- they agree with the fix by accident. Worth knowing,
        because the first version of this file stopped at those and looked
        complete.

        What actually separates the implementations is a matching remote
        process while the forward delivers nothing, so that is simulated
        directly: exec_command mocked to report a running process, the forward
        accepting and closing. The old code returns True here; the new code
        must not.

        This is a constructed case, not an observed one -- the retraction at
        the top of this file explains why the observation it was first
        attributed to did not hold. The shape is still the one the check has to
        get right.
        """
        from unittest.mock import patch

        class _Stdout:
            @staticmethod
            def read():
                return b"4242\n"          # pgrep found a process

        async def _fake_exec(machine_id, cmd, timeout=5):
            return None, _Stdout(), None

        async def go():
            proxy = _FakeProxy("eof")     # ...but nothing answers through it
            await proxy.start()
            tunnel_manager._STATE["m1"] = {
                "tunnel_up": 1, "state": "connected", "local_port": proxy.port}
            try:
                with patch("tunnel_manager_ssh.exec_command", _fake_exec):
                    return await probe_proxy("m1")
            finally:
                await proxy.stop()

        self.assertFalse(
            _run(go()),
            "a claude_proxy process exists on the remote host and the forward "
            "still delivers nothing -- reporting proxy_ok here would render "
            "the backend 'active' and invite a turn onto a path that cannot "
            "carry one")

    def test_no_known_port_is_not_proxy_ok(self):
        """No port means nothing to prove, and a guess would be a claim."""
        tunnel_manager._STATE["m2"] = {"tunnel_up": 1, "state": "connected"}
        self.assertFalse(_run(probe_proxy("m2")))

    def test_a_closed_port_is_not_proxy_ok(self):
        """Connection refused, as distinct from accepted-then-closed."""
        tunnel_manager._STATE["m3"] = {
            "tunnel_up": 1, "state": "connected", "local_port": 19999}
        self.assertFalse(_run(probe_proxy("m3")))


if __name__ == "__main__":
    unittest.main()
