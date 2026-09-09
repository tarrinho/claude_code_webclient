"""Does Activate actually change which backend a turn runs against?

Settings > Backends has an Activate button per machine
(routes/machines.py:handle_machine_activate -> db.ai_machine_activate), and it
does flip the row's `active` column. Reported live as "doesn't seem to work" --
tested here through the functions that actually decide the child process's
environment, per CLAUDE.md's governing rule: the console is the `claude` CLI
with different parameters, and every backend decision has to reach
runner.get_backend / runner._build_env or it never reaches the subprocess at
all, regardless of what the database row says.

THE BUG is narrower than it first looks, and the corrected diagnosis matters
for the fix. `get_backend()` only ever returns non-empty for
`machine.get("provider") == "claude_code"`:

    if not machine or machine.get("provider") != "claude_code":
        return {}

That is CORRECT for a "proxy"-provider machine (Settings shows it as "Claude
Code proxy"): such a row is not overriding ANTHROPIC_BASE_URL/ANTHROPIC_API_KEY
at all -- the actual model backend for a proxy turn is whatever the *remote*
claude_proxy.py host's own environment/login provides. get_backend()
deliberately staying blind to "proxy" machines is right, and is asserted below
as a regression guard rather than a bug.

The real gap is one level up, in what a "proxy" machine's host+port are FOR:
selecting *which* claude_proxy.py TCP host a turn connects to.
`runner.get_proxy_host()` is where that selection has to happen, and it never
consults `ai_machines` at all -- it reads a single global, unscoped
`settings.ai_machine_host` key with no UI control anywhere in web/index.html or
app.js, and the port is hardcoded to `config.PROXY_PORT` at both of its call
sites (`_execute_proxy`, `_do_proxy_stream`) with no per-machine override
whatsoever. The `port` field a user fills in when creating a proxy machine is
stored and never read again by anything.

So Activate on a "proxy" machine flips a database flag and a checkmark in the
UI, and every turn keeps connecting to whichever proxy host the *global*
setting or `config.PROXY_HOST`/`config.PROXY_PORT` names -- completely
independent of which machine row is marked active.

Activating an "anthropic" machine is NOT broken -- get_backend does handle
that branch, and this file's passing tests below confirm it (regression
guard, so a future change to that branch is caught too).
"""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import auth
import config
import db
import runner
from tests.testing_model import TESTING_MODEL


class ActivateBackendTestsBase(unittest.IsolatedAsyncioTestCase):
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
        await db.user_create("alice", None, auth.hash_password("pw"))
        await db.chat_create("c1", "Alice's", None, f"{self.tmp.name}/p", "alice")


class AnthropicActivateWorksTests(ActivateBackendTestsBase):
    """Regression guard: this half of Activate is not broken and must stay
    that way while the proxy half is fixed.
    """

    async def test_get_backend_follows_the_newly_activated_machine(self):
        await db.ai_machine_create(
            "m1", "First", "a.example", 443, "key-one", TESTING_MODEL,
            "https://one.example", None, "alice", provider="claude_code",
        )
        await db.ai_machine_create(
            "m2", "Second", "b.example", 443, "key-two", TESTING_MODEL,
            "https://two.example", None, "alice", provider="claude_code",
        )
        await db.ai_machine_activate("m1", "alice")
        backend = await runner.get_backend("c1", owner="alice")
        self.assertEqual(backend.get("base_url"), "https://one.example")
        self.assertEqual(backend.get("api_key"), "key-one")

        await db.ai_machine_activate("m2", "alice")
        backend = await runner.get_backend("c1", owner="alice")
        self.assertEqual(
            backend.get("base_url"), "https://two.example",
            "get_backend still reflects the machine that was active before "
            "the second Activate call",
        )
        self.assertEqual(backend.get("api_key"), "key-two")


class GetBackendIsRightlyBlindToProxyMachinesTests(ActivateBackendTestsBase):
    """SSH proxy machines must keep returning {} from get_backend --
    get_backend is only for claude_code/direct backends, and routing to a
    proxy target is handled by get_proxy_target, not by get_backend.
    """

    async def test_activating_a_proxy_machine_does_not_touch_get_backend(self):
        await db.ai_machine_create(
            "m1", "Proxy One", "10.0.0.5", 9001, None, "default",
            None, None, "alice", provider="ssh_proxy",
        )
        before = await runner.get_backend("c1", owner="alice")
        await db.ai_machine_activate("m1", "alice")
        after = await runner.get_backend("c1", owner="alice")
        self.assertEqual(before, after, "{}")
        self.assertEqual(after, {})


