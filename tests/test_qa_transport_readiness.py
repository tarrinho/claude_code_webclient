"""QA: the transport readiness check, and the two things it must not do.

Design: agreed in chat 2026-09-08. Adding a transport creates nothing on the
far side; turns need claude_proxy.py listening on config.PROXY_PORT with this
database's token, plus the claude CLI and python3 for it to use. Their absence
surfaces as "Cannot connect to proxy at 127.0.0.1:<port>" on every turn, which
reads as a local fault and is not one.

`parse_probe` is pure, so the table below is a set of remote states rather
than a mock of SSH.

Two cases exist because the first implementation got them wrong:

* **the probe template renders.** It was written with %(port)d, and the script
  is full of `printf '%s'` -- %-formatting raised "not enough arguments for
  format string" on every single call. Nothing else in the file would have
  caught it, because the template is only interpolated on the SSH path.
* **the CLI is resolved by path before PATH.** `command -v claude` reported
  MISSING on a host where the CLI was installed and working: a non-interactive
  SSH shell has no ~/.local/bin on PATH, and the proxy's unit does not use
  PATH anyway (it sets WC_CLAUDE_PATH). A check that cries wolf on a healthy
  host is one that gets switched off.
"""
from __future__ import annotations

import unittest
import unittest.mock

import transport_readiness as tr

_TOKEN = "T" * 43


def _sha16(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _raw(**over) -> str:
    fields = {
        "claude": "/home/u/.local/bin/claude",
        "python": "Python 3.13.11",
        "listening": "1",
        "token_len": "43",
        "token_sha": _sha16(_TOKEN),
        "service": "active",
    }
    fields.update(over)
    return "\n".join(f"{k}={v}" for k, v in fields.items())


def _by_name(raw: str) -> dict[str, tr.Check]:
    return {c.name: c for c in tr.parse_probe(raw, port=9000, local_token=_TOKEN)}


class ProbeTemplateTests(unittest.TestCase):
    def test_the_probe_renders_with_the_port_substituted(self):
        """The regression: %-formatting against a script full of printf '%s'."""
        script = tr.probe_script(9000)
        self.assertIn("127.0.0.1:9000", script)
        self.assertNotIn(tr._PORT_MARKER, script)

    def test_the_port_is_substituted_wherever_it_is_configured(self):
        self.assertIn("127.0.0.1:9002", tr.probe_script(9002))

    def test_the_probe_only_reads(self):
        """Init writes; check must not. Asserted because the probe is a shell
        script, where adding a write is one careless line."""
        script = tr.probe_script(9000)
        for writer in ("scp ", "mkdir", "systemctl --user start",
                       "systemctl --user enable", "> ~/", "rm ", "install "):
            self.assertNotIn(writer, script, f"the probe must not {writer!r}")


class ReadyHostTests(unittest.TestCase):
    def test_a_fully_deployed_host_passes_every_check(self):
        checks = tr.parse_probe(_raw(), port=9000, local_token=_TOKEN)
        self.assertTrue(all(c.ok for c in checks), [c.detail for c in checks])

    def test_the_cli_is_accepted_when_found_by_path(self):
        """A non-interactive SSH shell has no ~/.local/bin on PATH, so path
        resolution is the only answer that matches how the proxy launches."""
        checks = _by_name(_raw(claude="/home/u/.local/bin/claude"))
        self.assertTrue(checks["claude CLI"].ok)
        self.assertIn(".local/bin/claude", checks["claude CLI"].detail)


class MissingPiecesTests(unittest.TestCase):
    def test_todays_real_pre_deploy_state(self):
        """Measured on pentester before deploying: CLI and python3 present,
        no proxy, no token."""
        checks = _by_name(_raw(claude="MISSING", listening="0",
                               token_sha="none", service="absent"))
        self.assertFalse(checks["claude CLI"].ok)
        self.assertTrue(checks["python3"].ok)
        self.assertFalse(checks["proxy on 127.0.0.1:9000"].ok)
        self.assertFalse(checks["proxy token matches"].ok)

    def test_a_missing_proxy_names_the_service_state(self):
        """"nothing listening" alone does not say whether it crashed or was
        never installed."""
        checks = _by_name(_raw(listening="0", service="failed"))
        self.assertIn("failed", checks["proxy on 127.0.0.1:9000"].detail)

    def test_a_stale_token_is_caught_even_with_the_proxy_up(self):
        """The Kali3 failure: a proxy running happily on a token the database
        no longer holds, so every turn dies at the handshake."""
        checks = _by_name(_raw(token_sha=_sha16("a-different-token")))
        self.assertFalse(checks["proxy token matches"].ok)
        self.assertTrue(checks["proxy on 127.0.0.1:9000"].ok)

    def test_every_failing_check_offers_a_remedy(self):
        checks = tr.parse_probe(
            _raw(claude="MISSING", python="MISSING", listening="0",
                 token_sha="none"),
            port=9000, local_token=_TOKEN,
        )
        for c in checks:
            if not c.ok:
                self.assertTrue(c.remedy, f"{c.name} fails with no remedy")

    def test_no_local_token_does_not_read_as_a_match(self):
        """An empty local token must never satisfy the comparison -- otherwise
        a console with no token configured reports every host as ready."""
        checks = {c.name: c for c in tr.parse_probe(
            _raw(token_sha="none"), port=9000, local_token="")}
        self.assertFalse(checks["proxy token matches"].ok)


class ReadinessShapeTests(unittest.TestCase):
    def test_unreachable_is_not_ready_even_with_no_failing_checks(self):
        """Kali3 is offline: no checks ran at all, which must not read as
        'nothing failed, therefore ready'."""
        r = tr.Readiness(reachable=False, error="Connection timed out")
        self.assertEqual(r.checks, [])
        self.assertFalse(r.ready)

    def test_the_payload_carries_each_check_separately(self):
        r = tr.Readiness(reachable=True,
                         checks=tr.parse_probe(_raw(), port=9000,
                                               local_token=_TOKEN))
        payload = r.as_dict()
        self.assertTrue(payload["ready"])
        self.assertEqual(len(payload["checks"]), 4)
        self.assertEqual(
            {"name", "ok", "detail", "remedy"}, set(payload["checks"][0]))

    def test_the_token_value_never_appears_in_the_payload(self):
        r = tr.Readiness(reachable=True,
                         checks=tr.parse_probe(_raw(), port=9000,
                                               local_token=_TOKEN))
        self.assertNotIn(_TOKEN, str(r.as_dict()))


class _FakeSshHandle:
    """Stands in for the ssh -N -L subprocess.

    `_probe_forward` reads two things off this object: `.returncode` (None
    means still running, so the forward "worked") and, on a failed bind,
    `.stderr.read()`. It never sends the process any input and only calls
    terminate/kill/wait, all stubbed here.
    """

    def __init__(self, returncode=None, stderr=b""):
        self.returncode = returncode
        self.stderr = _Stream(stderr)
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    async def wait(self):
        self.returncode = 0
        return 0


class _Stream:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self):
        return self._data


