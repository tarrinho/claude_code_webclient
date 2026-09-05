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
    """Start tunnel for a ssh_proxy machine."""
    import tunnel_manager

    user = _user(req)
    data = await _body(req)
    machine_id = data.get("machine_id", "")
    if not machine_id:
        raise HTTPException(status_code=400, detail="machine_id required")

    machine = await db.ai_machine_get(machine_id, user)
    if not machine:
        raise HTTPException(status_code=404, detail="machine not found")
    if machine.get("provider") != "ssh_proxy":
        raise HTTPException(status_code=400, detail="not an ssh_proxy machine")

    # Create tunnel DB row if missing.
    try:
        existing = await db.ssh_tunnel_get(machine_id)
        if not existing:
            # ai_machines.id is a hex UUID string, never a small integer --
            # int(machine_id) raised ValueError on every real machine id,
            # silently swallowed by the except below, so this row was never
            # actually created and no ssh_proxy tunnel has ever connected
            # since the feature shipped. ssh_tunnels.machine_id is declared
            # INTEGER, but SQLite's dynamic typing stores a non-numeric TEXT
            # value in an INTEGER column as-is (verified directly against a
            # throwaway connection) -- passing the string through needs no
            # schema change, only removing the cast that was crashing this.
            await db.ssh_tunnel_create(
                machine_id=machine_id,
                local_port=config.TUNNEL_PORT_RANGE_LOW,
            )
    except Exception:  # tunnel creation is idempotent; skip errors here
        _log.warning("tunnel row creation failed, will use existing")

    await tunnel_manager.queue_command(machine_id, "START_TUNNEL")
    return {"ok": True, "status": "connecting"}


@router.post("/api/tunnel/stop")
async def tunnel_stop(req: Request):
    """Stop tunnel for a ssh_proxy machine."""
    import tunnel_manager

    user = _user(req)
    data = await _body(req)
    machine_id = data.get("machine_id", "")
    if not machine_id:
        raise HTTPException(status_code=400, detail="machine_id required")

    machine = await db.ai_machine_get(machine_id, user)
    if not machine:
        raise HTTPException(status_code=404, detail="machine not found")
    if machine.get("provider") != "ssh_proxy":
        raise HTTPException(status_code=400, detail="not an ssh_proxy machine")

    await tunnel_manager.queue_command(machine_id, "STOP_TUNNEL")
    return {"ok": True, "status": "disconnecting"}


@router.post("/api/tunnel/toggle")
async def tunnel_toggle(req: Request):
    """Toggle tunnel on/off for a ssh_proxy machine."""
    import tunnel_manager

    user = _user(req)
    data = await _body(req)
    machine_id = data.get("machine_id", "")
    if not machine_id:
        raise HTTPException(status_code=400, detail="machine_id required")

    machine = await db.ai_machine_get(machine_id, user)
    if not machine:
        raise HTTPException(status_code=404, detail="machine not found")
    if machine.get("provider") != "ssh_proxy":
        raise HTTPException(status_code=400, detail="not an ssh_proxy machine")

    status = await tunnel_manager.tunnel_status(machine_id)
    if status and status.get("tunnel_up"):
        await tunnel_manager.queue_command(machine_id, "STOP_TUNNEL")
        return {"ok": True, "status": "disconnecting"}
    else:
        await tunnel_manager.queue_command(machine_id, "START_TUNNEL")
        return {"ok": True, "status": "connecting"}


@router.get("/api/tunnel/status")
async def tunnel_status_endpoint(req: Request):
    """Return tunnel status for all ssh_proxy machines."""
    user = _user(req)
    import tunnel_manager

    machines = await db.ai_machines_list(user)
    result = {}
    for m in machines:
        if m.get("provider") == "ssh_proxy":
            mid = m["id"]
            status = await tunnel_manager.tunnel_status(mid)
            if status:
                result[mid] = {
                    "state": status.get("state"),
                    "tunnel_up": bool(status.get("tunnel_up")),
                    "proxy_ok": bool(status.get("proxy_ok")),
                    "local_port": status.get("local_port", 0),
                    "error_msg": status.get("error_msg"),
                    "connected_at": status.get("connected_at"),
                    "last_check": status.get("last_check"),
                }
    return result


@router.get("/api/tunnel/status/{machine_id}")
async def tunnel_machine_status(machine_id: str, req: Request):
    """Return tunnel status for one ssh_proxy machine."""
    user = _user(req)
    import tunnel_manager

    machine = await db.ai_machine_get(machine_id, user)
    if not machine or machine.get("provider") != "ssh_proxy":
        raise HTTPException(status_code=404, detail="ssh_proxy machine not found")

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

@router.post("/api/machines/{machine_id}/test")
async def machine_test(machine_id: str, req: Request):
    """Test machine connectivity, extended for ssh_proxy."""
    user = _user(req)
    from tunnel_manager import tunnel_status

    machine = await db.ai_machine_get(machine_id, user)
    if not machine:
        raise HTTPException(status_code=404, detail="machine not found")

    provider = machine.get("provider", "")
    result = {"provider": provider}

    if provider == "ssh_proxy":
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
    else:
        result["status"] = "configured"

    return result
