from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import tunnel_manager
import tunnel_manager_ssh


class SharedTransportConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tunnel_manager._STATE.clear()
        tunnel_manager._TRANSPORT_CONNECTIONS.clear()
        tunnel_manager._CONNECTING.clear()
        self.addAsyncCleanup(self._cleanup)

    async def _cleanup(self):
        tunnel_manager._STATE.clear()
        tunnel_manager._TRANSPORT_CONNECTIONS.clear()
        tunnel_manager._CONNECTING.clear()

    async def test_two_machines_same_transport_share_one_ssh_client(self):
        import db

        fake_transport_row = {
            "id": "t1", "ssh_host": "h", "ssh_user": "kali",
            "ssh_key_path": "k", "ssh_host_key_fingerprint": "",
        }
        fake_machine_a = {"id": "ma", "transport_id": "t1", "owner_id": "admin"}
        fake_machine_b = {"id": "mb", "transport_id": "t1", "owner_id": "admin"}

        async def fake_tunnel_get(machine_id):
            return {"owner_id": "admin", "ssh_port": 22}

        async def fake_machine_get(machine_id, owner_id):
            return fake_machine_a if machine_id == "ma" else fake_machine_b

        async def fake_transport_get(transport_id, owner_id):
            return fake_transport_row

        fake_ssh_client = MagicMock()
        fake_transport_obj = MagicMock()
        fake_ssh_client.get_transport.return_value = fake_transport_obj

        connect_calls = []

        def fake_paramiko_connect(**kwargs):
            connect_calls.append(kwargs)

        with patch.object(db, "ssh_tunnel_get", fake_tunnel_get), \
             patch.object(db, "ai_machine_get", fake_machine_get), \
             patch.object(db, "ssh_transport_get", fake_transport_get), \
             patch("paramiko.SSHClient", return_value=fake_ssh_client), \
             patch.object(fake_ssh_client, "connect", fake_paramiko_connect), \
             patch("tunnel_manager_ssh._check_key_permissions", return_value="k"), \
             patch("tunnel_manager_ssh._find_available_port", AsyncMock(side_effect=[9001, 9002])), \
             patch("tunnel_manager_forward.start_forward", MagicMock(return_value=MagicMock())):
            ok_a, client_a, transport_a, port_a, _, _, _ = await tunnel_manager_ssh.connect("ma")
            ok_b, client_b, transport_b, port_b, _, _, _ = await tunnel_manager_ssh.connect("mb")

        self.assertTrue(ok_a)
        self.assertTrue(ok_b)
        # Same underlying SSH connection reused, only one real paramiko
        # handshake performed.
        self.assertEqual(len(connect_calls), 1)
        self.assertIs(client_a, client_b)
        self.assertIs(transport_a, transport_b)
        # But each machine still gets its own forwarded local port.
        self.assertNotEqual(port_a, port_b)
        self.assertEqual(tunnel_manager._TRANSPORT_CONNECTIONS["t1"]["refcount"], 2)

    async def test_releasing_one_machine_keeps_shared_connection_alive(self):
        tunnel_manager._TRANSPORT_CONNECTIONS["t1"] = {
            "ssh_client": MagicMock(), "transport": MagicMock(), "refcount": 2,
        }
        tunnel_manager._STATE["ma"] = {
            "transport_id": "t1", "forward_server": MagicMock(),
            "ssh_client": tunnel_manager._TRANSPORT_CONNECTIONS["t1"]["ssh_client"],
            "transport": tunnel_manager._TRANSPORT_CONNECTIONS["t1"]["transport"],
        }
        tunnel_manager._release_machine("ma")
        self.assertIn("t1", tunnel_manager._TRANSPORT_CONNECTIONS)
        self.assertEqual(tunnel_manager._TRANSPORT_CONNECTIONS["t1"]["refcount"], 1)

    async def test_releasing_the_last_machine_closes_the_shared_connection(self):
        shared_client = MagicMock()
        shared_transport = MagicMock()
        tunnel_manager._TRANSPORT_CONNECTIONS["t1"] = {
            "ssh_client": shared_client, "transport": shared_transport, "refcount": 1,
        }
        tunnel_manager._STATE["ma"] = {
            "transport_id": "t1", "forward_server": MagicMock(),
            "ssh_client": shared_client, "transport": shared_transport,
        }
        tunnel_manager._release_machine("ma")
        self.assertNotIn("t1", tunnel_manager._TRANSPORT_CONNECTIONS)
        shared_transport.close.assert_called_once()
        shared_client.close.assert_called_once()

    async def test_reconnect_is_idempotent_and_does_not_leak_refcount(self):
        """Reproduces the reviewer's bug 1: one machine, three RECONNECTs
        with no explicit release in between (exactly what a flapping health
        check triggers via _tick's error-backoff path, or repeated
        START_TUNNEL commands). Before the fix, the RECONNECT branch called
        _try_connect() again without releasing this machine's own prior
        claim first -- since the registry entry was never removed, each
        reconnect's connect() call took the *reuse* branch and just
        incremented refcount with no matching decrement, so three reconnects
        left refcount at 4 and a single real release only brought it to 3,
        never actually closing anything. After the fix, each reconnect
        releases-then-reconnects, so this sole-owner machine's refcount
        stays flat at 1 across all three reconnects (each one fully tears
        down and re-establishes, since it is the only user), and the final
        real release brings it to exactly 0 and closes the live connection.
        """
        connections = []

        def fake_try_connect(machine_id):
            # Stands in for tunnel_manager_ssh.connect(): reuse the live
            # registry entry if one exists, otherwise perform a fresh
            # "handshake" (a brand new client/transport pair) -- exactly
            # what a real reconnect does once RECONNECT has released this
            # machine's prior claim first.
            entry = tunnel_manager._TRANSPORT_CONNECTIONS.get("t1")
            if entry is None:
                client, transport = MagicMock(), MagicMock()
                connections.append((client, transport))
                entry = {"ssh_client": client, "transport": transport, "refcount": 0}
                tunnel_manager._TRANSPORT_CONNECTIONS["t1"] = entry
            entry["refcount"] += 1
            tunnel_manager._STATE[machine_id].update({
                "state": "connected",
                "transport_id": "t1",
                "ssh_client": entry["ssh_client"],
                "transport": entry["transport"],
                "forward_server": MagicMock(),
            })

        tunnel_manager._STATE["ma"] = {
            "state": "connecting", "tunnel_up": 0, "proxy_ok": 0,
            "error_msg": None, "connected_at": None, "last_check": "t0",
        }

        with patch.object(tunnel_manager, "_try_connect", side_effect=fake_try_connect):
            # First RECONNECT is this machine's initial real connect (no
            # prior claim yet to release); the next two are the flapping
            # reconnects the bug report describes.
            tunnel_manager._handle_command("RECONNECT", "ma")
            tunnel_manager._handle_command("RECONNECT", "ma")
            tunnel_manager._handle_command("RECONNECT", "ma")

        # Exactly one real "handshake" per reconnect cycle (three total),
        # each superseding the last -- not one entry accumulating refcount.
        self.assertEqual(len(connections), 3)
        self.assertEqual(tunnel_manager._TRANSPORT_CONNECTIONS["t1"]["refcount"], 1)

        tunnel_manager._release_machine("ma")
        self.assertNotIn("t1", tunnel_manager._TRANSPORT_CONNECTIONS)
        final_client, final_transport = connections[-1]
        final_client.close.assert_called_once()
        final_transport.close.assert_called_once()

    async def test_concurrent_first_connects_on_same_transport_do_not_race(self):
        """Reproduces the reviewer's bug 2: two machines connecting to the
        same not-yet-live transport *concurrently* (asyncio.gather, not
        sequential awaits) must not both perform a real handshake. Without
        tunnel_manager._transport_lock serialising the whole
        check-connect-store sequence, both connect() calls could see no live
        registry entry, both call paramiko.SSHClient().connect() for real,
        and the second write would clobber (orphan, never close) the
        first's client/transport -- and releasing one machine could then
        drive the *other's* still-live connection to refcount zero and
        close it out from under it."""
        import db

        fake_transport_row = {
            "id": "t1", "ssh_host": "h", "ssh_user": "kali",
            "ssh_key_path": "k", "ssh_host_key_fingerprint": "",
        }
        fake_machine_a = {"id": "ma", "transport_id": "t1", "owner_id": "admin"}
        fake_machine_b = {"id": "mb", "transport_id": "t1", "owner_id": "admin"}

        async def fake_tunnel_get(machine_id):
            return {"owner_id": "admin", "ssh_port": 22}

        async def fake_machine_get(machine_id, owner_id):
            return fake_machine_a if machine_id == "ma" else fake_machine_b

        async def fake_transport_get(transport_id, owner_id):
            return fake_transport_row

        made_clients = []
        connect_calls = []

        def fake_paramiko_connect(**kwargs):
            connect_calls.append(kwargs)
            # Held open briefly (this runs in a real thread via
            # asyncio.to_thread) so the other machine's connect() call gets
            # a genuine chance to run concurrently and take the wrong branch
            # if _transport_lock is not actually excluding it.
            time.sleep(0.05)

        def make_client(*args, **kwargs):
            client = MagicMock()
            client.get_transport.return_value = MagicMock()
            client.connect = fake_paramiko_connect
            made_clients.append(client)
            return client

        with patch.object(db, "ssh_tunnel_get", fake_tunnel_get), \
             patch.object(db, "ai_machine_get", fake_machine_get), \
             patch.object(db, "ssh_transport_get", fake_transport_get), \
             patch("paramiko.SSHClient", side_effect=make_client), \
             patch("tunnel_manager_ssh._check_key_permissions", return_value="k"), \
             patch("tunnel_manager_ssh._find_available_port", AsyncMock(side_effect=[9001, 9002])), \
             patch("tunnel_manager_forward.start_forward", MagicMock(return_value=MagicMock())):
            result_a, result_b = await asyncio.gather(
                tunnel_manager_ssh.connect("ma"),
                tunnel_manager_ssh.connect("mb"),
            )

        ok_a, client_a, transport_a, port_a, _, _, _ = result_a
        ok_b, client_b, transport_b, port_b, _, _, _ = result_b

        self.assertTrue(ok_a)
        self.assertTrue(ok_b)
        # Exactly one real paramiko handshake, and only one SSHClient built,
        # despite both machines connecting concurrently.
        self.assertEqual(len(connect_calls), 1)
        self.assertEqual(len(made_clients), 1)
        self.assertIs(client_a, client_b)
        self.assertIs(transport_a, transport_b)
        self.assertNotEqual(port_a, port_b)
        self.assertEqual(tunnel_manager._TRANSPORT_CONNECTIONS["t1"]["refcount"], 2)

        # Releasing one machine must not close the connection the other is
        # still using.
        tunnel_manager._STATE["ma"] = {
            "transport_id": "t1", "forward_server": MagicMock(),
            "ssh_client": client_a, "transport": transport_a,
        }
        tunnel_manager._STATE["mb"] = {
            "transport_id": "t1", "forward_server": MagicMock(),
            "ssh_client": client_b, "transport": transport_b,
        }
        tunnel_manager._release_machine("ma")
        self.assertIn("t1", tunnel_manager._TRANSPORT_CONNECTIONS)
        client_a.close.assert_not_called()
        transport_a.close.assert_not_called()

        tunnel_manager._release_machine("mb")
        self.assertNotIn("t1", tunnel_manager._TRANSPORT_CONNECTIONS)
        client_a.close.assert_called_once()
        transport_a.close.assert_called_once()

    async def test_orphaned_successful_connect_releases_its_own_claim(self):
        """Reproduces Issue A from round 2 review: _release_machine (e.g.
        via STOP_TUNNEL) racing an in-flight _try_connect(). Simulated by
        having the mocked connect() itself pop _STATE[machine_id] just
        before returning successfully -- exactly what a concurrent
        _release_machine() call while the real paramiko handshake is still
        awaiting would do. Before the fix, _run()'s
        _STATE[machine_id].update(...) would KeyError (silently swallowed
        by the generic except), and the successful connect's registry claim
        and forward server would leak forever -- refcount could never reach
        zero, poisoning the transport for every future machine sharing it.
        After the fix, _run() notices _STATE[machine_id] is gone and
        releases the claim itself."""
        forward_server = MagicMock()
        shared_client = MagicMock()
        shared_transport = MagicMock()

        async def fake_connect(machine_id):
            # Simulate a real handshake succeeding and registering itself in
            # the shared registry...
            tunnel_manager._TRANSPORT_CONNECTIONS["t1"] = {
                "ssh_client": shared_client, "transport": shared_transport,
                "refcount": 1,
            }
            # ...but by the time it's about to return, STOP_TUNNEL has
            # already raced in and released this machine's _STATE entry.
            tunnel_manager._STATE.pop(machine_id, None)
            return (True, shared_client, shared_transport, 9001, 22, forward_server, "t1")

        tunnel_manager._STATE["ma"] = {
            "state": "connecting", "tunnel_up": 0, "proxy_ok": 0,
            "error_msg": None, "connected_at": None, "last_check": "t0",
        }

        captured_tasks = []
        real_create_task = asyncio.create_task

        def capturing_create_task(coro, *a, **kw):
            t = real_create_task(coro, *a, **kw)
            captured_tasks.append(t)
            return t

        with patch("tunnel_manager_ssh.connect", fake_connect), \
             patch("tunnel_manager_forward.stop_forward") as mock_stop_forward, \
             patch("asyncio.create_task", side_effect=capturing_create_task):
            tunnel_manager._try_connect("ma")
            await captured_tasks[0]

        self.assertNotIn("t1", tunnel_manager._TRANSPORT_CONNECTIONS)
        shared_transport.close.assert_called_once()
        shared_client.close.assert_called_once()
        mock_stop_forward.assert_called_once_with(forward_server)
        # The in-flight guard must also clear once the orphaned connect is
        # done being cleaned up, or this machine could never reconnect.
        self.assertNotIn("ma", tunnel_manager._CONNECTING)

    async def test_second_reconnect_while_one_in_flight_is_a_no_op(self):
        """Reproduces Issue B from round 2 review: two RECONNECT/
        START_TUNNEL actions for the same machine close together (nothing
        stopped this before the fix) each call _try_connect(), each
        spawning its own _run() task -- both connect successfully and both
        increment the same transport's shared refcount, even though only
        one machine is actually using the connection, leaving a phantom +1
        that never closes. After the fix, a second _try_connect() call for
        a machine already in _CONNECTING is a no-op: only one _run() task
        is ever spawned while one is in flight."""
        connect_calls = []

        async def fake_connect(machine_id):
            connect_calls.append(machine_id)
            return (True, MagicMock(), MagicMock(), 9001, 22, MagicMock(), "t1")

        tunnel_manager._STATE["ma"] = {
            "state": "connecting", "tunnel_up": 0, "proxy_ok": 0,
            "error_msg": None, "connected_at": None, "last_check": "t0",
        }

        captured_tasks = []
        real_create_task = asyncio.create_task

        def capturing_create_task(coro, *a, **kw):
            t = real_create_task(coro, *a, **kw)
            captured_tasks.append(t)
            return t

        with patch("tunnel_manager_ssh.connect", fake_connect), \
             patch("asyncio.create_task", side_effect=capturing_create_task):
            tunnel_manager._try_connect("ma")
            # A second call while the first's _run() task hasn't even had a
            # chance to run yet (and therefore hasn't discarded "ma" from
            # _CONNECTING) must be a no-op -- no second task spawned. This
            # assertion doesn't depend on any scheduling/timing: the guard
            # is a synchronous set membership check inside _try_connect
            # itself, before anything is awaited.
            tunnel_manager._try_connect("ma")
            self.assertEqual(len(captured_tasks), 1)
            await captured_tasks[0]

        self.assertEqual(len(connect_calls), 1)
        self.assertNotIn("ma", tunnel_manager._CONNECTING)