class ProbeForwardTests(unittest.IsolatedAsyncioTestCase):
    """`_probe_forward`: can this host actually open a working -L forward,
    right now -- the one thing the other four checks cannot see.

    Real asyncio TCP servers stand in for claude_proxy.py; only the `ssh`
    subprocess itself is stubbed, since a unit test has no SSH host to reach.
    The mocked `create_subprocess_exec` reads the chosen local port straight
    out of the real `-L` argument the function built, then starts a listener
    on that exact port before the function's own connection attempt runs --
    so the handshake path below is exercised for real, not asserted by
    inspecting arguments.
    """

    async def _run_with_fake_proxy(self, server_handler, *, ssh_handle=None):
        """Patch ssh; start *server_handler* on the port -L names; run the probe."""
        import asyncio
        import re

        server_holder = {}

        async def _fake_exec(*argv, **kwargs):
            fwd = next(a for a in argv if a.startswith("127.0.0.1:") and ":127.0.0.1:" in a)
            local_port = int(re.match(r"127\.0\.0\.1:(\d+):", fwd).group(1))
            server_holder["server"] = await asyncio.start_server(
                server_handler, "127.0.0.1", local_port)
            return ssh_handle if ssh_handle is not None else _FakeSshHandle()

        with unittest.mock.patch("asyncio.create_subprocess_exec", _fake_exec):
            try:
                return await tr._probe_forward(
                    "example.net", "kali", "/dev/null",
                    port=9000, local_token=_TOKEN,
                )
            finally:
                server = server_holder.get("server")
                if server:
                    server.close()
                    await server.wait_closed()

    async def test_the_proxy_accepting_the_handshake_is_ready(self):
        import asyncio
        import json

        async def _ack(reader, writer):
            raw = await reader.readuntil(b"\n")
            frame = json.loads(raw)
            self.assertEqual(frame["type"], "handshake")
            self.assertEqual(frame["protocol"], "webconsole-v1")
            self.assertEqual(frame["token"], _TOKEN)
            writer.write((json.dumps({"type": "ack"}) + "\n").encode())
            await writer.drain()
            writer.close()

        check = await self._run_with_fake_proxy(_ack)
        self.assertEqual(check.name, "tunnel forward")
        self.assertTrue(check.ok, check.detail)

    async def test_a_reply_that_is_not_ack_is_not_ready(self):
        """The proxy is there and answers, but not the way a real handshake
        accept looks -- a bad token, for instance, closes without an ack."""
        import json

        async def _wrong_reply(reader, writer):
            await reader.readuntil(b"\n")
            writer.write((json.dumps({"type": "error"}) + "\n").encode())
            await writer.drain()
            writer.close()

        check = await self._run_with_fake_proxy(_wrong_reply)
        self.assertFalse(check.ok)
        self.assertIn("not with ack", check.detail)

    async def test_the_connection_closing_with_no_reply_is_not_ready(self):
        """A bad or missing handshake: claude_proxy.py closes the socket
        without writing anything back (see claude_proxy.py's own handshake
        rejection path) -- must read as a failure, not hang or crash."""
        async def _silence(reader, writer):
            await reader.readuntil(b"\n")
            writer.close()

        check = await self._run_with_fake_proxy(_silence)
        self.assertFalse(check.ok)

    async def test_ssh_exiting_before_the_forward_opens_is_not_ready(self):
        """ExitOnForwardFailure=yes: the remote refused to bind the forward
        (e.g. AllowTcpForwarding no) and ssh has already exited nonzero --
        there is no listener to connect to at all."""
        import asyncio

        async def _fake_exec(*argv, **kwargs):
            return _FakeSshHandle(
                returncode=255, stderr=b"open failed: administratively prohibited")

        with unittest.mock.patch("asyncio.create_subprocess_exec", _fake_exec):
            check = await tr._probe_forward(
                "example.net", "kali", "/dev/null", port=9000, local_token=_TOKEN)
        self.assertEqual(check.name, "tunnel forward")
        self.assertFalse(check.ok)
        self.assertIn("administratively prohibited", check.detail)
        self.assertIn("AllowTcpForwarding", check.remedy)

    async def test_the_ssh_process_is_always_terminated(self):
        """Transient by construction: nothing is left running after the
        check, success or failure -- this module's whole read-only claim
        depends on it."""
        import json

        async def _ack(reader, writer):
            await reader.readuntil(b"\n")
            writer.write((json.dumps({"type": "ack"}) + "\n").encode())
            await writer.drain()
            writer.close()

        handle = _FakeSshHandle()
        await self._run_with_fake_proxy(_ack, ssh_handle=handle)
        self.assertTrue(handle.terminated)

    async def test_nothing_listening_at_all_is_not_ready(self):
        """ssh reports itself alive (forward bound) but nothing is behind it
        -- the proxy process is not running, distinct from the forward
        itself being refused."""
        import asyncio

        async def _fake_exec(*argv, **kwargs):
            return _FakeSshHandle()   # "alive"; no server ever started

        with unittest.mock.patch("asyncio.create_subprocess_exec", _fake_exec):
            check = await tr._probe_forward(
                "example.net", "kali", "/dev/null", port=9000, local_token=_TOKEN)
        self.assertFalse(check.ok)


