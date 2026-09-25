"""QA: a turn routed to an SSH-transport backend never runs on this host.

A backend with `transport_id` set exists to execute somewhere else. Until this
change, asking for one and starting a turn could silently run it on the console
host instead: `tunnel_status()` returned None whenever no tunnel had been
started, `get_proxy_target` fell through to the local proxy, and the only
signal was a log line no user sees.

Reproduced before the fix, against a throwaway database:

    tunnel state for that machine: None
    turn routed to        : 127.0.0.1:9000
    this host's own proxy : 127.0.0.1:9000

That is not a degraded turn. The backend carries its own base_url and
credentials, so the turn ran against a different endpoint than the operator
chose, and reported success. A turn that does not run is recoverable; a turn
that ran in the wrong place and said so nowhere a person looks is not.

Two separate defects were behind it:

* No in-memory tunnel state does NOT mean no tunnel. `tunnel_manager` keeps
  `_STATE` in memory only, while the port assignment is persisted in
  `ssh_tunnels`. Every console restart therefore looked like "no tunnel exists"
  to this code path, for tunnels that were perfectly real.
* When there genuinely is no tunnel, falling back to local was a choice, and
  the wrong one.

A third thing was found and removed rather than fixed: the comment here
promised that routing to a down tunnel would let `_execute_proxy` report
`"waiting_remote"` to the SSE client. That string appears nowhere in this
codebase and never has. A comment describing behaviour that does not exist is
worse than no comment, because it is load-bearing for the next reader's
judgement.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db
import runner

_OWNER_SQL = (
    "INSERT INTO ssh_transports (id, name, owner_id, ssh_host, ssh_user,"
    " ssh_key_path, created_at, updated_at)"
    " VALUES (?,?,?,?,?,?,'2026-09-25T00:00:00Z','2026-09-25T00:00:00Z')"
)
_MACHINE_SQL = (
    "INSERT INTO ai_machines (id, name, host, port, model, base_url, provider,"
    " active, enabled, owner_id, transport_id, created_at, updated_at)"
    " VALUES (?,?,?,?,?,?,?,1,1,?,?,'2026-09-25T00:00:00Z','2026-09-25T00:00:00Z')"
)


class TransportRoutingQA(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "p").mkdir(parents=True, exist_ok=True)
        self.db_patch = patch.object(config, "DB_PATH", str(root / "wc.db"))
        self.root_patch = patch.object(config, "PROJECTS_ROOT", str(root / "p"))
        self.db_patch.start()
        self.root_patch.start()
        await db.init()
        await db.user_create("admin", None, "x")
        self.owner = (await db.user_get_by_name("admin"))["id"]
        self.tid = "t" * 32
        self.mid = "m" * 32
        await db.db_conn.execute(
            _OWNER_SQL,
            (self.tid, "RemoteBox", self.owner, "remote.example", "kali", "/tmp/k"))
        await db.db_conn.execute(
            _MACHINE_SQL,
            (self.mid, "Remote Backend", "remote.example", 9000, "claude-opus-5",
             "https://api.anthropic.com", "claude_code", self.owner, self.tid))
        await db.db_conn.commit()
        await db.chat_create("c1", "Chat", None, str(root / "p"), self.owner)

    async def asyncTearDown(self):
        await db.close()
        self.db_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    async def _local(self):
        return await runner.get_proxy_host(), config.PROXY_PORT

    async def test_no_tunnel_refuses_instead_of_running_here(self):
        """The defect, stated as the property that replaces it."""
        with self.assertRaises(runner.TransportUnavailable) as caught:
            await runner.get_proxy_target("c1", self.owner)
        msg = str(caught.exception)
        self.assertIn("Remote Backend", msg,
                      "the refusal must name the backend the operator chose")
        self.assertIn("tunnel", msg.lower())
        self.assertIn("NOT run", msg,
                      "the message must say the turn did not run locally -- "
                      "otherwise the reader assumes it did and looks for output")

    async def test_the_refusal_is_an_oserror(self):
        """Load-bearing, and the reason no call site needed changing.

        Both proxy call sites wrap open_connection in `except (TimeoutError,
        OSError, ConnectionRefusedError)` and yield a {"type": "error"} frame.
        If this stopped being an OSError the refusal would escape as an
        unhandled exception instead of reaching the person who started the
        turn -- the failure would still be loud, but in the wrong place.
        """
        with self.assertRaises(OSError):
            await runner.get_proxy_target("c1", self.owner)

    async def test_a_persisted_tunnel_row_is_used_when_state_is_empty(self):
        """A restarted console is not an absent tunnel.

        tunnel_manager holds _STATE in memory; ssh_tunnels persists the port.
        Before this, every restart made live tunnels invisible to routing and
        sent their turns to the local proxy.
        """
        await db.ssh_tunnel_create(machine_id=self.mid, local_port=9457)
        host, port = await runner.get_proxy_target("c1", self.owner)
        self.assertEqual((host, port), ("127.0.0.1", 9457))

    async def test_a_live_tunnel_still_routes_to_its_port(self):
        """The working path, so the refusal cannot be satisfied by refusing
        everything -- which would pass both cases above while breaking every
        transport backend on the deployment."""
        with patch("tunnel_manager.tunnel_status",
                   return_value={"tunnel_up": 1, "proxy_ok": 1,
                                 "local_port": 9123}):
            host, port = await runner.get_proxy_target("c1", self.owner)
        self.assertEqual((host, port), ("127.0.0.1", 9123))

    async def test_a_local_backend_is_untouched(self):
        """Backends with no transport must keep routing to the local proxy.

        The change is scoped to `transport_id` being set; this is what says so.
        """
        await db.db_conn.execute(
            "UPDATE ai_machines SET transport_id = NULL WHERE id = ?", (self.mid,))
        await db.db_conn.commit()
        self.assertEqual(await runner.get_proxy_target("c1", self.owner),
                         await self._local())

    async def test_the_memory_pre_check_does_not_mask_the_refusal(self):
        """`memory_refusal` calls get_proxy_target to find the host it should
        measure. It must not turn the refusal into a memory complaint about a
        host nobody could reach -- the turn's own error is the useful one."""
        self.assertIsNone(await runner.memory_refusal("c1", self.owner))


if __name__ == "__main__":
    unittest.main()
