"""QA: clicking Test on an AI machine goes through the Claude Code CLI.

Pedro reported clicking Test on the Anthropic backend and getting:

    Could not reach api.anthropic.com:443: Endpoint requires an API key

`_test_anthropic_endpoint` sent `x-api-key` straight to the provider over
urllib -- the one place in this codebase that called a model API directly,
against CLAUDE.md's governing rule (the console never talks to a model API, it
spawns `claude`). It also gave a wrong verdict for a *host-login* backend: the
official Anthropic machine stores no key on purpose (credentials come from the
CLI's own OAuth login), so the raw probe had nothing to send, always got 401,
and reported "requires an API key" -- even though every real turn on that exact
backend succeeds through the CLI's login.

Fixed by running one real `-p` turn through the CLI with the environment
`backend_env.deltas` builds for the machine under test, and reading the CLI's
own stream-json frames for the verdict instead of asking the provider anything
ourselves. Verified live against both real backends before this was written:
official Anthropic API (host login) and the CF gateway (real + a deliberately
wrong key), all three giving the correct verdict in under 7s.

These tests guard the property structurally (no raw HTTP call survives in the
function) and behaviourally (both backends, both credential shapes, and the
kill-on-decision that keeps a bad key from turning a connectivity check into a
two-minute retry storm).
"""
from __future__ import annotations

import ast
import inspect
import json
import unittest
from unittest.mock import AsyncMock, patch

from routes import machines as machine_routes


class _Stdout:
    """Async-iterable stdout yielding canned lines, then StopAsyncIteration."""

    def __init__(self, lines: list[str]):
        self._lines = [line.encode() for line in lines]

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _Proc:
    def __init__(self, lines: list[str]):
        self.stdout = _Stdout(lines)
        self.returncode: int | None = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


RESULT_OK = '{"type": "result", "is_error": false}'
RETRY_401 = ('{"type": "system", "subtype": "api_retry", "attempt": 1, '
             '"error_status": 401, "error": "authentication_failed"}')

# The exact machine that reproduced the report: official Anthropic API,
# no stored key -- so `api_key` below is None, matching a host-login backend.
ANTHROPIC_HOST_LOGIN = {
    "provider": "anthropic",
    "base_url": "https://api.anthropic.com",
    "model": "claude-sonnet-5",
}

CF_GATEWAY = {
    "provider": "anthropic",
    "base_url": "https://llm.ai-machine.cfappsecurity.com",
    "model": "vllm/Qwen3.6-35B-A3B-NVFP4",
}


async def _probe(machine: dict, api_key: str | None, lines: list[str]):
    """Run `_test_anthropic_endpoint` with a fake CLI process and no SSRF check
    getting in the way -- that check is `_test_anthropic_endpoint`'s own
    concern, covered separately in tests/test_machine_provider.py."""
    proc = _Proc(lines)
    spawn = AsyncMock(return_value=proc)
    with (
        patch("asyncio.create_subprocess_exec", spawn),
        patch.object(machine_routes, "_resolve_host", return_value="160.79.104.10"),
    ):
        response = await machine_routes._test_anthropic_endpoint(machine, api_key)
    return response, proc, spawn


class NoRawHttpToTheProviderTests(unittest.TestCase):
    """Structural: the function must not talk to a model API by itself.

    This is the exact shape of the original bug and the exact shape a careless
    revert would take, so it is worth asserting on the source rather than only
    on behaviour -- a mock that happens to still pass does not prove the raw
    call is gone.
    """

    def test_no_urllib_request_in_the_function(self):
        # Strip the docstring first: it names "x-api-key" and "urllib" while
        # explaining the old bug, and that mention must not trip this check.
        source = inspect.getsource(machine_routes._test_anthropic_endpoint)
        body = ast.get_docstring(
            ast.parse(source).body[0], clean=False
        )
        code_only = source.replace(body or "", "", 1)
        self.assertNotIn("urllib", code_only)
        self.assertNotIn("x-api-key", code_only)
        self.assertNotIn("urlopen", code_only)

    def test_it_spawns_the_cli(self):
        body = inspect.getsource(machine_routes._test_anthropic_endpoint)
        self.assertIn("create_subprocess_exec", body)
        self.assertIn("backend_env.deltas", body)


