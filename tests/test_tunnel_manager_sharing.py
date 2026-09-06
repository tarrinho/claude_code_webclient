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
        self.addAsyncCleanup(self._cleanup)

    async def _cleanup(self):
        tunnel_manager._STATE.clear()
        tunnel_manager._TRANSPORT_CONNECTIONS.clear()

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