class ProxyActivateFollowsTheMachineTests(ActivateBackendTestsBase):
    """runner.get_proxy_target is the fix: the (host, port) a turn connects
    to now follows the same pin/active resolution get_backend already used
    for claude_code machines, through the exact functions _execute_proxy and
    _do_proxy_stream call before asyncio.open_connection.
    """

    async def test_the_target_follows_the_newly_activated_machine(self):
        await db.ai_machine_create(
            "m1", "Proxy One", "10.0.0.5", 9001, None, "default",
            None, None, "alice", provider="claude_code",
        )
        await db.ai_machine_create(
            "m2", "Proxy Two", "10.0.0.6", 9002, None, "default",
            None, None, "alice", provider="claude_code",
        )
        await db.ai_machine_activate("m1", "alice")
        host_1, port_1 = await runner.get_proxy_target("c1", owner="alice")
        self.assertEqual((host_1, port_1), ("10.0.0.5", 9001))

        await db.ai_machine_activate("m2", "alice")
        host_2, port_2 = await runner.get_proxy_target("c1", owner="alice")
        self.assertEqual(
            (host_2, port_2), ("10.0.0.6", 9002),
            "get_proxy_target still reflects the machine that was active "
            "before the second Activate call",
        )

    async def test_the_proxy_machines_own_port_is_used_not_the_global_default(self):
        """Stored at creation, read by nothing before this fix: both call
        sites hardcoded config.PROXY_PORT regardless of what a proxy
        machine's own row said.
        """
        await db.ai_machine_create(
            "m1", "Proxy One", "10.0.0.5", 9999, None, "default",
            None, None, "alice", provider="claude_code",
        )
        await db.ai_machine_activate("m1", "alice")
        _host, port = await runner.get_proxy_target("c1", owner="alice")
        self.assertEqual(port, 9999)
        self.assertNotEqual(
            port, config.PROXY_PORT,
            "fixture is meaningless if the test machine's port happens to "
            "match the global default",
        )

    async def test_a_chat_pinned_to_a_different_machine_overrides_the_active_one(self):
        """Mirrors get_backend's own pin-beats-active precedence, which a
        conversation pinned to a specific machine already relies on for
        anthropic machines.
        """
        await db.ai_machine_create(
            "m1", "Proxy One", "10.0.0.5", 9001, None, "default",
            None, None, "alice", provider="claude_code",
        )
        await db.ai_machine_create(
            "m2", "Proxy Two", "10.0.0.6", 9002, None, "default",
            None, None, "alice", provider="claude_code",
        )
        await db.ai_machine_activate("m1", "alice")
        await db.chat_set_machine("c1", "alice", "m2")
        host, port = await runner.get_proxy_target("c1", owner="alice")
        self.assertEqual(
            (host, port), ("10.0.0.6", 9002),
            "the chat's pin must win over the owner's globally active machine",
        )

    async def test_with_no_proxy_machine_active_the_legacy_default_still_works(self):
        """No behaviour change for a deployment that has never used per-machine
        proxy rows: falls through to get_proxy_host()/config.PROXY_PORT
        exactly as before this fix existed.
        """
        host, port = await runner.get_proxy_target("c1", owner="alice")
        self.assertEqual(host, await runner.get_proxy_host())
        self.assertEqual(port, config.PROXY_PORT)

    async def test_an_active_anthropic_machine_does_not_hijack_the_proxy_target(self):
        """The owner's active machine can be an anthropic one (get_backend's
        machine) while the proxy path still needs somewhere to connect --
        an anthropic row must not be mistaken for a proxy target.
        """
        await db.ai_machine_create(
            "m1", "Anthropic", "a.example", 443, "key", TESTING_MODEL,
            "https://one.example", None, "alice", provider="claude_code",
        )
        await db.ai_machine_activate("m1", "alice")
        host, port = await runner.get_proxy_target("c1", owner="alice")
        self.assertEqual(host, await runner.get_proxy_host())
        self.assertEqual(port, config.PROXY_PORT)


if __name__ == "__main__":
    unittest.main()