class BothBackendsGoThroughTheCliTests(unittest.IsolatedAsyncioTestCase):
    """"Both backends" -- the official API and the gateway -- must both work,
    and both must be tested by actually spawning the CLI, not by guessing."""

    async def test_official_anthropic_with_host_login_is_reachable(self):
        """The exact case Pedro hit: no stored key, OAuth login instead.

        Before the fix this always reported "Endpoint requires an API key" --
        api_key=None here reproduces that starting condition, and the assertion
        is that it now succeeds, because the CLI (not this handler) is what
        actually holds the OAuth login.
        """
        response, _proc, spawn = await _probe(ANTHROPIC_HOST_LOGIN, None, [RESULT_OK])
        body = json.loads(response.body)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["status"], "reachable")
        spawn.assert_awaited_once()

    async def test_official_anthropic_with_a_bad_key_is_auth_failed(self):
        response, _proc, _spawn = await _probe(
            ANTHROPIC_HOST_LOGIN, "sk-ant-not-real", [RETRY_401])
        body = json.loads(response.body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["status"], "auth_failed")
        self.assertEqual(response.status_code, 502)

    async def test_cf_gateway_with_the_real_key_is_reachable(self):
        response, _proc, _spawn = await _probe(CF_GATEWAY, "real-gateway-key", [RESULT_OK])
        body = json.loads(response.body)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["status"], "reachable")

    async def test_cf_gateway_with_a_bad_key_is_auth_failed_not_reachable(self):
        """The bug in miniature for a gateway too: a bare TCP/HTTP check would
        call this reachable, which is what "reachable" meant before this fix
        -- accepted, not necessarily working."""
        response, _proc, _spawn = await _probe(CF_GATEWAY, "wrong-key", [RETRY_401])
        body = json.loads(response.body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["status"], "auth_failed")

    async def test_each_backend_gets_its_own_model_in_the_command(self):
        """A probe against the gateway must not run the Anthropic model id, or
        vice versa -- see CLAUDE.md 0.1: a model id only means something
        against its own backend."""
        _r, _p, spawn = await _probe(CF_GATEWAY, "k", [RESULT_OK])
        args = spawn.call_args.args
        self.assertIn("--model", args)
        self.assertEqual(args[args.index("--model") + 1], "vllm/Qwen3.6-35B-A3B-NVFP4")


class DoesNotHangOnABadKeyTests(unittest.IsolatedAsyncioTestCase):
    """A bad key makes the CLI retry with exponential backoff -- measured live:
    10 attempts, delays growing past 16 seconds. The probe must not sit through
    that; the first retry frame already carries the verdict."""

    async def test_the_process_is_killed_after_the_first_retry_frame(self):
        _response, proc, _spawn = await _probe(ANTHROPIC_HOST_LOGIN, "bad", [RETRY_401])
        self.assertTrue(proc.killed)

    async def test_a_second_retry_frame_is_never_read(self):
        """If the process were not killed, the loop would keep consuming
        frames. Feeding a second one and asserting it is never reached is
        the direct proof, independent of the `killed` flag."""
        second_retry = ('{"type": "system", "subtype": "api_retry", "attempt": 2, '
                        '"error_status": 401, "error": "authentication_failed"}')
        _response, proc, _spawn = await _probe(
            ANTHROPIC_HOST_LOGIN, "bad", [RETRY_401, second_retry])
        # The first frame was consumed and decided; the second must still be
        # sitting in the fake stdout, unread, because nothing asked for it.
        self.assertEqual(len(proc.stdout._lines), 1)


if __name__ == "__main__":
    unittest.main()
