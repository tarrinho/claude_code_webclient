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
    remote_path = (data.get("remote_path") or "").strip()
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
        remote_path=remote_path or "~/projects/claude-code-webconsole",
    )
    _log.info("ssh_transport created by user=%s name=%s", session["user"], name)
    return JSONResponse({"ok": True, "id": transport_id, "name": name})


@router.patch("/api/transports/{transport_id}")
async def handle_transport_patch(request: Request, transport_id: str):
    session = request.state.session
    data = await request.json()
    allowed = {"name", "ssh_host", "ssh_user", "ssh_key_path", "remote_path"}
    if not data or not set(data).issubset(allowed):
        raise HTTPException(status_code=400, detail="No valid fields to update")
    # Reject non-string text fields up front: the validators below call .strip()
    # and regex methods that would otherwise raise and surface as a 500.
    for field in allowed:
        if field in data and data[field] is not None and not isinstance(data[field], str):
            raise HTTPException(status_code=400, detail=f"{field} must be text or null")
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
    if "remote_path" in data and data["remote_path"] is not None:
        remote_path = data["remote_path"].strip()
        if not remote_path:
            raise HTTPException(status_code=400, detail="Remote path cannot be empty")
        data["remote_path"] = remote_path
    updated = await db.ssh_transport_update(transport_id, session["user"], **data)
    if not updated:
        raise HTTPException(status_code=404, detail="Transport not found")
    return JSONResponse({"ok": True})


