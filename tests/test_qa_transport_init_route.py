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

Check is asserted to stay read-only in the companion file
(test_qa_transport_readiness.py), where the probe script itself is inspected.
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
    async def _init(self, proc):
        with patch.object(tr.db, "ssh_transport_get",
                          AsyncMock(return_value=dict(_TRANSPORT))), \
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


if __name__ == "__main__":
    unittest.main()
