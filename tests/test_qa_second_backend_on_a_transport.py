"""QA: the second backend on a transport is routable too.

Reported 2026-09-26: "Node2-appsec via node1 can't connect to the AI Machine",
against a transport whose tunnel was up and healthy the whole time -- serving
its first backend, agent1, on port 9005.

`ssh_tunnels` is UNIQUE(machine_id), and every control that creates a row keys
off `machines[0]`: machines.js's SSH badge, its group status, and
_start_tunnel_for_transport. So the first backend added to a transport gets a
row and every later one gets nothing, with no click anywhere that would change
that. runner.get_proxy_target looked only at the machine's own row, found
none, and refused the turn.

The refusal was right -- running it locally would have used a different host
and different credentials than the backend the operator picked. What was wrong
is that there was a perfectly good forward to exactly the right host sitting
one row away.

A forward is per transport, not per backend. tunnel_manager refcounts one SSH
connection per transport, and the remote proxy is per host: base_url, key and
model travel with each turn and are applied by claude_proxy._backend_env when
it spawns the child. So any open forward to that host carries a turn for any
backend on it.

These cases fix the property in place: a backend routes through its
transport's tunnel regardless of which backend on that transport happens to
own the row. The suite had never had a transport with two backends, which is
the only reason this survived.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db
import runner

_T = (
    "INSERT INTO ssh_transports (id, name, owner_id, ssh_host, ssh_user,"
    " ssh_key_path, created_at, updated_at)"
    " VALUES (?,?,?,?,?,?,'2026-09-26T00:00:00Z','2026-09-26T00:00:00Z')"
)
_M = (
    "INSERT INTO ai_machines (id, name, host, port, model, base_url, provider,"
    " active, enabled, owner_id, transport_id, created_at, updated_at)"
    " VALUES (?,?,?,?,?,?,?,0,1,?,?,?,?)"
)


class SecondBackendRoutingQA(unittest.IsolatedAsyncioTestCase):
    """Two backends on one transport, as production has had since 15:47."""

    TRANSPORT = "t" * 32
    FIRST = "a" * 32       # agent1 -- holds the only ssh_tunnels row
    SECOND = "b" * 32      # Node2-appsec via node1 -- has none
    PORT = 9005

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "p").mkdir(parents=True, exist_ok=True)
        self.dbp = patch.object(config, "DB_PATH", str(root / "wc.db"))
        self.rootp = patch.object(config, "PROJECTS_ROOT", str(root / "p"))
        self.dbp.start()
        self.rootp.start()
        await db.init()
        await db.user_create("admin", None, "x")
        self.owner = (await db.user_get_by_name("admin"))["id"]

        await db.db_conn.execute(
            _T, (self.TRANSPORT, "Node1-QA", self.owner, "n1.example",
                 "kali-pt", "/tmp/k"))
        # created_at ordering matters: the first backend is the one that owns
        # the row in production, and ai_machine_ids_on_transport sorts by it.
        await db.db_conn.execute(
            _M, (self.FIRST, "agent1", "api.anthropic.com", 443,
                 "claude-opus-5", "https://api.anthropic.com", "claude_code",
                 self.owner, self.TRANSPORT, "2026-09-26T14:00:00Z",
                 "2026-09-26T14:00:00Z"))
        await db.db_conn.execute(
            _M, (self.SECOND, "Node2-appsec via node1",
                 "llm.ai-machine.example", 443, "vllm/Qwen3.6-35B-A3B-NVFP4",
                 "https://llm.ai-machine.example", "claude_code",
                 self.owner, self.TRANSPORT, "2026-09-26T15:47:00Z",
                 "2026-09-26T15:47:00Z"))
        await db.db_conn.commit()

        # Exactly production's shape: a row for the first backend only.
        await db.ssh_tunnel_create(machine_id=self.FIRST, local_port=self.PORT)

    async def asyncTearDown(self):
        await db.close()
        self.dbp.stop()
        self.rootp.stop()
        self.tmp.cleanup()

    async def _target(self, machine_id, status_for=None):
        """get_proxy_target for *machine_id*, with in-memory tunnel state
        present only for the machine named in *status_for*."""
        async def _status(mid):
            if status_for and mid == status_for:
                return {"tunnel_up": 1, "proxy_ok": 1, "local_port": self.PORT}
            return None

        machine = await db.ai_machine_get(machine_id, self.owner)
        with patch.object(db, "chat_routing",
                          return_value={"owner": self.owner, "machine": machine}), \
                patch("tunnel_manager.tunnel_status", _status):
            return await runner.get_proxy_target("chat-1", self.owner)

    async def test_the_second_backend_routes_through_the_shared_tunnel(self):
        """The reported bug. It owns no row, and must still route."""
        host, port = await self._target(self.SECOND)
        self.assertEqual((host, port), ("127.0.0.1", self.PORT))

    async def test_it_prefers_a_healthy_tunnel_over_a_bare_row(self):
        """With the first backend's tunnel live in memory, the second must
        reach it rather than falling back to the persisted port -- same
        address here, but the healthy path is the one that must win."""
        host, port = await self._target(self.SECOND, status_for=self.FIRST)
        self.assertEqual((host, port), ("127.0.0.1", self.PORT))

    async def test_the_first_backend_still_routes_to_its_own_row(self):
        """The control. A change that routed everything to some sibling would
        satisfy the case above while breaking the backend that worked."""
        host, port = await self._target(self.FIRST)
        self.assertEqual((host, port), ("127.0.0.1", self.PORT))

    async def test_a_transport_with_no_tunnel_at_all_still_refuses(self):
        """The refusal must survive. Falling through to the local proxy would
        run the turn on this host, against different credentials than the
        backend the operator chose -- which is the outcome this whole branch
        exists to prevent."""
        await db.ssh_tunnel_delete(self.FIRST)
        with self.assertRaises(runner.TransportUnavailable) as caught:
            await self._target(self.SECOND)
        self.assertIn("Node2-appsec via node1", str(caught.exception))

    async def test_a_sibling_on_another_transport_is_never_borrowed(self):
        """Routing to a tunnel on a DIFFERENT host would be the original sin
        in a new form: the turn runs somewhere the operator did not pick."""
        other_transport, other_machine = "z" * 32, "c" * 32
        await db.db_conn.execute(
            _T, (other_transport, "Elsewhere", self.owner, "other.example",
                 "kali", "/tmp/k"))
        await db.db_conn.execute(
            _M, (other_machine, "elsewhere-1", "api.anthropic.com", 443,
                 "claude-opus-5", "https://api.anthropic.com", "claude_code",
                 self.owner, other_transport, "2026-09-26T13:00:00Z",
                 "2026-09-26T13:00:00Z"))
        await db.db_conn.commit()
        await db.ssh_tunnel_create(machine_id=other_machine, local_port=9099)
        await db.ssh_tunnel_delete(self.FIRST)

        with self.assertRaises(runner.TransportUnavailable):
            await self._target(self.SECOND)


class MachineIdsOnTransportQA(unittest.IsolatedAsyncioTestCase):
    """The lookup the routing change rests on."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "p").mkdir(parents=True, exist_ok=True)
        self.dbp = patch.object(config, "DB_PATH", str(root / "wc.db"))
        self.rootp = patch.object(config, "PROJECTS_ROOT", str(root / "p"))
        self.dbp.start()
        self.rootp.start()
        await db.init()
        await db.user_create("admin", None, "x")
        self.owner = (await db.user_get_by_name("admin"))["id"]
        await db.db_conn.execute(
            _T, ("t1", "T1", self.owner, "h1", "u", "/tmp/k"))
        await db.db_conn.execute(
            _T, ("t2", "T2", self.owner, "h2", "u", "/tmp/k"))
        for mid, tid, created in (("m1", "t1", "2026-09-26T10:00:00Z"),
                                  ("m2", "t1", "2026-09-26T12:00:00Z"),
                                  ("m3", "t2", "2026-09-26T11:00:00Z")):
            await db.db_conn.execute(
                _M, (mid, mid, "h", 443, "m", "https://e", "claude_code",
                     self.owner, tid, created, created))
        await db.db_conn.commit()

    async def asyncTearDown(self):
        await db.close()
        self.dbp.stop()
        self.rootp.stop()
        self.tmp.cleanup()

    async def test_it_returns_only_that_transports_machines(self):
        self.assertEqual(await db.ai_machine_ids_on_transport("t1"), ["m1", "m2"])
        self.assertEqual(await db.ai_machine_ids_on_transport("t2"), ["m3"])

    async def test_an_unknown_transport_is_empty_not_everything(self):
        """A query that dropped its WHERE clause would return every machine
        and route turns to arbitrary hosts."""
        self.assertEqual(await db.ai_machine_ids_on_transport("nope"), [])

    async def test_the_order_is_stable(self):
        """Callers take "some sibling's port" from this list. Ordering by
        creation keeps that answer the same across calls, so a rename cannot
        silently move a running backend to a different forward."""
        self.assertEqual(
            await db.ai_machine_ids_on_transport("t1"),
            await db.ai_machine_ids_on_transport("t1"))


if __name__ == "__main__":
    unittest.main()
