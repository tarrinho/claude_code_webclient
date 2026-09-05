"""SSH transport layer for tunnel manager.

Handles SSH client lifecycle, key file validation, port allocation,
and exec_command wrapper. Imported by tunnel_manager.py.

Split boundary: tunnel state management (tunnel_manager) <-> SSH transport
(this file) <-> health probe (tunnel_manager_health).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time

import tunnel_manager

_log = logging.getLogger("wc.tunnel.ssh")


async def connect(machine_id: str):
    """Attempt SSH connect + port forward for *machine_id*.

    Returns (ok, ssh_client, transport, local_port, ssh_port).
    """
    import paramiko

    import db

    try:
        row = await db.ssh_tunnel_get(machine_id)
    except Exception:
        return _fail(machine_id, "no tunnel row")

    if not row:
        return _fail(machine_id, "no tunnel row")

    ssh_host = row.get("ssh_host", "")
    ssh_user = row.get("ssh_user", "kali")
    ssh_key_path = row.get("ssh_key_path", "")
    ssh_port = row.get("ssh_port", 22)

    if not ssh_host:
        return _fail(machine_id, "ssh_host is empty")
    if not ssh_key_path:
        return _fail(machine_id, "ssh_key_path is empty")

    try:
        _check_key_permissions(ssh_key_path)
    except Exception as exc:
        return _fail(machine_id, str(exc))

    try:
        local_port = await _find_available_port()
    except Exception as exc:
        _fail(machine_id, str(exc))
        return (False, None, None, 0, 0)

    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        await asyncio.to_thread(
            ssh_client.connect,
            hostname=ssh_host,
            username=ssh_user,
            key_filename=ssh_key_path,
            timeout=10,
        )
    except Exception as exc:
        ssh_client.close()
        return _fail(machine_id, str(exc)[:200])

    try:
        transport = ssh_client.get_transport()
        if not transport:
            ssh_client.close()
            return _fail(machine_id, "no transport after connect")
        transport.request_port_forward("127.0.0.1", local_port)
    except Exception as exc:
        ssh_client.close()
        return _fail(machine_id, str(exc)[:200])

    return (True, ssh_client, transport, local_port, ssh_port)


def _check_key_permissions(key_path: str) -> None:
    """Verify SSH key file permissions (must be <= 0o600)."""
    try:
        st = os.stat(key_path)
    except OSError as exc:
        raise FileNotFoundError(f"SSH key not found: {key_path}") from exc
    mode = st.st_mode & 0o777
    if mode > 0o600:
        raise PermissionError(
            f"SSH key {key_path} mode {oct(mode)} "
            f"(expected 0o600 or stricter)"
        )
    if not os.access(key_path, os.R_OK):
        raise PermissionError(f"SSH key {key_path} is not readable")


async def _find_available_port(low: int = 9000, high: int = 10000) -> int:
    """Find first available local port in range [low, high]."""
    for port in range(low, high):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
        except ConnectionRefusedError:
            return port
        except TimeoutError:
            continue
        except OSError:
            continue
    raise OSError(f"No available port in range {low}-{high}")


async def disconnect(machine_id: str) -> None:
    """Close SSH client and release port forward for *machine_id*."""
    state = tunnel_manager._STATE.get(machine_id)
    if not state:
        return
    transport = state.get("transport")
    ssh_client = state.get("ssh_client")
    if transport:
        with contextlib.suppress(Exception):
            transport.close()
    if ssh_client:
        with contextlib.suppress(Exception):
            ssh_client.close()
    state["ssh_client"] = None
    state["transport"] = None


async def exec_command(machine_id: str, cmd: str, timeout: int = 10):
    """Run *cmd* over the SSH tunnel. Returns (stdin, stdout, stderr)."""
    state = tunnel_manager._STATE.get(machine_id)
    if not state or not state.get("ssh_client"):
        raise RuntimeError("tunnel not connected")
    return state["ssh_client"].exec_command(cmd, timeout=timeout)


def _fail(machine_id: str, error_msg: str):
    """Return failure tuple and set error_msg on state."""
    state = tunnel_manager._STATE.get(machine_id)
    if state:
        state["state"] = "error"
        state["error_msg"] = error_msg
        state["last_check"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return (False, None, None, 0, 0)


async def test_ssh_connection(ssh_host: str, ssh_user: str, ssh_key_path: str):
    """Test SSH connectivity without creating a tunnel.

    Used by the initialization wizard.
    Returns {"ok": bool, "error": str | None}.
    """
    import paramiko

    if not ssh_host:
        return {"ok": False, "error": "SSH host is required"}
    if not ssh_key_path:
        return {"ok": False, "error": "SSH key path is required"}

    try:
        _check_key_permissions(ssh_key_path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        await asyncio.to_thread(
            ssh.connect,
            hostname=ssh_host,
            username=ssh_user,
            key_filename=ssh_key_path,
            timeout=5,
        )
        ssh.close()
        return {"ok": True, "error": None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


async def probe_remote(machine_id: str):
    """Probe remote host info via existing tunnel.

    Returns dict with probe results for each check.
    """
    state = tunnel_manager._STATE.get(machine_id)
    if not state or not state.get("ssh_client"):
        return {"ok": False, "error": "tunnel not connected"}

    results = {}
    for label, cmd in [
        ("claude", "which claude 2>/dev/null || echo NOT_FOUND"),
        ("python3", "python3 --version 2>&1 || echo NOT_FOUND"),
        ("disk", "df -h / 2>/dev/null | awk 'NR==2{print $5}'"),
        ("proxy", "pgrep -f claude_proxy.py 2>/dev/null || echo NOT_FOUND"),
    ]:
        try:
            _, stdout, _ = state["ssh_client"].exec_command(cmd, timeout=5)
            decoded = stdout.read().decode("utf-8", errors="replace")
            results[label] = decoded.strip()
        except Exception:
            results[label] = "ERROR"

    return {"ok": True, "results": results}
