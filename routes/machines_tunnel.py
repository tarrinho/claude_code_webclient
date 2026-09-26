"""Tunnel API endpoints for SSH proxy support.

Routes: POST/GET /api/tunnel, POST /api/init/ssh-test, POST /api/init/probe.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request

import config
import db

_log = logging.getLogger("wc.tunnel.api")

router = APIRouter()


async def _body(req: Request) -> dict:
    """Parse JSON body or return empty dict.

    Request.body() is a coroutine -- every caller here called it bare,
    passing the coroutine object itself to json.loads(), which raised
    TypeError on every single request and was swallowed by the except
    clause below. Every endpoint in this file read machine_id (or
    ssh_host/ssh_key_path, for the init-wizard routes) as empty no matter
    what the client actually sent: starting or stopping a tunnel, or
    running the SSH-test/probe steps of the init wizard, always failed
    with "machine_id required" before ever reaching real logic. Live since
    this file shipped (c5d131d) -- not a regression from today's other
    fixes.
    """
    try:
        return json.loads(await req.body())
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}


def _user(req: Request) -> str:
    """Extract user from request session."""
    return getattr(getattr(req, "state", None), "session", {}).get("user", "")


# ── Tunnel control ──────────────────────────────────────────────────────

@router.post("/api/tunnel/start")
async def tunnel_start(req: Request):
    """Start tunnel for a transport-routed machine."""
    import tunnel_manager

    user = _user(req)
    data = await _body(req)
    machine_id = data.get("machine_id", "")
    if not machine_id:
        raise HTTPException(status_code=400, detail="machine_id required")

    machine = await db.ai_machine_get(machine_id, user)
    if not machine:
        raise HTTPException(status_code=404, detail="machine not found")
    if not machine.get("transport_id"):
        raise HTTPException(status_code=400, detail="not a transport-routed machine")

    # Create the tunnel DB row if missing. Shared with a passing Check and
    # with Init, which queue the same command and need the same row -- see
    # db.ssh_tunnel_ensure.
    #
    # ai_machines.id is a hex UUID string, never a small integer --
    # int(machine_id) raised ValueError on every real machine id, silently
    # swallowed by a broad `except Exception` here that then logged "will use
    # existing", so the row was never actually created and no ssh_proxy tunnel
    # connected at all. ssh_tunnels.machine_id is declared INTEGER, but
    # SQLite's dynamic typing stores a non-numeric TEXT value in an INTEGER
    # column as-is (verified directly against a throwaway connection), so
    # passing the string through needs no schema change.
    #
    # That `except Exception` is gone rather than narrowed. It claimed to fall
    # back on an existing row in the one case where there is none, which is
    # how two separate bugs in this call chain stayed invisible: the caller
    # got {"ok": true, "status": "connecting"} either way.
    await db.ssh_tunnel_ensure(
        machine_id,
        config.TUNNEL_PORT_RANGE_LOW,
        config.TUNNEL_PORT_RANGE_HIGH,
        reserved=(config.PROXY_PORT,),
    )

    await tunnel_manager.queue_command(machine_id, "START_TUNNEL")
    return {"ok": True, "status": "connecting"}


@router.post("/api/tunnel/stop")
async def tunnel_stop(req: Request):
    """Stop tunnel for a transport-routed machine."""
    import tunnel_manager

    user = _user(req)
    data = await _body(req)
    machine_id = data.get("machine_id", "")
    if not machine_id:
        raise HTTPException(status_code=400, detail="machine_id required")

    machine = await db.ai_machine_get(machine_id, user)
    if not machine:
        raise HTTPException(status_code=404, detail="machine not found")
    if not machine.get("transport_id"):
        raise HTTPException(status_code=400, detail="not a transport-routed machine")

    await tunnel_manager.queue_command(machine_id, "STOP_TUNNEL")
    return {"ok": True, "status": "disconnecting"}


@router.post("/api/tunnel/toggle")
async def tunnel_toggle(req: Request):
    """Toggle tunnel on/off for a transport-routed machine."""
    import tunnel_manager

    user = _user(req)
    data = await _body(req)
    machine_id = data.get("machine_id", "")
    if not machine_id:
        raise HTTPException(status_code=400, detail="machine_id required")

    machine = await db.ai_machine_get(machine_id, user)
    if not machine:
        raise HTTPException(status_code=404, detail="machine not found")
    if not machine.get("transport_id"):
        raise HTTPException(status_code=400, detail="not a transport-routed machine")

    status = await tunnel_manager.tunnel_status(machine_id)
    if status and status.get("tunnel_up"):
        await tunnel_manager.queue_command(machine_id, "STOP_TUNNEL")
        return {"ok": True, "status": "disconnecting"}
    else:
        await tunnel_manager.queue_command(machine_id, "START_TUNNEL")
        return {"ok": True, "status": "connecting"}


@router.get("/api/tunnel/status")
async def tunnel_status_endpoint(req: Request):
    """Return tunnel status for all transport-routed machines."""
    user = _user(req)
    import tunnel_manager

    machines = await db.ai_machines_list(user)
    result = {}
    for m in machines:
        if m.get("transport_id"):
            mid = m["id"]
            status = await tunnel_manager.tunnel_status(mid)
            if not status:
                # Tunnel manager in-memory state may be empty if the manager
                # hasn't been started yet but the DB record says the tunnel is
                # up (e.g. a tunnel was started externally or the manager
                # process restarted). Fall back to the persisted row.
                db_row = await db.ssh_tunnel_get(mid)
                if db_row:
                    status = {
                        "state": db_row.get("state"),
                        "tunnel_up": db_row.get("tunnel_up"),
                        "proxy_ok": db_row.get("proxy_ok"),
                        "local_port": db_row.get("local_port", 0),
                        "error_msg": db_row.get("error_msg"),
                        "connected_at": db_row.get("connected_at"),
                        "last_check": db_row.get("last_check"),
                        # Marks this as last-known rather than just-checked.
                        # Consumed and removed below.
                        "_from_db": True,
                    }
            if status:
                # proxy_ok is reported, never inferred.
                #
                # This used to upgrade a falsy proxy_ok to True whenever the
                # persisted row said state='connected' and tunnel_up=1. The
                # comment justifying it said the stored proxy_ok "may be stale
                # even though the tunnel is actually connected" -- but every
                # field in that row is equally stale, so the inference took one
                # unverified value and manufactured a second from it.
                #
                # RETRACTION, 2026-09-25. This comment used to cite a
                # measurement: that all four of this deployment's transport
                # backends were reported healthy here while every forwarded
                # port answered EOF. That was wrong. The probe producing those
                # EOFs ran outside the console with an empty
                # config.PROXY_TOKEN, and claude_proxy.py closes without
                # replying on a token mismatch -- so it was reading correct
                # authentication refusals as dead tunnels. With the real token
                # all four answer `ack`.
                #
                # The change is kept because the argument for it never needed
                # that evidence: a persisted row records what was true when it
                # was written, and manufacturing a second unverified value
                # from a first one is wrong whether or not anything is broken
                # today. machines.js renders 'active' from proxy_ok alone,
                # above a comment reading "no render can claim a state it was
                # never told" -- so what this endpoint says it knows matters,
                # and it should not say more than it does.
                #
                # `stale` is carried so a caller can distinguish "last known"
                # from "just checked". local_port stays: the port assignment is
                # a durable fact about the row, not a claim about health, and
                # runner.get_proxy_target relies on it to route to the right
                # place after a restart.
                from_db = bool(status.pop("_from_db", False))
                result[mid] = {
                    "state": status.get("state"),
                    "tunnel_up": bool(status.get("tunnel_up")),
                    "proxy_ok": bool(status.get("proxy_ok")) and not from_db,
                    "local_port": status.get("local_port", 0),
                    "error_msg": status.get("error_msg"),
                    "connected_at": status.get("connected_at"),
                    "last_check": status.get("last_check"),
                    "stale": from_db,
                }
    return result


@router.get("/api/tunnel/status/{machine_id}")
async def tunnel_machine_status(machine_id: str, req: Request):
    """Return tunnel status for one transport-routed machine."""
    user = _user(req)
    import tunnel_manager

    machine = await db.ai_machine_get(machine_id, user)
    if not machine or not machine.get("transport_id"):
        raise HTTPException(status_code=404, detail="transport-routed machine not found")

    status = await tunnel_manager.tunnel_status(machine_id)
    if not status:
        return {"state": "none", "tunnel_up": False}

    return {
        "state": status.get("state"),
        "tunnel_up": bool(status.get("tunnel_up")),
        "proxy_ok": bool(status.get("proxy_ok")),
        "local_port": status.get("local_port", 0),
        "error_msg": status.get("error_msg"),
        "connected_at": status.get("connected_at"),
        "last_check": status.get("last_check"),
    }


# ── Init wizard ─────────────────────────────────────────────────────────

@router.post("/api/init/ssh-test")
async def init_ssh_test(req: Request):
    """Test SSH connection to a remote host."""
    from tunnel_manager_ssh import test_ssh_connection

    data = await _body(req)
    ssh_host = (data.get("ssh_host") or "").strip()
    ssh_user = (data.get("ssh_user") or "kali").strip()
    ssh_key_path = (data.get("ssh_key_path") or "").strip()

    if not ssh_host:
        raise HTTPException(status_code=400, detail="ssh_host required")
    if not ssh_key_path:
        raise HTTPException(status_code=400, detail="ssh_key_path required")

    result = await test_ssh_connection(ssh_host, ssh_user, ssh_key_path)
    return result


@router.post("/api/init/probe-remote")
async def init_probe_remote(req: Request):
    """Probe remote host capabilities (claude CLI, python3, disk)."""
    from tunnel_manager_ssh import probe_remote as _probe

    user = _user(req)
    data = await _body(req)
    machine_id = data.get("machine_id", "")
    if not machine_id:
        raise HTTPException(status_code=400, detail="machine_id required")

    machine = await db.ai_machine_get(machine_id, user)
    if not machine:
        raise HTTPException(status_code=404, detail="machine not found")

    result = await _probe(machine_id)
    return result


# ── Machine test extension ──────────────────────────────────────────────

async def ssh_proxy_test_result(machine_id: str, provider: str) -> dict:
    """The tunnel half of POST /api/machines/{id}/test, for a transport-routed
    machine.

    Deliberately not a route. It used to carry its own
    ``@router.post("/api/machines/{machine_id}/test")``, which is the same
    path routes/machines.py registers, and app.py includes this router first.
    Starlette matches the first route whose path and method fit and never
    consults a second, so the duplicate did not conflict, warn, or fail at
    startup -- it silently won, and routes.machines.handle_machine_test was
    unreachable code that still looked live in its own module.

    What that cost: the Test button never tested anything except a
    transport-routed machine.
    Every other provider fell through to a branch that returned
    ``{"status": "configured"}`` -- a description of the database row, not a
    probe of the endpoint -- with no ``ok`` key at all. The frontend keys on
    ``ok`` and falls back to ``status`` for the reason, so a healthy gateway
    reported "Could not reach llm.ai-machine.cfappsecurity.com:443:
    configured", which reads as a network failure and is not one.

    Now there is one route, in routes/machines.py, which calls this for the
    provider it applies to. The tunnel logic stays in the module that owns
    tunnels; the routing decision stays in the module that owns the endpoint.
    """
    from tunnel_manager import tunnel_status

    result = {"provider": provider}
    status = await tunnel_status(machine_id)
    result["tunnel_up"] = bool(status.get("tunnel_up", 0)) if status else False
    result["proxy_ok"] = bool(status.get("proxy_ok", 0)) if status else False
    if status and status.get("tunnel_up"):
        result["local_port"] = status.get("local_port", 0)
        result["status"] = "connected" if status.get("proxy_ok") else "connecting"
    else:
        result["status"] = "no_tunnel"
        error = status.get("error_msg") if status else None
        if error:
            result["error"] = error
    return result
