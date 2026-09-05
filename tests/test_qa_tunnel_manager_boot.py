"""QA: the tunnel manager must actually boot, and its own boot-time reconnect
scan must not crash on the row shape it reads.

Two bugs, both in the same startup path, both silent:

  * app.py called `tunnel_manager.start(db.system_sample_insert)` with no
    `await` -- unlike sysstats.start() one line above it (a *sync*
    function, correctly called bare), tunnel_manager.start is `async def`.
    The call created a coroutine object and discarded it; its body --
    including `asyncio.create_task(_loop(...))`, the line that makes the
    manager exist as a running background task at all -- never executed.
    No reconnect scan at boot, no consumer for queue_command(), and every
    other tunnel bug fixed alongside this one (the request body never
    being parsed, `int(machine_id)` on a UUID string, un-awaited
    ssh_tunnel_get/list_active reads) still left a queued START_TUNNEL
    command sitting in an asyncio.Queue forever, because nothing was
    reading from it. Confirmed live: `wc.tunnel_manager` never logged a
    single line, and /api/tunnel/status/<id> stayed {"state": "none"}
    indefinitely after a real, successful-looking start request.

  * tunnel_manager.start()'s own reconnect scan did
    `for row in rows` over an aiosqlite Cursor directly -- Cursor has
    __aiter__ but not __iter__, so a plain `for` raises TypeError
    immediately. Silently caught by the surrounding `except Exception`,
    same shape as db.ssh_tunnel_get's missing await. Harmless only because
    ssh_tunnels had no tunnel_up=1 rows yet to reconnect.
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import config
import db
import tunnel_manager

REPO = Path(__file__).resolve().parent.parent
APP_PY = REPO / "app.py"


class StartupCallSiteTests(unittest.TestCase):
    """Guards the call site in app.py directly: a regression here is
    invisible to every other test in this file, since they call
    tunnel_manager.start() themselves, correctly awaited."""

    def test_app_py_awaits_tunnel_manager_start(self):
        source = APP_PY.read_text(encoding="utf-8")
        match = re.search(r"^(.*tunnel_manager\.start\(.*\))\s*$", source, re.MULTILINE)
        self.assertIsNotNone(
            match, "tunnel_manager.start(...) is no longer called from app.py "
            "at all -- update this test if it moved, don't just delete it",
        )
        line = match.group(1).strip()
        self.assertTrue(
            line.startswith("await "),
            f"tunnel_manager.start(...) is called without await: {line!r} -- "
            "this only creates a coroutine object and discards it, so the "
            "manager's background loop never starts",
        )


class SshTunnelListActiveQA(unittest.IsolatedAsyncioTestCase):
    """db.ssh_tunnel_list_active has no callers anywhere in the codebase
    today -- checked with a repo-wide grep before writing this -- but it is
    a real function with the exact same bug as ssh_tunnel_get right next
    to it (`return cursor.fetchall()`, a coroutine, never awaited), and
    deserves its own regression guard independent of whether anything
    calls it yet."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(
            config, "PROJECTS_ROOT", f"{self.tmp.name}/projects"
        )
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_returns_a_real_list_not_a_coroutine(self):
        m1, m2 = "a" * 32, "b" * 32
        await db.ssh_tunnel_create(machine_id=m1, local_port=19010)
        await db.ssh_tunnel_create(machine_id=m2, local_port=19011)
        await db.ssh_tunnel_update(m1, tunnel_up=1, state="connected")
        # m2 stays tunnel_up=0 -- proves the WHERE clause, not just that
        # something is returned.

        result = await db.ssh_tunnel_list_active()

        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["machine_id"], m1)


