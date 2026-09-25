"""QA: tunnel status is reported, never inferred.

/api/tunnel/status falls back to the persisted `ssh_tunnels` row when the
tunnel manager has no in-memory state -- which is the normal condition after a
console restart, since `_STATE` lives in memory and the row does not. That
fallback is reasonable. What was not reasonable is what it did next:

    proxy_ok = bool(status.get("proxy_ok"))
    if not proxy_ok and status.get("state") == "connected" and status.get("tunnel_up"):
        proxy_ok = True

Every field in that row is equally stale, so this took one unverified value and
manufactured a second from it.

Measured on this deployment on 2026-09-25, after the console restarted at
00:27: all four transport-routed backends had stored state='connected',
tunnel_up=1, proxy_ok=1, and the endpoint reported them healthy.
`tunnel_manager_health.probe_proxy` returned False for every one, and every
forwarded port answered EOF. An `ssh -L` forward listens locally whether or not
anything is listening at the far end, so a live port proves the SSH session and
never the proxy behind it.

The console was telling an operator that four dead backends were ready to run
an agent. web/assets/machines.js renders the 'active' badge from `proxy_ok`
alone, directly under a comment reading "no render can claim a state it was
never told" -- true of the renderer, and false of what it was being told.
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
    " VALUES (?,?,?,?,?,?,'2026-09-25T00:00:00Z','2026-09-25T00:00:00Z')"
)
_M = (
    "INSERT INTO ai_machines (id, name, host, port, model, base_url, provider,"
    " active, enabled, owner_id, transport_id, created_at, updated_at)"
    " VALUES (?,?,?,?,?,?,?,1,1,?,?,'2026-09-25T00:00:00Z','2026-09-25T00:00:00Z')"
)


class TunnelStatusHonestyQA(unittest.IsolatedAsyncioTestCase):
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
        self.tid, self.mid = "t" * 32, "m" * 32
        await db.db_conn.execute(
            _T, (self.tid, "RemoteBox", self.owner, "r.example", "kali", "/tmp/k"))
        await db.db_conn.execute(
            _M, (self.mid, "Remote Backend", "r.example", 9000, "claude-opus-5",
                 "https://api.anthropic.com", "claude_code", self.owner, self.tid))
        await db.db_conn.commit()

    async def asyncTearDown(self):
        await db.close()
        self.dbp.stop()
        self.rootp.stop()
        self.tmp.cleanup()

    async def _status(self):
        """Call the endpoint body with no in-memory tunnel state."""
        from routes import machines_tunnel

        req = type("R", (), {"state": type("S", (), {"session": None})()})()
        with patch.object(machines_tunnel, "_user", return_value=self.owner), \
             patch("tunnel_manager.tunnel_status", return_value=None):
            return await machines_tunnel.tunnel_status_endpoint(req)

    async def _row(self, **kw):
        sets = ", ".join(f"{k} = ?" for k in kw)
        await db.db_conn.execute(
            f"UPDATE ssh_tunnels SET {sets} WHERE machine_id = ?",  # nosec B608
            (*kw.values(), self.mid))
        await db.db_conn.commit()

    async def test_a_stale_connected_row_is_not_reported_proxy_ok(self):
        """The defect, exactly as production held it."""
        await db.ssh_tunnel_create(machine_id=self.mid, local_port=9001)
        await self._row(state="connected", tunnel_up=1, proxy_ok=1)

        result = await self._status()
        entry = result[self.mid]
        self.assertFalse(
            entry["proxy_ok"],
            "a persisted row is last-known, not just-checked -- reporting it "
            "as proxy_ok renders the backend 'active' and invites an operator "
            "to start an agent on a tunnel with nothing behind it")
        self.assertTrue(entry["stale"],
                        "the caller must be able to tell last-known from checked")

    async def test_the_port_survives_even_though_health_does_not(self):
        """local_port is a durable assignment, not a claim about health.

        runner.get_proxy_target reads it to route to the right place after a
        restart, so suppressing it would trade a false 'healthy' for a turn
        sent to the wrong host -- the defect this pairs with.
        """
        await db.ssh_tunnel_create(machine_id=self.mid, local_port=9007)
        await self._row(state="connected", tunnel_up=1, proxy_ok=1)
        self.assertEqual((await self._status())[self.mid]["local_port"], 9007)

    async def test_live_state_is_reported_as_live(self):
        """The control. Without it, returning proxy_ok=False unconditionally
        would pass both cases above while marking every healthy tunnel dead."""
        from routes import machines_tunnel

        req = type("R", (), {"state": type("S", (), {"session": None})()})()
        live = {"state": "connected", "tunnel_up": 1, "proxy_ok": 1,
                "local_port": 9009, "error_msg": None, "connected_at": None,
                "last_check": None}
        with patch.object(machines_tunnel, "_user", return_value=self.owner), \
             patch("tunnel_manager.tunnel_status", return_value=live):
            result = await machines_tunnel.tunnel_status_endpoint(req)
        entry = result[self.mid]
        self.assertTrue(entry["proxy_ok"], "a live probe must still count")
        self.assertFalse(entry["stale"])


if __name__ == "__main__":
    unittest.main()