class CheckTransportIncludesTheForwardTests(unittest.IsolatedAsyncioTestCase):
    """check_transport wires the new check in -- gated the way the docstring
    promises, not run unconditionally."""

    async def test_no_local_token_skips_the_forward_probe_entirely(self):
        """An empty token means 'token matches' has already failed and
        explained why; attempting a handshake with nothing to send would
        just add a second, more confusing failure about the same cause."""
        called = unittest.mock.AsyncMock()
        with unittest.mock.patch.object(tr, "_probe_forward", called), \
                unittest.mock.patch(
                    "asyncio.create_subprocess_exec",
                    unittest.mock.AsyncMock(
                        return_value=_ProcStub(0, _raw().encode()))):
            result = await tr.check_transport(
                "example.net", "kali", __file__, port=9000, local_token="")
        called.assert_not_awaited()
        self.assertNotIn(
            "tunnel forward", [c.name for c in result.checks])

    async def test_a_real_token_runs_the_forward_probe_and_appends_it(self):
        forward_check = tr.Check("tunnel forward", True, "ok")
        called = unittest.mock.AsyncMock(return_value=forward_check)
        with unittest.mock.patch.object(tr, "_probe_forward", called), \
                unittest.mock.patch(
                    "asyncio.create_subprocess_exec",
                    unittest.mock.AsyncMock(
                        return_value=_ProcStub(0, _raw().encode()))):
            result = await tr.check_transport(
                "example.net", "kali", __file__, port=9000, local_token=_TOKEN)
        called.assert_awaited_once()
        self.assertIn(forward_check, result.checks)


class _ProcStub:
    """Stands in for the diagnostic ssh subprocess in check_transport itself
    (distinct from _FakeSshHandle, which stands in for the forward's own ssh
    process) -- returns a fixed exit code and combined stdout."""

    def __init__(self, returncode: int, out: bytes):
        self.returncode = returncode
        self._out = out

    async def communicate(self):
        return self._out, b""


if __name__ == "__main__":
    unittest.main()
