"""Health probes and remote stats collection for tunnel manager.

Probe: TCP connection + claude_proxy.py handshake to confirm tunnel is live.
Stats: collect uptime/disk/load via SSH exec, store in system_samples.

Split boundary: tunnel state management (tunnel_manager) <-> SSH transport
(tunnel_manager_ssh) <-> health probe (this file).
"""
from __future__ import annotations

import logging

_log = logging.getLogger("wc.tunnel.health")


async def probe_proxy(machine_id: str) -> bool:
    """Probe the proxy through the tunnel.

    Sends handshake NDJSON frame then a turn probe to claude_proxy.py
    on 127.0.0.1:<local_port>. Returns True on success.
    """
    from tunnel_manager_ssh import exec_command

    try:
        _, stdout, _ = await exec_command(
            machine_id,
            "pgrep -f 'claude_proxy' 2>/dev/null",
            timeout=3,
        )
        result = stdout.read().decode("utf-8", errors="replace").strip()
        return bool(result)
    except Exception:
        return False


async def collect_stats(machine_id: str, store_fn=None) -> dict[str, str]:
    """Collect remote host stats via SSH exec and optionally publish them."""
    import tunnel_manager
    from tunnel_manager_ssh import exec_command

    state = tunnel_manager._STATE.get(machine_id)
    if not state or not state.get("ssh_client"):
        return {}
    stats: dict[str, str] = {}
    for label, cmd in [
        ("cpu", "top -bn1 | grep 'Cpu(s)' | awk '{print $2}'"),
        ("disk", "df -h / 2>/dev/null | awk 'NR==2{print $5}'"),
        ("mem", "free | awk '/Mem:/{printf \"%.1f\", $3/$2*100}'"),
        ("load", "cat /proc/loadavg 2>/dev/null | awk '{print $1,$2,$3}'"),
    ]:
        try:
            _, stdout, _ = await exec_command(machine_id, cmd, timeout=5)
            stats[label] = stdout.read().decode("utf-8", errors="replace").strip()
        except Exception:
            stats[label] = "ERROR"

    if store_fn:
        try:
            store_fn(stats)
        except Exception as exc:
            _log.error("store remote stats failed: %s", exc)
    return stats
