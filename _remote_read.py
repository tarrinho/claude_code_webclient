"""Read /proc/meminfo from a remote transport via its SSH tunnel.

The tunnel_manager keeps live paramiko connections per transport.  This
module reuses the existing ``tunnel_manager_ssh.exec_command`` machinery
to ask a remote host for its memory stats, parses the output, and
returns it as a string suitable for :func:`resource_guard.parse_meminfo`.

Split from resource_guard to keep the guard pure (no async, no tunnel
imports) while still letting the caller -- runner.py -- do the SSH read
and hand the result back as meminfo.
"""
from __future__ import annotations

import asyncio
import logging
import sys

_log = logging.getLogger("wc._remote_read")

# ``free -k`` prints MemAvailable, MemTotal, MemFree, SwapTotal, SwapFree
# in the same format as ``/proc/meminfo``, so we can pipe it through
# parse_meminfo without any translation.
_FREE_CMD = "free -k"


async def remote_meminfo(
    machine_id: str,
    *,
    timeout: int = 5,
    read_cmd: str = _FREE_CMD,
) -> str | None:
    """Read the remote host's memory stats via SSH.

    Uses the existing tunnel manager SSH connection for *machine_id*.
    If the tunnel is down, the machine has no entry, or the command fails,
    returns ``None`` -- the caller must treat this as "not measured" and
    fail open.

    Returns the raw text output on success or ``None`` on any failure.
    """
    from tunnel_manager_ssh import exec_command

    try:
        _, stdout, _ = await exec_command(machine_id, read_cmd, timeout=timeout)
        return stdout.read().decode("utf-8", errors="replace").strip() or None
    except Exception:
        _log.debug("remote_meminfo exec failed for machine=%s", machine_id)
        return None


def remote_meminfo_sync(
    machine_id: str,
    *,
    timeout: int = 5,
    read_cmd: str = _FREE_CMD,
) -> str | None:
    """Synchronous wrapper around :func:`remote_meminfo`.

    Runs the async read in a new event loop if none is running.
    Useful from synchronous callers that cannot await.
    """
    try:
        loop = asyncio.get_running_loop()
        return loop.run_until_complete(
            remote_meminfo(machine_id, timeout=timeout, read_cmd=read_cmd)
        )
    except RuntimeError:
        return asyncio.run(
            remote_meminfo(machine_id, timeout=timeout, read_cmd=read_cmd)
        )
