"""QA: whoever queues START_TUNNEL must first make the row it needs.

Node1-Appsec spent 2026-09-26 stuck on Uninitialized. The log says why, and
says it every thirty seconds from 12:00:30 to 13:55:

    WARNING wc.tunnel_manager: tunnel_connect_failed
            machine=e25610e1bb244c10aae86ed0099c1856 reason=no tunnel row

with two operator actions in the middle that both reported success:

    13:53:46 INFO transport_check name=Node1-Appsec ready=True reachable=True
    13:55:38 INFO transport_init_done name=Node1-Appsec rc=0 ok=True

Check and Init both call `_start_tunnel_for_transport`, which queued
START_TUNNEL and returned True without creating the `ssh_tunnels` row.
`tunnel_manager_ssh` refuses a machine with no row, so the command could never
be serviced -- while the UI toasted "ready and connecting…" and the API
returned `tunnel_started: true`. `POST /api/tunnel/start` was the only path in
the codebase that created the row, which is why the SSH badge worked and the
other two did not.

These cases pin the property rather than the incident: a path that queues
START_TUNNEL leaves behind a row `tunnel_manager_ssh` would accept. A test
naming only Check would have passed while Init stayed broken -- the same trap
the transport probes fell into.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db

_T = (
    "INSERT INTO ssh_transports (id, name, owner_id, ssh_host, ssh_user,"
    " ssh_key_path, created_at, updated_at)"
    " VALUES (?,?,?,?,?,?,'2026-09-26T00:00:00Z','2026-09-26T00:00:00Z')"
)
_M = (
    "INSERT INTO ai_machines (id, name, host, port, model, base_url, provider,"
    " active, enabled, owner_id, transport_id, created_at, updated_at)"
    " VALUES (?,?,?,?,?,?,?,1,1,?,?,'2026-09-26T00:00:00Z','2026-09-26T00:00:00Z')"
)


class _DbFixture(unittest.IsolatedAsyncioTestCase):
    """A throwaway database. Never db.init() against the real one."""

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

    async def asyncTearDown(self):
        await db.close()
        self.dbp.stop()
        self.rootp.stop()
        self.tmp.cleanup()


class TunnelRowEnsureQA(_DbFixture):
    """`db.ssh_tunnel_ensure` is the one place a row gets made."""

    async def test_it_creates_a_row_when_there_is_none(self):
        row = await db.ssh_tunnel_ensure("machine-a", 9000, 9100)
        self.assertIsNotNone(row, "no row returned")
        self.assertIsNotNone(
            await db.ssh_tunnel_get("machine-a"),
            "ensure returned a row that is not in the database")

    async def test_it_is_idempotent(self):
        """Check, then Init, then the SSH badge is an ordinary sequence. The
        later calls must not fail, and must not renumber the port out from
        under a connection the first one started."""
        first = await db.ssh_tunnel_ensure("machine-a", 9000, 9100)
        second = await db.ssh_tunnel_ensure("machine-a", 9000, 9100)
        self.assertEqual(first["local_port"], second["local_port"])
        self.assertEqual(first["id"], second["id"], "a duplicate row was made")

    async def test_two_machines_never_share_a_local_port(self):
        """The old caller hardcoded TUNNEL_PORT_RANGE_LOW for every machine.
        `ssh_tunnels` constrains machine_id and nothing else, so the duplicate
        inserted happily and failed much later, when the second forward could
        not bind -- a runtime symptom a long way from its cause."""
        ports = set()
        for name in ("machine-a", "machine-b", "machine-c"):
            row = await db.ssh_tunnel_ensure(name, 9000, 9100)
            ports.add(row["local_port"])
        self.assertEqual(
            len(ports), 3, f"machines shared a local port: {sorted(ports)}")

    async def test_it_stays_off_the_reserved_ports(self):
        """With the shipped defaults TUNNEL_PORT_RANGE_LOW == PROXY_PORT ==
        9000, so a naive allocation hands the first transport-routed machine
        the local proxy's own port."""
        row = await db.ssh_tunnel_ensure(
            "machine-a", 9000, 9100, reserved=(9000, 9001))
        self.assertNotIn(row["local_port"], (9000, 9001))

    async def test_an_exhausted_range_raises_rather_than_inventing_a_port(self):
        """The failure this replaces was silent. A range with nothing free has
        to say so, not hand back a port already in use."""
        await db.ssh_tunnel_ensure("machine-a", 9000, 9000)
        with self.assertRaises(RuntimeError):
            await db.ssh_tunnel_ensure("machine-b", 9000, 9000)