class TunnelManagerBootQA(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", f"{self.tmp.name}/db")
        self.root_patch = patch.object(
            config, "PROJECTS_ROOT", f"{self.tmp.name}/projects"
        )
        self.db_patch.start()
        self.root_patch.start()
        await db.init()

    async def asyncTearDown(self):
        await tunnel_manager.stop()
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def test_awaiting_start_actually_creates_the_running_task(self):
        # The behavioural half of the app.py bug: prove start(), when
        # actually awaited the way the fixed call site does, leaves a real,
        # live background task behind -- not just that it returns without
        # raising.
        store_fn = AsyncMock()
        await tunnel_manager.start(store_fn)
        self.assertIsNotNone(tunnel_manager._task)
        self.assertFalse(tunnel_manager._task.done())

    async def test_boot_scan_does_not_crash_on_a_real_reconnectable_row(self):
        # The second bug: a tunnel_up=1 row must not make the boot-time
        # reconnect scan raise (silently, into the broad except) before
        # ever reaching asyncio.create_task -- reproduced with a real row
        # via the same ssh_tunnel_create the API path uses, so this is the
        # actual shape start() reads, not an idealised one.
        machine_id = "m" * 32
        await db.ssh_tunnel_create(machine_id=machine_id, local_port=19000)
        await db.ssh_tunnel_update(machine_id, tunnel_up=1, state="connected")

        store_fn = AsyncMock()
        await tunnel_manager.start(store_fn)

        # The task gets created unconditionally, after the try/except --
        # asserting only on _task would pass whether or not the scan itself
        # crashed, since that failure is swallowed either way. What the scan
        # crashing actually loses is the RECONNECT command it should have
        # queued; the loop task hasn't run its first iteration yet at this
        # point (create_task only schedules it), so the queue still holds
        # exactly what start() put there.
        self.assertEqual(
            tunnel_manager._queue.qsize(), 1,
            "no RECONNECT command was queued for the tunnel_up=1 row -- "
            "the boot-time scan raised before reaching queue.put_nowait()",
        )
        self.assertEqual(
            tunnel_manager._queue.get_nowait(), ("RECONNECT", machine_id)
        )

        self.assertIsNotNone(
            tunnel_manager._task,
            "the reconnect scan crashing must not prevent the loop task "
            "from being created",
        )
        self.assertFalse(tunnel_manager._task.done())

    async def test_connect_reads_ssh_fields_from_the_machine_not_the_tunnel_row(self):
        """tunnel_manager_ssh.connect() read ssh_host/ssh_user/ssh_key_path
        from the ssh_tunnels row -- which has no such columns at all, they
        live on ai_machines, the table the Settings form actually saves
        them to -- and did it with .get(), which sqlite3.Row (what
        db.ssh_tunnel_get returns despite its `-> dict` type hint) doesn't
        support either. Every real field came back empty/AttributeError,
        so connect() could never have worked regardless of anything else
        fixed alongside it.

        Doesn't reach a real network connection (no SSH server here to
        reach) -- proves the fields were read correctly by using a real,
        distinguishing failure mode instead: a deliberately-missing key
        file. The old bug fails with "ssh_host is empty" (or an
        AttributeError before ever getting that far); the fix fails with
        "SSH key not found: ...", which only happens once ssh_host and
        ssh_key_path were both non-empty and correctly read.
        """
        import tunnel_manager_ssh

        machine_id = "n" * 32
        await db.ai_machine_create(
            machine_id, "Reachability Test", "", 0, None,
            "claude-sonnet-5", None, None, "admin",
            provider="ssh_proxy",
            ssh_host="host.invalid",
            ssh_user="kali",
            ssh_key_path="/tmp/definitely-does-not-exist-qa-key",
        )
        await db.ssh_tunnel_create(machine_id=machine_id, local_port=19001)
        # _fail() (what a failed connect() reports through) only records
        # error_msg into an *existing* _STATE entry -- normally created by
        # _handle_command's START_TUNNEL branch before connect() is ever
        # reached. Calling connect() directly, bypassing that, needs the
        # same precondition set up by hand.
        tunnel_manager._STATE[machine_id] = {
            "state": "connecting", "tunnel_up": 0, "proxy_ok": 0,
            "error_msg": None, "connected_at": None, "last_check": None,
        }

        ok, ssh_client, transport, local_port, ssh_port = (
            await tunnel_manager_ssh.connect(machine_id)
        )
        self.assertFalse(ok)
        state = tunnel_manager._STATE.get(machine_id) or {}
        self.assertIn(
            "SSH key not found", state.get("error_msg") or "",
            f"expected a key-not-found failure (proving ssh_host/"
            f"ssh_key_path were read correctly), got: {state.get('error_msg')!r}",
        )


if __name__ == "__main__":
    unittest.main()
