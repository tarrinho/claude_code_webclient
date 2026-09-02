"""QA: `stream_turn` carries `owner` all the way to backend resolution.

`run_turn` has an `owner` parameter and `stream_turn` did not. It is the fallback
identity for a caller whose `chat_id` is not a row in `chats` — which is exactly
what the supervisor is, since `supervisor_<uuid>` and `subtask_<id>` are labels
rather than conversations. Without it `get_backend` finds no routing row, the
child process is handed no base URL and no API key, and every turn dies on
"Not logged in - Please run /login".

That failure was diagnosed once on the blocking path and fixed by passing the
argument at the `run_turn` call site. What made it worth a test rather than a
one-line change is where the parameter already was: `_do_proxy_stream` and
`_do_direct_stream` have both accepted `owner` and consumed it via `get_backend`
all along, and **neither of their callers passed it**. So the parameter existed at
the bottom of both chains, was unreachable from the top, and was permanently
`None` — a fix that had been half-applied and looked complete from either end
alone.

These assert the property end to end: what reaches `get_backend` is what the
caller passed. Signature checks alone would have passed against the half-wired
version, because every signature involved was already correct.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import config
import runner

OWNER = "pedro"
# Deliberately not a conversation id. This is the shape the supervisor uses, and
# the shape that makes `owner` load-bearing rather than decorative.
SUPERVISOR_CHAT_ID = "subtask_9cbf264d_t001"


class SignatureTests(unittest.TestCase):
    """Cheap, and they fail where no subprocess or proxy is available."""

    def test_stream_turn_accepts_owner(self):
        self.assertIn("owner", inspect.signature(runner.stream_turn).parameters)

    def test_it_matches_run_turn(self):
        """The two entry points must agree, or a caller swapping one for the
        other silently loses the argument — which is how this arose."""
        streaming = inspect.signature(runner.stream_turn).parameters
        blocking = inspect.signature(runner.run_turn).parameters
        self.assertIn("owner", blocking)
        self.assertEqual(
            streaming["owner"].default, blocking["owner"].default,
            "owner must default the same way on both entry points",
        )

    def test_every_link_in_both_chains_accepts_it(self):
        """A missing link is invisible from either end.

        `stream_turn` -> `_proxy_stream_turn` -> `_do_proxy_stream`, and the
        direct equivalent. The bottom of both already had the parameter; the
        middle did not, and nothing in a signature check on either end alone
        would have said so.
        """
        for name in ("stream_turn", "_proxy_stream_turn", "_do_proxy_stream",
                     "_execute_direct_stream", "_do_direct_stream"):
            with self.subTest(function=name):
                fn = getattr(runner, name)
                self.assertIn(
                    "owner", inspect.signature(fn).parameters,
                    f"{name} drops owner, so the chain is broken at that link",
                )


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    """The property that matters: the value arrives at `get_backend`."""

    async def _owner_seen_by_get_backend(self, proxy_enabled: bool):
        """Drive `stream_turn` far enough to record what `get_backend` was given.

        `get_backend` is the consumer whose answer decides whether the child gets
        credentials, so it is the honest place to observe. Everything past it is
        stubbed: this asserts the wiring, not the transport.

        `PROJECTS_ROOT` is pointed at a directory this test creates. It used to
        use the configured one, which exists on the machine this was written on
        and does not in a clean checkout -- so `stream_turn` raised on its
        work-dir check before reaching `get_backend`, `seen` stayed empty, and the
        case failed claiming `owner` had not been delivered when the wiring was
        fine. It passed in a working tree and failed in the committed tree, which
        is what extract-and-test is for and what a test depending on the ambient
        filesystem earns.
        """
        seen: dict[str, object] = {}

        async def fake_get_backend(chat_id, owner=None):
            seen["chat_id"] = chat_id
            seen["owner"] = owner
            # Shape `_build_env` accepts; enough to get past resolution.
            return {"provider": "anthropic", "base_url": "", "api_key": ""}

        async def fake_default_model(chat_id=None, owner=None):
            return "claude-sonnet-5"

        # Fail immediately after resolution, so nothing is spawned and no proxy
        # is contacted. The error path still yields, so the generator is drained.
        async def boom(*args, **kwargs):
            raise OSError("stubbed: no transport in this test")

        # The transport is stubbed to fail, so the generator either raises or
        # yields an error event. Both are fine and neither is what is under
        # test -- `suppress` on the exact types rather than a blind
        # `assertRaises(BaseException)`, which would also have swallowed an
        # AssertionError and reported a pass.
        # One group, evaluated left to right, so `root` is bound in time for the
        # PROJECTS_ROOT patch that follows it.
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(config, "PROJECTS_ROOT", root),
            patch.object(config, "PROXY_ENABLED", proxy_enabled),
            patch.object(runner, "get_backend", fake_get_backend),
            patch.object(runner, "get_default_model", fake_default_model),
            patch.object(runner, "get_proxy_host", AsyncMock(return_value="127.0.0.1")),
            patch.object(asyncio, "open_connection", boom),
            patch.object(asyncio, "create_subprocess_exec", boom),
            contextlib.suppress(OSError, runner.TurnError),
        ):
            async for _event in runner.stream_turn(
                "do the thing",
                None,
                root,
                SUPERVISOR_CHAT_ID,
                None,
                OWNER,
            ):
                pass
        # Reached at all? Without this the assertions below cannot tell "owner was
        # dropped" from "the turn never got as far as resolving a backend", and
        # those have different fixes. The first version could not, and reported
        # the wrong one.
        self.assertIn(
            "owner", seen,
            "get_backend was never called, so this says nothing about owner -- "
            "stream_turn failed earlier, most likely on its work_dir check",
        )
        return seen

    async def test_the_direct_path_delivers_owner(self):
        seen = await self._owner_seen_by_get_backend(proxy_enabled=False)
        self.assertEqual(
            seen.get("owner"), OWNER,
            "get_backend was given owner=None, so the child would get no "
            "credentials and the turn would die on 'Not logged in'",
        )
        self.assertEqual(seen.get("chat_id"), SUPERVISOR_CHAT_ID)

    async def test_owner_is_optional_and_defaults_to_none(self):
        """Conversation callers pass no owner and must keep working.

        Their `chat_id` *is* a row in `chats`, so routing resolves without one.
        A required parameter here would have broken every chat turn.
        """
        seen: dict[str, object] = {}

        async def fake_get_backend(chat_id, owner=None):
            seen["owner"] = owner
            return {"provider": "anthropic", "base_url": "", "api_key": ""}

        async def boom(*args, **kwargs):
            raise OSError("stubbed")

        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(config, "PROJECTS_ROOT", root),
            patch.object(config, "PROXY_ENABLED", False),
            patch.object(runner, "get_backend", fake_get_backend),
            patch.object(asyncio, "create_subprocess_exec", boom),
            contextlib.suppress(OSError, runner.TurnError),
        ):
            async for _event in runner.stream_turn(
                "hello", None, root, "a-real-chat-id",
            ):
                pass
        # Same guard as above: an absent `owner` key would otherwise satisfy
        # `assertIsNone` whether the default worked or the turn never reached
        # resolution at all.
        self.assertIn("owner", seen, "get_backend was never called")
        self.assertIsNone(seen["owner"])


if __name__ == "__main__":
    unittest.main()
