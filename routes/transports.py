"""Routes for /api/transports: SSH transport CRUD and connectivity test.

A transport is the SSH connection to a remote host (ssh_host/ssh_user/
ssh_key_path) -- not a backend. See
docs/superpowers/specs/2026-09-06-ssh-transport-backend-split-design.md.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import db
from net_validation import _HOST_PATTERN_LOCAL, _validate_host

_log = logging.getLogger("wc.app")

router = APIRouter()


@router.get("/api/transports")
async def handle_transports_list(request: Request):
    session = request.state.session
    transports = await db.ssh_transports_list(session["user"])
    return JSONResponse({"transports": transports})


@router.get("/api/transports/{transport_id}")
async def handle_transport_get(request: Request, transport_id: str):
    session = request.state.session
    transport = await db.ssh_transport_get(transport_id, session["user"])
    if not transport:
        raise HTTPException(status_code=404, detail="Transport not found")
    return JSONResponse(transport)


@router.post("/api/transports")
async def handle_transport_create(request: Request):
    session = request.state.session
    data = await request.json()
    name = (data.get("name") or "").strip()[:100]
    ssh_host = (data.get("ssh_host") or "").strip()
    ssh_user = (data.get("ssh_user") or "kali").strip()
    ssh_key_path = (data.get("ssh_key_path") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if not ssh_host:
        raise HTTPException(status_code=400, detail="SSH host is required")
    if not ssh_key_path:
        raise HTTPException(status_code=400, detail="SSH key path is required")
    if not _HOST_PATTERN_LOCAL.fullmatch(ssh_host):
        raise HTTPException(status_code=400, detail="Enter a valid hostname or IP address")
    _validate_host(ssh_host)
    transport_id = uuid.uuid4().hex
    await db.ssh_transport_create(
        transport_id, name, session["user"], ssh_host, ssh_user, ssh_key_path,
    )
    _log.info("ssh_transport created by user=%s name=%s", session["user"], name)
    return JSONResponse({"ok": True, "id": transport_id, "name": name})


@router.patch("/api/transports/{transport_id}")
async def handle_transport_patch(request: Request, transport_id: str):
    session = request.state.session
    data = await request.json()
    allowed = {"name", "ssh_host", "ssh_user", "ssh_key_path"}
    if not data or not set(data).issubset(allowed):
        raise HTTPException(status_code=400, detail="No valid fields to update")
    if "ssh_host" in data and data["ssh_host"] is not None:
        host = data["ssh_host"].strip()
        if not host:
            raise HTTPException(status_code=400, detail="SSH host cannot be empty")
        if not _HOST_PATTERN_LOCAL.fullmatch(host):
            raise HTTPException(status_code=400, detail="Enter a valid hostname or IP address")
        _validate_host(host)
        data["ssh_host"] = host
    if "ssh_key_path" in data and data["ssh_key_path"] is not None:
        key_path = data["ssh_key_path"].strip()
        if not key_path:
            raise HTTPException(status_code=400, detail="SSH key path cannot be empty")
        data["ssh_key_path"] = key_path
    if "ssh_user" in data and data["ssh_user"] is not None:
        user = data["ssh_user"].strip()
        if not user:
            raise HTTPException(status_code=400, detail="SSH user cannot be empty")
        data["ssh_user"] = user
    if "name" in data and data["name"] is not None:
        name = data["name"].strip()[:100]
        if not name:
            raise HTTPException(status_code=400, detail="Name cannot be empty")
        data["name"] = name
    updated = await db.ssh_transport_update(transport_id, session["user"], **data)
    if not updated:
        raise HTTPException(status_code=404, detail="Transport not found")
    return JSONResponse({"ok": True})


@router.delete("/api/transports/{transport_id}")
async def handle_transport_delete(request: Request, transport_id: str):
    session = request.state.session
    deleted = await db.ssh_transport_delete(transport_id, session["user"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Transport not found")
    return JSONResponse({"ok": True})


@router.post("/api/transports/test")
async def handle_transport_test_raw(request: Request):
    """Test SSH connectivity for the add-transport form, before it is saved."""
    from tunnel_manager_ssh import test_ssh_connection

    data = await request.json()
    result = await test_ssh_connection(
        (data.get("ssh_host") or "").strip(),
        (data.get("ssh_user") or "kali").strip(),
        (data.get("ssh_key_path") or "").strip(),
    )
    return JSONResponse(result)


@router.post("/api/transports/{transport_id}/test")
async def handle_transport_test_saved(request: Request, transport_id: str):
    """Test SSH connectivity for an already-saved transport."""
    from tunnel_manager_ssh import test_ssh_connection

    session = request.state.session
    transport = await db.ssh_transport_get(transport_id, session["user"])
    if not transport:
        raise HTTPException(status_code=404, detail="Transport not found")
    result = await test_ssh_connection(
        transport["ssh_host"], transport["ssh_user"], transport["ssh_key_path"],
    )
    return JSONResponse(result)
