"""QA: the Check and Init endpoints, and the ways they must not lie.

Init writes to a machine this console does not own — it copies files, installs
a token, and enables a systemd service. That makes two properties worth
asserting rather than trusting:

* **owner scoping.** Every other transport route scopes by owner; a privileged
  one that forgot would let any authenticated account deploy to somebody
  else's host.
* **a non-zero exit is a failure.** "Ran the command, must be fine" is the
  entire failure mode of shelling out. The deploy script exits non-zero when
  the remote service does not come up, and that has to reach the caller as an
  error, not as a 200 with discouraging text in it.

Check's probe itself is read-only and asserted so in the companion file
(test_qa_transport_readiness.py), where the probe script is inspected. The
route wrapping it is not purely read-only any more: a fully-passing Check
also starts the tunnel (Pedro reported running Check, seeing every check
green, and the transport still reading Uninitialized with nothing explaining
why -- the forward probe had already proved a working connection and thrown
it away). CheckStartsTheTunnelTests below covers that; it mirrors
InitStartsTheTunnelTests because the two routes now share
_start_tunnel_for_transport.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from routes import transports as tr


class _Request:
    def __init__(self, user: str = "admin"):
        self.state = type("S", (), {"session": {"user": user}})()


_TRANSPORT = {
    "id": "t-1",
    "name": "Pentester - Kali_MAc",
    "ssh_host": "pentester.example.net",
    "ssh_user": "claude-ai-machine",
    "ssh_key_path": "~/.ssh/id_ed25519",
}


class _Proc:
    """An asyncio subprocess stand-in with a chosen exit code and output."""

    def __init__(self, returncode: int, output: bytes = b""):
        self.returncode = returncode
        self._output = output

    async def communicate(self):
        return self._output, b""


def _body(response) -> dict:
    return json.loads(bytes(response.body))


class OwnerScopingTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_refuses_a_transport_that_is_not_yours(self):
        with patch.object(tr.db, "ssh_transport_get", AsyncMock(return_value=None)):
            with self.assertRaises(HTTPException) as caught:
                await tr.handle_transport_init(_Request("mallory"), "t-1")
        self.assertEqual(caught.exception.status_code, 404)

    async def test_check_refuses_a_transport_that_is_not_yours(self):
        with patch.object(tr.db, "ssh_transport_get", AsyncMock(return_value=None)):
            with self.assertRaises(HTTPException) as caught:
                await tr.handle_transport_check(_Request("mallory"), "t-1")
        self.assertEqual(caught.exception.status_code, 404)

    async def test_init_does_not_spawn_anything_for_a_foreign_transport(self):
        """The 404 must come before the subprocess, not after it."""
        spawn = AsyncMock()
        with patch.object(tr.db, "ssh_transport_get", AsyncMock(return_value=None)), \
                patch("asyncio.create_subprocess_exec", spawn):
            with self.assertRaises(HTTPException):
                await tr.handle_transport_init(_Request("mallory"), "t-1")
        spawn.assert_not_called()


class InitOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def _init(self, proc, machines=()):
        with patch.object(tr.db, "ssh_transport_get",
                          AsyncMock(return_value=dict(_TRANSPORT))), \
                patch.object(tr.db, "ai_machines_list",
                             AsyncMock(return_value=list(machines))), \
                patch("asyncio.create_subprocess_exec",
                      AsyncMock(return_value=proc)) as spawn:
            response = await tr.handle_transport_init(_Request(), "t-1")
        return response, spawn

    async def test_a_successful_deploy_reports_ok(self):
        response, _ = await self._init(_Proc(0, b"listening on 127.0.0.1:9000\n"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(_body(response)["ok"])

    async def test_a_failed_deploy_is_not_reported_as_success(self):
        """The whole point: exit 1 must not arrive as a cheerful 200."""
        response, _ = await self._init(_Proc(1, b"  NOT listening\n"))
        self.assertEqual(response.status_code, 502)
        body = _body(response)
        self.assertFalse(body["ok"])
        self.assertEqual(body["returncode"], 1)

    async def test_the_output_tail_reaches_the_caller(self):
        """A failure the user cannot see is a failure they cannot fix."""
        response, _ = await self._init(_Proc(1, b"line1\nline2\nNOT listening\n"))
        self.assertIn("NOT listening", _body(response)["output"])

    async def test_the_script_is_called_with_the_transport_name(self):
        """The deploy script resolves the host from ssh_transports by NAME; an
        id would find no transport and it would exit before doing anything."""
        _, spawn = await self._init(_Proc(0))
        argv = spawn.await_args.args
        self.assertIn(_TRANSPORT["name"], argv)
        self.assertIn("bash", argv[0])

    async def test_a_hanging_deploy_times_out_rather_than_hanging_the_request(self):
        class _Hang:
            returncode = None

            async def communicate(self):
                import asyncio

                await asyncio.sleep(3600)

        async def _expire(awaitable, timeout=None):
            # Close the coroutine we are refusing to await, so the test does
            # not leave an "never awaited" ResourceWarning behind it.
            awaitable.close()
            raise TimeoutError

        with patch.object(tr.db, "ssh_transport_get",
                          AsyncMock(return_value=dict(_TRANSPORT))), \
                patch("asyncio.create_subprocess_exec",
                      AsyncMock(return_value=_Hang())), \
                patch("asyncio.wait_for", _expire):
            response = await tr.handle_transport_init(_Request(), "t-1")
        self.assertEqual(response.status_code, 504)
        self.assertFalse(_body(response)["ok"])


class InitStartsTheTunnelTests(unittest.IsolatedAsyncioTestCase):
    """Pedro's request: Init does everything needed to become Active, not
    just the remote deploy half of it."""

    async def _init(self, proc, machines=()):
        with patch.object(tr.db, "ssh_transport_get",
                          AsyncMock(return_value=dict(_TRANSPORT))), \
                patch.object(tr.db, "ai_machines_list",
                             AsyncMock(return_value=list(machines))), \
                patch("asyncio.create_subprocess_exec",
                      AsyncMock(return_value=proc)) as spawn, \
                patch("tunnel_manager.queue_command",
                      AsyncMock()) as queue_command:
            response = await tr.handle_transport_init(_Request(), "t-1")
        return response, spawn, queue_command

    async def test_a_successful_deploy_starts_the_tunnel(self):
        machines = [{"id": "m-1", "transport_id": "t-1"}]
        response, _, queue_command = await self._init(_Proc(0), machines)
        self.assertTrue(_body(response)["tunnel_started"])
        queue_command.assert_awaited_once_with("m-1", "START_TUNNEL")

    async def test_only_machines_on_this_transport_are_considered(self):
        """A machine on a different transport must not be picked -- that
        would start a tunnel to the wrong host."""
        machines = [
            {"id": "m-other", "transport_id": "t-2"},
            {"id": "m-1", "transport_id": "t-1"},
        ]
        response, _, queue_command = await self._init(_Proc(0), machines)
        self.assertTrue(_body(response)["tunnel_started"])
        queue_command.assert_awaited_once_with("m-1", "START_TUNNEL")

    async def test_a_transport_with_no_machine_deploys_without_a_tunnel(self):
        """Nothing to start is not a failure -- deploy still succeeds, and the
        response says plainly that nothing was connected."""
        response, _, queue_command = await self._init(_Proc(0), machines=())
        self.assertTrue(_body(response)["ok"])
        self.assertFalse(_body(response)["tunnel_started"])
        queue_command.assert_not_awaited()

    async def test_a_failed_deploy_does_not_start_a_tunnel(self):
        """Connecting to a host whose proxy deploy just failed would put the
        badge on a tunnel with nothing behind it."""
        machines = [{"id": "m-1", "transport_id": "t-1"}]
        response, _, queue_command = await self._init(_Proc(1, b"NOT listening\n"), machines)
        self.assertFalse(_body(response)["ok"])
        self.assertFalse(_body(response)["tunnel_started"])
        queue_command.assert_not_awaited()


class CheckStartsTheTunnelTests(unittest.IsolatedAsyncioTestCase):
    """Pedro reported this exactly: ran Check, every check came back green,
    and the transport still read Uninitialized with nothing explaining why.
    _probe_forward had already opened a real forward and gotten a real ack,
    then discarded it by design -- a "ready" verdict was proof the connection
    works with nothing kept from the proof. A fully-passing Check now starts
    the tunnel instead of throwing that proof away.
    """

    def _readiness(self, *, ok: bool):
        import transport_readiness as treadi

        return treadi.Readiness(
            reachable=True,
            checks=[treadi.Check("claude CLI", ok, "detail")],
        )

    async def _check(self, *, ready: bool, machines=(), already_active=False):
        import transport_readiness as treadi

        with patch.object(tr.db, "ssh_transport_get",
                          AsyncMock(return_value=dict(_TRANSPORT))), \
                patch.object(tr.db, "setting_get", AsyncMock(return_value="tok")), \
                patch.object(treadi, "check_transport",
                             AsyncMock(return_value=self._readiness(ok=ready))), \
                patch.object(tr.db, "ai_machines_list",
                             AsyncMock(return_value=list(machines))), \
                patch("tunnel_manager.queue_command", AsyncMock()) as queue_command, \
                patch("tunnel_manager.tunnel_status", AsyncMock(
                    return_value={"proxy_ok": True} if already_active else None)):
            response = await tr.handle_transport_check(_Request(), "t-1")
        return response, queue_command

    async def test_a_fully_passing_check_starts_the_tunnel(self):
        machines = [{"id": "m-1", "transport_id": "t-1"}]
        response, queue_command = await self._check(ready=True, machines=machines)
        self.assertTrue(_body(response)["ready"])
        self.assertTrue(_body(response)["tunnel_started"])
        queue_command.assert_awaited_once_with("m-1", "START_TUNNEL")

    async def test_a_failing_check_does_not_start_anything(self):
        """The whole point of Check: a red check must never have a side
        effect that makes the badge lie about what was actually verified."""
        machines = [{"id": "m-1", "transport_id": "t-1"}]
        response, queue_command = await self._check(ready=False, machines=machines)
        self.assertFalse(_body(response)["ready"])
        self.assertFalse(_body(response)["tunnel_started"])
        queue_command.assert_not_awaited()

    async def test_a_transport_with_no_machine_is_ready_but_starts_nothing(self):
        response, queue_command = await self._check(ready=True, machines=())
        self.assertTrue(_body(response)["ready"])
        self.assertFalse(_body(response)["tunnel_started"])
        queue_command.assert_not_awaited()

    async def test_an_already_active_transport_is_not_bounced(self):
        """Clicking Check on a transport that is already connected must not
        reset a working tunnel -- that would be strictly worse than doing
        nothing."""
        machines = [{"id": "m-1", "transport_id": "t-1"}]
        response, queue_command = await self._check(
            ready=True, machines=machines, already_active=True)
        self.assertTrue(_body(response)["ready"])
        self.assertFalse(_body(response)["tunnel_started"])
        queue_command.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