class StartPathsCreateTheRowQA(_DbFixture):
    """Every path that queues START_TUNNEL, not just the one that was tested.

    Check and Init were broken while the SSH badge worked, and a suite that
    covered the badge alone reported the feature healthy throughout.
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tid, self.mid = "t" * 32, "m" * 32
        await db.db_conn.execute(
            _T, (self.tid, "Node1-QA", self.owner, "n1.example", "kali-pt",
                 "/tmp/k"))
        await db.db_conn.execute(
            _M, (self.mid, "agent1", "n1.example", 9000, "claude-opus-5",
                 "https://api.anthropic.com", "claude_code", self.owner,
                 self.tid))
        await db.db_conn.commit()

    async def test_check_and_init_leave_a_usable_row(self):
        """The regression, stated as its property.

        Asserting on the row rather than on the return value is deliberate:
        the broken version returned True and queued the command, so a test
        that trusted either would have passed throughout the outage.
        """
        import routes.transports as transports

        queued = []

        async def _fake_queue(machine_id, command):
            queued.append((machine_id, command))

        async def _fake_status(machine_id):
            return None

        with patch("tunnel_manager.queue_command", _fake_queue), \
             patch("tunnel_manager.tunnel_status", _fake_status):
            started = await transports._start_tunnel_for_transport(
                self.tid, self.owner)

        self.assertTrue(started, "nothing was queued")
        self.assertEqual(queued, [(self.mid, "START_TUNNEL")])

        row = await db.ssh_tunnel_get(self.mid)
        self.assertIsNotNone(
            row,
            "START_TUNNEL was queued with no ssh_tunnels row -- "
            "tunnel_manager_ssh refuses it with 'no tunnel row' on every "
            "retry, exactly as it did for Node1-Appsec all day")
        self.assertNotEqual(
            row["local_port"], config.PROXY_PORT,
            "the tunnel was given the local proxy's own port")

    async def test_the_ssh_badge_path_still_creates_its_row(self):
        """The one path that always worked. Without this the refactor could
        move the defect rather than remove it, and nothing would say so."""
        from routes import machines_tunnel

        queued = []

        async def _fake_queue(machine_id, command):
            queued.append((machine_id, command))

        req = type("R", (), {"state": type("S", (), {"session": None})()})()
        with patch.object(machines_tunnel, "_user", return_value=self.owner), \
             patch.object(machines_tunnel, "_body",
                          return_value={"machine_id": self.mid}), \
             patch("tunnel_manager.queue_command", _fake_queue):
            result = await machines_tunnel.tunnel_start(req)

        self.assertEqual(result["status"], "connecting")
        self.assertEqual(queued, [(self.mid, "START_TUNNEL")])
        self.assertIsNotNone(await db.ssh_tunnel_get(self.mid))

    async def test_the_toggle_path_creates_its_row_too(self):
        """The fourth producer of START_TUNNEL. Counting the callers is the
        whole lesson here -- one of the four was right and three were wrong,
        and the suite only covered the one that was right."""
        from routes import machines_tunnel

        queued = []

        async def _fake_queue(machine_id, command):
            queued.append((machine_id, command))

        req = type("R", (), {"state": type("S", (), {"session": None})()})()
        with patch.object(machines_tunnel, "_user", return_value=self.owner), \
             patch.object(machines_tunnel, "_body",
                          return_value={"machine_id": self.mid}), \
             patch("tunnel_manager.tunnel_status", return_value=None), \
             patch("tunnel_manager.queue_command", _fake_queue):
            result = await machines_tunnel.tunnel_toggle(req)

        self.assertEqual(result["status"], "connecting")
        self.assertEqual(queued, [(self.mid, "START_TUNNEL")])
        self.assertIsNotNone(
            await db.ssh_tunnel_get(self.mid),
            "toggle queued START_TUNNEL with no row for it to use")


if __name__ == "__main__":
    unittest.main()
