from __future__ import annotations

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