@router.delete("/api/transports/{transport_id}")
async def handle_transport_delete(request: Request, transport_id: str):
    session = request.state.session
    # ai_machines has no foreign key on transport_id, so a delete here would
    # otherwise leave any referencing backend permanently broken -- its
    # tunnel connect fails forever with "no transport row". Refuse instead.
    machines = await db.ai_machines_list(session["user"])
    referencing = [m for m in machines if m.get("transport_id") == transport_id]
    if referencing:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(referencing)} backend(s) still use this transport -- "
                "delete or repoint them first"
            ),
        )
    deleted = await db.ssh_transport_delete(transport_id, session["user"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Transport not found")
    return JSONResponse({"ok": True})


@router.post("/api/transports/test")
async def handle_transport_test_raw(request: Request):
    """Test SSH connectivity for the add-transport form, before it is saved."""
    from tunnel_manager_ssh import test_ssh_connection

    data = await request.json()
    ssh_host = (data.get("ssh_host") or "").strip()
    if ssh_host and not _HOST_PATTERN_LOCAL.fullmatch(ssh_host):
        raise HTTPException(status_code=400, detail="Enter a valid hostname or IP address")
    if ssh_host:
        _validate_host(ssh_host)
    result = await test_ssh_connection(
        ssh_host,
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


@router.post("/api/transports/{transport_id}/check")
async def handle_transport_check(request: Request, transport_id: str):
    """Is the far side ready to serve turns, and if not, what is missing?

    Read-only. SSH succeeding says nothing about whether turns can flow: the
    host also needs claude_proxy.py listening on config.PROXY_PORT with the
    same token this database holds, and the claude CLI and python3 for it to
    use. Adding a transport creates none of that, and its absence surfaces as
    "Cannot connect to proxy at 127.0.0.1:<port>" on every turn -- which reads
    as a local fault and is not one.

    Deliberately not tunnel_manager_ssh.probe_remote: that needs an
    established tunnel, and a cold transport is exactly when these questions
    matter most.
    """
    import config
    import transport_readiness

    session = request.state.session
    transport = await db.ssh_transport_get(transport_id, session["user"])
    if not transport:
        raise HTTPException(status_code=404, detail="Transport not found")

    token = (await db.setting_get("proxy_token") or "").strip()
    result = await transport_readiness.check_transport(
        transport["ssh_host"], transport["ssh_user"], transport["ssh_key_path"],
        port=config.PROXY_PORT, local_token=token,
    )
    _log.info(
        "transport_check name=%s ready=%s reachable=%s",
        transport["name"], result.ready, result.reachable,
    )
    return JSONResponse(result.as_dict())


@router.post("/api/transports/{transport_id}/init")
async def handle_transport_init(request: Request, transport_id: str):
    """Install and start claude_proxy.py on the transport host.

    Separate from /check, and separately clicked, because this one writes: it
    copies files, installs a token, writes a systemd --user unit and enables a
    service on a machine this console does not own. Everything else the
    console does to a transport only reads.

    Shells out to bin/wc-deploy-proxy.sh rather than reimplementing the six
    steps here. One implementation, for the reason backend_env.deltas exists:
    the script reads the port from config.PROXY_PORT and the token from this
    database, and those two facts are precisely what the earlier hand-written
    deploy scripts got wrong -- a proxy started on 9002 while the tunnel
    forwarded to 9000, and a token pasted in as a literal that went stale.
    A second implementation would be a second thing that has to agree.

    Idempotent: re-running redeploys the current code and restarts the
    service, so the button is safe to press again after a change here.

    On a successful deploy, also starts the tunnel for a machine on this
    transport -- Pedro's request that Init "does everything needed to become
    active" rather than leaving a second, easy-to-miss click (the SSH badge in
    the Backends list) as the only way to actually connect. Fire-and-forget,
    the same call the badge itself makes: tunnel_manager.queue_command runs
    the connect asynchronously, and the panel's 5s poll picks up the real
    state once it lands. A transport with no machine assigned yet has nothing
    to start -- deploy still succeeds, and the response says so rather than
    silently doing nothing.
    """
    import asyncio
    from pathlib import Path

    session = request.state.session
    transport = await db.ssh_transport_get(transport_id, session["user"])
    if not transport:
        raise HTTPException(status_code=404, detail="Transport not found")

    root = Path(__file__).resolve().parent.parent
    script = root / "bin" / "wc-deploy-proxy.sh"
    if not script.is_file():
        raise HTTPException(status_code=500, detail="deploy script missing")

    _log.info("transport_init_start name=%s", transport["name"])
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", str(script), transport["name"],
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        _log.warning("transport_init_timeout name=%s", transport["name"])
        return JSONResponse(
            {"ok": False, "error": "deploy timed out after 180s", "output": ""},
            status_code=504,
        )
    except OSError as exc:
        _log.exception("transport_init_spawn_failed name=%s", transport["name"])
        return JSONResponse(
            {"ok": False, "error": f"could not run the deploy script: {exc}"},
            status_code=500,
        )

    # Tail, not the whole thing: the interesting part of a failure is the end,
    # and the script prints remote systemctl status on failure.
    output = raw.decode("utf-8", "replace")
    tail = "\n".join(output.splitlines()[-20:])
    ok = proc.returncode == 0
    _log.info(
        "transport_init_done name=%s rc=%s ok=%s",
        transport["name"], proc.returncode, ok,
    )
    # A non-zero exit is reported as a failure rather than a cheerful success:
    # "ran the command, must be fine" is the whole failure mode here.
    if not ok:
        return JSONResponse(
            {"ok": False, "returncode": proc.returncode, "output": tail,
             "tunnel_started": False},
            status_code=502,
        )

    # Same choice the SSH badge makes (machines.js: "the first machine's
    # status speaks for the group") -- one shared tunnel per transport, so
    # starting it for any one machine on this transport brings the whole
    # connection up for all of them.
    machines = [
        m for m in await db.ai_machines_list(session["user"])
        if m.get("transport_id") == transport_id
    ]
    tunnel_started = False
    if machines:
        import tunnel_manager
        await tunnel_manager.queue_command(machines[0]["id"], "START_TUNNEL")
        tunnel_started = True
    else:
        _log.info(
            "transport_init_no_machine name=%s -- deployed but nothing to "
            "connect until a machine is assigned to it", transport["name"],
        )

    return JSONResponse(
        {"ok": True, "returncode": proc.returncode, "output": tail,
         "tunnel_started": tunnel_started},
        status_code=200,
    )
